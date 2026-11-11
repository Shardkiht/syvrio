"""LLM 首选 Provider 的离线回归：排序提示、回退、额度与缓存隔离。"""

from __future__ import annotations

import pytest

from syvrio.agent.session import SessionStore
from syvrio.agent.tools import build_tools
from syvrio.infra.config import ChatConfig, Config, LLMConfig, SearchConfig, UsageLimit
from syvrio.infra.search.base import (
    ProviderError,
    ProviderErrorKind,
    SearchFailure,
    SearchQuery,
    SearchResult,
)
from syvrio.infra.search.cache import SearchCache
from syvrio.infra.search.router import SearchRouter, _dedupe_failures
from syvrio.infra.search.usage import UsageLimiter
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.search


class _Provider:
    capabilities = {"search"}

    def __init__(self, name, results=None, error=None):
        self.name = name
        self.results = list(results or [])
        self.error = error
        self.calls: list[SearchQuery] = []

    def search(self, query):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.results)


def _strong(name: str, url: str) -> SearchResult:
    return SearchResult("Tibo roadmap", url, "details", None, name)


def test_auto_keeps_configured_order():
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    second = _Provider("second", [_strong("second", "https://example.com/2")])
    router = SearchRouter({"first": first, "second": second}, order=["first", "second"])
    outcome = router.search(SearchQuery("tibo"))
    assert outcome.provider == "first"
    assert not second.calls


def test_preferred_provider_is_tried_first_and_not_locked():
    weak = _Provider(
        "searxng",
        [SearchResult("Unrelated page", "https://example.com/x", "")],
    )
    strong = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": weak, "anysearch": strong},
        order=["anysearch", "searxng"],
    )
    outcome = router.search(SearchQuery("tibo", preferred_provider="searxng"))
    assert weak.calls and strong.calls
    assert outcome.provider == "anysearch"
    assert outcome.results[0].url == "https://example.com/a"


def test_unavailable_preferred_provider_degrades_with_diagnostics():
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    router = SearchRouter({"first": first}, order=["first"])
    outcome = router.search(SearchQuery("tibo", preferred_provider="not_registered"))
    assert outcome.provider == "first"
    assert any(
        item.provider == "not_registered" and item.kind == "unavailable" for item in outcome.failed
    )


def test_preferred_provider_cannot_bypass_usage_limit():
    limited = _Provider("searxng", [_strong("searxng", "https://example.com/limited")])
    backup = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    limiter = UsageLimiter({"searxng": UsageLimit(daily=1)}, clock=lambda: 0.0)
    limiter.record("searxng")
    router = SearchRouter(
        {"searxng": limited, "anysearch": backup},
        order=["anysearch", "searxng"],
        usage_limiter=limiter,
    )
    outcome = router.search(SearchQuery("tibo", preferred_provider="searxng"))
    assert not limited.calls
    assert backup.calls
    assert outcome.provider == "anysearch"
    assert any(item.kind == "quota" for item in outcome.failed)


def test_preferred_provider_respects_global_request_budget():
    weak = _Provider(
        "searxng",
        [SearchResult("Unrelated", "https://example.com/w", "")],
    )
    strong = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": weak, "anysearch": strong},
        order=["anysearch", "searxng"],
        max_requests=1,
    )
    outcome = router.search(SearchQuery("tibo", preferred_provider="searxng"))
    assert len(weak.calls) == 1 and not strong.calls
    assert any(item.kind == "budget_exhausted" for item in outcome.failed)


def test_preferred_provider_does_not_confuse_upstream_search_engine():
    class EngineProvider(_Provider):
        capabilities = {"search", "engines"}

    provider = EngineProvider("searxng", [_strong("searxng", "https://example.com/a")])
    router = SearchRouter({"searxng": provider}, order=["searxng"])
    outcome = router.search(
        SearchQuery(
            "tibo",
            engines=("baidu",),
            preferred_provider="searxng",
        )
    )
    assert outcome.results
    assert provider.calls[0].engines == ("baidu",)
    assert provider.calls[0].engine == "auto"


def test_cache_key_separates_preferred_and_auto(tmp_path):
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    second = _Provider("second", [_strong("second", "https://example.com/2")])
    router = SearchRouter(
        {"first": first, "second": second},
        order=["first", "second"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )
    preferred = router.search(SearchQuery("tibo", preferred_provider="second"))
    assert preferred.provider == "second" and not preferred.cached
    auto = router.search(SearchQuery("tibo"))
    assert auto.provider == "first"
    assert not auto.cached
    assert len(first.calls) == 1 and len(second.calls) == 1


def test_tool_schema_exposes_enabled_providers_and_preferred_param(tmp_path):
    first = _Provider("searxng", [_strong("searxng", "https://example.com/a")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/b")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
    )
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(),
        search=SearchConfig(enabled=True),
    )
    service = MemoryService(config)
    service.init_vault()
    tools = build_tools(service, SessionStore(config.vault).create(), search_router=router)
    spec = tools._tools["search"]
    preferred = spec.parameters["properties"]["preferred_provider"]
    assert preferred["enum"] == ["auto", "searxng", "anysearch"]
    assert "官方公告" in spec.description
    assert "个人爆料" in spec.description

    outcome = tools.execute("search", {"query": "tibo", "preferred_provider": "anysearch"})
    assert outcome.ok is True
    assert first.calls == []
    assert second.calls
    assert outcome.data["provider"] == "anysearch"


def test_llm_multiple_providers_run_in_requested_order():
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    second = _Provider("second", [_strong("second", "https://example.com/2")])
    third = _Provider("third", [_strong("third", "https://example.com/3")])
    router = SearchRouter(
        {"first": first, "second": second, "third": third},
        order=["first", "second", "third"],
    )
    outcome = router.search(SearchQuery("tibo", providers=("second", "third"), strategy="auto"))
    assert outcome.attempted == ["second"]
    assert not first.calls and not third.calls
    assert outcome.provider == "second"


def test_auto_falls_back_after_llm_list_quality_is_weak():
    weak = _Provider("searxng", [SearchResult("Unrelated", "https://example.com/w", "")])
    strong = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter({"searxng": weak, "anysearch": strong}, order=["searxng", "anysearch"])
    outcome = router.search(SearchQuery("tibo", providers=("searxng",), strategy="auto"))
    assert outcome.attempted == ["searxng", "anysearch"]
    assert outcome.results[0].provider == "anysearch"


def test_auto_appends_remaining_configured_providers():
    weak = _Provider("searxng", [SearchResult("Unrelated", "https://example.com/w", "")])
    backup = _Provider("open_websearch", [_strong("open_websearch", "https://example.com/o")])
    router = SearchRouter(
        {"searxng": weak, "open_websearch": backup},
        order=["open_websearch", "searxng"],
    )
    outcome = router.search(SearchQuery("tibo", providers=("searxng",), strategy="auto"))
    assert outcome.attempted == ["searxng", "open_websearch"]
    assert outcome.provider == "open_websearch"


def test_compare_reaches_at_least_two_providers():
    first = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
    )
    outcome = router.search(
        SearchQuery("tibo", providers=("searxng", "anysearch"), strategy="compare")
    )
    assert first.calls and second.calls
    assert outcome.attempted == ["searxng", "anysearch"]
    assert {item.provider for item in outcome.results} == {"searxng", "anysearch"}
    assert "交叉验证：已合并 2 家" in outcome.hint


def test_compare_reports_incomplete_when_budget_stops_before_second():
    first = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
        max_requests=1,
    )
    outcome = router.search(
        SearchQuery("tibo", providers=("searxng", "anysearch"), strategy="compare")
    )
    assert first.calls and not second.calls
    assert "交叉验证未完成" in outcome.hint
    assert outcome.attempted == ["searxng"]


def test_compare_dedupes_same_page_and_does_not_claim_independent_sources():
    first = _Provider("searxng", [_strong("searxng", "https://www.example.com/a?utm_source=x")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
    )
    outcome = router.search(SearchQuery("tibo", strategy="compare"))
    assert first.calls and second.calls
    assert len(outcome.results) == 1
    assert "同一页面" in outcome.hint and "不构成独立证据" in outcome.hint


def test_strict_only_calls_explicitly_selected_providers():
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    second = _Provider("second", [_strong("second", "https://example.com/2")])
    third = _Provider("third", [_strong("third", "https://example.com/3")])
    router = SearchRouter(
        {"first": first, "second": second, "third": third},
        order=["first", "second", "third"],
    )
    outcome = router.search(SearchQuery("tibo", providers=("second",), strategy="strict"))
    assert outcome.provider == "second"
    assert outcome.attempted == ["second"]
    assert not first.calls and not third.calls


def test_strict_requires_providers_and_reports_all_unavailable():
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    router = SearchRouter({"first": first}, order=["first"])
    with pytest.raises(ValueError, match="strict strategy requires"):
        router.search(SearchQuery("tibo", strategy="strict"))

    outcome = router.search(SearchQuery("tibo", providers=("ghost",), strategy="strict"))
    assert not first.calls
    assert not outcome.results
    assert "strict" in outcome.hint
    assert outcome.failed[0].provider == "ghost"


def test_invalid_and_duplicate_provider_names_are_safely_handled():
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    router = SearchRouter({"first": first}, order=["first"])
    outcome = router.search(
        SearchQuery(
            "tibo",
            providers=("ghost", "first", "first"),
            strategy="auto",
        )
    )
    assert outcome.provider == "first"
    assert outcome.attempted == ["first"]
    assert [item.provider for item in outcome.failed] == ["ghost"]


def test_cache_key_separates_compare_and_auto(tmp_path):
    first = _Provider("first", [_strong("first", "https://example.com/1")])
    second = _Provider("second", [_strong("second", "https://example.com/2")])
    router = SearchRouter(
        {"first": first, "second": second},
        order=["first", "second"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )
    compared = router.search(SearchQuery("tibo", strategy="compare"))
    assert set(compared.attempted) == {"first", "second"}
    auto = router.search(SearchQuery("tibo", strategy="auto"))
    assert auto.provider == "first"
    assert not auto.cached
    assert len(first.calls) == 2 and len(second.calls) == 1


def test_provider_selection_normalization_handles_json_strings_and_limits():
    from syvrio.infra.search.base import normalize_search_providers

    assert normalize_search_providers(None) == ()
    assert normalize_search_providers("") == ()
    assert normalize_search_providers("SearXNG") == ("searxng",)
    assert normalize_search_providers('["anysearch", "ANYSEARCH", "searxng"]') == (
        "anysearch",
        "searxng",
    )
    with pytest.raises(ValueError, match="at most 8"):
        normalize_search_providers([f"p{i}" for i in range(9)])
    with pytest.raises(ValueError, match="list of strings"):
        normalize_search_providers({"provider": "searxng"})


def test_compare_budget_truncation_is_not_cached(tmp_path):
    first = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
        cache=SearchCache(tmp_path / "cache.json", 60),
        max_requests=1,
    )
    first_outcome = router.search(
        SearchQuery("tibo", providers=("searxng", "anysearch"), strategy="compare")
    )
    assert "交叉验证未完成" in first_outcome.hint
    assert first_outcome.verification == "incomplete"
    assert "请求次数上限" in first_outcome.hint or "超时" in first_outcome.hint

    second_outcome = router.search(
        SearchQuery("tibo", providers=("searxng", "anysearch"), strategy="compare")
    )
    assert second_outcome.cached is False
    assert len(first.calls) == 2 and not second.calls


def test_compare_truncation_keeps_actual_contributor_verification(tmp_path):
    # 两家都已贡献结果但质量门控未达标，第三家被请求预算截断：
    # 截断结果不缓存，但验证状态必须如实反映已有两家来源。
    first = _Provider(
        "searxng",
        [SearchResult("Unrelated page", "https://example.com/s", "tibo", None, "searxng")],
    )
    second = _Provider(
        "anysearch",
        [SearchResult("Unrelated page", "https://example.com/a", "tibo", None, "anysearch")],
    )
    third = _Provider(
        "tavily",
        [SearchResult("Unrelated page", "https://example.com/t", "tibo", None, "tavily")],
    )
    router = SearchRouter(
        {"searxng": first, "anysearch": second, "tavily": third},
        order=["searxng", "anysearch", "tavily"],
        cache=SearchCache(tmp_path / "cache.json", 60),
        max_requests=2,
    )
    outcome = router.search(SearchQuery("tibo codex", strategy="compare"))

    assert outcome.verification == "complete"
    assert outcome.cached is False
    assert "交叉验证：已合并 2 家" in outcome.hint
    assert "请求次数上限" in outcome.hint
    assert len(first.calls) == 1 and len(second.calls) == 1 and not third.calls

    router.search(SearchQuery("tibo codex", strategy="compare"))
    assert len(first.calls) == 2


def test_cache_isolates_auto_and_compare_strategies(tmp_path):
    first = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )

    auto = router.search(SearchQuery("tibo"))
    compare = router.search(SearchQuery("tibo", strategy="compare"))
    cached_auto = router.search(SearchQuery("tibo"))

    assert auto.cached is False and auto.strategy == "auto"
    assert compare.cached is False and compare.strategy == "compare"
    assert compare.verification == "complete"
    assert cached_auto.cached is True
    assert cached_auto.strategy == "auto"
    assert cached_auto.attempted == ["searxng"]
    assert len(first.calls) == 2 and len(second.calls) == 1


def test_compare_cache_restores_complete_verification(tmp_path):
    first = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )
    first_outcome = router.search(SearchQuery("tibo", strategy="compare"))
    cached = router.search(SearchQuery("tibo", strategy="compare"))

    assert cached.cached is True
    assert cached.verification == "complete"
    assert cached.attempted == first_outcome.attempted == ["searxng", "anysearch"]
    assert "交叉验证：已合并 2 家" in cached.hint
    assert len(first.calls) == 1 and len(second.calls) == 1


def test_compare_cache_restores_incomplete_verification(tmp_path):
    only = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    empty = _Provider("anysearch", [])
    router = SearchRouter(
        {"searxng": only, "anysearch": empty},
        order=["searxng", "anysearch"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )
    first = router.search(SearchQuery("tibo", strategy="compare"))
    cached = router.search(SearchQuery("tibo", strategy="compare"))

    assert first.verification == "incomplete"
    assert cached.cached is True
    assert cached.verification == "incomplete"
    assert "交叉验证未完成：仅 1 家" in cached.hint
    assert cached.results and len(only.calls) == 1 and len(empty.calls) == 1


def test_compare_cache_restores_incomplete_diagnostics(tmp_path):
    first = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    failing = _Provider(
        "anysearch",
        error=ProviderError("anysearch", ProviderErrorKind.NETWORK, "upstream reset"),
    )
    router = SearchRouter(
        {"searxng": first, "anysearch": failing},
        order=["searxng", "anysearch"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )
    router.search(SearchQuery("tibo", strategy="compare"))
    cached = router.search(SearchQuery("tibo", strategy="compare"))

    assert cached.cached is True
    assert cached.verification == "incomplete"
    assert [(item.provider, item.kind) for item in cached.failed] == [("anysearch", "network")]
    assert "失败类型：network" in cached.hint
    assert len(first.calls) == 1 and len(failing.calls) == 1


def test_compare_cache_keeps_same_url_not_independent(tmp_path):
    first = _Provider("searxng", [_strong("searxng", "https://www.example.com/a?utm_source=x")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter(
        {"searxng": first, "anysearch": second},
        order=["searxng", "anysearch"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )
    first_outcome = router.search(SearchQuery("tibo", strategy="compare"))
    cached = router.search(SearchQuery("tibo", strategy="compare"))

    assert first_outcome.verification == "complete"
    assert len(first_outcome.results) == 1
    assert cached.cached is True and len(cached.results) == 1
    assert "同一页面" in cached.hint and "不构成独立证据" in cached.hint
    assert len(first.calls) == 1 and len(second.calls) == 1


def test_compare_cache_missing_verification_is_unknown(tmp_path):
    import json
    from dataclasses import asdict

    from syvrio.infra.search.cache import CACHE_VERSION

    path = tmp_path / "cache.json"
    result = _strong("searxng", "https://example.com/s")
    path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "entries": {
                    "legacy": {
                        "expires": 9999999999,
                        "provider": "searxng",
                        "strategy": "compare",
                        "results": [asdict(result)],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cache = SearchCache(path, 60)
    record = cache.get_record("legacy")
    assert record is not None
    assert record.verification == "unknown"
    assert cache.get("legacy") == ([result], "searxng")


def test_cache_hit_deduplicates_repeated_selection_failures(tmp_path):
    provider = _Provider("fake", [_strong("fake", "https://example.com/result")])
    router = SearchRouter(
        {"fake": provider},
        order=["fake"],
        cache=SearchCache(tmp_path / "cache.json", 60),
    )
    query = SearchQuery("tibo", providers=("fake", "missing"))

    first = router.search(query)
    cached = router.search(query)

    assert first.cached is False and cached.cached is True
    assert len(first.failed) == 1
    assert [item.to_dict() for item in cached.failed] == [item.to_dict() for item in first.failed]
    assert len(provider.calls) == 1


def test_failure_dedupe_keeps_distinct_kinds_and_details():
    failures = [
        SearchFailure("p", "timeout", "a"),
        SearchFailure("p", "timeout", "b"),
        SearchFailure("p", "network", "a"),
        SearchFailure("p", "timeout", "a"),
    ]
    assert [(item.provider, item.kind, item.detail) for item in _dedupe_failures(failures)] == [
        ("p", "timeout", "a"),
        ("p", "timeout", "b"),
        ("p", "network", "a"),
    ]


def test_tool_exposes_multi_provider_schema_and_rejects_strict_without_list(tmp_path):
    first = _Provider("searxng", [_strong("searxng", "https://example.com/s")])
    second = _Provider("anysearch", [_strong("anysearch", "https://example.com/a")])
    router = SearchRouter({"searxng": first, "anysearch": second}, order=["searxng", "anysearch"])
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(),
        search=SearchConfig(enabled=True),
    )
    service = MemoryService(config)
    service.init_vault()
    tools = build_tools(service, SessionStore(config.vault).create(), search_router=router)
    properties = tools._tools["search"].parameters["properties"]
    assert properties["providers"]["items"]["enum"] == ["searxng", "anysearch"]
    assert properties["strategy"]["enum"] == ["auto", "compare", "strict"]

    rejected = tools.execute("search", {"query": "tibo", "strategy": "strict"})
    assert rejected.ok is False
    assert not first.calls and not second.calls

    outcome = tools.execute(
        "search",
        {
            "query": "tibo",
            "providers": ["anysearch"],
            "strategy": "strict",
        },
    )
    assert outcome.ok is True
    assert outcome.data["attempted"] == ["anysearch"]
    assert not first.calls


def test_tool_schema_without_search_router_stays_valid(tmp_path):
    class FetchOnly:
        supports_fetch = True

        def search(self, query):
            return []

        def fetch(self, query):
            raise AssertionError

    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(),
        search=SearchConfig(enabled=True),
    )
    service = MemoryService(config)
    service.init_vault()
    tools = build_tools(service, SessionStore(config.vault).create(), search_router=FetchOnly())
    preferred = tools._tools["search"].parameters["properties"]["preferred_provider"]
    assert preferred["enum"] == ["auto"]
