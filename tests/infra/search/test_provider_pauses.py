"""供应商明确耗尽额度时，搜索和抓取共用持久化暂停状态。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from syvrio.infra.search.base import (
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
)
from syvrio.infra.search.fetch_router import FetchRouter
from syvrio.infra.search.pauses import ProviderPauses
from syvrio.infra.search.router import SearchRouter
from syvrio.infra.storage.database import database_path, transaction
from syvrio.infra.storage.paths import vault_paths

pytestmark = pytest.mark.search


@pytest.fixture(autouse=True)
def public_test_dns(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )


class _SearchProvider:
    def __init__(self, name: str, results=None, error: Exception | None = None):
        self.name = name
        self.capabilities = {"search"}
        self.results = list(results or [])
        self.error = error
        self.calls: list[SearchQuery] = []

    def search(self, query):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.results)


class _FetchProvider:
    def __init__(self, name: str, error: Exception | None = None):
        self.name = name
        self.capabilities = {"fetch"}
        self.error = error
        self.calls: list[str] = []

    def fetch(self, query):
        self.calls.append(query.url)
        if self.error is not None:
            raise self.error
        return FetchResult(query.url, query.url, "Title", "正文")


@pytest.mark.parametrize("name", ["exa", "tavily"])
def test_normal_agent_fetch_uses_independent_config(service, monkeypatch, name):
    from syvrio.agent.tools import build_tools
    from syvrio.infra import config as config_module
    from syvrio.infra.search import fetch_router as module
    from syvrio.infra.session import SessionStore

    provider = _FetchProvider(name)
    monkeypatch.setattr(config_module, "stored_key", lambda _: "synthetic-key")
    seen = []

    def build(provider_name, key, timeout):
        seen.append((provider_name, timeout))
        return provider

    monkeypatch.setattr(module, "_build_external_fetch_provider", build)
    service.config.search.enabled = True
    service.config.search.providers = ["open_websearch"]
    service.config.search.timeout = 3
    service.config.search.fetch_timeout = 47
    service.config.fetch.providers = [name]
    tools = build_tools(service, SessionStore(service.config.vault).create(), config=service.config)
    assert tools.execute("fetch_web", {"url": "https://example.com/a"}).ok
    assert tools.execute("fetch_web", {"url": "https://example.com/a"}).ok
    assert provider.calls == ["https://example.com/a", "https://example.com/a"]
    assert seen == [(name, 47)]


def test_search_and_fetch_share_credential_reads_only_within_registration(service, monkeypatch):
    from syvrio.agent.tools import build_tools
    from syvrio.infra import config
    from syvrio.infra.session import SessionStore

    names = []

    def read(name):
        names.append(name)
        return "synthetic-value"

    monkeypatch.setattr(config, "stored_key", read)
    service.config.search.enabled = True
    service.config.search.providers = service.config.fetch.providers = ["exa", "tavily"]
    session = SessionStore(service.config.vault).create()
    for _ in range(2):
        names.clear()
        tools = build_tools(service, session, config=service.config)
        assert {"search", "fetch_web"} <= {row["function"]["name"] for row in tools.schemas()}
        assert sorted(names) == ["EXA_API_KEY", "TAVILY_API_KEY"]


def test_build_tools_official_pause_shared_across_search_fetch(service, monkeypatch):
    from syvrio.agent.tools import build_tools
    from syvrio.infra.search import fetch_router as fetch_module
    from syvrio.infra.search import router as search_module
    from syvrio.infra.session import SessionStore

    search = _SearchProvider(
        "exa",
        error=ProviderError(
            "exa",
            "quota",
            "NO_MORE_CREDITS",
            code="NO_MORE_CREDITS",
            pause_reason="NO_MORE_CREDITS",
        ),
    )
    fallback = _SearchProvider(
        "fallback", [SearchResult("Synthetic entity", "https://example.com", "")]
    )
    fetch = _FetchProvider("exa")
    monkeypatch.setattr(
        search_module,
        "build_registry",
        lambda config, reader: {"exa": search, "fallback": fallback},
    )
    monkeypatch.setattr(fetch_module, "build_fetch_registry", lambda *args: {"exa": fetch})
    service.config.search.providers = ["exa", "fallback"]
    service.config.fetch.providers = ["exa"]
    session = SessionStore(service.config.vault).create()
    tools = build_tools(service, session, config=service.config)
    assert tools.execute("search", {"query": "Synthetic entity"}).ok
    assert len(search.calls) == 1 and len(fallback.calls) == 1
    assert not tools.execute("fetch_web", {"url": "https://example.com"}).ok
    assert not fetch.calls
    from syvrio.infra.search.router import build_search_router

    rebuilt = build_search_router(service.config.search, service.config.vault)
    outcome = rebuilt.search(SearchQuery("Synthetic entity", providers=("exa",), strategy="strict"))
    assert not outcome.results and outcome.failed[0].kind == "quota"
    assert len(search.calls) == 1 and len(fallback.calls) == 1


def test_success_and_transient_failures_do_not_write_usage(tmp_path):
    paths = vault_paths(tmp_path / "vault")
    pauses = ProviderPauses(paths)
    provider = _SearchProvider(
        "demo", [SearchResult("Synthetic entity", "https://example.com", "")]
    )
    fetch = _FetchProvider("demo")
    search_router = SearchRouter({"demo": provider}, provider_pauses=pauses)
    fetch_router = FetchRouter({"demo": fetch}, provider_pauses=pauses)
    with transaction(paths):
        pass
    with closing(sqlite3.connect(database_path(paths))) as watcher:
        before = watcher.execute("PRAGMA data_version").fetchone()
        for _ in range(2):
            assert search_router.search(SearchQuery("Synthetic entity")).results
            assert fetch_router.fetch("https://example.com").content == "正文"
        provider.error = ProviderError("demo", ProviderErrorKind.RATE_LIMIT, "HTTP 429")
        fetch.error = ProviderError("demo", ProviderErrorKind.NETWORK, "timeout")
        assert not search_router.search(SearchQuery("Synthetic entity")).results
        with pytest.raises(ProviderError):
            fetch_router.fetch("https://example.com")
        assert pauses.get("demo") is None
        assert watcher.execute("PRAGMA data_version").fetchone() == before
        assert not watcher.execute("SELECT 1 FROM provider_pauses").fetchall()


def test_only_active_pauses_survive_restart_expiry_and_resume(tmp_path):
    now = [1000.0]
    paths = vault_paths(tmp_path / "vault")
    store = ProviderPauses(paths, clock=lambda: now[0])
    store.note_error("exa", ProviderError("exa", "quota", "empty", pause_reason="empty"))
    store.note_error(
        "you", ProviderError("you", "quota", "reset", pause_reason="reset", resume_at=1010)
    )
    reopened = ProviderPauses(paths, clock=lambda: now[0])
    assert set(reopened.active()) == {"exa", "you"}
    assert reopened.get("exa").reason == "empty"
    assert reopened.get("you").resume_at == 1010
    now[0] = 1010
    assert reopened.get("you") is None
    assert set(reopened.active()) == {"exa"}
    reopened.resume("exa")
    assert store.active() == {}


def test_pause_and_resume_rollback_with_vault_transaction(tmp_path):
    paths = vault_paths(tmp_path / "vault")
    pauses = ProviderPauses(paths)
    error = ProviderError("demo", "quota", "empty", pause_reason="empty")
    with pytest.raises(RuntimeError, match="abort"), transaction(paths):
        pauses.note_error("demo", error)
        assert pauses.get("demo").reason == "empty"
        raise RuntimeError("abort")
    assert pauses.get("demo") is None
    pauses.note_error("demo", error)
    with pytest.raises(RuntimeError, match="abort"), transaction(paths):
        pauses.resume("demo")
        assert pauses.get("demo") is None
        raise RuntimeError("abort")
    assert pauses.get("demo").reason == "empty"


@pytest.mark.parametrize(
    ("name", "logged_name"),
    [("anysearch", "anysearch"), ("synthetic-unlisted-provider", "unknown")],
)
def test_quota_reported_on_fallback_and_skip_but_not_cache_hit(tmp_path, name, logged_name):
    from syvrio.infra.observability import diagnostics
    from syvrio.infra.search.cache import SearchCache
    from syvrio.infra.search.providers.anysearch import AnySearchProvider

    paths = vault_paths(tmp_path / "vault")
    pauses = ProviderPauses(paths)
    private = "synthetic-credential-sentinel"
    attempts = []

    def upstream(request):
        attempts.append(request.url.path)
        return httpx.Response(402, json={"message": private, "code": private})

    provider = AnySearchProvider(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(upstream))
    )
    fallback = _SearchProvider(
        "backup", [SearchResult("Synthetic entity", "https://example.com/a", "")]
    )
    router = SearchRouter(
        {name: provider, "backup": fallback}, cache=SearchCache(paths, 60), provider_pauses=pauses
    )
    query = SearchQuery("Synthetic entity")
    log = tmp_path / "logs" / "syvrio.log"
    diagnostics.stop_diagnostics()
    try:
        assert diagnostics.start_diagnostics(log) == log
        first = router.search(query)
        assert not first.cached and first.results
        assert first.to_payload()["quota_exhausted"] == [name]
        cached = router.search(query)
        assert cached.cached and cached.results
        assert cached.to_payload()["quota_exhausted"] == []
        assert not any(f.kind == "quota" for f in cached.failed)
        skipped = router.search(
            SearchQuery("Synthetic entity", providers=(name,), strategy="strict")
        )
        assert not skipped.results
        assert skipped.to_payload()["quota_exhausted"] == [name]
        assert attempts == ["/v1/search"]
    finally:
        diagnostics.stop_diagnostics()

    data = log.read_text(encoding="utf-8")
    quota_events = [
        json.loads(line) for line in data.splitlines() if "search.quota_exhausted" in line
    ]
    assert [event["state"] for event in quota_events] == [{"provider": logged_name}] * 2
    assert all("traceback" not in event for event in quota_events)
    assert private not in data
    if name != logged_name:
        assert name not in data


def test_quota_without_reset_information_skips_remaining_engines_for_this_search(tmp_path):
    paths = vault_paths(tmp_path / "vault")
    pauses = ProviderPauses(paths)
    provider = _SearchProvider("demo", error=ProviderError("demo", "quota", "HTTP 402"))
    provider.capabilities.add("engines")
    router = SearchRouter(
        {"demo": provider},
        engines=["bing"],
        fallback_engines=["baidu", "duckduckgo"],
        max_engines=3,
        provider_pauses=pauses,
    )
    for _ in range(2):
        outcome = router.search(SearchQuery("Synthetic entity"))
        assert not outcome.results
        assert outcome.to_payload()["quota_exhausted"] == ["demo"]
        assert pauses.get("demo") is None
    assert [call.engines for call in provider.calls] == [("bing",), ("bing",)]


def test_fetch_official_pause_blocks_future_search(tmp_path):
    error = ProviderError("exa", "quota", "NO_MORE_CREDITS", pause_reason="NO_MORE_CREDITS")
    fetch = _FetchProvider("exa", error=error)
    paths = vault_paths(tmp_path / "vault")
    router = FetchRouter({"exa": fetch}, provider_pauses=ProviderPauses(paths))
    with pytest.raises(ProviderError) as error:
        router.fetch("https://example.com/a")
    assert error.value.quota_exhausted == ["exa"]
    search = _SearchProvider("exa")
    other = SearchRouter({"exa": search}, provider_pauses=ProviderPauses(paths))
    result = other.search(SearchQuery("synthetic"))
    assert not search.calls
    assert result.failed[0].kind == "quota"
    assert "NO_MORE_CREDITS" in result.failed[0].detail
