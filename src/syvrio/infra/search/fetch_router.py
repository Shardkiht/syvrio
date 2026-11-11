"""网页正文抓取的独立路由：与搜索的 provider 顺序相互独立。

抓取入口统一做公网 URL 校验（输入与重定向后的 final_url），provider
只负责自身 API 的请求与映射；失败按序回退，全部失败时汇总诊断。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..config import FetchConfig, SearchConfig
from ..paths import SyvrioPaths, vault_paths
from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchFailure,
    assert_public_fetch_url,
)
from .open_websearch import OpenWebSearchProvider

# 需要厂商 API Key 的外部抓取服务；列进 fetch.providers 且 Key 可读才注册。
_EXTERNAL_FETCH_PROVIDERS: dict[str, tuple[str, str]] = {
    "tavily": ("tavily.py:TavilyFetchProvider", "TAVILY_API_KEY"),
    "exa": ("exa.py:ExaFetchProvider", "EXA_API_KEY"),
    "you": ("you.py:YouFetchProvider", "YDC_API_KEY"),
    "volcengine": ("volcengine.py:VolcengineFetchProvider", "VOLCENGINE_SEARCH_API_KEY"),
}
_OPTIONAL_FETCH_KEY_PROVIDERS = {
    "anysearch": "ANYSEARCH_API_KEY",
    "parallel_mcp": "PARALLEL_API_KEY",
}
FETCH_PROVIDERS = {"open_websearch", *_EXTERNAL_FETCH_PROVIDERS, *_OPTIONAL_FETCH_KEY_PROVIDERS}


class FetchProvider(Protocol):
    name: str
    capabilities: set[str]

    def fetch(self, query: FetchQuery) -> FetchResult: ...


class FetchRouter:
    """按配置顺序尝试支持 fetch 的 provider；失败降级，错误统一标记为 fetch。"""

    def __init__(
        self,
        registry: dict[str, FetchProvider] | None = None,
        *,
        order: list[str] | None = None,
        usage_limiter: Any | None = None,
    ):
        self.registry = dict(registry or {})
        self.order = list(dict.fromkeys(order or list(self.registry)))
        # 与搜索共用同一厂商计数池；None 表示未启用额度限制。
        self.usage_limiter = usage_limiter

    @property
    def supports_fetch(self) -> bool:
        return any("fetch" in provider.capabilities for provider in self.registry.values())

    def fetch(self, url: str, max_chars: int = 4000) -> FetchResult:
        text = url.strip()
        if not text:
            raise ValueError("抓取地址为空")
        assert_public_fetch_url(text)
        failures: list[SearchFailure] = []
        for name in self.order:
            provider = self.registry.get(name)
            if provider is None or "fetch" not in provider.capabilities:
                continue
            if self.usage_limiter is not None:
                # 达到厂商上限即跳过该 provider，继续回退下一家。
                if not self.usage_limiter.acquire(name):
                    failures.append(
                        SearchFailure(
                            name,
                            ProviderErrorKind.QUOTA.value,
                            self.usage_limiter.reason(name),
                        )
                    )
                    continue
            try:
                result = provider.fetch(FetchQuery(text, max_chars))
            except ProviderError as exc:
                if self.usage_limiter is not None:
                    self.usage_limiter.note_error(name, exc)
                failures.append(SearchFailure.from_error(exc))
                continue
            except Exception as exc:
                failures.append(SearchFailure(name, "unavailable", str(exc)))
                continue
            if result.final_url and result.final_url != text:
                # 外部服务未必披露跳转链；拒绝已知的非公网最终地址。
                assert_public_fetch_url(result.final_url, "重定向后的抓取地址")
            return result
        if failures:
            detail = "；".join(f"{item.provider}: {item.detail}" for item in failures)
            # 这里失败的是抓取而不是搜索；provider 标明 fetch，避免上层误判。
            raise ProviderError("fetch", "unavailable", detail)
        raise ProviderError("fetch", "unavailable", "没有支持网页抓取的 provider")


def build_fetch_registry(
    fetch_config: FetchConfig, search_config: SearchConfig
) -> dict[str, FetchProvider]:
    registry: dict[str, FetchProvider] = {}
    for name in fetch_config.providers:
        if name == "open_websearch":
            # 与搜索共用同一 daemon 配置；抓取能力只在 http transport 下可用。
            registry[name] = OpenWebSearchProvider(
                base_url=search_config.base_url,
                timeout=search_config.timeout,
                transport=search_config.transport,
                engines=search_config.engines,
                fetch_timeout=search_config.fetch_timeout,
            )
        elif name in FETCH_PROVIDERS:
            env_name = _OPTIONAL_FETCH_KEY_PROVIDERS.get(name)
            from ..config import read_api_key

            api_key = read_api_key(env_name or _EXTERNAL_FETCH_PROVIDERS[name][1])
            if not api_key and env_name is None:
                # 未配置 Key 的厂商直接跳过：不参与尝试，也不报错。
                continue
            registry[name] = _build_external_fetch_provider(
                name, api_key, search_config.fetch_timeout
            )
    return registry


def _build_external_fetch_provider(name: str, api_key: str, timeout: float) -> FetchProvider:
    if name == "anysearch":
        from .anysearch import AnySearchFetchProvider

        return AnySearchFetchProvider(api_key, timeout=timeout)
    if name == "parallel_mcp":
        from .parallel_mcp import ParallelMcpFetchProvider

        return ParallelMcpFetchProvider(api_key, timeout=timeout)
    if name == "tavily":
        from .tavily import TavilyFetchProvider

        return TavilyFetchProvider(api_key, timeout=timeout)
    if name == "exa":
        from .exa import ExaFetchProvider

        return ExaFetchProvider(api_key, timeout=timeout)
    if name == "you":
        from .you import YouFetchProvider

        return YouFetchProvider(api_key, timeout=timeout)
    if name == "volcengine":
        from .volcengine import VolcengineFetchProvider

        return VolcengineFetchProvider(api_key, timeout=timeout)
    raise ValueError(f"未知的外部抓取 provider: {name}")


def build_fetch_router(
    fetch_config: FetchConfig,
    search_config: SearchConfig,
    vault: Path | SyvrioPaths | None = None,
) -> FetchRouter:
    limiter = None
    if vault is not None:
        paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        paths.validate()
        from .usage import UsageLimiter

        limiter = UsageLimiter(search_config.usage_limits, paths.state_dir / "usage.json")
    return FetchRouter(
        build_fetch_registry(fetch_config, search_config),
        order=fetch_config.providers,
        usage_limiter=limiter,
    )
