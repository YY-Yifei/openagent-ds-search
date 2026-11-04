"""URL 来源台账：区分「检索结果里出现过的链接」与「模型自己拼出来的链接」。

为什么需要它：`fetch_web` 可以被网页里的提示注入诱导去抓任何地址 —— 把不该外发的
信息编码进 URL 的查询串，就是一次静默外泄。系统提示词只能*劝*模型别这么干，
代码层得能*看见*这次抓取的目标到底从哪来。

设计上刻意保持轻：
* 只记 `(host, path)` 的规范化结果，不记正文、不记查询串；
* 按 **host** 判定「已知」—— 同站点下的子页面视为同一来源（否则每次翻页都告警）；
* **不阻断**，只标注。用户完全有权直接给一个链接，那也合法 —— 提示模型
  「这条不是检索结果里的地址，请确认来源」比直接拒绝更有用。

台账是进程级、会话级的（CLI 一次运行一个会话）。`reset()` 供测试与 `/reset` 调用。
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: 已知 host（小写）。
_KNOWN_HOSTS: set[str] = set()
#: 已知的规范化 (host, path)，用于更精确的判断与调试。
_KNOWN_URLS: set[tuple[str, str]] = set()


def _split(url: str) -> tuple[str, str]:
    """把 url 拆成 (host, path)；拆不出来就给 ('', '')。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return "", ""
    host = (parts.hostname or "").strip().lower()
    path = (parts.path or "").rstrip("/") or "/"
    return host, path


def note_urls(urls: "list[str] | tuple[str, ...] | None") -> None:
    """登记一批「来自检索结果」的链接。空值/非法值静默跳过。"""
    for raw in urls or ():
        host, path = _split(str(raw or ""))
        if not host:
            continue
        _KNOWN_HOSTS.add(host)
        _KNOWN_URLS.add((host, path))


def is_known(url: str) -> bool:
    """这个链接的来源已知吗（同 host 即视为已知）？"""
    host, _path = _split(url)
    return bool(host) and host in _KNOWN_HOSTS


def known_hosts() -> tuple[str, ...]:
    """当前已登记的 host 列表（排序后，供报告与测试用）。"""
    return tuple(sorted(_KNOWN_HOSTS))


def reset() -> None:
    """清空台账 —— 新会话、`/reset`、测试之间的隔离。"""
    _KNOWN_HOSTS.clear()
    _KNOWN_URLS.clear()
