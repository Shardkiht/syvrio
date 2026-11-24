"""Application Layer 组合入口：只协调核心服务，不包含界面逻辑。"""

from __future__ import annotations

from threading import RLock

from ..agent.runtime import AgentRuntime
from ..infra.search.daemon import ensure_search_daemon
from ..infra.vault.sync import GitSyncService
from ..memory.service import MemoryService
from .conversation import ConversationUseCases
from .git import GitUseCases
from .memory import MemoryUseCases
from .reflection import ReflectionUseCases
from .settings import SettingsUseCases
from .turns import TurnRegistry


class ApplicationService:
    def __init__(
        self,
        runtime: AgentRuntime,
        service: MemoryService,
        background=None,
        *,
        git: GitSyncService,
        registry: TurnRegistry | None = None,
        settings_lock: RLock | None = None,
    ) -> None:
        self.conversation = ConversationUseCases(
            runtime,
            runtime.sessions,
            registry=registry,
            settings_lock=settings_lock,
            background_job=background.session_job if background else None,
        )
        self.memory = MemoryUseCases(service)
        self.settings = SettingsUseCases(service, runtime)
        self.reflection = ReflectionUseCases(background)
        self.git = GitUseCases(git)
        self._service = service

    # ---------- 生命周期 ----------

    def warm_search(self) -> str | None:
        search = self._service.config.search
        if not search.enabled or not search.auto_start or search.transport != "http":
            return None
        return ensure_search_daemon(search.base_url)

    def close(self, distill: bool = True) -> dict:
        return self.conversation.runtime.close(distill=distill)

    @property
    def config(self):
        return self._service.config

    @property
    def pending_count(self) -> int:
        return self.memory.pending_count()
