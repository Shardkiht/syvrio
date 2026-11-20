"""Resident-owned memory work. Model calls run outside the Vault write lock."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..infra.diagnostics import record_event
from ..infra.jobs import encode_job, job_directory, read_job
from ..infra.transaction import vault_transaction
from ..session import SessionStore
from . import context_fill, distillation
from .dream import Dream
from .schedule import DreamSchedule


class BackgroundMemory:
    def __init__(self, service, session_busy: Callable[[str], bool] | None = None):
        self.service = service
        self.sessions = SessionStore(service.paths)
        self.busy = session_busy or (lambda session_id: False)
        self.dream = Dream(service)
        self.schedule = DreamSchedule(service, self.dream)
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._signals = {name: threading.Event() for name in ("distillation", "context", "dream")}
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        with self._lock:
            if self._stop is not None:
                return
            stop = self._stop = threading.Event()
            self._threads = [
                threading.Thread(
                    target=self._work, args=(lane, stop), name=f"syvrio-{lane}", daemon=True
                )
                for lane in self._signals
            ]
            for thread in self._threads:
                thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._stop is None:
                return
            self._stop.set()
            self.wake()
            for thread in self._threads:
                thread.join()
            self._threads.clear()
            self._stop = None

    def wake(self) -> None:
        for signal in self._signals.values():
            signal.set()

    def request_distillation(self, session_id: str, through: str | None = None) -> None:
        distillation.request(self.sessions, session_id, through)
        self._signals["distillation"].set()

    def request_dream(self, max_requests: int = 20) -> str:
        job_id = self.dream.request(max_requests)
        self._signals["dream"].set()
        return job_id

    def cancel_dream(self) -> None:
        self.dream.cancel()
        self._signals["dream"].set()

    def dream_status(self) -> dict:
        return {
            **self.dream.status(),
            "enabled": self.service.config.chat.auto_reflect,
            "next_at": self.schedule.next_at(),
        }

    def _discover(self, stop: threading.Event) -> None:
        for session in self.sessions.unconsolidated(limit=sys.maxsize):
            if stop.is_set():
                return
            try:
                self.request_distillation(session.id, session.last_user_message_id())
            except (OSError, ValueError, KeyError, TypeError) as exc:
                record_event("catchup.failed", exc)

    def _work(self, lane: str, stop: threading.Event) -> None:
        discover = lane == "distillation"
        signal = self._signals[lane]
        while not stop.is_set():
            signal.clear()
            progressed = False
            try:
                if discover:
                    self._discover(stop)
                    discover = False
                if lane == "dream":
                    self.schedule.tick()
                    if self.dream.due() and not stop.is_set():
                        job = self.dream.status()
                        try:
                            self.dream.step(stop)
                        except Exception as exc:
                            if not stop.is_set():
                                self.dream.failed(exc, job["id"], job["generation"])
                                record_event("reflection.failed", exc)
                        progressed = True
                else:
                    for path in sorted(job_directory(self.service.paths, lane).glob("*.json")):
                        if stop.is_set():
                            break
                        try:
                            progressed |= self._step_job(lane, path, stop)
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            record_event("catchup.failed", exc)
            except Exception as exc:
                record_event("reflection.failed" if lane == "dream" else "catchup.failed", exc)
            if not progressed and not stop.is_set():
                signal.wait(2.0)

    def _step_job(self, lane: str, path: Path, stop: threading.Event) -> bool:
        with vault_transaction(self.service.paths):
            job = read_job(path)
            if not job or job.get("retry_at", 0) > time.time():
                return False
        try:
            if lane == "context":
                context_fill.fill(self.service, path, stop)
                return True
            return distillation.process(self.service, self.sessions, path, self.busy, stop)
        except Exception as exc:
            record_event("catchup.failed", exc)
            if stop.is_set():
                return False
            with vault_transaction(self.service.paths) as tx:
                if read_job(path) == job:
                    failures = job.get("failures", 0) + 1
                    job.update(
                        failures=failures,
                        retry_at=time.time() + min(3600, 5 * 2 ** min(10, failures - 1)),
                    )
                    tx.write(path, encode_job(job))
            return False
