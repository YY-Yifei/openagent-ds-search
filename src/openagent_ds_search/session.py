"""会话层：多轮对话的状态、用量与斜线指令。

为什么单独一层：`agent.py` 管「这个 agent 有什么能力」，`cli.py` 管「怎么跟人说话」，
而「这次对话走到第几轮、上文占了多少、什么时候该清空」是第三件事 —— 它既可被
CLI 用，也可被测试直接驱动，不必经过 stdin。

两条设计纪律（都是踩过的坑）：

1. **指令只认白名单**。用户的问题经常以 `/` 开头（`/v1/tools/search 有哪些参数？`
   就是真实用例），拿「首字符是 /」当指令会把它吞掉。所以只认 `/exit`、`/reset`
   这类明确的名字，其余一律当问题原样送进模型。
2. **不清空 agent 实例**。多轮靠的就是同一个 `Agent` 反复调用 —— 它的
   `state.context` 自己会累积。`reset()` 只清 `context` 与 `summary` 两处，
   这是实测能切断上文的最小操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 粗估用：中文约 1.6 字符 / token，英文约 4 字符 / token。取 1.6 偏保守
#: （宁可高估用量，也不要让人以为还很空）。只用于给人看的读数，不用于决策。
CHARS_PER_TOKEN = 1.6


@dataclass(frozen=True)
class Command:
    """一条解析出来的斜线指令。"""

    name: str
    arg: str = ""


@dataclass(frozen=True)
class Stats:
    """会话用量读数（都是可核对的事实，估值单独标注）。"""

    turns: int
    messages: int
    chars: int
    summary_chars: int

    @property
    def est_tokens(self) -> int:
        """粗估 token 数（供人判断离压缩阈值还有多远）。"""
        return int((self.chars + self.summary_chars) / CHARS_PER_TOKEN)


#: 指令名 → 规范名。同义词在这里合并，解析处只有一条分支。
_ALIASES: dict[str, str] = {
    "exit": "exit",
    "quit": "exit",
    "q": "exit",
    "reset": "reset",
    "clear": "reset",
    "context": "context",
    "ctx": "context",
    "stats": "context",
    "compact": "compact",
    "help": "help",
    "h": "help",
    "?": "help",
}

#: **可能**花掉一次模型调用的指令 —— 要提前说明，不能悄悄计费。
#: 注意是「可能」：低于压缩阈值时 AgentScope 直接返回，不会产生调用。
COSTLY_COMMANDS = frozenset({"compact"})

HELP_TEXT = """可用指令（只有下面这些；其余以 / 开头的内容都当成问题）：
  /help            显示这份帮助
  /context         显示会话用量（轮次 / 消息数 / 字符数 / 粗估 token）
  /compact         尝试压缩上文（**达到阈值才真的压缩**，那时才调模型、才计费）
  /reset           清空本次会话（只清上文，不退出）
  /exit 或 /quit   退出（Ctrl+D / Ctrl+C 也可）
"""


def parse_command(line: str) -> Command | None:
    """把一行输入解析成指令；不是白名单指令就返回 None（当普通问题处理）。

    Args:
        line: 用户输入的一行原文。

    Returns:
        `Command`，或 None 表示「这是问题，别拦」。
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    head, _, arg = stripped[1:].partition(" ")
    name = _ALIASES.get(head.strip().lower())
    if name is None:
        return None
    return Command(name=name, arg=arg.strip())


def _block_chars(block: object) -> int:
    """数一个消息 block 的字符数（文本与提示都算，工具调用/结果也占上下文）。"""
    for attr in ("text", "hint", "input", "output"):
        value = getattr(block, attr, None)
        if isinstance(value, str) and value:
            return len(value)
    return 0


class Conversation:
    """一段多轮对话：包住一个可复用的 agent，管轮次与上文用量。"""

    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.turns = 0

    # ---------------------------------------------------------------- 状态
    def stats(self) -> Stats:
        """当前上文的用量读数。"""
        state = self.agent.state  # type: ignore[attr-defined]
        context = getattr(state, "context", []) or []
        chars = 0
        for msg in context:
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                chars += sum(_block_chars(b) for b in content)
        summary = getattr(state, "summary", "") or ""
        summary_chars = len(summary) if isinstance(summary, str) else 0
        return Stats(
            turns=self.turns,
            messages=len(context),
            chars=chars,
            summary_chars=summary_chars,
        )

    def report(self) -> str:
        """一行给人看的用量说明，含离压缩阈值还有多远。"""
        stats = self.stats()
        limit = getattr(getattr(self.agent, "model", None), "context_size", 0) or 0
        parts = [
            f"会话第 {stats.turns} 轮",
            f"{stats.messages} 条消息",
            f"{stats.chars:,} 字符",
            f"粗估 ~{stats.est_tokens:,} tokens",
        ]
        if limit:
            pct = min(100.0, stats.est_tokens / limit * 100)
            parts.append(f"占上下文上限 {limit:,} 的 {pct:.0f}%")
        if stats.summary_chars:
            parts.append(f"含摘要 {stats.summary_chars:,} 字符")
        return " · ".join(parts)

    # ------------------------------------------------------------ 变更操作
    def reset(self) -> None:
        """清空上文。只动这两处 —— 换掉 agent 会丢掉工具与权限配置。"""
        state = self.agent.state  # type: ignore[attr-defined]
        context = getattr(state, "context", None)
        if isinstance(context, list):
            context.clear()
        state.summary = ""
        self.turns = 0

    def count_turn(self) -> None:
        """记一轮问答（在模型回答成功之后调）。"""
        self.turns += 1


def split_input_lines(text: str) -> list[str]:
    """把一段 stdin 文本切成问题列表：丢空行、保留每一行的原样。

    用于管道模式（`printf 'q1\\nq2\\n' | python -m ...`）—— 每行一个问题，
    共享同一段会话。
    """
    return [line.strip() for line in text.splitlines() if line.strip()]
