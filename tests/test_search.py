"""搜索工具的离线验收：不碰网络，用假传输层验证行为。"""

from __future__ import annotations

import json

import pytest

from openagent_ds_search import search as search_mod
from openagent_ds_search.search import search_web

PAYLOAD_OK = {
    "results": [
        {
            "title": "DeepSeek API 文档",
            "url": "https://api-docs.deepseek.com/",
            "site_name": "DeepSeek",
            "date": "2026-10-01T00:00:00Z",
            "snippet": "这是给人看的短摘要，不该出现在工具输出里。",
            "summary": "这是给模型读的长摘要，600~800 字符那一档。",
            "content": "",
            "rerank_score": 2.1,
            "authority": 4,
        },
        {
            "title": "没有摘要的条目",
            "url": "https://example.com/a",
            "site_name": None,
            "date": None,
            "snippet": "snippet only",
            "summary": "",
            "content": "抓下来的正文。",
            "rerank_score": 0.4,
            "authority": -1,
        },
    ],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("DS_SEARCH_RETRIES", "2")
    monkeypatch.delenv("DS_SEARCH_TIMEOUT", raising=False)
    yield
    search_mod.TRANSPORT = None


def _install(monkeypatch, responses):
    """装一个按顺序返回 responses 的假传输层，并记录调用次数。"""
    calls: list[dict] = []

    def fake(url, headers, payload, timeout):
        calls.append({"url": url, "headers": headers, "payload": payload})
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    search_mod.TRANSPORT = fake
    return calls


def test_formats_results_for_the_model(monkeypatch):
    calls = _install(monkeypatch, [(200, json.dumps(PAYLOAD_OK))])
    out = search_web("deepseek", count=2)

    assert "DeepSeek API 文档" in out
    assert "https://api-docs.deepseek.com/" in out
    assert "给模型读的长摘要" in out          # summary 被采用
    assert "给人看的短摘要" not in out        # snippet 不该混进来
    assert "相关度 2.1" in out and "权威度 4" in out
    assert "抓下来的正文" in out              # summary 为空时回退到 content

    body = calls[0]["payload"]
    assert body == {"query": "deepseek", "count": 2, "need_content": False}
    assert calls[0]["url"] == "https://api.example.com/v1/tools/search"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_empty_results_is_not_an_error(monkeypatch):
    _install(monkeypatch, [(200, json.dumps({"results": []}))])
    out = search_web("没什么好结果")
    assert "没有返回任何结果" in out
    assert not out.startswith("[错误]")


def test_401_is_not_retried(monkeypatch):
    calls = _install(monkeypatch, [(401, '{"error":{"message":"invalid api key"}}')])
    out = search_web("x")
    assert out.startswith("[错误]") and "401" in out
    assert len(calls) == 1          # 401 重试没有意义


def test_429_is_retried_then_succeeds(monkeypatch):
    calls = _install(
        monkeypatch,
        [(429, '{"error":{"message":"rate limited"}}'), (200, json.dumps(PAYLOAD_OK))],
    )
    out = search_web("deepseek")
    assert "DeepSeek API 文档" in out
    assert len(calls) == 2


def test_gives_up_after_retry_budget(monkeypatch):
    calls = _install(monkeypatch, [(503, '{"error":{"message":"unavailable"}}')])
    out = search_web("x")
    assert out.startswith("[错误]") and "重试" in out
    assert len(calls) == 3           # 1 次 + 2 次重试


def test_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _install(monkeypatch, [(200, "{}")])
    out = search_web("x")
    assert out.startswith("[错误]") and "DEEPSEEK_API_KEY" in out


def test_bad_arguments(monkeypatch):
    _install(monkeypatch, [(200, json.dumps(PAYLOAD_OK))])
    assert search_web("   ").startswith("[错误]")
    assert "过长" in search_web("x" * 200)


def test_count_is_clamped(monkeypatch):
    calls = _install(monkeypatch, [(200, json.dumps({"results": []}))])
    search_web("x", count=999)
    assert calls[0]["payload"]["count"] == 50
    search_web("x", count=0)
    assert calls[1]["payload"]["count"] == 1


def test_ori_question_is_forwarded(monkeypatch):
    calls = _install(monkeypatch, [(200, json.dumps({"results": []}))])
    search_web("rust 2026", ori_question="Rust 现在最新版本是多少？")
    assert calls[0]["payload"]["ori_question"] == "Rust 现在最新版本是多少？"
