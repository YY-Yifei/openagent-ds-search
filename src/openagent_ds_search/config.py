"""配置：全部来自环境变量，缺什么就明确报什么。

一条贯穿全模块的纪律：**非法配置不该让程序起不来，更不该让工具抛异常**。
环境变量是人的手写输入 —— 多打一个字母、留个空、写成负数都正常。
所以所有数值型读取都走 `env_positive_int` / `env_float`：读不出合法的就退回默认，
并把「默认值是什么」写在调用处，而不是让 `int('abc')` 在第 47 层调用栈里炸开。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.deepseek.com"

#: 用**真名**，不用别名。`deepseek-chat` / `deepseek-reasoner` 都是会被服务端静默
#: 路由到 `deepseek-flash` 的别名（AgentScope 的 model card 已标 `status: sunset`），
#: 写它们不报错、但配置与实际的模型对不上 —— 详见 `models.py`。真名只有两个：
#: `deepseek-flash`（快、便宜）、`deepseek-v4-pro`（带推理）。
DEFAULT_MODEL = "deepseek-flash"

#: 上下文窗口（token），决定 AgentScope 在多少用量时触发压缩（阈值 = 0.8 × 本值）。
#:
#: 这个数字现在有**权威出处**：`GET /models` 里 `deepseek-flash` 与 `deepseek-v4-pro`
#: 都自报 `context_window: 1048576`（= 2^20 = 1M）、`max_output_tokens: 393216`
#: （2026-11-04 用真 key 实测）。`--check` 每次都会拿这个值和配置比一遍。
#:
#: 曾经踩过的坑值得留着：AgentScope 的 `DeepSeekChatModel.__init__` **不读 model card**，
#: 它的 `context_size` 构造参数默认只有 `65536`；不显式传，服务端给 1M 也会被当成 64k 用，
#: 压缩阈值因此提前 16 倍。所以 `build_model()` 必须显式把这个值传下去。
#:
#: 口语的「1M」有两种可能：2^20 = 1,048,576 与十进制 1,000,000（差 4.9%）。这里取服务端
#: 自报的那个精确值；要严格按十进制来，设 `DEEPSEEK_CONTEXT_SIZE=1000000`。
DEFAULT_CONTEXT_SIZE = 1_048_576

#: 压缩触发比例：AgentScope 的默认值，这里只用来在文档与 `--check` 里算「阈值大约多少」。
#: 真正的计算仍由 AgentScope 负责，改这个常量不会改变它的行为。
COMPRESS_TRIGGER_RATIO = 0.8

#: 每轮对话里每种工具允许的最大调用次数 —— **硬预算，代码强制**。
#:
#: 为什么必须写进代码：系统提示词里那句「最多三次」只是请求，不是约束。模型偏离
#: 指令、或被网页提示注入操纵时，那一行字拦不住任何一次计费。`max_iters` 限的是
#: ReAct 迭代数（搜索 3 次 + 抓取 3 次 = 6 次工具调用，20 步远够），替代不了单工具预算。
DEFAULT_MAX_SEARCH_CALLS = 3
DEFAULT_MAX_FETCH_CALLS = 3


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def env_positive_int(name: str, default: int) -> int:
    """读一个正整数环境变量；缺失、非数字、<=0 一律退回 `default`。

    这是全项目唯一的「环境变量 → int」入口。集中一处的好处是：行为只可能错一次，
    且可以用一组参数化测试把它钉死（`abc` / `-1` / `0` / `" 7 "` / 空串）。

    Args:
        name: 环境变量名。
        default: 读不到合法值时的返回值（可以是 0，表示「不设上限」）。

    Returns:
        合法正整数，或 `default`。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 2.0) -> float:
    """读一个浮点环境变量；非法或越界退回 `default`。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


@dataclass(frozen=True)
class Settings:
    """一次运行所需的全部配置。"""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    context_size: int = DEFAULT_CONTEXT_SIZE
    temperature: float = 0.3
    max_tokens: int = 2048
    stream: bool = True
    max_iters: int = 20
    max_search_calls: int = DEFAULT_MAX_SEARCH_CALLS
    max_fetch_calls: int = DEFAULT_MAX_FETCH_CALLS


def load_settings(**overrides: object) -> Settings:
    """从环境变量装载配置，`overrides` 优先级最高。

    环境变量：
        DEEPSEEK_API_KEY       必填，模型与搜索共用
        DEEPSEEK_BASE_URL      可选，默认 https://api.deepseek.com
        DEEPSEEK_MODEL         可选，默认 deepseek-flash（真名；别名会被静默路由）
        DEEPSEEK_CONTEXT_SIZE  可选，默认 1048576（服务端自报的 1M；阈值 = 0.8 × 本值）
        DS_MAX_SEARCH_CALLS    可选，每轮搜索次数硬上限，默认 3
        DS_MAX_FETCH_CALLS     可选，每轮抓取次数硬上限，默认 3
        OPENAGENT_STREAM       可选，0 关闭流式
    """
    api_key = str(
        overrides.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    ).strip()
    if not api_key:
        raise ConfigError(
            "缺少 DEEPSEEK_API_KEY。请先设置：\n"
            "    Windows:  $env:DEEPSEEK_API_KEY = 'sk-...'\n"
            "    Linux/macOS:  export DEEPSEEK_API_KEY='sk-...'",
        )

    stream_env = os.environ.get("OPENAGENT_STREAM", "1").strip()
    fields: dict[str, object] = {
        "api_key": api_key,
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
        "context_size": env_positive_int("DEEPSEEK_CONTEXT_SIZE", DEFAULT_CONTEXT_SIZE),
        "max_search_calls": env_positive_int("DS_MAX_SEARCH_CALLS", DEFAULT_MAX_SEARCH_CALLS),
        "max_fetch_calls": env_positive_int("DS_MAX_FETCH_CALLS", DEFAULT_MAX_FETCH_CALLS),
        "stream": stream_env not in ("0", "false", "False", ""),
    }
    fields.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**fields)  # type: ignore[arg-type]
