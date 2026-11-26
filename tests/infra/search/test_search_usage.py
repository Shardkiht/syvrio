"""轻量额度限制（UsageLimiter）的回归测试：全部用假时钟与临时文件。"""

from __future__ import annotations

import pytest

from syvrio.infra.config import FetchConfig, SearchConfig, UsageLimit
from syvrio.infra.search.base import (
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
)
from syvrio.infra.search.fetch_router import FetchRouter, build_fetch_router
from syvrio.infra.search.router import SearchRouter
from syvrio.infra.search.usage import UsageLimiter
from syvrio.infra.storage.database import transaction
from syvrio.infra.storage.paths import vault_paths

pytestmark = pytest.mark.search


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


def test_unlimited_provider_is_never_blocked():
    limiter = UsageLimiter({}, clock=lambda: 0.0)
    assert limiter.remaining("tavily") is None
    assert limiter.allows("tavily")
    limiter.record("tavily")
    assert limiter.remaining("tavily") is None


def test_daily_window_counts_and_resets():
    now = [1_700_000_000.0]
    limits = {"tavily": UsageLimit(daily=2)}
    limiter = UsageLimiter(limits, clock=lambda: now[0])

    assert limiter.remaining("tavily") == 2
    limiter.record("tavily")
    limiter.record("tavily")
    assert limiter.remaining("tavily") == 0
    assert not limiter.allows("tavily")

    now[0] += 86_400  # 越过一天：计数重置
    assert limiter.remaining("tavily") == 2
    assert limiter.allows("tavily")


def test_monthly_window_counts_and_resets():
    now = [1_700_000_000.0]  # 2023-11
    limits = {"exa": UsageLimit(monthly=1)}
    limiter = UsageLimiter(limits, clock=lambda: now[0])
    limiter.record("exa")
    assert not limiter.allows("exa")
    now[0] += 40 * 86_400  # 跨月
    assert limiter.allows("exa")


def _persisted_usage(paths):
    result = {}
    with transaction(paths, write=False) as db:
        for provider, period, window, count in db.execute("SELECT * FROM usage"):
            result.setdefault(provider, {})[period] = {"window": window, "count": count}
        for provider, reason, resume_at in db.execute("SELECT * FROM quota_pauses"):
            result[provider]["paused"] = {"reason": reason, "resume_at": resume_at}
    return result


def test_counts_persist_across_instances(tmp_path):
    path = vault_paths(tmp_path / "vault")
    limits = {"tavily": UsageLimit(daily=3)}
    first = UsageLimiter(limits, path, clock=lambda: 0.0)
    first.record("tavily")

    second = UsageLimiter(limits, path, clock=lambda: 0.0)
    assert second.remaining("tavily") == 2
    data = _persisted_usage(path)
    assert set(data["tavily"]) == {"daily", "monthly"}
    assert data["tavily"]["daily"]["count"] == data["tavily"]["monthly"]["count"] == 1


def test_usage_and_pause_follow_the_vault_transaction(tmp_path):
    paths = vault_paths(tmp_path / "vault")
    limiter = UsageLimiter({}, paths)
    limiter.record("exa")

    with pytest.raises(RuntimeError, match="abort"), transaction(paths) as db:
        limiter.record("exa")
        limiter.pause("exa", "quota exhausted")
        assert (
            db.execute(
                "SELECT units FROM usage WHERE provider='exa' AND period='daily'"
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute("SELECT reason FROM quota_pauses WHERE provider='exa'").fetchone()[0]
            == "quota exhausted"
        )
        raise RuntimeError("abort")

    with transaction(paths, write=False):
        state = limiter.status("exa")
    assert state["daily"]["used"] == 1 and not state["paused"]


def test_reload_sees_other_instance_writes(tmp_path):
    path = vault_paths(tmp_path / "vault")
    limits = {"volcengine": UsageLimit(daily=2)}
    searcher = UsageLimiter(limits, path, clock=lambda: 0.0)
    fetcher = UsageLimiter(limits, path, clock=lambda: 0.0)

    searcher.record("volcengine")
    # Fetch 实例在 Search 实例落盘后应看到同一计数池。
    assert fetcher.remaining("volcengine") == 1


def test_empty_database_starts_with_full_budget(tmp_path):
    path = vault_paths(tmp_path / "vault")
    limiter = UsageLimiter({"tavily": UsageLimit(daily=1)}, path, clock=lambda: 0.0)
    assert limiter.remaining("tavily") == 1


def _limited_router(tmp_path, pre_used: int = 0):
    limited = _SearchProvider("limited", [SearchResult("无关", "https://example.com/w", "")])
    fallback = _SearchProvider(
        "fallback", [SearchResult("Tibo roadmap", "https://example.com/s", "d")]
    )
    limiter = UsageLimiter(
        {"limited": UsageLimit(daily=1)},
        vault_paths(tmp_path / "vault"),
        clock=lambda: 0.0,
    )
    for _ in range(pre_used):
        limiter.record("limited")
    router = SearchRouter(
        {"limited": limited, "fallback": fallback},
        order=["limited", "fallback"],
        usage_limiter=limiter,
    )
    return router, limited, fallback, limiter


def test_search_falls_back_when_provider_limit_reached(tmp_path):
    router, limited, fallback, _ = _limited_router(tmp_path, pre_used=1)
    outcome = router.search(SearchQuery("tibo"))

    assert limited.calls == []
    assert fallback.calls
    assert outcome.results[0].url == "https://example.com/s"
    assert any(
        failure.kind == "quota" and failure.provider == "limited" for failure in outcome.failed
    )


def test_search_counts_failed_requests(tmp_path):
    broken = _SearchProvider(
        "limited", error=ProviderError("limited", ProviderErrorKind.NETWORK, "reset")
    )
    fallback = _SearchProvider("fallback", [SearchResult("Tibo", "https://example.com/s", "d")])
    limiter = UsageLimiter(
        {"limited": UsageLimit(daily=1)},
        vault_paths(tmp_path / "vault"),
        clock=lambda: 0.0,
    )
    router = SearchRouter(
        {"limited": broken, "fallback": fallback},
        order=["limited", "fallback"],
        usage_limiter=limiter,
    )

    router.search(SearchQuery("tibo"))  # 失败也计数 → limited 额度用尽
    router.search(SearchQuery("tibo"))

    assert len(broken.calls) == 1  # 第二次不再调用（无无限重试）
    assert limiter.remaining("limited") == 0


def test_fetch_and_search_share_provider_pool(tmp_path):
    search_provider = _SearchProvider("tavily", [SearchResult("无关", "https://example.com/w", "")])
    fetch_provider = _FetchProvider("tavily")
    limiter = UsageLimiter(
        {"tavily": UsageLimit(daily=1)},
        vault_paths(tmp_path / "vault"),
        clock=lambda: 0.0,
    )
    search_router = SearchRouter({"tavily": search_provider}, usage_limiter=limiter)
    fetch_router = FetchRouter({"tavily": fetch_provider}, order=["tavily"], usage_limiter=limiter)

    search_router.search(SearchQuery("tibo"))  # Search 用掉唯一一次额度
    outcome = search_router.search(SearchQuery("tibo"))
    assert len(search_provider.calls) == 1
    assert any(f.kind == "quota" for f in outcome.failed)

    # Fetch 与 Search 同池：额度已用尽时 Fetch 不发请求、直接报错。
    assert limiter.remaining("tavily") == 0
    with pytest.raises(ProviderError, match="已达到本地调用上限"):
        fetch_router.fetch("https://example.com/a", 1000)
    assert fetch_provider.calls == []


def test_fetch_router_falls_back_when_limit_reached(tmp_path):
    limited = _FetchProvider("limited")
    fallback = _FetchProvider("fallback")
    limiter = UsageLimiter(
        {"limited": UsageLimit(daily=1)},
        vault_paths(tmp_path / "vault"),
        clock=lambda: 0.0,
    )
    limiter.record("limited")  # 预记账：额度已用尽
    router = FetchRouter(
        {"limited": limited, "fallback": fallback},
        order=["limited", "fallback"],
        usage_limiter=limiter,
    )

    result = router.fetch("https://example.com/a", 1000)
    assert limited.calls == []
    assert fallback.calls == ["https://example.com/a"]
    assert result.content == "正文"


def test_release_gives_back_one_pre_charged_call(tmp_path):
    path = vault_paths(tmp_path / "vault")
    limiter = UsageLimiter({"tavily": UsageLimit(daily=2)}, path, clock=lambda: 0.0)
    reservation = limiter.acquire("tavily")
    assert reservation is not None
    limiter.release(reservation)
    limiter.release(reservation)

    assert limiter.remaining("tavily") == 2
    data = _persisted_usage(path)
    assert set(data["tavily"]) == {"daily", "monthly"}
    assert data["tavily"]["daily"]["count"] == data["tavily"]["monthly"]["count"] == 0


@pytest.mark.parametrize("day,month", [(15, 1), (16, 1), (1, 2)])
def test_failed_fetch_refunds_only_its_original_windows(tmp_path, day, month):
    from datetime import datetime

    now = [datetime(2025, 1, 15, 12).timestamp()]
    path = vault_paths(tmp_path / "vault")
    limiter = UsageLimiter({}, path, clock=lambda: now[0])
    other = UsageLimiter({}, path, clock=lambda: now[0])

    class DelayedFailure(_FetchProvider):
        def fetch(self, query):
            now[0] = datetime(2025, month, day, 13).timestamp()
            assert other.acquire(self.name) is not None
            raise ProviderError(self.name, ProviderErrorKind.NETWORK, "connection failed")

    provider = DelayedFailure("tavily")
    with pytest.raises(ProviderError, match="connection failed"):
        FetchRouter({"tavily": provider}, usage_limiter=limiter).fetch("https://example.com/a")
    status = other.status("tavily")
    assert status["daily"]["used"] == status["monthly"]["used"] == 1


def test_failed_fetch_does_not_count_as_a_used_call(tmp_path):
    broken = _FetchProvider(
        "limited", error=ProviderError("limited", ProviderErrorKind.UNAVAILABLE, "HTTP 502")
    )
    fallback = _FetchProvider("fallback")
    limiter = UsageLimiter(
        {"limited": UsageLimit(daily=1)},
        vault_paths(tmp_path / "vault"),
        clock=lambda: 0.0,
    )
    router = FetchRouter(
        {"limited": broken, "fallback": fallback},
        order=["limited", "fallback"],
        usage_limiter=limiter,
    )

    result = router.fetch("https://example.com/a", 1000)

    # 失败的抓取退回预扣次数：下次仍会给该 provider 一次机会。
    assert len(broken.calls) == 1
    assert limiter.remaining("limited") == 1
    assert result.content == "正文"


def test_successful_fetch_still_counts(tmp_path):
    provider = _FetchProvider("tavily")
    limiter = UsageLimiter(
        {"tavily": UsageLimit(daily=2)},
        vault_paths(tmp_path / "vault"),
        clock=lambda: 0.0,
    )
    router = FetchRouter({"tavily": provider}, order=["tavily"], usage_limiter=limiter)

    router.fetch("https://example.com/a", 1000)

    assert limiter.remaining("tavily") == 1


def test_build_routers_wire_limiter_from_config(tmp_path):
    search_config = SearchConfig(usage_limits={"tavily": UsageLimit(daily=5)})
    from syvrio.infra.config import load_config

    router = build_fetch_router(FetchConfig(), search_config, tmp_path / "vault")
    assert router.usage_limiter is not None
    assert router.usage_limiter.remaining("tavily") == 5

    config_path = tmp_path / "syvrio.toml"
    config_path.write_text(
        "[search.usage_limits.exa]\ndaily = 7\nmonthly = 0\n",
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    assert loaded.search.usage_limits["exa"] == UsageLimit(daily=7)


def test_request_budget_does_not_charge_unattempted_provider(tmp_path):
    first, second = _SearchProvider("first"), _SearchProvider("second")
    limiter = UsageLimiter({name: UsageLimit(daily=5) for name in ("first", "second")})
    router = SearchRouter(
        {"first": first, "second": second},
        order=["first", "second"],
        max_requests=1,
        usage_limiter=limiter,
    )
    router.search(SearchQuery("synthetic query"))
    assert len(first.calls) == 1 and not second.calls
    assert limiter.remaining("first") == 4
    assert limiter.remaining("second") == 5


def test_daily_limit_rejects_second_fetch():
    provider = _FetchProvider("exa")
    limiter = UsageLimiter({"exa": UsageLimit(daily=1)})
    router = FetchRouter({"exa": provider}, usage_limiter=limiter)
    router.fetch("https://example.com/a")
    with pytest.raises(ProviderError):
        router.fetch("https://example.com/a")
    assert len(provider.calls) == 1


@pytest.mark.parametrize("name", ["exa", "tavily"])
def test_normal_agent_fetch_uses_independent_config(service, monkeypatch, name):
    from syvrio.agent.tools import build_tools
    from syvrio.infra import config as config_module
    from syvrio.infra.search import fetch_router as module
    from syvrio.infra.session import SessionStore

    provider = _FetchProvider(name)
    monkeypatch.setattr(config_module, "read_api_key", lambda _: "synthetic-key")
    seen = []

    def build(provider_name, key, timeout):
        seen.append((provider_name, timeout))
        return provider

    monkeypatch.setattr(module, "_build_external_fetch_provider", build)
    service.config.search.enabled = True
    service.config.search.providers = ["open_websearch"]
    service.config.search.timeout = 3
    service.config.search.fetch_timeout = 47
    service.config.search.usage_limits = {name: UsageLimit(daily=1)}
    service.config.fetch.providers = [name]
    tools = build_tools(service, SessionStore(service.config.vault).create())
    assert tools.execute("fetch_web", {"url": "https://example.com/a"}).ok
    assert not tools.execute("fetch_web", {"url": "https://example.com/a"}).ok
    assert provider.calls == ["https://example.com/a"]
    assert seen == [(name, 47)]


@pytest.mark.parametrize(
    "name,daily,monthly",
    [
        ("baidu_search", 50, 0),
        ("tavily", 0, 800),
        ("exa", 0, 1000),
        ("you", 0, 1500),
        ("volcengine", 0, 450),
        ("open_websearch", 0, 0),
    ],
)
def test_runtime_defaults_without_setup(tmp_path, name, daily, monthly):
    from syvrio.infra.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(f"[search]\nproviders = ['{name}']\n", encoding="utf-8")
    limit = load_config(path).search.usage_limits[name]
    assert (limit.daily, limit.monthly) == (daily, monthly)


def test_explicit_zero_and_partial_overrides(tmp_path):
    from syvrio.infra.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        "[search.usage_limits.tavily]\ndaily=0\nmonthly=0\n[search.usage_limits.exa]\ndaily=7\n",
        encoding="utf-8",
    )
    limits = load_config(path).search.usage_limits
    assert (limits["tavily"].daily, limits["tavily"].monthly) == (0, 0)
    assert (limits["exa"].daily, limits["exa"].monthly) == (7, 1000)


def test_dual_windows_and_restart(tmp_path):
    from datetime import datetime

    now = [datetime(2025, 1, 15, 12).timestamp()]
    path = vault_paths(tmp_path / "vault")
    limits = {"exa": UsageLimit(daily=1, monthly=2)}
    limiter = UsageLimiter(limits, path, clock=lambda: now[0])
    assert limiter.acquire("exa") and not limiter.acquire("exa")
    now[0] += 86400
    restarted = UsageLimiter(limits, path, clock=lambda: now[0])
    assert restarted.acquire("exa")
    now[0] += 86400
    assert not restarted.acquire("exa")  # 即使跨天，月度额度仍已耗尽。
    now[0] = datetime(2025, 2, 1, 12).timestamp()
    assert restarted.acquire("exa")


def test_persisted_state_contains_only_both_windows_and_optional_pause(tmp_path):
    from datetime import datetime

    path = vault_paths(tmp_path / "vault")
    clock = datetime(2025, 1, 15, 12).timestamp
    limiter = UsageLimiter({"exa": UsageLimit(daily=10, monthly=20)}, path, clock=clock)
    assert limiter.acquire("exa")
    limiter.pause("exa", "balance")
    expected = {
        "daily": {"window": "2025-01-15", "count": 1},
        "monthly": {"window": "2025-01", "count": 1},
        "paused": {"reason": "balance", "resume_at": None},
    }
    assert _persisted_usage(path) == {"exa": expected}
    restarted = UsageLimiter({}, path, clock=clock)
    assert not restarted.allows("exa")
    restarted.resume("exa")
    del expected["paused"]
    assert _persisted_usage(path) == {"exa": expected}
    assert set(restarted.status("exa")) == {"daily", "monthly", "paused"}


def test_concurrent_instances_cannot_exceed_limit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = vault_paths(tmp_path / "vault")
    limits = {"exa": UsageLimit(daily=7, monthly=10)}
    instances = [UsageLimiter(limits, path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda i: instances[i % 4].acquire("exa"), range(50)))
    assert sum(receipt is not None for receipt in accepted) == 7
    assert instances[0].status("exa")["monthly"]["used"] == 7


def test_pause_restart_expiry_and_manual_resume(tmp_path):
    now = [1000.0]
    path = vault_paths(tmp_path / "vault")
    limiter = UsageLimiter({}, path, clock=lambda: now[0])
    limiter.pause("exa", "balance")
    restarted = UsageLimiter({}, path, clock=lambda: now[0])
    assert not restarted.acquire("exa")
    restarted.resume("exa")
    assert restarted.acquire("exa")
    restarted.pause("exa", "confirmed_reset", 1100.0)
    assert not limiter.acquire("exa")
    now[0] = 1100
    assert limiter.acquire("exa")
    assert limiter.status("exa")["daily"]["used"] == 2


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
        search_module, "build_registry", lambda config: {"exa": search, "fallback": fallback}
    )
    monkeypatch.setattr(fetch_module, "build_fetch_registry", lambda *args: {"exa": fetch})
    service.config.search.providers = ["exa", "fallback"]
    service.config.fetch.providers = ["exa"]
    session = SessionStore(service.config.vault).create()
    tools = build_tools(service, session)
    assert tools.execute("search", {"query": "Synthetic entity"}).ok
    assert len(search.calls) == 1 and len(fallback.calls) == 1
    assert not tools.execute("fetch_web", {"url": "https://example.com"}).ok
    assert not fetch.calls
    from syvrio.infra.search.router import build_search_router

    rebuilt = build_search_router(service.config.search, service.config.vault)
    outcome = rebuilt.search(SearchQuery("Synthetic entity", providers=("exa",), strategy="strict"))
    assert not outcome.results and outcome.failed[0].kind == "quota"
    assert len(search.calls) == 1 and len(fallback.calls) == 1


def test_search_cache_does_not_charge_or_dispatch(tmp_path):
    from syvrio.infra.search.cache import SearchCache

    provider = _SearchProvider(
        "exa", [SearchResult("Synthetic entity", "https://example.com/a", "")]
    )
    limiter = UsageLimiter({"exa": UsageLimit(daily=1, monthly=2)}, vault_paths(tmp_path / "vault"))
    router = SearchRouter(
        {"exa": provider},
        cache=SearchCache(vault_paths(tmp_path / "vault"), 60),
        usage_limiter=limiter,
    )
    assert router.search(SearchQuery("Synthetic entity")).results
    limiter.pause("exa", "balance")
    assert router.search(SearchQuery("Synthetic entity")).cached
    assert len(provider.calls) == 1 and limiter.status("exa")["daily"]["used"] == 1


def test_fetch_official_pause_blocks_future_search(tmp_path):
    error = ProviderError("exa", "quota", "NO_MORE_CREDITS", pause_reason="NO_MORE_CREDITS")
    fetch = _FetchProvider("exa", error=error)
    path = vault_paths(tmp_path / "vault")
    router = FetchRouter({"exa": fetch}, usage_limiter=UsageLimiter({}, path))
    with pytest.raises(ProviderError):
        router.fetch("https://example.com/a")
    search = _SearchProvider("exa")
    other = SearchRouter({"exa": search}, usage_limiter=UsageLimiter({}, path))
    result = other.search(SearchQuery("synthetic"))
    assert not search.calls and "Official quota pause" in result.failed[0].detail
