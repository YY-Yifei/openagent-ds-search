"""模型自证：配置里写的名字与规格，未必是服务端真正在跑的那个。

背景（2026-11 在真实 key 上逐个实测）：
* `GET /models` 只列出 `deepseek-flash` 与 `deepseek-v4-pro`；
* 请求 `deepseek-chat` / `deepseek-reasoner` 仍然返回 200，但响应里的 `model` 字段是
  `deepseek-flash` —— 服务端把它们**静默路由**了，一个字也没警告；
* AgentScope 自带的 model card 里，`deepseek-chat` 与 `deepseek-reasoner` 都写着
  `status: sunset`（`deprecated_at: 2026-07-24`），`deepseek-v4-flash` /
  `deepseek-v4-pro` 才是 `active`；
* `GET /models` 还自报 `context_window: 1048576`（= 1M）与 `max_output_tokens: 393216`
  —— 这是上下文窗口唯一的权威出处，比 yaml 里的元数据可信。

于是出现最糟的一类错误：**配置在说谎，而且不报错**。你以为在用 A，实际在跑 B，
成本与能力的评估全落在错的那一个上。

本模块只做三件事，都很便宜（第三条免费）：
1. `is_sunset()`：已知的 sunset / 别名名字，一查便知；
2. `probe_served_model()`：发一次 `max_tokens=8` 的最小请求，读响应里的 `model`
   字段 —— 那是服务端**自己说的**实际模型，不是我们的推断；
3. `probe_model_card()`：`GET /models`，读服务端自报的上下文窗口与输出上限。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import _client
from .config import Settings

#: 会话补全路径（与工具接口的 `/v1/tools/*` 不是一个前缀，别拼错）。
CHAT_COMPLETIONS_PATH = "/chat/completions"

#: 模型清单路径：`GET /models`，只读、不计费，自报每个模型的上下文与输出上限。
MODELS_PATH = "/models"

#: 探针只要 8 个 token —— 够服务端回一个 `model` 字段，计费小到可忽略。
PROBE_MAX_TOKENS = 8
PROBE_TIMEOUT = 30.0

#: 已知的「名字还在、但已不是它自己」的模型名。写进配置不报错，只是会被路由走。
SUNSET_MODELS = frozenset({"deepseek-chat", "deepseek-reasoner", "deepseek-coder"})


class ModelProbeError(RuntimeError):
    """探针失败：网络、鉴权、响应格式都可能。`message` 必须是人话。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ModelCard:
    """服务端自报的模型规格。每个字段都来自 `/models`，不是我们的推断。"""

    id: str
    context_window: int
    max_output_tokens: int


def is_sunset(model: str) -> bool:
    """这个名字是不是已知的 sunset / 别名（会被服务端路由到别的模型）？"""
    return model.strip().lower() in SUNSET_MODELS


def probe_served_model(
    settings: Settings,
    transport: _client.Transport | None = None,
) -> str:
    """问服务端「你到底在跑哪个模型」，返回它自报的名字。

    Args:
        settings: 已装载的配置（取 api_key / base_url / model）。
        transport: 传输层钩子，测试里替换掉即可完全离线。

    Returns:
        服务端响应里的 `model` 字段，例如 `deepseek-flash`。

    Raises:
        ModelProbeError: 网络失败、非 200、响应里没有 `model` 字段。
    """
    send = transport or _client.urllib_transport
    url = settings.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }

    status, body = send(url, headers, payload, PROBE_TIMEOUT)
    if status == -1:
        raise ModelProbeError(f"网络错误：{body}")
    if status != 200:
        detail = _client.error_detail(body)
        raise ModelProbeError(f"HTTP {status}：{detail}", status=status)

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ModelProbeError(f"响应不是 JSON：{body[:200]}") from exc

    served = str((data or {}).get("model") or "").strip()
    if not served:
        raise ModelProbeError(f"响应里没有 model 字段：{json.dumps(data, ensure_ascii=False)[:200]}")
    return served


def probe_model_card(
    settings: Settings,
    transport: _client.Transport | None = None,
    model: str | None = None,
) -> ModelCard:
    """问服务端「这个模型的上下文窗口到底多大」，返回它自报的规格。

    这是唯一值得信的出处：AgentScope 的 model card 只是文档性元数据，适配层
    `DeepSeekChatModel` 根本不读它（它的 `context_size` 默认 65536）。所以
    「配置里的上下文窗口对不对」只能拿服务端的 `/models` 来核。

    Args:
        settings: 已装载的配置（取 api_key / base_url / model）。
        transport: 传输层钩子；`payload` 传 `None` 表示 GET，测试里替换即可离线。
        model: 要查哪个模型的卡。**省略则用 `settings.model`**；当配置名被服务端
            静默路由时（`deepseek-chat` → `deepseek-flash`），必须传服务端**实际的**
            那个名字 —— 否则清单里查不到别名，体检会误判为失败（评审 2.3）。

    Returns:
        匹配该模型名的那张卡（id 大小写不敏感）。

    Raises:
        ModelProbeError: 网络失败、非 200、清单里没有这个模型、没有自报
            `context_window`。
    """
    send = transport or _client.urllib_transport
    url = settings.base_url.rstrip("/") + MODELS_PATH
    headers = {"Authorization": f"Bearer {settings.api_key}"}

    status, body = send(url, headers, None, PROBE_TIMEOUT)  # payload=None → GET
    if status == -1:
        raise ModelProbeError(f"网络错误：{body}")
    if status != 200:
        raise ModelProbeError(f"HTTP {status}：{_client.error_detail(body)}", status=status)

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ModelProbeError(f"响应不是 JSON：{body[:200]}") from exc

    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ModelProbeError(
            f"响应里没有模型清单：{json.dumps(data, ensure_ascii=False)[:200]}",
        )

    target = (model or settings.model).strip()
    wanted = target.lower()
    chosen: dict[str, object] | None = None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip().lower() == wanted:
            chosen = entry
            break
    if chosen is None:
        ids = [str(e.get("id")) for e in entries if isinstance(e, dict)]
        raise ModelProbeError(f"清单里没有 {target}（服务端只有：{', '.join(ids)}）")

    window = chosen.get("context_window")
    if not isinstance(window, int) or window <= 0:
        raise ModelProbeError(
            f"{wanted} 没有自报 context_window："
            f"{json.dumps(chosen, ensure_ascii=False)[:200]}",
        )
    output = chosen.get("max_output_tokens")
    return ModelCard(
        id=str(chosen.get("id") or target),
        context_window=window,
        max_output_tokens=output if isinstance(output, int) and output > 0 else 0,
    )
