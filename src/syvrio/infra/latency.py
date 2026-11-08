"""Per-turn timing for chat requests. Only numeric measurements are logged."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import count
from threading import Event
from time import monotonic

from .diagnostics import record_event


_turns = count(1)
_calls = count(1)
_active_turn: ContextVar[tuple[TurnLatency, int] | None] = ContextVar("active_turn_latency", default=None)
_active_model: ContextVar[TurnLatency | None] = ContextVar("active_model_latency", default=None)


def elapsed_ms(started: float) -> int:
    return round((monotonic() - started) * 1000)


class TurnLatency:
    def __init__(self) -> None:
        self.id = next(_turns)
        self.started = monotonic()
        self.step = 0
        self.seen: set[str] = set()

    def event(self, event: str, *, step: int | None = None, **state: bool | int) -> None:
        record_event(f"latency.{event}", turn=self.id, step=self.step if step is None else step,
                     at_ms=elapsed_ms(self.started), **state)

    @contextmanager
    def model_step(self) -> Iterator[float]:
        self.step += 1
        self.seen.clear()
        started = monotonic()
        _active_turn.set((self, self.step))
        token = _active_model.set(self)
        self.event("model.started")
        try:
            yield started
        except BaseException:
            self.event("model.finished", ok=False, elapsed_ms=elapsed_ms(started))
            raise
        finally:
            _active_model.reset(token)


@contextmanager
def track_turn(cancel: Event | None) -> Iterator[TurnLatency]:
    trace = TurnLatency()
    token = _active_turn.set((trace, 0))
    trace.event("turn.started")
    ok = False
    try:
        yield trace
        ok = True
    finally:
        trace.event("turn.finished", ok=ok, cancelled=bool(cancel and cancel.is_set()))
        _active_turn.reset(token)


def mark_model(event: str) -> None:
    trace = _active_model.get()
    if trace is not None and event not in trace.seen:
        trace.seen.add(event)
        trace.event(f"model.{event}")


@contextmanager
def track_completion() -> Iterator[None]:
    """Time synchronous requests, including recall rewriting and memory extraction."""
    active = _active_turn.get()
    if active is None:
        yield
        return
    trace, step = active
    call = next(_calls)
    started = monotonic()
    trace.event("completion.started", step=step, call=call)
    ok = False
    try:
        yield
        ok = True
    finally:
        trace.event("completion.finished", step=step, call=call, ok=ok, elapsed_ms=elapsed_ms(started))
