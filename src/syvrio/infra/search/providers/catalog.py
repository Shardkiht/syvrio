"""搜索与抓取共用的供应商目录、凭据要求和传输能力。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

OPEN_WEBSEARCH = "open_websearch"


@dataclass(frozen=True)
class ProviderSpec:
    label: str
    key_env: str | None = None
    optional_key: bool = False
    fetch: bool = True


PROVIDERS = {
    OPEN_WEBSEARCH: ProviderSpec("open-websearch（免费，无需 Key）"),
    "anysearch": ProviderSpec(
        "AnySearch（匿名可用，可选 Key）", "ANYSEARCH_API_KEY", optional_key=True
    ),
    "parallel_mcp": ProviderSpec(
        "Parallel MCP（匿名可用，可选 Key）", "PARALLEL_API_KEY", optional_key=True
    ),
    "searxng": ProviderSpec("自托管 SearXNG（无需 Key）", fetch=False),
    "baidu_search": ProviderSpec("百度千帆 AI 搜索", "BAIDU_SEARCH_API_KEY", fetch=False),
    "tavily": ProviderSpec("Tavily", "TAVILY_API_KEY"),
    "exa": ProviderSpec("Exa", "EXA_API_KEY"),
    "you": ProviderSpec("You.com", "YDC_API_KEY"),
    "volcengine": ProviderSpec("火山引擎豆包搜索", "VOLCENGINE_SEARCH_API_KEY"),
}


def provider_selection_guidance(providers: Sequence[str]) -> str:
    """按实际启用的供应商生成场景说明；不参与路由或结果排序。"""
    enabled = set(providers)
    scenarios = (
        ("volcengine", "中文热点用 volcengine"),
        ("baidu_search", "国内官网和政策用 baidu_search"),
        ("anysearch", "中文补查用 anysearch"),
        ("exa", "英文技术资料优先 exa"),
        ("tavily", "英文补查用 tavily"),
        ("parallel_mcp", "复杂资料用 parallel_mcp"),
    )
    hints = "；".join(hint for name, hint in scenarios if name in enabled)
    return f"按目标信源选择：{hints}。" if hints else ""


def uses_websearch_daemon(transport: str) -> bool:
    # 只有 MCP 使用 stdio；其余沿用适配器的 HTTP 传输。
    return transport != "mcp"


def supports_fetch(name: str, transport: str) -> bool:
    provider = PROVIDERS.get(name)
    return bool(
        provider and provider.fetch and (name != OPEN_WEBSEARCH or uses_websearch_daemon(transport))
    )
