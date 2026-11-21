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
from ..session import Session, SessionStore
from . import context_fill, distillation, titles
from .dream import Dream
from .schedule import DreamSchedule


class BackgroundMemory:
    def __init__(self, service, session_busy: Callable[[str], bool] | None = None):
        self.service = service
        self.sessions = SessionStore(service.paths)
        self.busy = session_busy or (lambda session_id: False)
        self.live_session: Callable[[str], Session | None] = lambda session_id: None
        self.dream = Dream(service)
        self.schedule = DreamSchedule(service, self.dream)
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._signals = {
            name: threading.Event() for name in ("distillation", "context", "dream", "title")
        }
        self._threads: list[threading.Thread] = []
        # Changed under the Vault write lock, including during admission to a model call.
        self._working_sessions: dict[str, str] = {}

    def session_job(self, session_id: str) -> str | None:
        with self.service.write_lock:
            return next(
                (lane for lane, sid in self._working_sessions.items() if sid == session_id), None
            )

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

    def request_dream(self, max_requests: int | None = None) -> str:
        job_id = self.dream.request(max_requests)
        self._signals["dream"].set()
        return job_id

    def request_title(self, session_id: str) -> None:
        titles.request(self.sessions, session_id)
        self._signals["title"].set()

    def pending_titles(self) -> set[str]:
        folder = job_directory(self.service.paths, "title")
        pending = set()
        for path in folder.glob("*.json"):
            try:
                if job := read_job(path):
                    pending.add(job["id"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                record_event("session.title_failed", exc)
        return pending

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
        with vault_transaction(self.service.paths) as tx:
            job = read_job(path)
            if not job or job.get("retry_at", 0) > time.time():
                return False
            if lane == "context":
                entry = self.service.store.find(job["id"])
                session_id = entry.source_session_id if entry else None
            else:
                session_id = job["id"]
            if session_id:
                # Workers open active sessions only. Archive is never scanned for jobs or evidence.
                if not self.sessions.exists(session_id):
                    tx.write(path, None)
                    return True
                self._working_sessions[lane] = session_id
        try:
            if lane == "title":
                return titles.process(self.service, self.sessions, path, stop, self.live_session)
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
        finally:
            with self.service.write_lock:
                self._working_sessions.pop(lane, None)
