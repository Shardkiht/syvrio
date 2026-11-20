"""Submit Dream requests and display the resident scheduler's status."""

from __future__ import annotations

from datetime import UTC, datetime

from ..background_memory import BackgroundMemory
from ..time import format_local_timestamp
from .contracts import ReflectionSchedule


class ReflectionUseCases:
    def __init__(self, background: BackgroundMemory):
        self.background = background

    def run(self, mode: str, budget: int = 20) -> str:
        if mode != "dream":
            raise ValueError("Use /dream to regenerate the complete profile and hypotheses.")
        return self.background.request_dream(budget)

    def cancel(self) -> None:
        self.background.cancel_dream()

    def status(self) -> dict:
        return self.background.dream_status()

    def schedule(self) -> ReflectionSchedule:
        status = self.status()
        next_at = status["next_at"]
        next_run = (
            format_local_timestamp(datetime.fromtimestamp(next_at, UTC).isoformat())
            if next_at
            else None
        )
        return ReflectionSchedule(
            running=status["state"] == "queued",
            enabled=status["enabled"],
            profile_next=next_run,
            dream_next=next_run,
            profile_pending=status.get("phase")
            if status["state"] in {"queued", "paused"}
            else None,
            dream_pending=status["state"] in {"queued", "paused"},
        )
