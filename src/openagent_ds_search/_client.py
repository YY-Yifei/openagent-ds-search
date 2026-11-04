"""DeepSeek 工具接口的共用传输层：一个 POST、一套错误分级、零额外依赖。

`/v1/tools/search` 与 `/v1/tools/fetch` 的错误语义完全一致（400/401/402/403/404
重试无用，408/429/5xx 才值得等），所以重试逻辑只该有一份 —— 两处各抄一遍，
迟早会有一处走样。

`label` 只影响错误文案里那个动词（"搜索" / "抓取"）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT = 30.0

#: 可重试的状态码（官方文档：408/429/5xx 才值得等）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

#: 传输层签名：(url, headers, payload, timeout) -> (status, body_text)。
#: 传 status=-1 表示「网络层炸了」，按可重试处理；`payload=None` 表示发 **GET**
#: （`GET /models` 用 —— 只读清单不必再开第二套传输层）。
Transport = Callable[
    [str, dict[str, str], dict[str, Any] | None, float], tuple[int, str]
]


class ClientError(Exception):
    """请求失败。`retryable` 表示「换个时间再试有意义」。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status


def urllib_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, str]:
    """默认传输层：stdlib urllib，不引入额外依赖。`payload=None` 时发 GET。"""
    # 工具接口是 POST；GET /models 是只读清单，payload 传 None 即走 GET。
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001  网络异常统一降级成 -1
        return -1, f"{type(e).__name__}: {e}"


def error_detail(body: str) -> str:
    """从错误响应里抠出人话。"""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return (body or "").strip()[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err:
            return str(err)[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


def _env_int(primary: str, fallback: str, default: int) -> int:
    """先读新名字，再读旧名字 —— 改配置名不该让老用户的 `.env` 静默失效。"""
    raw = os.environ.get(primary) or os.environ.get(fallback) or str(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_float(primary: str, fallback: str, default: float) -> float:
    raw = os.environ.get(primary) or os.environ.get(fallback) or str(default)
    try:
        return float(raw)
    except ValueError:
        return default


def post(
    path: str,
    payload: dict[str, Any],
    *,
    transport: Transport | None = None,
    label: str = "请求",
) -> dict[str, Any]:
    """POST 到 DeepSeek 工具接口，按文档分别对待各类错误。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ClientError("缺少环境变量 DEEPSEEK_API_KEY，无法调用 DeepSeek 工具接口。")

    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    timeout = _env_float("DS_TOOLS_TIMEOUT", "DS_SEARCH_TIMEOUT", DEFAULT_TIMEOUT)
    retries = _env_int("DS_TOOLS_RETRIES", "DS_SEARCH_RETRIES", 2)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    send = transport or urllib_transport
    url = base_url + path

    last_error = ""
    for attempt in range(retries + 1):
        try:
            status, body = send(url, headers, payload, timeout)
        except Exception as e:  # noqa: BLE001  传输层自己炸了也当可重试
            status, body = -1, f"{type(e).__name__}: {e}"

        if status == 200:
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                raise ClientError(f"响应不是合法 JSON: {e}", status=200) from e
            if not isinstance(data, dict):
                raise ClientError(
                    f"响应结构异常：顶层是 {type(data).__name__}", status=200
                )
            return data

        detail = error_detail(body) or "(空响应体)"
        last_error = f"HTTP {status}: {detail}"

        # —— 下面这些「重试也没用」，立刻失败，别浪费计费窗口 ——
        if status == 401:
            raise ClientError(f"API key 无效或被拒绝（401）：{detail}", status=401)
        if status == 402:
            raise ClientError(f"账户余额不足（402）：{detail}", status=402)
        if status == 400:
            raise ClientError(f"{label}请求不合法（400）：{detail}", status=400)
        if status in (403, 404):
            raise ClientError(f"{label}资源不可用（{status}）：{detail}", status=status)

        # —— 408 / 429 / 5xx / 网络层异常：退避重试 ——
        if status in RETRYABLE_STATUS or status < 0:
            if attempt < retries:
                time.sleep(min(2.0**attempt * 0.5, 4.0))
                continue
            raise ClientError(
                f"{label}服务不可用，重试 {retries} 次后仍失败 —— {last_error}",
                retryable=True,
                status=status,
            )
        raise ClientError(f"{label}失败 —— {last_error}", status=status)

    raise ClientError(f"{label}失败 —— {last_error}", retryable=True)
