"""界面在当前模型回合中转交的可选用户补充。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, Lock


@dataclass
class InsertionRequest:
    text: str
    finished: Event = field(default_factory=Event)
    applied: bool = False


class TurnInsertion:
    def __init__(self) -> None:
        self._lock = Lock()
        self._accepting = False
        self._pending: InsertionRequest | None = None

    @contextmanager
    def model_request(self) -> Iterator[None]:
        """只在生成期间预留插入机会，执行一批工具时始终关闭插入窗口。"""
        with self._lock:
            self._accepting = True
        try:
            yield
        finally:
            with self._lock:
                self._accepting = False

    def offer(self, text: str) -> InsertionRequest | None:
        with self._lock:
            if not self._accepting or self._pending is not None:
                return None
            self._pending = InsertionRequest(text)
            return self._pending

    @property
    def pending(self) -> InsertionRequest | None:
        with self._lock:
            return self._pending

    def settle(self, *, applied: bool = False) -> None:
        with self._lock:
            if self._pending is not None:
                self._pending.applied = applied
                self._pending.finished.set()
                self._pending = None
