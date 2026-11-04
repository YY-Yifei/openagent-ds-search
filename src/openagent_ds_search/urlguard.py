"""URL 校验与「内网地址」拦截 —— 抓取前的第一道代码层防线。

为什么要拦内网：`fetch_web` 由 DeepSeek 服务端代为抓取，我们看不到它到底连了谁。
但把 `http://169.254.169.254/latest/meta-data/`（云元数据）、`http://127.0.0.1:6379`
这类地址交给外部服务去取，本身就是一次「把内网探测外包出去」的尝试 —— 网页提示
注入最喜欢这种手法。系统提示词能劝模型别做，但**拒绝必须写在代码里**。

判定是保守的：只拦明确的私有/环回/链路本地/内网域名，不拦任何公网地址。
拦掉了正常需求怎么办？用真实域名 —— 这个工具本来就只抓公网站点。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: 明确属于内网的域名后缀（mDNS、Kubernetes 内部服务、云厂商内部域名等）。
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".svc",
    ".cluster.local",
)

#: 明确的内网主机名。
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    },
)


def _is_private_ip(host: str) -> bool:
    """host 是 IP 且落在私有/环回/链路本地/保留段里吗？"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # is_private 覆盖 10/8、172.16/12、192.168/16、127/8、::1、fc00::/7
    # is_link_local 覆盖 169.254/16 与 fe80::/10
    # is_reserved / is_multicast / is_unspecified 一并拒绝（云元数据就在 169.254）
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def blocked_reason(url: str) -> str | None:
    """这个 url 是否指向内网？是就返回原因，否则 None。

    Args:
        url: 已经通过基础校验（scheme / host / 无凭据）的地址。

    Returns:
        人话原因，或 None。
    """
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").strip().lower()
    except ValueError:
        return None
    if not host:
        return None

    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        return f"目标是内网主机名（{host}），本工具只抓公网页面。"
    if _is_private_ip(host):
        return f"目标是内网/保留地址（{host}），本工具只抓公网页面。"
    return None
