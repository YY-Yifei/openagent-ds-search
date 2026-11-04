"""上下文窗口的离线验收：那个数字是谁定的、默认多大、能不能改、能不能被核对。

来历值得记一笔（这也是一类「不报错的错误」）：
* 服务端 `GET /models` 自报 `context_window: 1048576`（= 2^20 = 1M）；
* 但 `DeepSeekChatModel.__init__` **不读 model card** —— `context_size` 是构造参数，
  默认 `65536`；不显式传，1M 的模型会被当成 64k 用，压缩阈值提前 16 倍；
* 于是 yaml 写 1M、实际跑 64k，配置与事实差了一个量级。

压缩阈值 = `trigger_ratio (0.8) × context_size`，所以这个数字直接决定「多少用量开始压缩」。
断言四件事：默认就是 1M（有服务端出处）、环境变量能改、非法值不许把程序搞挂、
`--check` 能把「配置 vs 服务端」比出来。
"""

from __future__ import annotations

import json

import pytest

from openagent_ds_search import cli
from openagent_ds_search.agent import build_model
from openagent_ds_search.config import (
    COMPRESS_TRIGGER_RATIO,
    DEFAULT_CONTEXT_SIZE,
    Settings,
    load_settings,
)
from openagent_ds_search.models import ModelCard, ModelProbeError, probe_model_card


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)


# ------------------------------------------------------------------ 默认值
def test_default_is_one_meg_from_the_server():
    """默认值 = 服务端 `/models` 自报的 `context_window`（2^20），不是我拍的。"""
    assert DEFAULT_CONTEXT_SIZE == 2**20 == 1_048_576
    assert load_settings().context_size == 1_048_576


def test_default_trigger_threshold_is_much_later_than_before():
    """阈值从 0.8 × 65536 ≈ 52k 抬到 ≈ 839k —— 这是这一轮的行为变化，写死它。"""
    old = int(0.8 * 65536)
    new = int(COMPRESS_TRIGGER_RATIO * DEFAULT_CONTEXT_SIZE)
    assert old == 52_428
    assert new == 838_860


def test_settings_default_field_matches_constant():
    assert Settings(api_key="sk-test").context_size == DEFAULT_CONTEXT_SIZE


# ------------------------------------------------------------------ 可覆盖
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1000000", 1_000_000),  # 十进制 1M，比 2^20 小 4.9%
        ("65536", 65_536),  # 想回到保守值也随时可以
        ("8192", 8_192),
    ],
)
def test_env_can_change_it(monkeypatch, raw, expected):
    monkeypatch.setenv("DEEPSEEK_CONTEXT_SIZE", raw)
    assert load_settings().context_size == expected


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "  "])
def test_illegal_value_falls_back(monkeypatch, raw):
    """配置写错不该让程序起不来 —— 退回默认值，而不是抛异常或传个 0 进去。"""
    monkeypatch.setenv("DEEPSEEK_CONTEXT_SIZE", raw)
    assert load_settings().context_size == DEFAULT_CONTEXT_SIZE


# ------------------------------------------------------------------ 真的传到了
def test_build_model_actually_passes_it_through():
    """配置写在 Settings 上不算数；要真的落到模型实例上才算数。"""
    settings = Settings(api_key="sk-test", context_size=123_456, stream=False)
    model = build_model(settings)
    assert model.context_size == 123_456


def test_build_model_default_is_now_one_meg():
    model = build_model(load_settings())
    assert model.context_size == DEFAULT_CONTEXT_SIZE


# ------------------------------------------------------------ /models 自证
_MODELS = json.dumps(
    {
        "object": "list",
        "data": [
            {
                "id": "deepseek-flash",
                "name": "DeepSeek-V4.1-Flash",
                "context_window": 1_048_576,
                "max_output_tokens": 393_216,
            },
            {
                "id": "deepseek-v4-pro",
                "context_window": 1_048_576,
                "max_output_tokens": 393_216,
            },
        ],
    },
)


def _transport(status: int, body: str, seen: list | None = None):
    def send(url, headers, payload, timeout):  # noqa: ANN001, ANN202
        if seen is not None:
            seen.append({"url": url, "payload": payload, "headers": headers})
        return status, body

    return send


def _settings(model: str = "deepseek-flash") -> Settings:
    return Settings(api_key="sk-test", base_url="https://api.example.com", model=model)


def test_probe_model_card_reads_server_numbers():
    card = probe_model_card(_settings(), transport=_transport(200, _MODELS))
    assert card == ModelCard(
        id="deepseek-flash",
        context_window=1_048_576,
        max_output_tokens=393_216,
    )


def test_probe_model_card_uses_get():
    """关键：清单是 GET（payload=None）。若发成 POST，服务端会 404/405，测试就白写了。"""
    seen: list = []
    probe_model_card(_settings(), transport=_transport(200, _MODELS, seen))
    assert seen[0]["url"] == "https://api.example.com/models"
    assert seen[0]["payload"] is None
    assert seen[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_probe_model_card_matches_id_case_insensitively():
    card = probe_model_card(_settings("DEEPSEEK-FLASH"), transport=_transport(200, _MODELS))
    assert card.id == "deepseek-flash"


def test_probe_model_card_rejects_unknown_model_and_lists_options():
    with pytest.raises(ModelProbeError) as e:
        probe_model_card(_settings("deepseek-chat"), transport=_transport(200, _MODELS))
    msg = str(e.value)
    assert "清单里没有 deepseek-chat" in msg
    assert "deepseek-flash" in msg and "deepseek-v4-pro" in msg


def test_probe_model_card_requires_context_window():
    body = json.dumps({"data": [{"id": "deepseek-flash"}]})
    with pytest.raises(ModelProbeError) as e:
        probe_model_card(_settings(), transport=_transport(200, body))
    assert "context_window" in str(e.value)


def test_probe_model_card_reports_http_error():
    body = json.dumps({"error": {"message": "Authentication Fails"}})
    with pytest.raises(ModelProbeError) as e:
        probe_model_card(_settings(), transport=_transport(401, body))
    assert e.value.status == 401


def test_probe_model_card_reports_network_failure():
    with pytest.raises(ModelProbeError) as e:
        probe_model_card(_settings(), transport=_transport(-1, "ConnectionResetError: boom"))
    assert "网络错误" in str(e.value)


def test_probe_model_card_rejects_non_json():
    with pytest.raises(ModelProbeError):
        probe_model_card(_settings(), transport=_transport(200, "<html>502</html>"))


# ------------------------------------------------------------ CLI 集成
def _stub_all(monkeypatch, card: ModelCard | None = None) -> None:
    """四个真调用全换掉：两处探针 + 两个工具。"""
    monkeypatch.setattr(cli, "probe_served_model", lambda *_a, **_k: "deepseek-flash")
    monkeypatch.setattr(
        cli,
        "probe_model_card",
        lambda *_a, **_k: card
        or ModelCard(id="deepseek-flash", context_window=1_048_576, max_output_tokens=393_216),
    )
    monkeypatch.setattr(cli, "search_web", lambda *a, **k: "搜索摘要 OK")
    monkeypatch.setattr(cli, "fetch_web", lambda *a, **k: "正文 OK")


def test_check_confirms_context_window_matches(monkeypatch, capsys):
    _stub_all(monkeypatch)

    assert cli._check() == 0
    out = capsys.readouterr().out
    assert "context_window = 1,048,576" in out
    assert "与上限一致" in out
    assert "超过服务端上限" not in out


def test_check_flags_config_larger_than_server(monkeypatch, capsys):
    """配置比服务端更乐观 —— 必须喊出来，否则长会话会被硬拒而不自知。"""
    _stub_all(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_CONTEXT_SIZE", "2000000")

    assert cli._check() == 0  # 还是能跑，所以不算失败
    out = capsys.readouterr().out
    assert "[!] 超过服务端上限 1,048,576" in out
    assert "DEEPSEEK_CONTEXT_SIZE" in out


def test_check_says_ok_when_config_is_conservative(monkeypatch, capsys):
    _stub_all(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_CONTEXT_SIZE", "65536")

    assert cli._check() == 0
    out = capsys.readouterr().out
    assert "小于上限" in out


def test_check_fails_when_context_probe_fails(monkeypatch, capsys):
    _stub_all(monkeypatch)

    def boom(_settings, **_kwargs):  # _check 现在按 model=served 查卡，桩要跟上签名
        raise ModelProbeError("HTTP 401：Authentication Fails", status=401)

    monkeypatch.setattr(cli, "probe_model_card", boom)

    assert cli._check() == 1
    assert "上下文探针失败" in capsys.readouterr().out
