"""搜索聚合的离线回归：只使用 fake provider，不访问网络。"""

from __future__ import annotations

import hashlib

import pytest

from syvrio.infra.search.base import SearchQuery, SearchResult
from syvrio.infra.search.cache import SearchCache
from syvrio.infra.search.router import SearchRouter
from syvrio.infra.storage.paths import vault_paths

pytestmark = pytest.mark.search


class FakeProvider:
    capabilities = {"search"}

    def __init__(self, name: str, results: list[SearchResult]):
        self.name = name
        self.results = results
        self.calls: list[SearchQuery] = []

    def search(self, query: SearchQuery) -> list[SearchResult]:
        self.calls.append(query)
        return list(self.results)


def row(provider: str, label: str, url: str | None = None) -> SearchResult:
    return SearchResult(
        title=f"{provider} {label}",
        url=url or f"https://{provider}.example/{label}",
        snippet=f"snippet from {provider} {label}",
        provider=provider,
    )


def test_single_provider_preserves_adapter_order_and_stable_url_dedup():
    provider = FakeProvider(
        "a",
        [
            row("a", "one", "https://www.example.com/shared?utm_source=x"),
            row("a", "two"),
            row("a", "duplicate", "https://example.com/shared"),
            row("a", "three"),
        ],
    )
    outcome = SearchRouter({"a": provider}).search(SearchQuery("q", limit=3))

    assert [item.title for item in outcome.results] == ["a one", "a two", "a three"]
    assert outcome.results[0].url == "https://example.com/shared"
    assert len(provider.calls) == 1


def test_multiple_providers_interleave_by_priority_without_reordering_rows():
    first = FakeProvider("a", [row("a", "one"), row("a", "two"), row("a", "three")])
    second = FakeProvider("b", [row("b", "one"), row("b", "two"), row("b", "three")])
    third = FakeProvider("c", [row("c", "unused")])
    router = SearchRouter({"a": first, "b": second, "c": third}, order=["a", "b", "c"])

    outcome = router.search(SearchQuery("q", limit=6))

    assert [(item.provider, item.title) for item in outcome.results] == [
        ("a", "a one"),
        ("b", "b one"),
        ("a", "a two"),
        ("b", "b two"),
        ("a", "a three"),
        ("b", "b three"),
    ]
    assert not third.calls


def test_dedup_shortfall_falls_back_and_keeps_first_interleaved_position():
    first = FakeProvider(
        "a",
        [row("a", "first", "https://www.example.com/shared?utm_source=x"), row("a", "two")],
    )
    second = FakeProvider(
        "b",
        [row("b", "duplicate", "https://example.com/shared"), row("b", "two")],
    )
    third = FakeProvider("c", [row("c", "unused")])
    router = SearchRouter({"a": first, "b": second, "c": third}, order=["a", "b", "c"])

    outcome = router.search(SearchQuery("q", limit=3))

    assert [item.title for item in outcome.results] == ["a first", "a two", "b two"]
    assert outcome.results[0].url == "https://example.com/shared"
    assert len(first.calls) == len(second.calls) == 1
    assert not third.calls


def test_same_title_and_snippet_on_distinct_urls_are_not_content_deduped():
    first = SearchResult("same", "https://a.example/one", "same")
    second = SearchResult("same", "https://b.example/two", "same")
    provider = FakeProvider("a", [first, second])

    outcome = SearchRouter({"a": provider}).search(SearchQuery("q", limit=2))

    assert [item.url for item in outcome.results] == [first.url, second.url]


def test_request_budget_stops_before_next_provider_and_reports_current_results():
    first = FakeProvider("a", [row("a", "one")])
    second = FakeProvider("b", [row("b", "one")])
    router = SearchRouter({"a": first, "b": second}, max_requests=1)

    outcome = router.search(SearchQuery("q", limit=2))

    assert [item.provider for item in outcome.results] == ["a"]
    assert not second.calls
    assert any(item.kind == "budget_exhausted" for item in outcome.failed)
    assert "请求次数上限" in outcome.hint


def test_compare_gets_two_nonempty_providers_but_same_url_is_not_independent():
    first = FakeProvider("a", [row("a", "one"), row("a", "two"), row("a", "three")])
    second = FakeProvider("b", [row("b", "duplicate", "https://a.example/one"), row("b", "other")])
    router = SearchRouter({"a": first, "b": second}, order=["a", "b"])

    outcome = router.search(SearchQuery("q", limit=3, strategy="compare"))

    assert outcome.verification == "complete"
    assert len(first.calls) == len(second.calls) == 1
    assert len(outcome.results) == 3
    assert outcome.results[0].provider == "a"
    assert "同一页面被多家返回不构成独立证据" in outcome.hint


def test_compare_budget_reports_incomplete_without_calling_second_provider():
    first = FakeProvider("a", [row("a", "one"), row("a", "two")])
    second = FakeProvider("b", [row("b", "one")])
    router = SearchRouter({"a": first, "b": second}, max_requests=1)

    outcome = router.search(SearchQuery("q", limit=2, strategy="compare"))

    assert outcome.verification == "incomplete"
    assert not second.calls
    assert "交叉验证未完成" in outcome.hint
    assert "请求次数上限" in outcome.hint


def test_v9_ranked_cache_is_invalidated(tmp_path):
    provider = FakeProvider("a", [row("a", "fresh")])
    cache = SearchCache(vault_paths(tmp_path / "vault"), ttl=60)
    router = SearchRouter({"a": provider}, cache=cache)
    legacy_raw = "\x1f".join(
        [
            "search-v9",
            "q",
            "1",
            "",
            "",
            "auto",
            "auto",
            "",
            "",
            "3",
            "baidu,duckduckgo,sogou",
            "25",
            "9",
            "a",
        ]
    )
    legacy_key = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()
    cache.set(
        legacy_key,
        [row("a", "stale", "https://a.example/stale")],
        "a",
        strategy="auto",
        attempted=["a"],
        contributors=["a"],
    )

    outcome = router.search(SearchQuery("q", limit=1))

    assert outcome.cached is False
    assert [item.title for item in outcome.results] == ["a fresh"]
    assert len(provider.calls) == 1
    assert router.search(SearchQuery("q", limit=1)).cached is True
