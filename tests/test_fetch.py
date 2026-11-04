"""fetch 工具的离线验收：URL 校验、截断、错误分级。不碰网络。"""

from __future__ import annotations

import json

import pytest

from openagent_ds_search import fetch as fetch_mod
from openagent_ds_search.fetch import fetch_web

PAGE = {
    "title": "DeepSeek 官方文档",
    "content": "# 网页搜索\n\n这是正文内容，模型应该读到它。",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("DS_TOOLS_RETRIES", "2")
    monkeypatch.delenv("DS_SEARCH_RETRIES", raising=False)
    yield
    fetch_mod.TRANSPORT = None


def _install(responses):
    """装一个按顺序返回 responses 的假传输层，并记录调用。"""
    calls: list[dict] = []

    def fake(url, headers, payload, timeout):
        calls.append({"url": url, "headers": headers, "payload": payload})
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    fetch_mod.TRANSPORT = fake
    return calls


def _body(out: str) -> str:
    """取分隔线之后的正文 —— 头部提示里也会出现同名字符，不能整体 count。"""
    return out.split("-" * 40, 1)[1].strip("\n")


def test_returns_title_and_body():
    calls = _install([(200, json.dumps(PAGE))])
    out = fetch_web("https://api-docs.deepseek.com/zh-cn/guides/search")

    assert "DeepSeek 官方文档" in out
    assert "这是正文内容，模型应该读到它。" in out
    assert calls[0]["url"] == "https://api.example.com/v1/tools/fetch"
    assert calls[0]["payload"] == {"url": "https://api-docs.deepseek.com/zh-cn/guides/search"}


def test_long_content_is_truncated_with_hint():
    _install([(200, json.dumps({"title": "长文", "content": "字" * 5000}))])
    out = fetch_web("https://example.com/long", max_chars=500)

    assert "已截断" in out
    assert "5000 字符" in out
    assert len(_body(out)) == 500, "正文应被钳在 500 字符"


def test_max_chars_is_clamped():
    _install([(200, json.dumps({"title": "t", "content": "x" * 30000}))])
    out = fetch_web("https://example.com/huge", max_chars=999999)
    assert len(_body(out)) == 20000, "HARD_MAX_CHARS = 20000"


@pytest.mark.parametrize(
    "url,fragment",
    [
        ("", "不能为空"),
        ("ftp://example.com/a", "http/https"),
        ("example.com/a", "scheme"),
        ("https:///a", "主机名"),
        ("https://user:pass@example.com/", "用户名或密码"),
        ("https://example.com:abc/", "不合法"),
        ("https://e.com/" + "a" * 9000, "过长"),
    ],
)
def test_rejects_bad_url_without_touching_network(url, fragment):
    calls = _install([(200, json.dumps(PAGE))])
    out = fetch_web(url)
    assert out.startswith("[错误]"), out
    assert fragment in out, out
    assert calls == [], "URL 非法时不该发请求（会计费）"


def test_403_is_not_retried():
    calls = _install([(403, '{"error":{"message":"blocked by policy"}}')])
    out = fetch_web("https://example.com/secret")
    assert out.startswith("[错误]") and "403" in out
    assert len(calls) == 1, "403 是站点策略，重试没有意义"


def test_404_is_reported_as_no_content():
    calls = _install([(404, '{"error":{"message":"no content"}}')])
    out = fetch_web("https://example.com/gone")
    assert "取不到可用正文" in out
    assert len(calls) == 1


def test_429_is_retried():
    calls = _install(
        [(429, '{"error":{"message":"slow down"}}'), (200, json.dumps(PAGE))],
    )
    out = fetch_web("https://example.com/ok")
    assert "这是正文内容" in out
    assert len(calls) == 2


def test_empty_content_is_an_error_not_a_success():
    _install([(200, json.dumps({"title": "只有脚本", "content": ""}))])
    out = fetch_web("https://example.com/spa")
    assert out.startswith("[错误]") and "正文为空" in out


def test_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _install([(200, json.dumps(PAGE))])
    out = fetch_web("https://example.com")
    assert out.startswith("[错误]") and "DEEPSEEK_API_KEY" in out
