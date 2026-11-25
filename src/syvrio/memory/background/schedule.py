"""独立于客户端连接执行完整 Dream 的策略。"""

from __future__ import annotations

import time

from ...infra.storage.database import transaction
from .dream import Dream, _state
from .jobs import job_key, read_job, save_job


class DreamSchedule:
    def __init__(self, service, dream: Dream):
        self.service = service
        self.dream = dream
        self.path = job_key(service.paths, "dream", "schedule")

    def next_at(self) -> float | None:
        with transaction(self.service.paths):
            return (read_job(self.path) or {}).get("next_at")

    def tick(self, now: float | None = None) -> None:
        config = self.service.config.chat
        if not config.auto_reflect:
            return
        now = time.time() if now is None else now
        interval = max(60.0, config.dream_auto_interval_hours * 3600)
        with transaction(self.service.paths):
            saved = read_job(self.path)
            if saved and saved["next_at"] > now:
                return
            if saved:
                job = read_job(self.dream.path) or {}
                metadata = self.service.profile.metadata() or {}
                _, _, _, version = _state(self.service)
                if (
                    job.get("state") in {"paused", "failed"}
                    or version != metadata.get("version")
                    or not self.service.profile.read_text()
                ):
                    self.dream.request()
            save_job(self.path, {"next_at": now + interval})
