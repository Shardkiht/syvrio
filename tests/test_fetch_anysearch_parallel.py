"""New Fetch adapters: wire contracts, safe normalization, and routing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from syvrio.infra.config import FetchConfig, SearchConfig
from syvrio.infra.search.anysearch import AnySearchFetchProvider
from syvrio.infra.search.base import FetchQuery, ProviderError, ProviderErrorKind
from syvrio.infra.search.fetch_router import FetchRouter, build_fetch_registry
from syvrio.infra.search.parallel_mcp import FETCH_MCP_URL, ParallelMcpFetchProvider
from syvrio.infra.search.usage import UsageLimiter

pytestmark = pytest.mark.search
URL = "https://example.com"


def rest(handler, key=""):
    return AnySearchFetchProvider(
        key, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )


@pytest.mark.parametrize("key", ["", "fake-key"])
def test_anysearch_extract_auth_html_and_truncation(key):
    def handler(request):
        assert str(request.url) == "https://api.anysearch.com/v1/extract"
        assert request.headers.get("authorization") == (f"Bearer {key}" if key else None)
        assert json.loads(request.content) == {"url": URL}
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "url": URL,
                    "final_url": URL + "/redirect",
                    "title": "Example",
                    "content": "<article><script>ignore</script><p>One &amp; Two</p></article>",
                },
            },
        )

    page = rest(handler, key).fetch(FetchQuery(URL, 5))
    assert (page.final_url, page.title, page.content, page.truncated) == (
        URL + "/redirect",
        "Example",
        "One &",
        True,
    )


@pytest.mark.parametrize(
    "status,kind",
    [
        (302, "bad_response"),
        (401, "auth"),
        (402, "quota"),
        (403, "auth"),
        (422, "bad_response"),
        (429, "rate_limit"),
        (503, "unavailable"),
    ],
)
def test_anysearch_extract_status_does_not_expose_error_body(status, kind):
    def handler(request):
        return httpx.Response(
            status,
            headers={"Location": "https://unrelated.invalid"},
            json={"message": "upstream credential"},
        )

    with pytest.raises(ProviderError) as caught:
        rest(handler, "fake-key").fetch(FetchQuery(URL))
    assert caught.value.kind.value == kind
    assert "upstream credential" not in str(caught.value)
    assert bool(caught.value.pause_reason) == (status == 402)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"code":0,"data":{"content":" "}}',
        b'{"code":0,"data":{"content":"<script>ignored</script>"}}',
        b'{"code":9,"data":{"content":"must not use"}}',
    ],
)
def test_anysearch_empty_or_malformed(body):
    with pytest.raises(ProviderError) as caught:
        rest(lambda r: httpx.Response(200, content=body)).fetch(FetchQuery(URL))
    assert caught.value.kind == ProviderErrorKind.BAD_RESPONSE


class StubClient:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("structured,key", [(True, "fake-key"), (False, "")])
def test_parallel_extract_structured_and_legacy_content(structured, key):
    data = {
        "results": [
            {
                "url": URL + "/final",
                "title": "Example",
                "full_content": "Complete article" if structured else None,
                "excerpts": ["First excerpt", "Second excerpt"],
            }
        ]
    }
    client = StubClient(
        SimpleNamespace(
            structured_content=data if structured else None,
            is_error=False,
            content=[SimpleNamespace(text=json.dumps(data))],
        )
    )

    def factory(headers, timeout):
        assert headers == ({"Authorization": f"Bearer {key}"} if key else {})
        assert timeout == 3
        return client

    page = ParallelMcpFetchProvider(key, timeout=3, client_factory=factory).fetch(
        FetchQuery(URL, 14)
    )
    assert client.calls == [
        (
            "web_fetch",
            {"urls": [URL], "full_content": True},
            {"timeout": 3, "raise_on_error": False},
        )
    ]
    assert client.closed
    assert (page.final_url, page.title, page.content, page.truncated) == (
        URL + "/final",
        "Example",
        "Complete artic" if structured else "First excerpt\n",
        True,
    )


@pytest.mark.parametrize(
    "result,kind,paused",
    [
        (TimeoutError(), "timeout", False),
        (ConnectionError(), "network", False),
        (
            SimpleNamespace(
                is_error=True, content=[SimpleNamespace(text="Unknown tool web_fetch")]
            ),
            "bad_response",
            False,
        ),
        (
            SimpleNamespace(is_error=True, content=[SimpleNamespace(text="HTTP 402 secret")]),
            "quota",
            True,
        ),
        (
            SimpleNamespace(
                is_error=False,
                structured_content={
                    "results": [{"url": URL, "full_content": " ", "excerpts": [None, " "]}]
                },
            ),
            "bad_response",
            False,
        ),
        (
            SimpleNamespace(
                is_error=False,
                structured_content={
                    "results": [],
                    "errors": [
                        {"url": URL, "http_status_code": 429, "content": "target-side limit"}
                    ],
                },
            ),
            "bad_response",
            False,
        ),
    ],
)
def test_parallel_errors_close_and_never_turn_response_into_page(result, kind, paused):
    client = StubClient(result)
    provider = ParallelMcpFetchProvider(client_factory=lambda *_: client)
    with pytest.raises(ProviderError) as caught:
        provider.fetch(FetchQuery(URL))
    assert caught.value.kind.value == kind
    assert bool(caught.value.pause_reason) == paused
    assert "secret" not in str(caught.value)
    assert client.closed


def test_parallel_streamable_http_initializes_calls_once_and_closes(monkeypatch):
    import httpx2
    from fastmcp.client.transports import StreamableHttpTransport

    from syvrio.infra.search import parallel_mcp

    calls = []

    async def server(request):
        assert str(request.url) == FETCH_MCP_URL
        assert request.headers["authorization"] == "Bearer test-placeholder"
        if request.method == "GET":
            return httpx2.Response(405)
        if request.method == "DELETE":
            calls.append("DELETE")
            assert request.headers["mcp-session-id"] == "test-session"
            return httpx2.Response(204)
        message = json.loads(request.content)
        method = message["method"]
        calls.append(method)
        reply = {"jsonrpc": "2.0", "id": message.get("id")}
        if method == "server/discover":
            return httpx2.Response(
                200, json=reply | {"error": {"code": -32601, "message": "Method not found"}}
            )
        if method == "initialize":
            reply["result"] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
            return httpx2.Response(
                200,
                headers={"mcp-session-id": "test-session", "content-type": "text/event-stream"},
                text="event: message\ndata: " + json.dumps(reply) + "\n\n",
            )
        assert request.headers["mcp-session-id"] == "test-session"
        if method == "notifications/initialized":
            return httpx2.Response(202)
        if method == "tools/list":
            return httpx2.Response(
                200,
                json=reply
                | {"result": {"tools": [{"name": "web_fetch", "inputSchema": {"type": "object"}}]}},
            )
        assert method == "tools/call"
        assert message["params"]["name"] == "web_fetch"
        reply["result"] = {
            "content": [],
            "structuredContent": {"results": [{"url": URL, "full_content": "page"}]},
        }
        return httpx2.Response(200, json=reply)

    from fastmcp import Client

    def mock_sdk_client(headers, timeout, url):
        def http_client(**kwargs):
            return httpx2.AsyncClient(transport=httpx2.MockTransport(server), **kwargs)

        return Client(
            StreamableHttpTransport(url, headers=headers, httpx_client_factory=http_client),
            timeout=timeout,
            init_timeout=timeout,
        )

    monkeypatch.setattr(parallel_mcp, "_default_client", mock_sdk_client)
    try:
        result = ParallelMcpFetchProvider("test-placeholder", timeout=2).fetch(FetchQuery(URL))
    except ProviderError as error:
        pytest.fail(f"{error}; requests sent: {calls}")
    assert result.content == "page"
    assert [method for method in calls if method not in ("server/discover", "tools/list")] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "DELETE",
    ]


def test_registry_preserves_order_and_keeps_keys_optional(monkeypatch):
    from syvrio.infra import config

    providers = [
        "exa",
        "parallel_mcp",
        "open_websearch",
        "anysearch",
        "tavily",
        "you",
        "volcengine",
    ]
    keys = {"EXA_API_KEY", "TAVILY_API_KEY", "YDC_API_KEY", "VOLCENGINE_SEARCH_API_KEY"}
    requested = []

    def read_key(name):
        requested.append(name)
        return "dummy" if name in keys else ""

    monkeypatch.setattr(config, "read_api_key", read_key)
    registry = build_fetch_registry(FetchConfig(providers=providers), SearchConfig())
    assert list(registry) == providers
    assert {"ANYSEARCH_API_KEY", "PARALLEL_API_KEY"} <= set(requested)
    assert all("fetch" in instance.capabilities for instance in registry.values())


def test_fetch_quota_fallback_shares_limiter_and_sanitizes_final_url(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    attempts = []

    def quota(request):
        attempts.append("anysearch")
        return httpx.Response(402, json={"message": "private response"})

    def parallel(headers, timeout):
        attempts.append("parallel_mcp")
        return StubClient(
            SimpleNamespace(
                structured_content={"results": [{"url": URL, "excerpts": ["fallback content"]}]}
            )
        )

    limiter = UsageLimiter(SearchConfig().usage_limits)
    router = FetchRouter(
        {
            "anysearch": rest(quota),
            "parallel_mcp": ParallelMcpFetchProvider(client_factory=parallel),
        },
        usage_limiter=limiter,
    )
    with pytest.raises(ValueError):
        router.fetch("http://127.0.0.1/")
    assert not attempts
    assert router.fetch(URL).content == "fallback content"
    assert router.fetch(URL).content == "fallback content"
    assert attempts == ["anysearch", "parallel_mcp", "parallel_mcp"]
    assert "official_quota_exhausted" in limiter.reason("anysearch")

    redirect = rest(
        lambda r: httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "final_url": "http://169.254.169.254",
                    "content": "private",
                },
            },
        )
    )
    with pytest.raises(ValueError):
        FetchRouter({"anysearch": redirect}).fetch(URL)
