"""离线跑通完整的 ReAct 循环：真 harness + 假模型 + 假传输层。

证明两件事 ——
1. AgentScope 的循环真的会调到我注册的工具，工具结果真的回到模型手里；
2. 「搜索 → 抓正文」这条两段式链路能在同一个循环里走完，而不是只在文档里成立。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentscope.agent import Agent, ReActConfig
from agentscope.credential import DeepSeekCredential
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse, DeepSeekChatModel
from agentscope.permission import PermissionMode

from openagent_ds_search import fetch as fetch_mod
from openagent_ds_search import search as search_mod
from openagent_ds_search.agent import SYSTEM_PROMPT, build_toolkit

SEARCH_RESPONSE = {
    "results": [
        {
            "title": "答案在这里",
            "url": "https://example.com/answer",
            "site_name": "Example",
            "date": "2026-11-01T00:00:00Z",
            "summary": "关键事实：摘要里说答案是 42。",
            "snippet": "",
            "content": "",
            "rerank_score": 2.5,
            "authority": 4,
        },
    ],
}

FETCH_RESPONSE = {
    "title": "答案在这里",
    "content": "## 第 3 节\n\n正文更详细：答案是 42，并且给出了推导过程。",
}


def _tool_call(name: str, args: dict) -> ChatResponse:
    return ChatResponse(
        content=[
            # 注意：ToolCallBlock.input 是 JSON **字符串**，不是 dict
            ToolCallBlock(id=f"call_{name}", name=name, input=json.dumps(args)),
        ],
        is_last=True,
    )


def _text(text: str) -> ChatResponse:
    return ChatResponse(content=[TextBlock(text=text)], is_last=True)


class FakeDeepSeek(DeepSeekChatModel):
    """继承真模型类（白拿 formatter），只把网络调用换成一段脚本。"""

    def __init__(self, script: list[ChatResponse]) -> None:
        super().__init__(
            credential=DeepSeekCredential(api_key="sk-fake", base_url="https://invalid.example"),
            model="fake-deepseek",
            stream=False,
        )
        self.script = script
        self.calls = 0
        self.seen_tool_messages: list[str] = []

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):  # noqa: ANN001, ANN201
        self.calls += 1
        for m in messages:  # 把工具结果记下来，供断言使用
            if getattr(m, "role", "") in ("tool", "assistant"):
                self.seen_tool_messages.append(str(m.content))
        idx = min(self.calls - 1, len(self.script) - 1)
        return self.script[idx]

    async def count_tokens(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return 10


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    def fake(url, headers, payload, timeout):  # noqa: ANN001, ANN202
        if url.endswith("/v1/tools/search"):
            return 200, json.dumps(SEARCH_RESPONSE)
        if url.endswith("/v1/tools/fetch"):
            return 200, json.dumps(FETCH_RESPONSE)
        return 404, '{"error":{"message":"unknown path"}}'

    monkeypatch.setattr(search_mod, "TRANSPORT", fake)
    monkeypatch.setattr(fetch_mod, "TRANSPORT", fake)
    yield
    search_mod.TRANSPORT = None
    fetch_mod.TRANSPORT = None


def _run(script: list[ChatResponse]) -> tuple[FakeDeepSeek, str]:
    model = FakeDeepSeek(script)
    agent = Agent(
        name="test-agent",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        toolkit=build_toolkit(),
        react_config=ReActConfig(max_iters=6),
    )
    agent.state.permission_context.mode = PermissionMode.BYPASS

    reply = asyncio.run(agent.reply(UserMsg(name="user", content="答案是几？")))
    text = "".join(b.text for b in reply.content if getattr(b, "type", "") == "text")
    return model, text


def test_toolkit_exposes_the_two_search_stages():
    toolkit = build_toolkit()
    schemas = asyncio.run(toolkit.get_tool_schemas())  # 这个接口是 async
    names = [s.get("function", {}).get("name") or s.get("name") for s in schemas]
    assert names == ["search_web", "fetch_web"], f"工具箱应当只有这两个，实际是 {names}"


def test_full_react_loop_calls_the_search_tool():
    model, text = _run([_tool_call("search_web", {"query": "答案是多少", "count": 3}), _text("答案是 42。")])

    assert model.calls == 2, "应当有两次模型调用：先要工具，再给答案"
    assert "42" in text
    assert any("答案在这里" in s for s in model.seen_tool_messages), (
        "搜索工具的结果没有回到模型手里"
    )


def test_search_then_fetch_in_one_loop():
    """两段式：先检索定位，再抓正文补细节 —— 这就是「fetch 属于搜索」的证据。"""
    model, text = _run(
        [
            _tool_call("search_web", {"query": "答案是多少"}),
            _tool_call("fetch_web", {"url": "https://example.com/answer", "max_chars": 1000}),
            _text("根据原文第 3 节，答案是 42。"),
        ],
    )

    assert model.calls == 3
    seen = "\n".join(model.seen_tool_messages)
    assert "摘要里说答案是 42" in seen, "检索摘要没有回到模型手里"
    assert "推导过程" in seen, "抓取正文没有回到模型手里"
    assert "42" in text
