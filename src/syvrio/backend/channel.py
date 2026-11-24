"""组装各常驻对话，管理持有 Vault 的进程所拥有的资源。"""

from __future__ import annotations

import threading
from pathlib import Path

from ..agent.runtime import AgentRuntime
from ..application.channels import ConversationDirectory
from ..application.commands import CommandUseCases
from ..application.conversation import ConversationUseCases
from ..application.service import ApplicationService
from ..background_memory import BackgroundMemory
from ..errors import ConversationError
from ..infra.diagnostics import record_event
from ..infra.locking import LockTimeout
from ..infra.transaction import recover_vault_transaction
from ..llm import LiteLLMChatModel
from ..memory.service import MemoryService, get_service
from ..session import SessionStore
from .ownership import BackendLease
from .scheduler import GitScheduler


class ResidentChannel:
    def __init__(
        self,
        config_path: Path | None = None,
        vault_path: Path | None = None,
        service: MemoryService | None = None,
        *,
        parent: ResidentChannel | None = None,
    ) -> None:
        self._parent = parent
        self._service = (
            parent._service if parent else service or get_service(config_path, vault_path)
        )
        self._sessions = parent._sessions if parent else SessionStore(self._service.paths)
        settings_lock = parent.scheduling.settings_lock if parent else threading.RLock()
        with settings_lock:
            model = (
                parent._runtime.model
                if parent and parent._runtime.model_config is self._service.config.llm
                else LiteLLMChatModel(self._service.config.llm)
            )
            self._runtime = AgentRuntime(self._service, model, self._sessions)
        self._runtime.start()
        self._runtime.allow_local_paths = False
        self._background = parent._background if parent else BackgroundMemory(self._service)
        self._runtime.on_distillation = self._background.request_distillation
        self._runtime.on_title_needed = self._background.request_title
        self._app_service = ApplicationService(
            self._runtime,
            self._service,
            self._background,
            registry=parent.scheduling.registry if parent else None,
            settings_lock=settings_lock,
        )
        self._conversation = self._app_service.conversation
        self.scheduling = self._conversation.scheduling
        if parent is None:
            self.directory = ConversationDirectory(self, lambda: type(self)(parent=self))
            self._background.busy = self.directory.session_busy
            self._background.live_session = self.directory.live_session
        else:
            self.directory = parent.directory
        self._git_scheduler = (
            parent._git_scheduler if parent else GitScheduler(self._app_service.git)
        )
        self._commands = CommandUseCases(self._app_service)
        self._client_lock = BackendLease(self._service.paths)
        self._started = False
        self._search_warmup: threading.Thread | None = None

    @property
    def conversation(self) -> ConversationUseCases:
        return self._conversation

    # 生命周期

    def startup(self, ownership: BackendLease | None = None) -> None:
        try:
            if ownership is None:
                self._client_lock.acquire(timeout=0)
            else:
                self._client_lock = ownership
        except (LockTimeout, OSError) as exc:
            raise ConversationError(
                "client_busy", "A resident Syvrio backend already owns this Vault."
            ) from exc
        try:
            recover_vault_transaction(self._service.paths)
            if not (self._service.config.vault / ".git").exists():
                self._service.init_vault()
            try:
                self._service.commit_pending_changes()
            except Exception as exc:
                record_event("backend.startup_hook_failed", exc)
            self._background.start()
            self._git_scheduler.start()
            self._started = True
        except BaseException:
            self._background.stop()
            if ownership is None:
                self._client_lock.release()
            raise
        self._search_warmup = threading.Thread(
            target=self._warm_search, name="syvrio-search-warmup", daemon=True
        )
        try:
            self._search_warmup.start()
        except RuntimeError as exc:
            record_event("backend.startup_hook_failed", exc)

    def _warm_search(self) -> None:
        try:
            self._app_service.warm_search()
        except Exception as exc:
            record_event("backend.startup_hook_failed", exc)

    def shutdown(self) -> None:
        if self._parent is None:
            for channel in self.directory.close():
                if channel is not self:
                    channel.shutdown()
        if not self.scheduling.shutdown():
            return
        # 在所有写入方停止后才释放客户端锁。关闭时登记剩余工作，
        # 供下次启动处理，避免在关闭过程中再次调用模型。
        with self.scheduling.command_lock:
            try:
                self._runtime.close(distill=False)
            except Exception as exc:
                record_event("web.shutdown_distill_failed", exc)
            finally:
                if self._started:
                    self._git_scheduler.stop()
                    self._background.stop()
                    if self._search_warmup is not None and self._search_warmup.is_alive():
                        self._search_warmup.join()
                    self._client_lock.release()
                    self._started = False
