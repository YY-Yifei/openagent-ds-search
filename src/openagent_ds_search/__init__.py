"""openagent-ds-search —— harness 交给 AgentScope，唯一能力是 DeepSeek 联网搜索。"""

from .agent import SYSTEM_PROMPT, build_agent, build_toolkit
from .config import Settings, load_settings
from .search import SearchError, search_web

__all__ = [
    "SYSTEM_PROMPT",
    "build_agent",
    "build_toolkit",
    "Settings",
    "load_settings",
    "SearchError",
    "search_web",
]
__version__ = "0.1.0"
