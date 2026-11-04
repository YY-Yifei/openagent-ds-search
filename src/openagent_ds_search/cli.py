"""命令行：单次问答 / 多轮对话 / 体检。

单次（和以前一样，一问一答，结束即退出）：
    openagent-ds-search "DeepSeek 搜索 API 怎么计费"

多轮交互（同一个 agent 反复问，上文累积）：
    openagent-ds-search -i
    openagent-ds-search          # 无参数且连着终端时也进交互

多轮管道（每行一个问题，共享同一段会话）：
    printf 'DS 搜索怎么计费？\\n那 count 最大多少？\\n' | openagent-ds-search

其它：
    openagent-ds-search --check          # 只体检，两个接口各真打一次
    openagent-ds-search --count 3 "..."  # 每次搜索少要几条
    openagent-ds-search --dump "..."     # 不进模型，直看工具原始返回

多轮之所以能成立：整个会话复用**同一个** `Agent` 实例，它的 `state.context`
自己累积历史。所以这里的纪律是「不重建、不重置」—— 重建 agent 就等于失忆。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import provenance
from .agent import FETCH_TOOL, SEARCH_TOOL, TOOL_NAMES, build_agent
from .config import COMPRESS_TRIGGER_RATIO, ConfigError, Settings, load_settings
from .fetch import fetch_web
from .models import ModelProbeError, is_sunset, probe_model_card, probe_served_model
from .search import search_web
from .session import HELP_TEXT, Conversation, parse_command, split_input_lines


def _count_arg(raw: str) -> int:
    """解析 `--count`：非数字或越界都给人话，不静默忽略（评审 2.4）。"""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要一个 1~50 的整数，收到 {raw!r}") from None
    if not 1 <= value <= 50:
        raise argparse.ArgumentTypeError(f"需要在 1~50 之间，收到 {value}")
    return value


def _setup_stream_console() -> None:
    """控制台编码兜底：只放宽 errors，不改编码 —— 改了反而在 GBK 管道里乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _msg_text(msg: object) -> str:
    """从 Msg 里取纯文本。"""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def _banner(settings: Settings, mode: str) -> str:
    """标题行：让人一眼看出自己在什么模式、手上有什么工具。"""
    return (
        f"{'=' * 60}\n"
        f"openagent-ds-search  |  {settings.model}  |  {mode}\n"
        f"工具: {', '.join(TOOL_NAMES)}\n"
        f"{'=' * 60}"
    )


async def _ask(agent: object, question: str, settings: Settings) -> str:
    """问一句，流式打印，返回完整文本。

    一次调用 = 一轮用户提问，所以在这里把**工具预算重置为满额**
    （`agent.tool_budget` 由 `build_agent` 挂上）。放在这里而不是 construct 时，
    是因为多轮复用同一个 agent 实例，预算必须按「轮」而不是按「会话」计。
    """
    budget = getattr(agent, "tool_budget", None)
    if budget is not None:
        budget.reset()

    from agentscope.message import UserMsg

    user_msg = UserMsg(name="user", content=question)
    if not settings.stream:
        reply = await agent.reply(user_msg)  # type: ignore[attr-defined]
        text = _msg_text(reply)
        print(text)
        return text

    from agentscope.event import (  # 延迟导入：版本差异时不至于整个 CLI 起不来
        TextBlockDeltaEvent,
        ToolCallStartEvent,
        ToolResultEndEvent,
    )

    chunks: list[str] = []
    async for event in agent.reply_stream(user_msg):  # type: ignore[attr-defined]
        if isinstance(event, ToolCallStartEvent):
            print(f"\n  [工具] 调用 {event.tool_call_name} ...", flush=True)
        elif isinstance(event, ToolResultEndEvent):
            print(f"  [结果] 工具返回（{getattr(event, 'state', '')}）", flush=True)
            print("-" * 60, flush=True)
        elif isinstance(event, TextBlockDeltaEvent):
            chunks.append(event.delta)
            print(event.delta, end="", flush=True)
    print()
    return "".join(chunks)


# --------------------------------------------------------------- 会话内指令
async def _compact(agent: object, conv: Conversation) -> None:
    """手动压缩上文。诚实说明：**达到阈值才会真的压缩**，压缩时才调模型、才计费。

    AgentScope 的 `compress_context()` 在用量低于 `trigger_ratio × context_size`
    时直接返回（见 `agentscope/agent/_agent.py`），所以「立即压缩」是个不准的说法 ——
    它只是「立即**尝试**」。低于阈值时既不压缩，也不会产生摘要模型的调用。
    """
    from agentscope.agent import Agent as AgentClass  # 只为类型清晰

    assert isinstance(agent, AgentClass)
    before = conv.stats()
    trigger = agent.context_config.trigger_ratio * (agent.model.context_size or 0)
    print(
        f"[..] 正在尝试压缩（当前粗估 ~{before.est_tokens:,} tokens，"
        f"触发阈值约 {trigger:,.0f}）…",
    )
    try:
        await agent.compress_context()
    except Exception as e:  # noqa: BLE001
        print(f"[x] 压缩失败：{type(e).__name__}: {e}")
        return
    after = conv.stats()
    if after.messages >= before.messages and after.chars >= before.chars:
        print("[..] 未压缩：当前用量没到触发阈值，压缩器没有动作（这是正常的）")
    else:
        print(
            f"[ok] 已压缩：{before.messages} → {after.messages} 条消息，"
            f"{before.chars:,} → {after.chars:,} 字符",
        )


async def _handle_command(cmd_name: str, conv: Conversation, agent: object) -> bool:
    """执行一条会话指令；返回 False 表示要退出会话。"""
    if cmd_name == "exit":
        print("再见。")
        return False
    if cmd_name == "help":
        print(HELP_TEXT)
    elif cmd_name == "reset":
        conv.reset()
        provenance.reset()
        print("[ok] 会话已清空 —— 接下来的问题不再包含之前的内容（来源台账一并清空）。")
    elif cmd_name == "context":
        print(conv.report())
    elif cmd_name == "compact":
        await _compact(agent, conv)
    return True


# -------------------------------------------------------------------- 多轮
async def _repl(agent: object, settings: Settings) -> int:
    """交互式多轮：一行一问，上文累积，直到 /exit 或 EOF。"""
    conv = Conversation(agent)
    print(_banner(settings, "多轮对话"))
    print("输入问题开始；/help 看指令；Ctrl+C 中断当前轮；/exit 退出。\n")

    while True:
        try:
            line = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not line:
            continue

        cmd = parse_command(line)
        if cmd is not None:
            if not await _handle_command(cmd.name, conv, agent):
                return 0
            continue

        try:
            await _ask(agent, line, settings)
        except KeyboardInterrupt:
            # 中断的只是这一轮；agent 会把悬挂的工具调用收尾，会话仍然可用。
            print("\n[!] 这一轮已中断（上文保留，可以接着问）")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"\n[x] 这一轮失败：{type(e).__name__}: {e}")
            continue
        conv.count_turn()
        print(f"  [{conv.report()}]\n")
    return 0


async def _pipe(lines: list[str], agent: object, settings: Settings) -> int:
    """管道多轮：每行一个问题，共享同一段会话。"""
    conv = Conversation(agent)
    print(_banner(settings, f"多轮对话（管道 {len(lines)} 行）"))
    for index, line in enumerate(lines, 1):
        cmd = parse_command(line)
        if cmd is not None:
            if not await _handle_command(cmd.name, conv, agent):
                return 0
            continue
        print(f"\n[{index}/{len(lines)}] {line}")
        await _ask(agent, line, settings)
        conv.count_turn()
        print(f"  [{conv.report()}]")
    return 0


async def _one_shot(question: str, settings: Settings) -> int:
    """单次问答：问完就退，不留会话。"""
    agent = build_agent(settings)
    print(_banner(settings, "单次"))
    await _ask(agent, question, settings)
    return 0


# -------------------------------------------------------------------- 体检
def _check() -> int:
    """体检：配置在不在、两个工具通不通、服务端实际跑哪个模型、上下文窗口够不够。

    两处「自证」都不靠我们的推断，而是让服务端自己说：
    * 模型名 —— 发一次 `max_tokens=8` 的请求，读响应里的 `model`（计费极小）；
    * 上下文窗口 —— `GET /models` 自报的 `context_window`（只读，不计费）。
    """
    print("== openagent-ds-search 体检 ==")
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"[x] {e}")
        return 2

    print(f"[ok] DEEPSEEK_API_KEY 已设置（{settings.api_key[:7]}...，{len(settings.api_key)} 字符）")
    print(f"[..] base_url = {settings.base_url}")
    print(f"[..] model    = {settings.model}")
    print(f"[..] tools    = {', '.join(TOOL_NAMES)}")

    print("\n== 模型自证：配置名 vs 服务端实际服务的模型（计费极小） ==")
    try:
        served = probe_served_model(settings)
    except ModelProbeError as e:
        print(f"[x] 模型探针失败：{e}")
        return 1
    if served == settings.model:
        print(f"[ok] 配置 {settings.model} → 服务端 {served}（一致）")
    else:
        print(f"[!] 配置 {settings.model} → 服务端实际 {served}（名字被静默路由了）")
        print("    这不算错误，但评估成本与能力时要以 served 为准。")
    if is_sunset(settings.model):
        print(f"[!] {settings.model} 是已知的 sunset 别名，建议在 DEEPSEEK_MODEL 里写真名。")

    print("\n== 上下文窗口自证：配置 vs 服务端 /models 自报（GET，不计费） ==")
    try:
        # 按**服务端实际服务的**模型名查卡：配置写成别名时，清单里根本没有那个名字，
        # 用原名查会把「名字被路由」这个已发现的提示变成一次误判失败（评审 2.3）。
        card = probe_model_card(settings, model=served)
    except ModelProbeError as e:
        print(f"[x] 上下文探针失败：{e}")
        return 1
    trigger = settings.context_size * COMPRESS_TRIGGER_RATIO
    print(
        f"[..] 服务端 {card.id}: context_window = {card.context_window:,}"
        f" / max_output_tokens = {card.max_output_tokens:,}",
    )
    print(
        f"[..] 配置 context_size = {settings.context_size:,}"
        f"（压缩阈值 ≈ {trigger:,.0f} tokens）",
    )
    if settings.context_size == card.context_window:
        print("[ok] 与上限一致 —— 1M 是真 1M，不是适配层默认的 65,536")
    elif settings.context_size < card.context_window:
        print("[ok] 小于上限（保守：压缩更早触发，请求不会被撑爆）")
    else:
        print(f"[!] 超过服务端上限 {card.context_window:,} —— 压缩阈值会晚到，长会话可能被硬拒")
        print("    请调小 DEEPSEEK_CONTEXT_SIZE，别让配置比服务端更乐观。")

    print("\n== 真实搜一次（成功会计费 1 次） ==")
    result = search_web("DeepSeek", count=2)
    if result.startswith("[错误]"):
        print(f"[x] 搜索不可用：{result}")
        return 1
    print("[ok] 搜索可用，前 300 字：")
    print(result[:300])

    print("\n== 真实抓一页（成功会计费 1 次） ==")
    page = fetch_web("https://example.com", max_chars=500)
    if page.startswith("[错误]"):
        print(f"[x] 抓取不可用：{page}")
        return 1
    print("[ok] 抓取可用，前 300 字：")
    print(page[:300])
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_stream_console()
    parser = argparse.ArgumentParser(
        prog="openagent-ds-search",
        description=(
            "只带联网检索能力的 agent：search 定位 + fetch 读全文"
            "（harness 由 AgentScope 提供）。不带参数进入多轮对话。"
        ),
    )
    parser.add_argument("question", nargs="*", help="要问的问题；给了就是单次问答")
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="进入多轮对话（同一 agent 反复问，上文累积）",
    )
    parser.add_argument("--check", action="store_true", help="只做体检，不进入对话")
    parser.add_argument(
        "--count",
        type=_count_arg,
        default=None,
        help="把每次搜索条数钳到该上限 1-50（越界或非数字会直接报错）",
    )
    parser.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    parser.add_argument(
        "--dump",
        action="store_true",
        help=f"不进模型，直接打印 {SEARCH_TOOL} / {FETCH_TOOL} 的原始返回（调试用）",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _check()

    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"[x] {e}")
        return 2

    if args.no_stream:
        settings = Settings(**{**settings.__dict__, "stream": False})
    if args.count is not None:
        os.environ["DS_SEARCH_MAX_COUNT"] = str(args.count)

    question = " ".join(args.question).strip()

    if args.dump:
        if not question:
            print("[x] --dump 需要一个搜索词")
            return 2
        print(search_web(question, count=args.count or 3))
        return 0

    if question:
        return asyncio.run(_one_shot(question, settings))

    if args.interactive or sys.stdin.isatty():
        return asyncio.run(_repl(build_agent(settings), settings))

    lines = split_input_lines(sys.stdin.read())
    if not lines:
        parser.print_help()
        return 2
    return asyncio.run(_pipe(lines, build_agent(settings), settings))


if __name__ == "__main__":
    raise SystemExit(main())
