"""Bounded, staggered dispatch for one search's upstream requests."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from threading import BoundedSemaphore

from .base import (
    ProviderError,
    SearchFailure,
    SearchProvider,
    SearchQuery,
    SearchResult,
)
from .usage import UsageLimiter

MAX_IN_FLIGHT = 4
HEDGE_DELAY = 0.25
_PROCESS_LIMIT = 16
_POOL = ThreadPoolExecutor(max_workers=_PROCESS_LIMIT, thread_name_prefix="search-upstream")
_SLOTS = BoundedSemaphore(_PROCESS_LIMIT)


@dataclass(frozen=True)
class SearchAttempt:
    name: str
    provider: SearchProvider
    query: SearchQuery
    engine_slot: int


def _request(
    attempt: SearchAttempt, timeout: float, limiter: UsageLimiter | None
) -> tuple[list[SearchResult], list[SearchFailure], bool]:
    try:
        results = attempt.provider.search(replace(attempt.query, timeout=timeout))
        return results, list(getattr(attempt.provider, "last_partial_failures", ()) or ()), True
    except ProviderError as exc:
        if limiter is not None:
            limiter.note_error(attempt.name, exc)
        return [], [SearchFailure.from_error(exc)], False
    except Exception as exc:
        return [], [SearchFailure(attempt.name, "unavailable", str(exc))], False


class SearchRun:
    def __init__(
        self,
        attempts: list[SearchAttempt],
        timeout: float,
        max_requests: int,
        limiter: UsageLimiter | None,
        failures: list[SearchFailure],
    ) -> None:
        self.pending = attempts
        self.deadline = time.perf_counter() + timeout
        self.max_requests = max_requests
        self.limiter = limiter
        self.failures = failures
        self.attempted: list[str] = []
        self.timed_out = False
        self.budget_exhausted = False

    def skip_provider(self, name: str) -> None:
        self.pending[:] = [attempt for attempt in self.pending if attempt.name != name]

    def results(self) -> Iterator[tuple[SearchAttempt, list[SearchResult]]]:
        active: dict[Future, SearchAttempt] = {}
        sent = 0
        parallel = False
        hedge_at = None
        last_name = self.pending[0].name if self.pending else ""
        try:
            while active or self.pending:
                remaining = self.deadline - time.perf_counter()
                if remaining <= 0:
                    self.timed_out = True
                    self.failures.append(
                        SearchFailure(last_name, "timeout", "搜索总超时预算已用尽")
                    )
                    return
                if (
                    active
                    and hedge_at is not None
                    and time.perf_counter() >= hedge_at
                    and not any(future.done() for future in active)
                ):
                    parallel = True
                width = MAX_IN_FLIGHT if parallel else 1
                while len(active) < width and self.pending and sent < self.max_requests:
                    if any(future.done() for future in active) or not _SLOTS.acquire(
                        blocking=False
                    ):
                        break
                    # Prefer an independent provider before speculating on more requests to one already running.
                    active_names = {attempt.name for attempt in active.values()}
                    index = next(
                        (i for i, item in enumerate(self.pending) if item.name not in active_names),
                        0,
                    )
                    attempt = self.pending.pop(index)
                    last_name = attempt.name
                    remaining = self.deadline - time.perf_counter()
                    if remaining <= 0:
                        _SLOTS.release()
                        self.pending.insert(index, attempt)
                        break
                    try:
                        if self.limiter is not None and not self.limiter.acquire(attempt.name):
                            self.failures.append(
                                SearchFailure(
                                    attempt.name, "quota", self.limiter.reason(attempt.name)
                                )
                            )
                            self.skip_provider(attempt.name)
                            _SLOTS.release()
                            continue
                        future = _POOL.submit(
                            _request, attempt, round(max(remaining, 0.05), 3), self.limiter
                        )
                    except BaseException:
                        _SLOTS.release()
                        raise
                    future.add_done_callback(lambda _: _SLOTS.release())
                    active[future] = attempt
                    sent += 1
                    if attempt.name not in self.attempted:
                        self.attempted.append(attempt.name)
                    if not parallel:
                        hedge_at = time.perf_counter() + HEDGE_DELAY

                if not active:
                    if not self.pending:
                        return
                    if sent >= self.max_requests:
                        self.budget_exhausted = True
                        self.failures.append(
                            SearchFailure(
                                self.pending[0].name,
                                "budget_exhausted",
                                "单次搜索请求次数预算已用尽",
                            )
                        )
                        return
                    time.sleep(min(0.025, max(0.0, remaining)))
                    continue
                timeout = max(0.0, self.deadline - time.perf_counter())
                if not parallel and self.pending and sent < self.max_requests and MAX_IN_FLIGHT > 1:
                    timeout = min(timeout, max(0.0, hedge_at - time.perf_counter()))
                done, _ = wait(active, timeout=timeout, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                self.timed_out = time.perf_counter() >= self.deadline
                for future in [item for item in active if item in done]:
                    attempt = active.pop(future)
                    results, failures, ok = future.result()
                    self.failures.extend(failures)
                    if not ok:
                        self.pending[:] = [
                            item
                            for item in self.pending
                            if (item.name, item.engine_slot) != (attempt.name, attempt.engine_slot)
                        ]
                    yield attempt, results
                if self.timed_out:
                    self.failures.append(
                        SearchFailure(last_name, "timeout", "搜索总超时预算已用尽")
                    )
                    return
        finally:
            for future in active:
                future.cancel()
