"""Git 同步用例：只协调 GitSyncService，不处理界面状态。"""

from __future__ import annotations

from ..infra.gitsync import SyncResult
from ..memory.service import MemoryService


class GitUseCases:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    @property
    def running(self) -> bool:
        return self.service.git_sync.running

    def due(self) -> bool:
        return self.service.git_sync.due()

    def status(self) -> dict:
        return self.service.git_sync.status()

    def sync(
        self,
        trigger: str,
        on_progress=None,
        *,
        wait_for_git: bool = False,
    ) -> SyncResult:
        return self.service.git_sync.sync(trigger, on_progress, wait_for_git=wait_for_git)
