"""Reflection 调度判定与运行用例；TUI 生命周期仍负责触发定时器。"""

from __future__ import annotations

from threading import Event

from ..memory.service import MemoryService
from ..time import format_local_timestamp
from .contracts import ReflectionSchedule


class ReflectionUseCases:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    @property
    def engine(self):
        return self.service.reflection

    def auto_check_due(self) -> bool:
        return self.engine.auto_check_due()

    def auto_plan(self):
        return self.engine.auto_plan()

    def run(
        self,
        mode: str,
        budget: int,
        *,
        cancel: Event | None = None,
        auto: bool = False,
    ):
        return self.engine.run(
            mode,
            budget,
            cancel_event=cancel,
            auto=auto,
            refresh_summary=mode == "profile",
        )

    def cancel(self) -> None:
        self.engine.cancel()

    def schedule(self) -> ReflectionSchedule:
        overview = self.engine.schedule_overview()
        profile = overview["profile"]
        dream = overview["dream"]
        config = self.service.config.chat
        return ReflectionSchedule(
            running=False,
            enabled=bool(config.auto_reflect),
            profile_next=format_local_timestamp(profile.get("next_attempt_at")),
            dream_next=format_local_timestamp(dream.get("next_attempt_at")),
            profile_pending=(
                f"{profile.get('pending', 0)} pending changes"
                + (", inference batch pending" if profile.get("pending_inferences") else "")
            ),
            dream_pending=bool(dream.get("pending")),
        )
