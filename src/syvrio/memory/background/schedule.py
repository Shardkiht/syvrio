"""独立于客户端连接执行完整 Dream 的策略。"""

from __future__ import annotations

import time

from .dream import Dream
from .jobs import job_key, read_job, save_job


class DreamSchedule:
    def __init__(self, service, dream: Dream):
        self.service = service
        self.dream = dream
        self.path = job_key(service.paths, "dream", "schedule")

    def next_at(self) -> float | None:
        return (read_job(self.path) or {}).get("next_at")

    def tick(self, now: float | None = None) -> None:
        config = self.service.config.chat
        if not config.auto_reflect:
            return
        now = time.time() if now is None else now
        interval = max(60.0, config.dream_auto_interval_hours * 3600)
        with self.service.work.read():
            saved = read_job(self.path)
            if saved and saved["next_at"] > now:
                return
            revision = self.service.dream_version()
            if saved:
                fingerprint = self.dream.current_evidence_fingerprint()
        with self.service.work.write():
            if read_job(self.path) != saved or self.service.dream_version() != revision:
                return
            if saved:
                self.dream.request_if_needed(fingerprint)
            save_job(self.path, {"next_at": now + interval})
