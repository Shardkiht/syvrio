"""常驻服务管理的记忆任务，模型调用在 Vault 写锁之外执行。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext

from ...infra.observability.diagnostics import record_event
from ...infra.session import Session, SessionStore
from ...infra.storage.database import transaction
from . import context_fill, distillation, titles
from .dream import Dream
from .jobs import JobKey, list_jobs, pending_title_ids, read_job, save_job
from .maintenance import Maintenance
from .schedule import DreamSchedule


class BackgroundMemory:
    def __init__(
        self,
        service,
        session_busy: Callable[[str], bool] | None = None,
        *,
        work: Callable[[], AbstractContextManager[bool]] = lambda: nullcontext(True),
    ):
        self._work_slot = work
        self.service = service
        self.sessions = SessionStore(service.paths)
        self.busy = session_busy or (lambda session_id: False)
        self.live_session: Callable[[str], Session | None] = lambda session_id: None
        self.dream = Dream(service)
        self.schedule = DreamSchedule(service, self.dream)
        self.maintenance = Maintenance(service)
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._signals = {
            name: threading.Event()
            for name in ("distillation", "context", "dream", "title", "maintenance")
        }
        self._threads: list[threading.Thread] = []
        # 在 Vault 写锁内修改，模型调用的准入阶段也遵循这一约定。
        self._working_sessions: dict[str, str] = {}

    def session_job(self, session_id: str) -> str | None:
        with transaction(self.service.paths):
            return next(
                (lane for lane, sid in self._working_sessions.items() if sid == session_id), None
            )

    def start(self) -> None:
        with self._lock:
            if self._stop is not None:
                return
            self.maintenance.schedule_first_run()
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

    def request_dream(self, *, retry_now: bool = False) -> str:
        job_id = self.dream.request(retry_now=retry_now)
        self._signals["dream"].set()
        return job_id

    def request_title(self, session_id: str) -> None:
        titles.request(self.sessions, session_id)
        self._signals["title"].set()

    def request_maintenance(self) -> dict:
        status = self.maintenance.request()
        self._signals["maintenance"].set()
        return status

    def pending_titles(self, session_ids: list[str]) -> set[str]:
        return pending_title_ids(self.service.paths, session_ids)

    def dream_status(self) -> dict:
        status = self.dream.status(live=True)
        state = status["state"]
        budget = self.service.config.chat.default_budget
        pause_reason = None
        if state == "paused":
            if not budget:
                pause_reason = "budget_disabled"
            elif status.get("error") == "DreamOutputTruncated":
                pause_reason = "output_limit"
            else:
                pause_reason = "budget_exhausted"
        return {
            **status,
            "pause_reason": pause_reason,
            "can_run": (
                status.get("retry_at", 0) > time.time()
                if state == "queued"
                else bool(budget) and state != "running"
            ),
            "enabled": self.service.config.chat.auto_reflect,
            "next_at": self.schedule.next_at(),
        }

    def _discover(self, stop: threading.Event) -> None:
        for session_id in self.sessions.pending_sessions():
            if stop.is_set():
                return
            try:
                self.request_distillation(session_id)
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
                    with self._work_slot() as admitted:
                        if admitted:
                            self._discover(stop)
                            discover = False
                if lane == "maintenance":
                    if self.maintenance.due(time.time()):
                        with self._work_slot() as admitted:
                            if admitted:
                                progressed = self.maintenance.tick(stop)
                elif lane == "dream":
                    if (
                        self.service.config.chat.auto_reflect
                        and (self.schedule.next_at() or 0) <= time.time()
                    ):
                        with self._work_slot() as admitted:
                            if admitted:
                                self.schedule.tick()
                    if self.dream.due() and not stop.is_set():
                        with self._work_slot() as admitted:
                            if admitted:
                                job = self.dream.status()
                                try:
                                    self.dream.step(stop)
                                except Exception as exc:
                                    if not stop.is_set():
                                        self.dream.failed(exc, job["id"], job["generation"])
                                        record_event("reflection.failed", exc)
                                progressed = True
                else:
                    for path in list_jobs(self.service.paths, lane):
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

    def _step_job(self, lane: str, path: JobKey, stop: threading.Event) -> bool:
        with self._work_slot() as admitted:
            return self._run_job(lane, path, stop) if admitted else False

    def _run_job(self, lane: str, path: JobKey, stop: threading.Event) -> bool:
        with transaction(self.service.paths):
            job = read_job(path)
            if not job or job.get("retry_at", 0) > time.time():
                return False
            if lane == "context":
                entry = self.service.store.find(job["id"])
                session_id = entry.source_session_id if entry else None
            else:
                session_id = job["id"]
            if session_id:
                # 后台任务只打开活动会话，查找任务和证据时均跳过归档。
                if not self.sessions.exists(session_id):
                    save_job(path, None)
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
            with transaction(self.service.paths):
                if read_job(path) == job:
                    failures = job.get("failures", 0) + 1
                    job.update(
                        failures=failures,
                        retry_at=time.time() + min(3600, 5 * 2 ** min(10, failures - 1)),
                    )
                    save_job(path, job)
            return False
        finally:
            with transaction(self.service.paths):
                self._working_sessions.pop(lane, None)
