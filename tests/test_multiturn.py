"""多轮对话的离线验收：真 harness + 假模型。

多轮是最容易「悄悄坏掉」的功能 —— 它没有报错，只有模型突然失忆。
所以这里断言的不是「能跑」，而是三件可核对的事实：
1. 第 2 轮请求里**真的带着**第 1 轮的问答；
2. `/reset` 之后**真的看不到**了；
3. `/v1/tools/search` 这种以 `/` 开头的问题**不会被**当成指令吞掉。
"""

from __future__ import annotations

import asyncio
import builtins
import io
import sys

import pytest

from agentscope.agent import Agent, ReActConfig
from agentscope.credential import DeepSeekCredential
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, DeepSeekChatModel
from agentscope.permission import PermissionMode

from openagent_ds_search import cli
from openagent_ds_search.agent import SYSTEM_PROMPT
from openagent_ds_search.config import Settings
from openagent_ds_search.session import (
    Conversation,
    parse_command,
    split_input_lines,
)


class RecordingModel(DeepSeekChatModel):
    """记住每次被调用时收到的 messages —— 多轮的唯一判据就在这里。"""

    def __init__(self) -> None:
        super().__init__(
            credential=DeepSeekCredential(api_key="sk-fake", base_url="https://invalid.example"),
            model="fake-deepseek",
            stream=False,
        )
        self.seen: list[list[str]] = []

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):  # noqa: ANN001, ANN201
        self.seen.append(
            [
                f"{m.role}:{''.join(getattr(b, 'text', '') for b in m.content) if isinstance(m.content, list) else m.content}"
                for m in messages
            ],
        )
        return ChatResponse(
            content=[TextBlock(text=f"ANSWER-{len(self.seen)}")],
            is_last=True,
        )

    async def count_tokens(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return 10


def _make_agent(model: DeepSeekChatModel) -> Agent:
    agent = Agent(
        name="test-agent",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        react_config=ReActConfig(max_iters=4),
    )
    agent.state.permission_context.mode = PermissionMode.BYPASS
    return agent


def _flat(round_msgs: list[str]) -> str:
    return " | ".join(round_msgs)


# ---------------------------------------------------------------- 多轮本质
def test_second_turn_sees_first_turn() -> None:
    """复用同一实例时，第 2 轮请求必须带着第 1 轮的问答。"""
    model = RecordingModel()
    agent = _make_agent(model)

    asyncio.run(agent.reply(UserMsg(name="user", content="我叫阿黎。")))
    asyncio.run(agent.reply(UserMsg(name="user", content="我叫什么名字？")))

    first, second = _flat(model.seen[0]), _flat(model.seen[1])
    # 第 1 轮当然看不到第 2 轮的提问
    assert "我叫什么名字" not in first
    # 第 2 轮既看得到第 1 轮的问题，也看得到第 1 轮的回答
    assert "我叫阿黎" in second
    assert "ANSWER-1" in second
    assert "我叫什么名字" in second
    assert len(agent.state.context) == 4  # user / assistant / user / assistant


def test_reset_clears_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset 之后，上一段对话不能残留在请求里。"""
    model = RecordingModel()
    agent = _make_agent(model)
    conv = Conversation(agent)

    asyncio.run(agent.reply(UserMsg(name="user", content="我叫阿黎。")))
    conv.count_turn()
    assert "我叫阿黎" in _flat(model.seen[-1])

    conv.reset()
    asyncio.run(agent.reply(UserMsg(name="user", content="我叫什么名字？")))

    after_reset = _flat(model.seen[-1])
    assert "我叫阿黎" not in after_reset
    assert "ANSWER-1" not in after_reset
    assert conv.turns == 0


def test_stats_counts_context_growth() -> None:
    """用量读数要跟着上下文真实增长，而不是给个假数。"""
    model = RecordingModel()
    agent = _make_agent(model)
    conv = Conversation(agent)

    before = conv.stats()
    assert before.messages == 0 and before.chars == 0

    asyncio.run(agent.reply(UserMsg(name="user", content="一二三四五")))
    conv.count_turn()
    after = conv.stats()

    assert after.messages > before.messages
    assert after.chars > before.chars
    assert after.turns == 1
    assert "会话第 1 轮" in conv.report()


# ------------------------------------------------------------ 指令白名单
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/exit", "exit"),
        ("/quit", "exit"),
        ("/q", "exit"),
        ("/reset", "reset"),
        ("/clear", "reset"),
        ("/context", "context"),
        ("/stats", "context"),
        ("/compact", "compact"),
        ("/help", "help"),
        ("/?", "help"),
        ("  /exit  ", "exit"),
    ],
)
def test_recognized_commands(line: str, expected: str) -> None:
    cmd = parse_command(line)
    assert cmd is not None and cmd.name == expected


@pytest.mark.parametrize(
    "line",
    [
        "/v1/tools/search 有哪些请求参数？",   # 真实用例：问题本身以 / 开头
        "/usr/local/bin 是干什么的",
        "普通问题",
        "exit",                              # 没有斜线就不算指令
        "/",
        "/nonexistent",
    ],
)
def test_non_commands_are_left_alone(line: str) -> None:
    """**这条最重要**：以 / 开头的真问题不能被指令解析吞掉。"""
    assert parse_command(line) is None


def test_split_input_lines() -> None:
    assert split_input_lines("q1\n\n  q2  \n") == ["q1", "q2"]
    assert split_input_lines("") == []


# ------------------------------------------------------------------ CLI 层
class _FakeAgentHandle:
    """包一层：让 CLI 拿到「像 Agent 的东西」，同时保留假模型供断言。"""

    def __init__(self, agent: Agent, model: RecordingModel) -> None:
        self._agent = agent
        self.model = model

    def __getattr__(self, item: str):  # noqa: ANN401
        return getattr(self._agent, item)


@pytest.fixture()
def cli_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    created: list[RecordingModel] = []

    def fake_build_agent(settings=None):  # noqa: ANN001, ANN202
        model = RecordingModel()
        created.append(model)
        return _make_agent(model)

    monkeypatch.setattr(cli, "build_agent", fake_build_agent)
    return created


def _settings() -> Settings:
    return Settings(api_key="sk-test", stream=False)


def test_repl_keeps_context_between_questions(
    cli_env: list[RecordingModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """交互模式下连问两句，第二句必须带着第一句 —— 这是「多轮」的定义。"""
    answers = iter(["第一问：我叫阿黎", "第二问：我叫什么", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda *_: next(answers))

    code = asyncio.run(cli._repl(fake_build := cli.build_agent(_settings()), _settings()))
    assert code == 0
    assert fake_build is not None

    model = cli_env[0]
    assert len(model.seen) == 2, "两次提问应各触发一次模型调用"
    assert "我叫阿黎" in _flat(model.seen[1])
    assert "ANSWER-1" in _flat(model.seen[1])


def test_repl_reports_usage_after_each_turn(
    cli_env: list[RecordingModel],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = iter(["问题一", "/context", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda *_: next(answers))

    asyncio.run(cli._repl(cli.build_agent(_settings()), _settings()))
    out = capsys.readouterr().out
    assert "会话第 1 轮" in out
    assert "粗估" in out


def test_pipe_mode_runs_each_line_as_a_turn(
    cli_env: list[RecordingModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管道模式：每行一个问题，共享同一段会话。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO("第一问：我叫阿黎\n第二问：我叫什么\n"))

    code = cli.main([])
    assert code == 0

    model = cli_env[0]
    assert len(model.seen) == 2
    assert "我叫阿黎" in _flat(model.seen[1])


def test_pipe_mode_honours_reset(
    cli_env: list[RecordingModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管道里也能用 /reset —— 第 3 个问题不该看到第 1 个。"""
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO("第一问：我叫阿黎\n/reset\n第三问：我叫什么\n"),
    )

    assert cli.main([]) == 0
    model = cli_env[0]
    assert len(model.seen) == 2, "/reset 不触发模型调用"
    assert "我叫阿黎" not in _flat(model.seen[-1])


def test_single_shot_is_unchanged(
    cli_env: list[RecordingModel],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """带问题的调用仍是「一问一答就退」，不因为多了多轮而改变语义。"""
    assert cli.main(["一句话的问题"]) == 0
    out = capsys.readouterr().out
    assert "单次" in out
    assert len(cli_env[0].seen) == 1
