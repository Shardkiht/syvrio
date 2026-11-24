"""搜索与抓取共用的供应商目录、凭据要求和传输能力。"""

from __future__ import annotations

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


def uses_websearch_daemon(transport: str) -> bool:
    # 只有 MCP 使用 stdio；其余沿用适配器的 HTTP 传输。
    return transport != "mcp"


def supports_fetch(name: str, transport: str) -> bool:
    provider = PROVIDERS.get(name)
    return bool(
        provider and provider.fetch and (name != OPEN_WEBSEARCH or uses_websearch_daemon(transport))
    )
