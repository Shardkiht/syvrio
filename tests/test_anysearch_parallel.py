from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from syvrio.infra.config import SearchConfig, UsageLimit, load_config
from syvrio.infra.search.anysearch import AnySearchProvider
from syvrio.infra.search.base import (
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
)
from syvrio.infra.search.parallel_mcp import MCP_URL, ParallelMcpProvider
from syvrio.infra.search.router import SearchRouter, build_registry
from syvrio.infra.search.usage import UsageLimiter

pytestmark = pytest.mark.search


def _anysearch(handler, api_key: str = "") -> AnySearchProvider:
    return AnySearchProvider(
        api_key,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_anysearch_anonymous_keeps_query_and_maps_results():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _response(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "results": [
                        {
                            "title": "Java 17 Spring Boot",
                            "url": "https://example.com/java",
                            "snippet": "Spring Boot requirements",
                        },
                        {"title": "missing url", "snippet": "ignored"},
                    ],
                    "metadata": {"total_results": 2},
                },
            }
        )

    results = _anysearch(handler).search(SearchQuery("Java 17 Spring Boot", limit=5))
    assert seen["auth"] is None
    assert seen["body"] == {
        "query": "Java 17 Spring Boot",
        "max_results": 5,
        "zone": "cn",
        "language": "zh-CN",
    }
    assert results == [
        SearchResult(
            "Java 17 Spring Boot",
            "https://example.com/java",
            "Spring Boot requirements",
            None,
            "anysearch",
        )
    ]


def test_anysearch_key_uses_bearer():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _response({"code": 0, "data": {"results": []}})

    assert _anysearch(handler, "sk-placeholder").search(SearchQuery("q")) == []
    assert seen["auth"] == "Bearer sk-placeholder"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ProviderErrorKind.AUTH),
        (403, ProviderErrorKind.AUTH),
        (429, ProviderErrorKind.RATE_LIMIT),
        (500, ProviderErrorKind.UNAVAILABLE),
    ],
)
def test_anysearch_http_error_classification(status, kind):
    provider = _anysearch(lambda request: _response({"error": "x"}, status))
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is kind


def test_anysearch_timeout_and_invalid_payload():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as caught:
        _anysearch(timeout).search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.TIMEOUT

    provider = _anysearch(lambda request: httpx.Response(200, content=b"not-json"))
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.BAD_RESPONSE

    provider = _anysearch(lambda request: _response({"code": 7, "message": "bad"}))
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.BAD_RESPONSE
    assert caught.value.code == "7"


def test_anysearch_truncates_long_snippet():
    text = "x" * 900
    provider = _anysearch(
        lambda request: _response(
            {
                "code": 0,
                "data": {
                    "results": [{"title": "t", "url": "https://example.com/", "content": text}]
                },
            }
        )
    )
    results = provider.search(SearchQuery("q"))
    assert len(results[0].snippet) <= 601
    assert results[0].snippet.endswith("…")


class FakeClient:
    def __init__(self, result=None, error=None, record: dict | None = None):
        self.result = result
        self.error = error
        self.record = record if record is not None else {}

    async def __aenter__(self):
        self.record["entered"] = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.record["closed"] = True

    async def call_tool(self, name, arguments, timeout=None):
        self.record.update(name=name, arguments=arguments, timeout=timeout)
        if self.error is not None:
            raise self.error
        return self.result


def _parallel(result=None, error=None, record: dict | None = None):
    record = record if record is not None else {}
    provider = ParallelMcpProvider(
        client_factory=lambda headers, timeout: (
            record.update(headers=headers, client_timeout=timeout)
            or FakeClient(result, error, record)
        )
    )
    return provider, record


def _structured(results: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        is_error=False,
        structured_content={"results": results},
        content=[],
    )


def test_parallel_anonymous_and_key_modes_share_one_path():
    payload = _structured(
        [
            {"url": "https://example.com/a", "title": "A", "excerpts": ["one"]},
        ]
    )
    provider, record = _parallel(payload)
    assert provider.search(SearchQuery("Java 17"))[0].provider == "parallel_mcp"
    assert record["headers"] == {}
    assert record["name"] == "web_search"
    assert record["arguments"] == {
        "objective": "Java 17",
        "search_queries": ["Java 17"],
    }
    assert record["closed"] is True

    keyed = ParallelMcpProvider(
        "sk-placeholder",
        client_factory=lambda headers, timeout: (
            record.update(headers=headers) or FakeClient(payload, None, {})
        ),
    )
    keyed.search(SearchQuery("q"))
    assert record["headers"] == {"Authorization": "Bearer sk-placeholder"}


def test_parallel_structured_mapping_dedup_dates_and_truncation():
    long_excerpt = "y" * 900
    result = _structured(
        [
            {
                "url": "https://Example.com/page/#one",
                "title": "First",
                "publish_date": "2024-06-01T12:00:00Z",
                "excerpts": [long_excerpt, {"text": "second"}],
            },
            {
                "url": "https://example.com/page",
                "title": "duplicate",
                "publish_date": "2099-01-01",
                "excerpts": ["dup"],
            },
            {
                "url": "https://example.com/other",
                "title": "Other",
                "publish_date": "not-a-date",
                "excerpts": [],
            },
        ]
    )
    provider, _ = _parallel(result)
    results = provider.search(SearchQuery("q", limit=5))
    assert [item.title for item in results] == ["First", "Other"]
    assert results[0].published == "2024-06-01"
    assert results[1].published is None
    assert results[0].snippet.endswith("…")
    assert len(results[0].snippet) <= 601


def test_parallel_accepts_json_text_content_and_fenced_json():
    payload = {
        "result": {
            "results": [{"url": "https://example.com/text", "title": "Text", "excerpts": ["e"]}]
        }
    }
    result = SimpleNamespace(
        is_error=False,
        structured_content=None,
        content=[SimpleNamespace(text="```json\n" + json.dumps(payload) + "\n```")],
    )
    provider, _ = _parallel(result)
    assert provider.search(SearchQuery("q"))[0].title == "Text"


def test_parallel_tool_error_and_unparseable_payload():
    provider, record = _parallel(
        SimpleNamespace(is_error=True, structured_content=None, content=[])
    )
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.BAD_RESPONSE
    assert record["closed"] is True

    provider, _ = _parallel(SimpleNamespace(is_error=False, structured_content=None, content=[]))
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.BAD_RESPONSE


def test_parallel_timeout_and_auth_errors_are_explicit():
    provider, record = _parallel(error=TimeoutError())
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.TIMEOUT
    assert record["closed"] is True

    request = httpx.Request("POST", MCP_URL)
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("unauthorized", request=request, response=response)
    provider, _ = _parallel(error=error)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.AUTH


async def test_parallel_search_works_inside_running_event_loop():
    provider, record = _parallel(
        _structured([{"url": "https://example.com/loop", "title": "Loop"}])
    )
    results = provider.search(SearchQuery("q"))
    assert results[0].url == "https://example.com/loop"
    assert record["closed"] is True


def test_parallel_uses_real_fastmcp_protocol_in_memory():
    from fastmcp import Client, FastMCP

    server = FastMCP("synthetic-parallel")

    @server.tool
    def web_search(objective: str, search_queries: list[str]) -> dict:
        return {
            "results": [
                {
                    "url": "https://example.com/mcp",
                    "title": "MCP result",
                    "publish_date": "2024-06-01",
                    "excerpts": ["synthetic excerpt for " + search_queries[0]],
                }
            ]
        }

    provider = ParallelMcpProvider(
        client_factory=lambda headers, timeout: Client(server, timeout=timeout)
    )
    results = provider.search(SearchQuery("synthetic query"))
    assert results[0].title == "MCP result"
    assert "synthetic query" in results[0].snippet


def test_optional_providers_register_without_keys(monkeypatch):
    monkeypatch.setattr("syvrio.infra.search.router.read_api_key", lambda env_name: "")
    registry = build_registry(
        SearchConfig(providers=["anysearch", "parallel_mcp", "open_websearch"])
    )
    assert set(registry) == {"anysearch", "parallel_mcp", "open_websearch"}
    assert registry["anysearch"].name == "anysearch"
    assert registry["parallel_mcp"].name == "parallel_mcp"


def test_optional_providers_use_keys_when_present(monkeypatch):
    seen: dict[str, str] = {}

    def fake_read(env_name: str) -> str:
        seen[env_name] = "configured"
        return ""

    monkeypatch.setattr("syvrio.infra.search.router.read_api_key", fake_read)
    registry = build_registry(SearchConfig(providers=["anysearch", "parallel_mcp"]))
    assert set(registry) == {"anysearch", "parallel_mcp"}
    assert set(seen) == {"ANYSEARCH_API_KEY", "PARALLEL_API_KEY"}


def test_router_falls_back_and_usage_limit_is_local():
    class Failing:
        name = "parallel_mcp"
        capabilities = {"search"}

        def search(self, query):
            raise ProviderError("parallel_mcp", ProviderErrorKind.AUTH, "HTTP 401")

    class Working:
        name = "anysearch"
        capabilities = {"search"}
        calls = 0

        def search(self, query):
            type(self).calls += 1
            return [
                SearchResult(
                    "Synthetic query", "https://example.com", "synthetic", provider="anysearch"
                )
            ]

    router = SearchRouter(
        {"parallel_mcp": Failing(), "anysearch": Working()},
        order=["parallel_mcp", "anysearch"],
        timeout_budget=5.0,
    )
    outcome = router.search(SearchQuery("synthetic query"))
    assert outcome.results[0].provider == "anysearch"
    assert outcome.failed[0].kind == "auth"

    Working.calls = 0
    limiter = UsageLimiter({"anysearch": UsageLimit(daily=1)})
    limited = SearchRouter(
        {"anysearch": Working()},
        order=["anysearch"],
        usage_limiter=limiter,
    )
    assert limited.search(SearchQuery("synthetic query")).results
    assert Working.calls == 1
    blocked = limited.search(SearchQuery("synthetic query"))
    assert blocked.results == []
    assert blocked.failed[0].kind == "quota"
    assert Working.calls == 1


def test_config_roundtrip_keeps_anysearch_options(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[search]\nanysearch_zone = 'us'\nanysearch_language = 'en-US'\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.search.anysearch_zone == "us"
    assert config.search.anysearch_language == "en-US"
    assert config.search.usage_limits["anysearch"].daily == 20
    assert config.search.usage_limits["parallel_mcp"].daily == 20
