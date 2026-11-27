"""协调常驻进程的后台工作、写请求与软件更新交接。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Lock

from ...infra.storage.locking import LockTimeout, VaultLock
from ...infra.storage.paths import syvrio_home


class ResidentActivity:
    def __init__(self) -> None:
        self._lock = Lock()
        self._writes = 0
        self._background = 0
        self._ticket = ""

    def _reserved(self) -> bool:
        if self._ticket:
            # 更新进程异常退出也会释放系统锁，后台任务无需等客户端重连才能恢复。
            lock = VaultLock(syvrio_home() / "update" / "update.lock")
            try:
                lock.acquire(timeout=0)
            except LockTimeout:
                return True
            lock.release()
            self._ticket = ""
        return False

    @property
    def background_busy(self) -> bool:
        with self._lock:
            return self._background > 0

    @contextmanager
    def work(self, *, background: bool = True) -> Iterator[bool]:
        with self._lock:
            admitted = not self._reserved()
            if admitted:
                if background:
                    self._background += 1
                else:
                    self._writes += 1
        try:
            yield admitted
        finally:
            if admitted:
                with self._lock:
                    if background:
                        self._background -= 1
                    else:
                        self._writes -= 1

    def reserve(self, ticket: str, chat_busy: Callable[[], bool]) -> bool:
        with self._lock:
            if self._reserved() or self._writes or self._background or chat_busy():
                return False
            self._ticket = ticket
            return True

    def release(self, ticket: str) -> None:
        with self._lock:
            if self._ticket == ticket:
                self._ticket = ""
