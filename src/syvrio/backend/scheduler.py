"""按 Git 已有的到期规则运行同步，无需客户端保持连接。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager

from ..application.git import GitUseCases
from ..infra.observability.diagnostics import record_event


class GitScheduler:
    def __init__(self, git: GitUseCases, work: Callable[[], AbstractContextManager[bool]]) -> None:
        self._work_slot = work
        self.git = git
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="syvrio-git-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.git.due():
                    with self._work_slot() as admitted:
                        if admitted:
                            self.git.sync("scheduled")
            except Exception as exc:
                record_event("git_sync.scheduler_failed", exc)
            self._stop.wait(30)
