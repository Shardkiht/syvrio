"""Policy for running a complete Dream independently of connected clients."""

from __future__ import annotations

import time

from ..infra.jobs import encode_job, job_path, read_job
from ..infra.transaction import vault_transaction
from .dream import Dream, _state


class DreamSchedule:
    def __init__(self, service, dream: Dream):
        self.service = service
        self.dream = dream
        self.path = job_path(service.paths, "dream", "schedule")

    def next_at(self) -> float | None:
        with vault_transaction(self.service.paths):
            return (read_job(self.path) or {}).get("next_at")

    def tick(self, now: float | None = None) -> None:
        config = self.service.config.chat
        if not config.auto_reflect:
            return
        now = time.time() if now is None else now
        interval = max(60.0, config.dream_auto_interval_hours * 3600)
        with vault_transaction(self.service.paths) as tx:
            saved = read_job(self.path)
            if saved and saved["next_at"] > now:
                return
            if saved:
                job = read_job(self.dream.path) or {}
                metadata = read_job(self.dream.metadata) or {}
                _, _, _, version = _state(self.service)
                if (
                    job.get("state") in {"paused", "failed"}
                    or version != metadata.get("version")
                    or not self.service.profile.path.exists()
                ):
                    self.dream.request()
            tx.write(self.path, encode_job({"next_at": now + interval}))
