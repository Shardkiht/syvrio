from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import SearchConfig, read_api_key
from ..paths import SyvrioPaths, vault_paths
from .attempts import SearchAttempt, SearchRun
from .base import (
    FetchResult,
    SearchFailure,
    SearchOutcome,
    SearchProvider,
    SearchQuery,
    SearchResult,
    normalize_search_providers,
    normalize_url,
)
from .cache import SearchCache
from .fetch_router import FetchRouter
from .open_websearch import OpenWebSearchProvider
from .quality import (
    QueryAnalysis,
    ScoreBreakdown,
    analyze_query,
    has_core_hit,
    quality_gate,
    score_entry,
)

DEFAULT_PROVIDER = "open_websearch"
KNOWN_PROVIDERS = {DEFAULT_PROVIDER}
SEARCH_STRATEGIES = ("auto", "compare", "strict")
MAX_SELECTED_PROVIDERS = 8


def _dedupe_failures(failures: Sequence[SearchFailure]) -> list[SearchFailure]:
    """按 (provider, kind, detail) 稳定去重，保留首次出现的顺序。"""
    seen: set[tuple[str, str, str]] = set()
    unique: list[SearchFailure] = []
    for failure in failures:
        key = (failure.provider, failure.kind, failure.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(failure)
    return unique


# 需要厂商 API Key 的外部搜索服务；列进 providers 且 Key 可读才会注册。
_EXTERNAL_PROVIDERS = {
    "baidu_search": ("baidu.py:BaiduSearchProvider", "BAIDU_SEARCH_API_KEY"),
    "tavily": ("tavily.py:TavilySearchProvider", "TAVILY_API_KEY"),
    "exa": ("exa.py:ExaSearchProvider", "EXA_API_KEY"),
    "you": ("you.py:YouSearchProvider", "YDC_API_KEY"),
    "volcengine": ("volcengine.py:VolcengineSearchProvider", "VOLCENGINE_SEARCH_API_KEY"),
}

# 匿名可用、配置 Key 后自动升级认证的外部搜索服务。
_OPTIONAL_KEY_PROVIDERS = {
    "anysearch": "ANYSEARCH_API_KEY",
    "parallel_mcp": "PARALLEL_API_KEY",
}


def _build_optional_provider(name: str, api_key: str, config: SearchConfig) -> SearchProvider:
    if name == "anysearch":
        from .anysearch import AnySearchProvider

        return AnySearchProvider(
            api_key,
            timeout=config.timeout,
            zone=config.anysearch_zone,
            language=config.anysearch_language,
        )
    if name == "parallel_mcp":
        from .parallel_mcp import ParallelMcpProvider

        return ParallelMcpProvider(api_key, timeout=max(15.0, config.timeout))
    raise ValueError(f"未知的可选搜索 provider: {name}")


def _build_external_provider(name: str, api_key: str, timeout: float) -> SearchProvider:
    # 延迟导入：未配置 Key 时不必加载这些模块。
    if name == "baidu_search":
        from .baidu import BaiduSearchProvider

        return BaiduSearchProvider(api_key, timeout=timeout)
    if name == "tavily":
        from .tavily import TavilySearchProvider

        return TavilySearchProvider(api_key, timeout=timeout)
    if name == "exa":
        from .exa import ExaSearchProvider

        return ExaSearchProvider(api_key, timeout=timeout)
    if name == "you":
        from .you import YouSearchProvider

        return YouSearchProvider(api_key, timeout=timeout)
    if name == "volcengine":
        from .volcengine import VolcengineSearchProvider

        return VolcengineSearchProvider(api_key, timeout=timeout)
    raise ValueError(f"未知的外部搜索 provider: {name}")


# 空结果与时效性查询的缓存短命 TTL；成功结果沿用配置的 cache_ttl。
EMPTY_RESULT_TTL = 300.0
FRESHNESS_TTL = 600.0


def query_variants(text: str, context_terms: Sequence[str] = ()) -> list[str]:
    """用会话上下文派生查询变体（确定性规则，不调用 LLM）。

    短人物名、英文账号、产品代号单独搜索常无有效结果；把上下文里的
    专有名词逐个追加，最多两个变体。原始拼写原样保留。
    """
    variants: list[str] = []
    lowered = text.lower()
    for term in context_terms:
        term = str(term).strip()
        if not term or term.lower() in lowered:
            continue
        variant = f"{text} {term}"
        if variant not in variants:
            variants.append(variant)
        if len(variants) == 2:
            break
    return variants


@dataclass(slots=True)
class PoolEntry:
    result: SearchResult
    # (provider, engine, rank_in_engine) 证据列表：同一页面被多个来源返回时
    # 全部保留；provider 与 engine 是两层身份，交叉统计按 (provider, engine) 计。
    ranks: list[tuple[str, str, int]]
    # 最近一次 ranked 计算的逐特征贡献，供解释与调试。
    score: ScoreBreakdown = dataclasses.field(default_factory=ScoreBreakdown)


class CandidatePool:
    """跨引擎/变体的统一候选池：URL 去重合并，保留各路排名证据。"""

    def __init__(self) -> None:
        self._entries: dict[str, PoolEntry] = {}
        self._order: list[str] = []

    @property
    def size(self) -> int:
        return len(self._order)

    def has_provider(self, name: str) -> bool:
        """该 Provider 是否为本池贡献过结果（即使 URL 与已有条目重复）。"""
        return any(
            any(provider == name for provider, _, _ in entry.ranks)
            for entry in self._entries.values()
        )

    def providers(self) -> list[str]:
        """实际贡献过结果的 Provider，按首次贡献顺序排列。"""
        names: list[str] = []
        for entry in self._entries.values():
            for provider, _, _ in entry.ranks:
                if provider not in names:
                    names.append(provider)
        return names

    def add(self, results: list[SearchResult]) -> None:
        for rank, result in enumerate(results, 1):
            url = normalize_url(result.url)
            key = url or f"{result.title}\x00{result.snippet}"
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = PoolEntry(result, [(result.provider, result.engine, rank)])
                self._order.append(key)
                continue
            entry.ranks.append((result.provider, result.engine, rank))
            # 同一页面多来源合并时保留更完整的标题/摘要。
            if len(result.title) > len(entry.result.title):
                entry.result.title = result.title
            if len(result.snippet) > len(entry.result.snippet):
                entry.result.snippet = result.snippet

    def ranked_entries(self, analysis: QueryAnalysis) -> list[PoolEntry]:
        """确定性多特征排序；平分保持首次入池顺序（稳定可复现）。"""
        entries = [self._entries[key] for key in self._order]
        for entry in entries:
            entry.score = score_entry(entry.result, analysis, entry.ranks)
        if analysis.core_terms:
            entries.sort(key=lambda entry: -entry.score.total)
        return entries

    def ranked(self, analysis: QueryAnalysis) -> list[SearchResult]:
        return [entry.result for entry in self.ranked_entries(analysis)]


def weak_share_hint(merged: list[SearchResult], analysis: QueryAnalysis) -> str:
    """输出中多数条目与核心词无关时如实提示，避免整批被当作相关结果。

    上游单字切词（如 bing 把「智谱」拆成「智」）会让候选池混入大量噪音；
    少数弱候选是常态，不提示，过半才提示。
    """
    if len(merged) < 2:
        return ""
    weak = sum(1 for result in merged if not has_core_hit(result, analysis))
    if weak * 2 > len(merged):
        return "部分结果与查询核心词无关，仅供参考。"
    return ""


def build_registry(config: SearchConfig) -> dict[str, SearchProvider]:
    registry: dict[str, SearchProvider] = {}
    for name in config.providers:
        if name == DEFAULT_PROVIDER:
            registry[name] = OpenWebSearchProvider(
                base_url=config.base_url,
                timeout=config.timeout,
                transport=config.transport,
                engines=config.engines,
                fetch_timeout=config.fetch_timeout,
            )
        elif name in _EXTERNAL_PROVIDERS:
            env_name = _EXTERNAL_PROVIDERS[name][1]
            api_key = read_api_key(env_name)
            if not api_key:
                # 未配置 Key 的厂商直接跳过：不参与尝试，也不报错。
                continue
            registry[name] = _build_external_provider(name, api_key, config.timeout)
        elif name in _OPTIONAL_KEY_PROVIDERS:
            # 无 Key 也注册：适配器按空 Key 走匿名模式，配置 Key 后自动认证。
            registry[name] = _build_optional_provider(
                name, read_api_key(_OPTIONAL_KEY_PROVIDERS[name]), config
            )
        elif name == "searxng":
            from .searxng import SearxngProvider

            registry[name] = SearxngProvider(
                config.searxng_base_url, timeout=config.searxng_timeout
            )
    return registry


def build_search_router(
    config: SearchConfig,
    vault: Path | SyvrioPaths | None = None,
) -> "SearchRouter":
    cache = None
    limiter = None
    if vault is not None:
        paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        paths.validate()
        if config.cache_ttl > 0:
            cache = SearchCache(paths.cache_dir / "search" / "search-cache.json", config.cache_ttl)
        from .usage import UsageLimiter

        limiter = UsageLimiter(config.usage_limits, paths.state_dir / "usage.json")
    return SearchRouter(
        build_registry(config),
        order=config.providers,
        max_results=config.max_results,
        cache=cache,
        engines=config.engines,
        fallback_engines=config.fallback_engines,
        max_engines=config.max_engines,
        timeout_budget=config.timeout_budget,
        max_requests=config.max_requests,
        usage_limiter=limiter,
    )


class SearchRouter:
    """按配置顺序路由；失败时降级，结果统一在这里归一化和截断。

    上游引擎回退（bing 空结果/失败 → baidu → duckduckgo...）只对支持多引擎的
    provider 生效；Syvrio 的 SearchProvider 与上游搜索引擎是两层概念，不混用。
    """

    def __init__(
        self,
        registry: dict[str, SearchProvider],
        *,
        order: list[str] | None = None,
        max_results: int = 5,
        cache: SearchCache | None = None,
        engines: list[str] | tuple[str, ...] | None = None,
        fallback_engines: Sequence[str] | None = ("baidu", "duckduckgo", "sogou"),
        max_engines: int = 3,
        timeout_budget: float = 25.0,
        max_requests: int = 9,
        usage_limiter: Any | None = None,
    ):
        self.registry = registry
        self.order = list(dict.fromkeys(order or list(registry)))
        self.max_results = max(1, max_results)
        self.cache = cache
        self.engines = tuple(engines or ())
        self.fallback_engines = tuple(fallback_engines or ())
        self.max_engines = max(1, max_engines)
        self.timeout_budget = timeout_budget
        # 单次 search() 的跨 Provider 请求次数上限：默认 9 恰好容纳
        # 单 Provider 的最坏情况（3 个引擎槽位 × 原始查询 + 2 个变体）。
        self.max_requests = max(1, max_requests)
        # 抓取调度委托独立 FetchRouter，按需惰性构建。
        self._fetch_router: FetchRouter | None = None
        # 厂商调用上限（Search/Fetch 共池）；None 表示未启用额度限制。
        self.usage_limiter = usage_limiter

    @property
    def supports_fetch(self) -> bool:
        return any("fetch" in provider.capabilities for provider in self.registry.values())

    def search(self, query: SearchQuery) -> SearchOutcome:
        text = query.query.strip()
        if not text:
            raise ValueError("搜索关键词为空")
        limit = max(1, min(query.limit or self.max_results, 15))
        engine = (query.engine or "auto").strip()
        strategy = (query.strategy or "auto").strip().lower()
        if strategy not in SEARCH_STRATEGIES:
            raise ValueError(f"unsupported search strategy: {strategy}")
        requested = normalize_search_providers(query.providers, limit=MAX_SELECTED_PROVIDERS)
        preferred = (query.preferred_provider or "auto").strip().lower()
        selection_failures: list[SearchFailure] = []
        if engine != "auto":
            # 历史兼容：显式 SearchQuery.engine 直接指定单一 provider。
            names = [engine]
            strategy = "auto"
        else:
            enabled = [name for name in self.order if name in self.registry]
            if strategy == "strict" and not requested:
                raise ValueError("strict strategy requires at least one provider")
            if not requested and preferred not in ("", "auto"):
                requested = (preferred,)
            valid = [name for name in requested if name in enabled]
            for name in requested:
                if name not in enabled:
                    selection_failures.append(
                        SearchFailure(name, "unavailable", "未启用或未注册的搜索 provider")
                    )
            if strategy == "strict":
                # strict 只信任用户/LLM 明确列出且实际启用的来源。
                names = valid
            else:
                # auto/compare 在显式列表之后补齐配置中的剩余来源，保留可靠回退。
                names = valid + [name for name in enabled if name not in valid]
        # 缓存必须区分实际使用的上游引擎，否则改配置后会命中旧引擎的结果。
        # v7：context、策略、首选/显式 Provider 与实际尝试顺序都进键。
        engines = tuple(query.engines) or self.engines
        analysis = analyze_query(text, query.context_terms)
        # 向上游多拉候选（展示条数的 3 倍，10~30），重排后仍只输出 limit；
        # 不能为了默认展示 5 条就只向上游要 5 条。
        fetch_limit = min(30, max(10, limit * 3))
        cache_key = self._cache_key(
            text,
            limit,
            engine,
            query.lang,
            query.freshness,
            names,
            engines,
            query,
            strategy,
            requested,
        )
        if strategy == "strict" and not names:
            return SearchOutcome(
                failed=selection_failures,
                hint="strict 指定的 Provider 均未启用或未注册。",
                strategy=strategy,
            )
        if self.cache is not None:
            cached = self.cache.get_record(cache_key)
            if cached is not None:
                results = cached.results
                hint = "未找到相关结果。"
                if results:
                    hint = weak_share_hint(results, analysis)
                    if not any(has_core_hit(result, analysis) for result in results):
                        hint = "候补引擎未找到与查询核心词直接相关的结果，以下为各引擎返回的候选。"
                if cached.strategy == "compare":
                    hint = f"{hint} {self._compare_note(cached.verification, cached.contributors, cached.failed)}".strip()
                return SearchOutcome(
                    results=results,
                    provider=cached.provider,
                    cached=True,
                    hint=hint,
                    failed=_dedupe_failures([*selection_failures, *cached.failed]),
                    attempted=cached.attempted,
                    strategy=cached.strategy or strategy,
                    verification=cached.verification,
                )

        failures = list(selection_failures)
        pool = CandidatePool()
        attempts: list[SearchAttempt] = []
        variants = query_variants(text, query.context_terms) if not query.engines else []
        for name in names:
            provider = self.registry.get(name)
            if provider is None:
                failures.append(SearchFailure(name, "unavailable", "未注册的搜索 provider"))
                continue
            if query.engines:
                slots = (query.engines,)
            elif "engines" in provider.capabilities:
                slots = self._engine_slots(engines)
            else:
                slots = ((),)
            for index, slot in enumerate(slots):
                for phrase in [text, *variants] if index == 0 else [text]:
                    attempts.append(
                        SearchAttempt(
                            name,
                            provider,
                            SearchQuery(
                                phrase,
                                limit=fetch_limit,
                                engine=engine,
                                lang=query.lang,
                                freshness=query.freshness,
                                engines=slot,
                            ),
                            index,
                        )
                    )
            variants = []

        run = SearchRun(
            attempts, self.timeout_budget, self.max_requests, self.usage_limiter, failures
        )
        successful: list[str] = []
        name_hint = names[-1] if names else ""
        with closing(run.results()) as responses:
            for attempt, raw in responses:
                results = normalize_results(raw)
                for result in results:
                    if not result.engine and attempt.query.engines:
                        result.engine = attempt.query.engines[0]
                if not results:
                    continue
                pool.add(results)
                name_hint = attempt.name
                if pool.has_provider(attempt.name) and attempt.name not in successful:
                    successful.append(attempt.name)
                if run.timed_out or not quality_gate(pool.ranked(analysis), analysis).should_stop:
                    continue
                if strategy == "compare" and len(successful) < 2:
                    run.skip_provider(attempt.name)
                    continue
                break

        outcome = self._final_outcome(
            failures,
            pool,
            analysis,
            limit,
            cache_key,
            name_hint=name_hint,
            budget_exhausted=run.budget_exhausted,
            timed_out=run.timed_out,
            attempted=run.attempted,
            strategy=strategy,
            contributors=successful if strategy == "compare" else pool.providers(),
        )
        if strategy == "compare":
            return self._with_compare_hint(outcome, successful, failures)
        return outcome

    def _compare_note(
        self,
        verification: str,
        contributors: Sequence[str],
        failures: Sequence[SearchFailure],
    ) -> str:
        """compare 必须如实说明是否真正完成多源检索，不把单源结果说成交叉验证。"""
        if verification == "complete":
            count = len(dict.fromkeys(contributors)) or 2
            return (
                f"交叉验证：已合并 {count} 家 Provider 的结果；同一页面被多家返回不构成独立证据。"
            )
        if verification == "unknown":
            return "交叉验证状态未知（旧缓存缺少验证信息）。"
        count = len(dict.fromkeys(contributors))
        kinds = sorted({item.kind for item in failures})
        suffix = f"（失败类型：{', '.join(kinds)}）" if kinds else ""
        return f"交叉验证未完成：仅 {count} 家 Provider 返回结果{suffix}。"

    def _with_compare_hint(
        self,
        outcome: SearchOutcome,
        successful: list[str],
        failures: list[SearchFailure],
    ) -> SearchOutcome:
        outcome.verification = "complete" if len(successful) >= 2 else "incomplete"
        note = self._compare_note(outcome.verification, successful, failures)
        outcome.hint = f"{outcome.hint} {note}".strip()
        return outcome

    def _final_outcome(
        self,
        failures: list[SearchFailure],
        pool: CandidatePool,
        analysis: QueryAnalysis,
        limit: int,
        cache_key: str,
        name_hint: str = "",
        budget_exhausted: bool = False,
        timed_out: bool = False,
        attempted: list[str] | None = None,
        strategy: str = "auto",
        contributors: list[str] | None = None,
    ) -> SearchOutcome:
        failures = _dedupe_failures(failures)
        merged = pool.ranked(analysis)[:limit]
        truncated = budget_exhausted or timed_out
        contributors = list(dict.fromkeys(contributors or []))
        verification = ""
        if strategy == "compare":
            verification = "complete" if len(contributors) >= 2 else "incomplete"
        # 预算/超时截断时如实告知，且绝不当成完整成功结果写入缓存。
        truncation_hint = ""
        if budget_exhausted:
            truncation_hint = "已达到单次搜索的请求次数上限，以下为当前最佳候选。"
        elif timed_out:
            truncation_hint = "搜索总超时预算已用尽，以下为当前最佳候选。"
        if merged and any(has_core_hit(result, analysis) for result in merged):
            # 池里至少有核心词命中：正常完成才按成功语义缓存（时效查询短 TTL）。
            if not truncated:
                self._cache_put(
                    cache_key,
                    merged,
                    name_hint,
                    analysis,
                    strategy=strategy,
                    attempted=attempted,
                    contributors=contributors,
                    verification=verification,
                    failures=failures,
                )
            weak = weak_share_hint(merged, analysis)
            hints = [item for item in (truncation_hint, weak) if item]
            return SearchOutcome(
                results=merged,
                provider=name_hint,
                failed=failures,
                hint=" ".join(hints),
                attempted=list(attempted or []),
                strategy=strategy,
                verification=verification,
            )
        if merged:
            # 候补都没命中核心词：返回候选但如实提示低相关，不伪装成精准命中；
            # 正常完成时低相关候选只短 TTL 缓存，给之后的回退留机会。
            if not truncated:
                self._cache_put(
                    cache_key,
                    merged,
                    name_hint,
                    analysis,
                    short=True,
                    strategy=strategy,
                    attempted=attempted,
                    contributors=contributors,
                    verification=verification,
                    failures=failures,
                )
            base = "候补引擎未找到与查询核心词直接相关的结果，以下为各引擎返回的候选。"
            hint = f"{base} {truncation_hint}".strip() if truncation_hint else base
            return SearchOutcome(
                results=merged,
                provider=name_hint,
                failed=failures,
                hint=hint,
                attempted=list(attempted or []),
                strategy=strategy,
                verification=verification,
            )
        if self.cache is not None and not failures and not truncated:
            # 真正的零命中（没有任何上游失败）才短 TTL 缓存；失败导致的空
            # 永不入缓存，否则验证码/限流期间会一直拿到空。
            self.cache.set(
                cache_key,
                [],
                name_hint,
                ttl=EMPTY_RESULT_TTL,
                strategy=strategy,
                attempted=list(attempted or []),
                contributors=contributors,
                verification=verification,
            )
        if failures and all(
            failure.kind
            in (
                "timeout",
                "network",
                "rate_limit",
                "unavailable",
                "bad_response",
                "auth",
                "quota",
                "budget_exhausted",
            )
            for failure in failures
        ):
            hint = "所有搜索引擎均失败。"
        else:
            hint = "未找到相关结果。"
        if any(failure.kind == "auth" for failure in failures):
            hint += " 存在鉴权失败：请检查对应厂商的 API Key 环境变量。"
        if any(failure.kind == "budget_exhausted" for failure in failures):
            hint += " 已达到单次搜索的请求次数上限。"
        if any(
            failure.provider == "searxng" and "settings.yml" in failure.detail
            for failure in failures
        ):
            hint += " SearXNG 返回 403：请确认实例已在 settings.yml 的 search.formats 中启用 json。"
        if any(failure.provider == "searxng" and failure.kind == "network" for failure in failures):
            hint += " 请确认 SearXNG 实例已启动且 searxng_base_url 可达。"
        if any(
            "open-websearch" in failure.detail or "daemon" in failure.detail for failure in failures
        ):
            hint += " 请确认 open-websearch 已启动：npx open-websearch@latest serve"
        return SearchOutcome(
            failed=failures,
            hint=hint,
            attempted=list(attempted or []),
            strategy=strategy,
        )

    def _cache_put(
        self,
        cache_key: str,
        results: list[SearchResult],
        name: str,
        analysis: QueryAnalysis,
        short: bool = False,
        *,
        strategy: str = "auto",
        attempted: list[str] | None = None,
        contributors: list[str] | None = None,
        verification: str = "",
        failures: list[SearchFailure] | None = None,
    ) -> None:
        if self.cache is None or not results:
            return
        metadata = {
            "strategy": strategy,
            "attempted": list(attempted or []),
            "contributors": list(contributors or []),
            "verification": verification,
            "failed": list(failures or []),
        }
        # 时效性查询（今天/最新/latest...）成功结果也只短缓存。
        if short or analysis.freshness_requested:
            self.cache.set(cache_key, results, name, ttl=FRESHNESS_TTL, **metadata)
        else:
            self.cache.set(cache_key, results, name, **metadata)

    def _exhausted(self, failures: list[SearchFailure]) -> SearchOutcome:
        if (
            failures
            and all(
                failure.kind in ("timeout", "network", "rate_limit", "unavailable", "bad_response")
                for failure in failures
            )
            and not any(
                "未注册" in failure.detail or "预算" in failure.detail for failure in failures
            )
        ):
            hint = "所有搜索引擎均失败。"
        else:
            hint = "未找到相关结果。"
        if any(
            "open-websearch" in failure.detail or "daemon" in failure.detail for failure in failures
        ):
            hint += " 请确认 open-websearch 已启动：npx open-websearch@latest serve"
        return SearchOutcome(failed=failures, hint=hint)

    def _engine_slots(self, default_engines: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
        """自动搜索的引擎尝试序列；显式指定引擎时不进入本方法（保持用户语义）。"""
        if default_engines:
            ordered = list(default_engines)
            ordered += [item for item in self.fallback_engines if item not in ordered]
            slots = [(item,) for item in ordered[: self.max_engines]]
        else:
            # 未配置首选引擎：首个请求交给 daemon 默认引擎，再按候补序列降级。
            slots = [()] + [
                (item,) for item in self.fallback_engines[: max(0, self.max_engines - 1)]
            ]
        if not slots:
            slots = [()]
        return tuple(slots)

    def fetch(self, url: str, max_chars: int = 4000) -> FetchResult:
        """抓取搜索结果正文；兼容入口，实际调度委托独立 FetchRouter。"""
        if self._fetch_router is None:
            self._fetch_router = FetchRouter(
                self.registry, order=self.order, usage_limiter=self.usage_limiter
            )
        return self._fetch_router.fetch(url, max_chars)

    def _cache_key(
        self,
        text: str,
        limit: int,
        engine: str,
        lang: str | None,
        freshness: str | None,
        names: list[str],
        engines: tuple[str, ...],
        query: SearchQuery,
        strategy: str = "auto",
        requested: tuple[str, ...] = (),
    ) -> str:
        # context（保留顺序，影响变体生成）、auto/explicit 引擎策略、回退
        # 序列与预算、Provider 选择策略都参与键；任一变化即视为新查询。
        engine_strategy = "explicit" if query.engines else "auto"
        context_sig = "\x1e".join(term.strip() for term in query.context_terms if term.strip())
        raw = "\x1f".join(
            [
                "search-v7",
                text,
                str(limit),
                engine,
                lang or "",
                freshness or "",
                strategy,
                engine_strategy,
                ",".join(requested),
                context_sig,
                str(self.max_engines),
                ",".join(self.fallback_engines),
                f"{self.timeout_budget:g}",
                str(self.max_requests),
                *engines,
                *names,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_results(results: list[SearchResult]) -> list[SearchResult]:
    """URL 规范化 + 请求内去重；不在这里截断，候选量由上游 fetch_limit 决定。"""
    seen: set[str] = set()
    normalized: list[SearchResult] = []
    for result in results:
        url = normalize_url(result.url)
        key = url or f"{result.title}\x00{result.snippet}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            SearchResult(
                title=result.title,
                url=url,
                snippet=result.snippet,
                published=result.published,
                provider=result.provider,
                engine=result.engine,
            )
        )
    return normalized
