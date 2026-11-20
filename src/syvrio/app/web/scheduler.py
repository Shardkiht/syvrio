"""Run Git's existing due policy independently of connected clients."""

from __future__ import annotations

import threading

from ...application.git import GitUseCases
from ...infra.diagnostics import record_event


class GitScheduler:
    def __init__(self, git: GitUseCases) -> None:
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
                    self.git.sync("scheduled")
            except Exception as exc:
                record_event("git_sync.scheduler_failed", exc)
            self._stop.wait(30)
