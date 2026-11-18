"""SearXNG 适配器离线回归：MockTransport，不访问真实实例。"""

from __future__ import annotations

import httpx
import pytest

from syvrio.infra.config import SearchConfig, UsageLimit
from syvrio.infra.search.base import (
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
)
from syvrio.infra.search.router import SearchRouter, build_registry
from syvrio.infra.search.searxng import (
    MAX_RESPONSE_BYTES,
    SearxngProvider,
    normalize_base_url,
)

pytestmark = pytest.mark.search

BASE = "http://127.0.0.1:8888"


def _provider(handler, base_url: str = BASE) -> SearxngProvider:
    return SearxngProvider(
        base_url,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_success_maps_fields_and_keeps_provider_separate_from_engine():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return _json(
            {
                "results": [
                    {
                        "title": "Spring Boot 3.5 requirements",
                        "url": "https://example.com/spring",
                        "content": "Java 17 is required.",
                        "publishedDate": "2025-05-20T08:00:00Z",
                        "engine": "bing",
                    },
                    {
                        "title": "secondary",
                        "url": "https://example.com/second",
                        "content": "",
                        "engine": ["duckduckgo", "brave"],
                    },
                ],
                "answers": ["answer card"],
                "suggestions": ["spring boot"],
            }
        )

    results = _provider(handler).search(
        SearchQuery("Spring Boot", limit=5, lang="zh-CN", freshness="week")
    )
    assert seen["params"]["q"] == "Spring Boot"
    assert seen["params"]["format"] == "json"
    assert seen["params"]["language"] == "zh-CN"
    assert seen["params"]["time_range"] == "week"
    assert results[0] == SearchResult(
        "Spring Boot 3.5 requirements",
        "https://example.com/spring",
        "Java 17 is required.",
        "2025-05-20",
        "searxng",
        "bing",
    )
    assert results[1].engine == "duckduckgo,brave"
    assert results[1].provider == "searxng"


def test_empty_results_are_normal_not_error():
    assert _provider(lambda request: _json({"results": []})).search(SearchQuery("nothing")) == []


def test_invalid_records_are_filtered_and_title_falls_back_to_url():
    payload = {
        "results": [
            {"title": "", "url": "https://example.com/no-title", "content": "ok"},
            {"title": "missing url", "content": "ignored"},
            {"title": "bad scheme", "url": "javascript:alert(1)", "content": "x"},
            {"title": "answer", "url": "https://example.com/a", "type": "answer"},
            {"title": "not a dict"},
            {
                "title": "  " + "t" * 400,
                "url": "https://example.com/long",
                "content": "c" * 800,
            },
        ]
    }
    results = _provider(lambda request: _json(payload)).search(SearchQuery("q"))
    assert [item.url for item in results] == [
        "https://example.com/no-title",
        "https://example.com/long",
    ]
    assert results[0].title == "https://example.com/no-title"
    assert results[1].title.endswith("…") and len(results[1].title) == 301
    assert results[1].snippet.endswith("…") and len(results[1].snippet) == 601


def test_internal_duplicate_urls_are_merged_with_shared_normalization():
    payload = {
        "results": [
            {
                "title": "first",
                "url": "https://www.example.com/page/?utm_source=bing",
                "content": "a",
            },
            {
                "title": "second",
                "url": "https://example.com/page?utm_medium=social",
                "content": "b",
            },
            {"title": "other", "url": "https://example.com/page?page=2", "content": "c"},
        ]
    }
    results = _provider(lambda request: _json(payload)).search(SearchQuery("q"))
    assert [item.url for item in results] == [
        "https://www.example.com/page/?utm_source=bing",
        "https://example.com/page?page=2",
    ]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2025-05-20", "2025-05-20"),
        ("2025/5/2T08:00:00Z", "2025-05-02"),
        ("2099-01-01", None),
        ("2025-13-01", None),
        ("not-a-date", None),
        (None, None),
    ],
)
def test_published_date_trust_rules(value, expected):
    payload = {
        "results": [
            {
                "title": "dated",
                "url": "https://example.com/d",
                "content": "",
                "publishedDate": value,
            }
        ]
    }
    results = _provider(lambda request: _json(payload)).search(SearchQuery("q"))
    assert results[0].published == expected


def test_403_mentions_json_format_configuration():
    provider = _provider(lambda request: httpx.Response(403, text="forbidden"))
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.BAD_RESPONSE
    assert "settings.yml" in caught.value.detail
    assert "json" in caught.value.detail


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ProviderErrorKind.AUTH),
        (402, ProviderErrorKind.QUOTA),
        (429, ProviderErrorKind.RATE_LIMIT),
        (500, ProviderErrorKind.UNAVAILABLE),
        (503, ProviderErrorKind.UNAVAILABLE),
        (404, ProviderErrorKind.BAD_RESPONSE),
    ],
)
def test_http_error_classification(status, kind):
    provider = _provider(lambda request: httpx.Response(status, text="x"))
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is kind


def test_timeout_and_connection_errors_are_classified():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as timeout:
        _provider(slow).search(SearchQuery("q"))
    assert timeout.value.kind is ProviderErrorKind.TIMEOUT
    with pytest.raises(ProviderError) as network:
        _provider(refused).search(SearchQuery("q"))
    assert network.value.kind is ProviderErrorKind.NETWORK


def test_non_json_and_oversized_responses_fail_loudly():
    with pytest.raises(ProviderError) as html:
        _provider(lambda request: httpx.Response(200, text="<html>search</html>")).search(
            SearchQuery("q")
        )
    assert html.value.kind is ProviderErrorKind.BAD_RESPONSE
    assert "JSON" in html.value.detail

    with pytest.raises(ProviderError) as oversized:
        _provider(
            lambda request: httpx.Response(200, content=b"{" + b" " * MAX_RESPONSE_BYTES)
        ).search(SearchQuery("q"))
    assert oversized.value.kind is ProviderErrorKind.BAD_RESPONSE
    assert "MiB" in oversized.value.detail


def test_partial_engine_failures_are_kept_as_diagnostics():
    payload = {
        "results": [{"title": "ok", "url": "https://example.com/a", "content": "x"}],
        "unresponsive_engines": [
            ["bing", "timeout"],
            ["duckduckgo", "HTTP 429 too many requests"],
        ],
    }
    provider = _provider(lambda request: _json(payload))
    assert len(provider.search(SearchQuery("q"))) == 1
    assert [(item.provider, item.kind) for item in provider.last_partial_failures] == [
        ("bing", "timeout"),
        ("duckduckgo", "rate_limit"),
    ]


def test_all_engines_unresponsive_with_no_results_is_an_error():
    payload = {"results": [], "unresponsive_engines": [["bing", "timeout"]]}
    with pytest.raises(ProviderError) as caught:
        _provider(lambda request: _json(payload)).search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.TIMEOUT
    assert "bing" in caught.value.detail


@pytest.mark.parametrize(
    "value",
    [
        "",
        "127.0.0.1:8888",
        "ftp://127.0.0.1/search",
        "http://user:pass@127.0.0.1:8888",
        "http://127.0.0.1:8888/search?x=1",
        "http://127.0.0.1:notaport",
    ],
)
def test_base_url_validation_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_base_url(value)


def test_base_url_allows_local_and_path_prefix():
    assert normalize_base_url("http://127.0.0.1:8888/") == "http://127.0.0.1:8888"
    assert (
        normalize_base_url("HTTPS://Searx.Local:8443/searxng/")
        == "https://searx.local:8443/searxng"
    )


def test_registry_registers_searxng_without_key_and_defaults():
    config = SearchConfig(providers=["searxng"])
    registry = build_registry(config)
    assert isinstance(registry["searxng"], SearxngProvider)
    assert config.searxng_base_url == "http://127.0.0.1:8888"
    assert config.searxng_timeout == 10.0
    assert config.usage_limits["searxng"] == UsageLimit(0, 0)


class _NamedProvider:
    capabilities = {"search"}

    def __init__(self, name, results):
        self.name = name
        self.results = results
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        return list(self.results)


def test_router_falls_back_after_searxng_failure():
    searxng = _NamedProvider("searxng", [])
    anysearch = _NamedProvider(
        "anysearch",
        [SearchResult("Tibo roadmap", "https://example.com/t", "", None, "anysearch")],
    )
    router = SearchRouter(
        {"searxng": searxng, "anysearch": anysearch},
        order=["searxng", "anysearch"],
    )
    outcome = router.search(SearchQuery("tibo"))
    assert searxng.calls and anysearch.calls
    assert outcome.provider == "anysearch"
    assert outcome.results[0].url == "https://example.com/t"
