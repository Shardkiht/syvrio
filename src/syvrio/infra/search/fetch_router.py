"""网页正文抓取的独立路由：与搜索的 provider 顺序相互独立。

抓取入口统一做公网 URL 校验（输入与重定向后的 final_url），provider
只负责自身 API 的请求与映射；失败按序回退，全部失败时汇总诊断。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..config import FetchConfig, SearchConfig, read_api_key
from ..storage.paths import SyvrioPaths, vault_paths
from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    SearchFailure,
    assert_public_fetch_url,
    exhausted_providers,
)
from .pauses import ProviderPauses, paused_failure, provider_failure
from .providers.catalog import OPEN_WEBSEARCH, PROVIDERS, supports_fetch
from .providers.open_websearch import OpenWebSearchProvider


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
        provider_pauses: ProviderPauses | None = None,
    ):
        self.registry = dict(registry or {})
        self.order = list(dict.fromkeys(order or list(self.registry)))
        self.provider_pauses = provider_pauses

    @property
    def supports_fetch(self) -> bool:
        return any("fetch" in provider.capabilities for provider in self.registry.values())

    def fetch(self, url: str, max_chars: int | None = None) -> FetchResult:
        text = url.strip()
        if not text:
            raise ValueError("抓取地址为空")
        assert_public_fetch_url(text)
        failures: list[SearchFailure] = []
        best_partial: FetchResult | None = None
        for name in self.order:
            provider = self.registry.get(name)
            if provider is None or "fetch" not in provider.capabilities:
                continue
            failure = paused_failure(name, self.provider_pauses)
            if failure:
                failures.append(failure)
                continue
            try:
                result = provider.fetch(FetchQuery(text, max_chars))
            except ProviderError as exc:
                failures.append(provider_failure(name, exc, self.provider_pauses))
                continue
            except Exception as exc:
                failures.append(SearchFailure(name, "unavailable", str(exc)))
                continue
            result.provider = name
            if result.final_url and result.final_url != text:
                # 外部服务未必披露跳转链；拒绝已知的非公网最终地址。
                assert_public_fetch_url(result.final_url, "重定向后的抓取地址")
            if result.content_kind == "page" and (
                not result.truncated
                or (max_chars is not None and 0 < max_chars <= len(result.content))
            ):
                result.quota_exhausted = exhausted_providers(failures)
                return result
            # 服务仅返回部分内容时继续抓取；若最终都不完整，优先保留正文，其次比较长度。
            if best_partial is None or (result.content_kind == "page", len(result.content)) > (
                best_partial.content_kind == "page",
                len(best_partial.content),
            ):
                best_partial = result
        exhausted = exhausted_providers(failures)
        if best_partial is not None:
            best_partial.quota_exhausted = exhausted
            return best_partial
        if failures:
            detail = "；".join(f"{item.provider}: {item.detail}" for item in failures)
            # 这里失败的是抓取而不是搜索；provider 标明 fetch，避免上层误判。
            kind = "quota" if all(item.kind == "quota" for item in failures) else "unavailable"
            raise ProviderError("fetch", kind, detail, quota_exhausted=exhausted)
        raise ProviderError("fetch", "unavailable", "没有支持网页抓取的 provider")


def build_fetch_registry(
    fetch_config: FetchConfig,
    search_config: SearchConfig,
    read_key: Callable[[str], str] = read_api_key,
) -> dict[str, FetchProvider]:
    registry: dict[str, FetchProvider] = {}
    for name in fetch_config.providers:
        if not supports_fetch(name, search_config.transport):
            continue
        if name == OPEN_WEBSEARCH:
            registry[name] = OpenWebSearchProvider(
                base_url=search_config.base_url,
                timeout=search_config.timeout,
                transport=search_config.transport,
                engines=search_config.engines,
                fetch_timeout=search_config.fetch_timeout,
            )
        else:
            spec = PROVIDERS[name]
            api_key = read_key(spec.key_env) if spec.key_env else ""
            if not api_key and not spec.optional_key:
                # 未配置 Key 的厂商直接跳过：不参与尝试，也不报错。
                continue
            registry[name] = _build_external_fetch_provider(
                name, api_key, search_config.fetch_timeout
            )
    return registry


def _build_external_fetch_provider(name: str, api_key: str, timeout: float) -> FetchProvider:
    if name == "anysearch":
        from .providers.anysearch import AnySearchFetchProvider

        return AnySearchFetchProvider(api_key, timeout=timeout)
    if name == "parallel_mcp":
        from .providers.parallel_mcp import ParallelMcpFetchProvider

        return ParallelMcpFetchProvider(api_key, timeout=timeout)
    if name == "tavily":
        from .providers.tavily import TavilyFetchProvider

        return TavilyFetchProvider(api_key, timeout=timeout)
    if name == "exa":
        from .providers.exa import ExaFetchProvider

        return ExaFetchProvider(api_key, timeout=timeout)
    if name == "you":
        from .providers.you import YouFetchProvider

        return YouFetchProvider(api_key, timeout=timeout)
    if name == "volcengine":
        from .providers.volcengine import VolcengineFetchProvider

        return VolcengineFetchProvider(api_key, timeout=timeout)
    raise ValueError(f"未知的外部抓取 provider: {name}")


def build_fetch_router(
    fetch_config: FetchConfig,
    search_config: SearchConfig,
    vault: Path | SyvrioPaths | None = None,
    read_key: Callable[[str], str] = read_api_key,
) -> FetchRouter:
    pauses = None
    if vault is not None:
        paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        paths.validate()
        pauses = ProviderPauses(paths)
    return FetchRouter(
        build_fetch_registry(fetch_config, search_config, read_key),
        order=fetch_config.providers,
        provider_pauses=pauses,
    )
