"""Reserve the interval between the idle check and stopping the Web service."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from fastapi import FastAPI, Request

from ...infra.locking import LockTimeout, VaultLock
from ...infra.paths import syvrio_home
from .channel import APIError
from .http import STATE_MODIFYING, error_response


class UpdateAdmission:
    def __init__(self, app: FastAPI, busy: Callable[[], bool]):
        self._lock = Lock()
        self._writes = 0
        self._ticket = ""
        self._busy = busy
        app.middleware("http")(self.guard)

    def _reserved(self) -> bool:
        if self._ticket:
            # The worker owns this OS lock until switching or rollback is finished.
            # A killed worker releases it automatically, so a client is never stranded.
            update_lock = VaultLock(syvrio_home() / "update" / "update.lock")
            try:
                update_lock.acquire(timeout=0)
            except LockTimeout:
                return True
            update_lock.release()
            self._ticket = ""
        return False

    def reserve(self, ticket: str) -> None:
        with self._lock:
            if self._reserved() or self._writes or self._busy():
                raise APIError(
                    "update_busy", "Wait for active work to finish before updating.", 409
                )
            self._ticket = ticket

    def release(self, ticket: str) -> None:
        with self._lock:
            if self._ticket == ticket:
                self._ticket = ""

    async def guard(self, request: Request, call_next):
        if request.method not in STATE_MODIFYING or request.url.path in {
            "/api/resident/update_drain",
            "/api/resident/update_resume",
            "/api/resident/shutdown",
        }:
            return await call_next(request)
        with self._lock:
            if self._reserved():
                return error_response(
                    "update_busy", "The service is restarting for an update.", 409
                )
            self._writes += 1
        try:
            return await call_next(request)
        finally:
            with self._lock:
                self._writes -= 1
