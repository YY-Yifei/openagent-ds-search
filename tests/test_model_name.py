"""模型名的离线验收：配置里写的名字，是不是服务端真正在跑的那个。

这组测试守的是一类**不报错的错误**：`deepseek-chat` / `deepseek-reasoner` 请求仍返回
200，但服务端把响应里的 `model` 换成了 `deepseek-flash` —— 配置在说谎，一个字都不警告。
所以断言的不是「能跑」，而是：
1. 默认模型必须是真名，且不在 sunset 清单里（防止有人手滑改回去）；
2. 探针读的是**响应里的 `model`**，不是我们自己的入参（否则永远自证成功）；
3. 两者不一致时，`--check` 必须把它喊出来。
"""

from __future__ import annotations

import json

import pytest

from openagent_ds_search import cli, models
from openagent_ds_search.config import DEFAULT_MODEL, Settings
from openagent_ds_search.models import (
    ModelCard,
    ModelProbeError,
    is_sunset,
    probe_served_model,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)


def _settings(model: str = DEFAULT_MODEL) -> Settings:
    return Settings(api_key="sk-test", base_url="https://api.example.com", model=model)


def _transport(status: int, body: str, seen: list | None = None):
    """假传输层：记下请求，回放固定响应。"""

    def send(url, headers, payload, timeout):  # noqa: ANN001, ANN202
        if seen is not None:
            seen.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return status, body

    return send


def _ok(model: str) -> str:
    return json.dumps({"id": "x", "model": model, "choices": []})


# ------------------------------------------------------------ 默认值 = 真名
def test_default_model_is_a_real_name():
    """默认值不许是 sunset 别名 —— 这是这一轮修复的正题。"""
    assert DEFAULT_MODEL == "deepseek-flash"
    assert not is_sunset(DEFAULT_MODEL)


def test_sunset_list_covers_known_aliases():
    """已知的别名必须都在清单里（大小写与空白都要容错）。"""
    for name in ("deepseek-chat", "deepseek-reasoner"):
        assert is_sunset(name)
        assert is_sunset(f"  {name.upper()}  ")


def test_load_settings_defaults_to_flash():
    """不设 DEEPSEEK_MODEL 时，装出来的就是真名。"""
    from openagent_ds_search.config import load_settings

    assert load_settings().model == "deepseek-flash"


# ------------------------------------------------------------ 探针行为
def test_probe_returns_served_model():
    served = probe_served_model(_settings(), transport=_transport(200, _ok("deepseek-flash")))
    assert served == "deepseek-flash"


def test_probe_payload_is_minimal_and_hits_chat_endpoint():
    seen: list = []
    probe_served_model(
        _settings(),
        transport=_transport(200, _ok("deepseek-flash"), seen),
    )
    req = seen[0]
    assert req["url"] == "https://api.example.com/chat/completions"
    assert req["payload"]["model"] == "deepseek-flash"
    assert req["payload"]["stream"] is False
    assert req["payload"]["max_tokens"] == models.PROBE_MAX_TOKENS == 8
    assert len(req["payload"]["messages"]) == 1
    assert req["headers"]["Authorization"] == "Bearer sk-test"


def test_probe_reads_response_not_request():
    """关键：请求 deepseek-chat、响应说 deepseek-flash —— 必须回 flash。"""
    served = probe_served_model(_settings("deepseek-chat"), transport=_transport(200, _ok("deepseek-flash")))
    assert served == "deepseek-flash"
    assert served != "deepseek-chat"


def test_probe_rejects_empty_model_field():
    """200 但没有 model 字段 = 失败，不能返回空串冒充成功。"""
    with pytest.raises(ModelProbeError) as e:
        probe_served_model(_settings(), transport=_transport(200, json.dumps({"choices": []})))
    assert "model" in str(e.value)


def test_probe_reports_http_error():
    body = json.dumps({"error": {"message": "Authentication Fails"}})
    with pytest.raises(ModelProbeError) as e:
        probe_served_model(_settings(), transport=_transport(401, body))
    assert e.value.status == 401
    assert "Authentication Fails" in str(e.value)


def test_probe_reports_network_failure():
    with pytest.raises(ModelProbeError) as e:
        probe_served_model(_settings(), transport=_transport(-1, "ConnectionResetError: boom"))
    assert "网络错误" in str(e.value)


def test_probe_rejects_non_json():
    with pytest.raises(ModelProbeError) as e:
        probe_served_model(_settings(), transport=_transport(200, "<html>502</html>"))
    assert "JSON" in str(e.value)


# ------------------------------------------------------------ CLI 集成
def _stub_tools(monkeypatch) -> None:
    """把真调用都换掉 —— 模型探针的测试不该顺带花钱，也不该顺带联网。

    `probe_model_card()` 走的是 `GET /models`（不计费，但那仍然是网络）。
    这组测试只关心「模型名」这一段，所以上下文探针固定回一张正常的卡。
    """
    monkeypatch.setattr(cli, "search_web", lambda *a, **k: "搜索摘要 OK")
    monkeypatch.setattr(cli, "fetch_web", lambda *a, **k: "正文 OK")
    monkeypatch.setattr(
        cli,
        "probe_model_card",
        lambda *_a, **_k: ModelCard(
            id="deepseek-flash", context_window=1_048_576, max_output_tokens=393_216
        ),
    )


def test_check_states_model_when_consistent(monkeypatch, capsys):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(cli, "probe_served_model", lambda *_a, **_k: "deepseek-flash")

    assert cli._check() == 0
    out = capsys.readouterr().out
    assert "→ 服务端 deepseek-flash（一致）" in out
    assert "[!]" not in out


def test_check_warns_when_name_is_routed(monkeypatch, capsys):
    """配 deepseek-chat、服务端跑 flash —— 必须同时喊出「被路由」和「是 sunset」。"""
    _stub_tools(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    monkeypatch.setattr(cli, "probe_served_model", lambda *_a, **_k: "deepseek-flash")

    assert cli._check() == 0  # 仍可用，所以不算失败
    out = capsys.readouterr().out
    assert "服务端实际 deepseek-flash" in out
    assert "sunset" in out


def test_check_fails_when_probe_fails(monkeypatch, capsys):
    """探针失败 = 模型根本用不了，退出码必须非 0。"""
    _stub_tools(monkeypatch)

    def boom(_settings, **_kwargs):  # _check 现在按 model=served 查卡，桩要跟上签名
        raise ModelProbeError("HTTP 401：Authentication Fails", status=401)

    monkeypatch.setattr(cli, "probe_served_model", boom)

    assert cli._check() == 1
    out = capsys.readouterr().out
    assert "模型探针失败" in out
    assert "401" in out
