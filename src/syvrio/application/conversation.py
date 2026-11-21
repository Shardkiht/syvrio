"""聊天、会话生命周期、FIFO 与撤销状态机（纯 Python，不依赖 Textual）。"""

from __future__ import annotations

from threading import Event, RLock
from typing import Callable

from ..agent.context import CompactionPhase
from ..agent.insertion import InsertionRequest, TurnInsertion
from ..agent.runtime import AgentRuntime, TurnSettings
from ..errors import ConversationError, SessionProtectedError
from ..infra.attachments import AttachmentBatch
from ..infra.diagnostics import record_event
from ..infra.secrets import redact_secrets
from ..llm import ChatModelError, GenerationCancelled
from ..memory.models import PendingItem
from ..memory.turn_undo import TurnUndoError
from ..session import Session, SessionStore
from .contracts import (
    QueuedItem,
    QueueSnapshot,
    Submission,
    SwitchResult,
    TurnCallbacks,
    TurnResult,
    UndoOutcome,
)
from .scheduling import ConversationScheduler
from .turns import ConversationTurn, TurnRegistry

QueuedLine = ConversationTurn


SAFE_COMMANDS = frozenset(
    {
        "/new",
        "/sessions",
        "/exit",
        "/quit",
        "/help",
        "/model",
        "/status",
        "/topics",
        "/summary",
        "/pending",
        "/git",
        "/think",
        "/context",
        "/setup",
        "/rename",
        "/import",
        "/inferences",
        "/profile",
        "/dream",
    }
)


class ConversationUseCases:
    """Own the queue and active turn of one conversation, independent of its clients."""

    def __init__(
        self,
        runtime: AgentRuntime,
        sessions: SessionStore,
        *,
        registry: TurnRegistry | None = None,
        settings_lock: RLock | None = None,
        background_job: Callable[[str], str | None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.sessions = sessions
        self._background_job = background_job
        self._lock = RLock()
        self._navigation_busy = False
        self._ask_user_message_id: str | None = None
        self.active_turn: ConversationTurn | None = None
        self._cancel: Event | None = None
        self._turn_settings: TurnSettings | None = None
        self._insertion: TurnInsertion | None = None
        self._undo_requested = False
        self._undo_target_session_id: str | None = None
        self._undo_target_message_id: str | None = None
        self._undo_expected_message_id: str | None = None
        self.queue: list[QueuedLine] = []
        self.queue_paused_after_undo = False
        self.pending_new = False
        self.pending_switch: str | None = None
        self.pending_command: QueuedLine | None = None
        self.scheduling = ConversationScheduler(self, self._lock, registry, settings_lock)

    # ---------- 状态查询 ----------

    @property
    def busy(self) -> bool:
        return self._navigation_busy or self.ask_active

    @property
    def navigation_busy(self) -> bool:
        return self._navigation_busy

    @property
    def ask_active(self) -> bool:
        return self.active_turn is not None

    @property
    def active_turn_id(self) -> int | None:
        turn = self.active_turn
        return turn.turn_id if turn else None

    @property
    def ask_in_flight(self) -> str | None:
        turn = self.active_turn
        return turn.session_id if turn else None

    @property
    def ask_user_message_id(self) -> str | None:
        return self._ask_user_message_id

    @property
    def cancel_event(self) -> Event | None:
        return self._cancel

    @property
    def undo_requested(self) -> bool:
        return self._undo_requested

    @property
    def undo_target_session_id(self) -> str | None:
        return self._undo_target_session_id

    @property
    def undo_target_message_id(self) -> str | None:
        return self._undo_target_message_id

    @property
    def has_session(self) -> bool:
        return self.runtime.has_session

    @property
    def current(self) -> Session:
        return self.runtime.current

    @property
    def current_id(self) -> str | None:
        session = self.runtime.session
        return session.id if session is not None else None

    @property
    def model_label(self) -> str:
        return self.runtime.model_label

    @property
    def last_plan(self):
        return self.runtime.last_plan

    @property
    def last_plan_session(self) -> str | None:
        return self.runtime.last_plan_session

    def plan_is_current(self) -> bool:
        if self.runtime.last_plan_session is None:
            return True
        return (
            self.runtime.has_session and self.runtime.last_plan_session == self.runtime.current.id
        )

    def queue_snapshot(self) -> QueueSnapshot:
        with self._lock:
            return QueueSnapshot(
                tuple(
                    QueuedItem(item.session_id, item.line, index)
                    for index, item in enumerate(self.queue)
                ),
                self.queue_paused_after_undo,
            )

    # ---------- 导航状态 ----------

    def begin_navigation(self) -> None:
        self._navigation_busy = True

    def end_navigation(self) -> None:
        self._navigation_busy = False

    def set_navigation_busy(self, value: bool) -> None:  # 兼容测试与旧调用
        self._navigation_busy = value

    # ---------- 提交路由 ----------

    def submit_during_ask(
        self, line: str, attachments: AttachmentBatch | None = None
    ) -> Submission:
        command = line.split(" ", 1)[0].lower() if line.startswith("/") else ""
        if command == "/undo":
            return self.request_undo()
        if line.startswith("/") and command in SAFE_COMMANDS:
            return Submission("command")
        if not self.runtime.has_session:
            self.runtime.ensure_session()
        session_id = self.runtime.session.id
        item = QueuedLine(session_id=session_id, line=line, attachments=attachments)
        with self._lock:
            self.queue.append(item)
            index = len(self.queue) - 1
        return Submission(
            "queued",
            session_id=session_id,
            item=QueuedItem(session_id, line, index),
        )

    def enqueue(self, session_id: str, line: str) -> QueuedItem:
        with self._lock:
            item = QueuedLine(session_id, line)
            self.queue.append(item)
            return QueuedItem(session_id, line, len(self.queue) - 1)

    def take_next(self) -> QueuedLine | None:
        with self._lock:
            return self.queue.pop(0) if self.queue else None

    def take_next_command(self) -> QueuedLine | None:
        with self._lock:
            for index, item in enumerate(self.queue):
                if item.line.startswith("/"):
                    return self.queue.pop(index)
        return None

    def purge_undo(self) -> tuple[int, bool]:
        with self._lock:
            kept = [item for item in self.queue if not self._is_undo(item.line)]
            removed = len(self.queue) - len(kept)
            self.queue[:] = kept
            pending_removed = bool(
                self.pending_command and self._is_undo(self.pending_command.line)
            )
            if pending_removed:
                self.pending_command = None
            return removed, pending_removed

    def pause_after_undo(self, restored: bool) -> None:
        with self._lock:
            self.queue_paused_after_undo = restored and bool(self.queue)

    def resume_after_undo(self) -> None:
        with self._lock:
            self.queue_paused_after_undo = False

    def set_pending_command(self, item: QueuedLine | None) -> None:
        with self._lock:
            self.pending_command = item

    def take_pending_command(self) -> QueuedLine | None:
        with self._lock:
            item = self.pending_command
            self.pending_command = None
            return item

    def request_new(self) -> None:
        with self._lock:
            self.pending_new = True
            self.pending_switch = None

    def request_switch(self, session_id: str) -> None:
        with self._lock:
            self.pending_switch = session_id
            self.pending_new = False

    def take_pending_navigation(self) -> tuple[str, str | None] | None:
        with self._lock:
            if self.pending_new:
                self.pending_new = False
                return ("new", None)
            if self.pending_switch:
                session_id = self.pending_switch
                self.pending_switch = None
                return ("switch", session_id)
            return None

    @staticmethod
    def _is_undo(line: str) -> bool:
        text = line.strip()
        return text.startswith("/") and text.split(" ", 1)[0].lower() == "/undo"

    # ---------- 请求生命周期 ----------

    def request_cancel(self) -> bool:
        with self._lock:
            if not self.ask_active or self._cancel is None:
                return False
            self._cancel.set()
            return True

    def begin_turn(
        self,
        session_id: str | None = None,
        *,
        turn: ConversationTurn | None = None,
        turn_id: int | None = None,
    ) -> Event:
        token = Event()
        with self._lock:
            self._turn_settings = self.runtime.turn_settings()
            if turn is None:
                turn = (
                    ConversationTurn(session_id, "")
                    if turn_id is None
                    else ConversationTurn(session_id, "", turn_id=turn_id)
                )
            self.active_turn = turn
            self._cancel = token
            self._insertion = TurnInsertion()
            self._ask_user_message_id = None
            self._undo_requested = False
            self._undo_expected_message_id = None
            self._undo_target_session_id = None
            self._undo_target_message_id = None
        return token

    def insert_message(self, turn_id: int, text: str) -> InsertionRequest | None:
        text = text.strip()
        if not text or text.startswith("/"):
            return None
        with self._lock:
            if (
                self.active_turn_id != turn_id
                or self._insertion is None
                or self._cancel is None
                or self._cancel.is_set()
            ):
                return None
            return self._insertion.offer(text)

    def bind_session(self, session: Session) -> None:
        with self._lock:
            if self.active_turn and self.active_turn.session_id is None:
                self.active_turn.session_id = session.id

    def bind_session_id(self, session_id: str) -> None:
        with self._lock:
            if self.active_turn and self.active_turn.session_id is None:
                self.active_turn.session_id = session_id

    def bind_user_message(self, message_id: str) -> None:
        with self._lock:
            if self.ask_active:
                self._ask_user_message_id = message_id

    def request_undo(self, expected_message_id: str | None = None) -> Submission:
        with self._lock:
            if not self.ask_active:
                return Submission("undo_now")
            if expected_message_id is not None and expected_message_id != self._ask_user_message_id:
                raise TurnUndoError(
                    "This message is no longer the latest. Refresh the conversation."
                )
            if self._undo_requested:
                return Submission("undo_busy", message="Undo already in progress.")
            self._undo_requested = True
            self._undo_target_session_id = self.ask_in_flight
            self._undo_target_message_id = self._ask_user_message_id
            self._undo_expected_message_id = expected_message_id
            if self._cancel is not None:
                self._cancel.set()
        removed, pending_removed = self.purge_undo()
        total = removed + (1 if pending_removed else 0)
        message = "Stopping generation and undoing this turn…"
        if total:
            message += f" Discarded {total} queued /undo."
        return Submission("undo", message=message)

    def run_turn(
        self,
        line: str,
        callbacks: TurnCallbacks,
        cancel: Event,
        session_id: str | None = None,
        attachments: AttachmentBatch | None = None,
        *,
        replace_message_id: str | None = None,
    ) -> TurnResult:
        bound = {"session_id": session_id, "user_message_id": None}

        def on_session(opened: Session) -> None:
            nonlocal session
            session = opened
            bound["session_id"] = session.id
            self.bind_session(session)
            if callbacks.on_session is not None:
                callbacks.on_session(session)

        def on_user_message(message_id: str) -> None:
            bound["user_message_id"] = message_id
            self.bind_user_message(message_id)
            if callbacks.on_user_message is not None:
                callbacks.on_user_message(message_id)

        def on_inserted(message_id: str, text: str) -> None:
            bound["user_message_id"] = message_id
            self.bind_user_message(message_id)
            if callbacks.on_inserted is not None:
                callbacks.on_inserted(message_id, text)

        session = None
        status = "ok"
        reply = None
        content = ""
        error = None
        exception = None
        with self._lock:
            turn_id = self.active_turn_id
            insertion = self._insertion
            settings = self._turn_settings
        if turn_id is None:
            record_event("turn.unbound", RuntimeError("run_turn without begin_turn"))
            return TurnResult(status="failed", error="No active turn", turn_id=None)
        try:
            if session_id and not (
                self.runtime.has_session and self.runtime.session.id == session_id
            ):
                # 后台任务执行时从磁盘新鲜加载，不跨切换复用内存对象（对象会过期）。
                session = self.sessions.load(session_id)
            reply = self.runtime.ask(
                line,
                session=session,
                turn_settings=settings,
                **({"attachments": attachments} if attachments else {}),
                **(
                    {"replace_message_id": replace_message_id}
                    if replace_message_id is not None
                    else {}
                ),
                on_session=on_session,
                on_user_message=on_user_message,
                on_event=callbacks.on_event,
                on_delta=callbacks.on_delta,
                on_thinking=callbacks.on_thinking,
                on_progress=callbacks.on_progress,
                on_tool=callbacks.on_tool,
                on_compaction=callbacks.on_compaction,
                cancel=cancel,
                insertion=insertion,
                on_inserted=on_inserted,
            )
            content = reply.content
        except GenerationCancelled:
            status = "cancelled"
        except ChatModelError as exc:
            status = "failed"
            error = redact_secrets(str(exc))[:2000]
            exception = exc
            record_event("chat.failed", exc)
        except Exception as exc:
            status = "failed"
            error = redact_secrets(f"Request failed: {exc}")[:2000]
            exception = exc
            record_event("chat.failed", exc)
        finally:
            if insertion is not None:
                insertion.settle()

        if status == "failed" and session is not None and bound["user_message_id"]:
            try:
                mid = bound["user_message_id"]
                self.sessions.append(
                    session,
                    {
                        "type": "turn_failed",
                        "id": f"failure-{mid}",
                        "message_id": mid,
                        "content": error or "Request failed.",
                    },
                )
            except OSError as exc:
                record_event("chat.history_write_failed", exc)

        undo = None
        if self.undo_requested:
            undo = self.perform_undo(bound["session_id"], bound["user_message_id"], error)
            status = "undo"
        return TurnResult(
            status=status,
            session_id=bound["session_id"],
            user_message_id=bound["user_message_id"],
            turn_id=turn_id,
            reply=reply,
            content=content,
            error=error,
            exception=exception,
            undo=undo,
        )

    def end_turn(self, turn_id: int) -> bool:
        """只有匹配的活动 Turn 才能释放请求状态；缺失或过期 ID 一律拒绝。"""
        if turn_id is None:
            return False
        with self._lock:
            if self.active_turn_id != turn_id:
                return False
            self._ask_user_message_id = None
            self.active_turn = None
            self._cancel = None
            self._turn_settings = None
            if self._insertion is not None:
                self._insertion.settle()
                self._insertion = None
            return True

    def perform_undo(
        self,
        session_id: str | None = None,
        user_message_id: str | None = None,
        error: str | None = None,
    ) -> UndoOutcome:
        with self._lock:
            target_sid = self._undo_target_session_id or session_id or self.ask_in_flight
            target_mid = user_message_id or self._undo_target_message_id
            expected = self._undo_expected_message_id
        restored: str | None = None
        failed = False
        public_error = None
        if target_sid and target_mid:
            try:
                # 始终从磁盘加载目标会话：后台 Worker 的写入可能晚于切回时
                # 载入的内存对象，直接用当前对象会看不到刚追加的用户消息。
                target_session = self.sessions.load(target_sid)
                restored = self.runtime.undo(
                    target_session, message_id=target_mid, expected_latest=expected
                )
            except Exception as exc:
                failed = True
                record_event("undo.failed", exc)
                public_error = str(exc) if isinstance(exc, TurnUndoError) else None
                error = error or str(exc)
        with self._lock:
            queued_count = len(self.queue)
            self.queue_paused_after_undo = restored is not None and bool(self.queue)
            self._undo_requested = False
            self._undo_target_session_id = None
            self._undo_target_message_id = None
        if error:
            record_event("chat.failed", error)
        return UndoOutcome(
            restored=restored,
            target_session_id=target_sid,
            failed=failed,
            error=error,
            queued_count=queued_count,
            paused=self.queue_paused_after_undo,
            public_error=public_error,
        )

    def undo_current(self, expected_message_id: str | None = None) -> str | None:
        return (
            self.runtime.undo(expected_latest=expected_message_id)
            if self.runtime.has_session
            else None
        )

    # ---------- 会话生命周期 ----------

    def navigate(self, session_id: str | None) -> SwitchResult:
        with self.scheduling.operation("new" if session_id is None else "switch"):
            if session_id is not None and not self.session_exists(session_id):
                raise ConversationError("session_not_found", "Session not found.")
            try:
                return self.switch(session_id)
            except (KeyError, OSError, ValueError) as exc:
                record_event("web.switch_failed", exc)
                raise ConversationError(
                    "session_unreadable", "Session could not be restored."
                ) from exc

    def switch(self, session_id: str | None) -> SwitchResult:
        stats: dict = {}
        if session_id is None:
            with self.runtime.service.write_lock:
                stats = self.runtime.close(distill=False)
                self.runtime.start()
            return SwitchResult(None, None, stats)
        session = self.runtime.resume(session_id, close_current=True)
        return SwitchResult(session, f"{session.id} · {session.title()}", stats)

    def rename_current(self, title: str) -> None:
        self.runtime.rename(title)

    def rename_session(self, session_id: str, title: str) -> None:
        if self.runtime.has_session and session_id == self.runtime.current.id:
            self.runtime.rename(title)
        else:
            self.sessions.rename(self.sessions.load(session_id), title)

    def archive_guard(self, session_id: str) -> tuple[bool, str | None]:
        if (
            (self.pending_command and self.pending_command.session_id == session_id)
            or self.scheduling.session_busy(session_id)
            or any(item.session_id == session_id for item in self.queue)
        ):
            return False, "Session is generating or has queued tasks; try again later."
        if self._background_job and (job := self._background_job(session_id)):
            return (
                False,
                f"A background {job} task is running for this conversation. Try again when it finishes.",
            )
        return True, None

    def archive_session(self, session_id: str) -> None:
        self.set_archived(session_id, True)

    def set_archived(self, session_id: str, archived: bool) -> None:
        with self._lock, self.runtime.service.write_lock:
            allowed, reason = self.archive_guard(session_id)
            if not allowed:
                raise SessionProtectedError(reason)
            self.sessions.set_archived(session_id, archived)
            if archived and self.current_id == session_id:
                # The transcript has moved. Closing it would request distillation.
                self.runtime.start()
                self.scheduling.history_changed()

    def delete_guard(self, session_id: str) -> tuple[bool, str | None]:
        if self.current_id == session_id:
            return False, "Archive the conversation before permanently deleting it."
        allowed, reason = self.archive_guard(session_id)
        if not allowed:
            return False, reason
        return self._source_guard(session_id)

    def _source_guard(self, session_id: str) -> tuple[bool, str | None]:
        try:
            reference = self.runtime.service.memory_referencing_session(session_id)
        except (OSError, ValueError) as exc:
            record_event("session.sources_unavailable", exc)
            return False, "Could not verify saved memory sources. The session was kept."
        if reference:
            return (
                False,
                f"Saved memory {reference} cites this session. Its original messages must be kept.",
            )
        return True, None

    def deletion_preview(
        self, session_id: str, *, archived: bool = False
    ) -> tuple[list[PendingItem], str]:
        with self.runtime.service.write_lock:
            if not self.sessions.exists(session_id, archived=archived):
                raise KeyError(session_id)
            allowed, reason = self._source_guard(session_id)
            if not allowed:
                raise SessionProtectedError(reason)
            return self.runtime.service.queue.deletion_preview(session_id)

    def delete_session(self, session_id: str, pending_revision: str = "") -> bool:
        with self.runtime.service.write_lock:
            if not self.sessions.exists(session_id, archived=True):
                if self.sessions.exists(session_id):
                    raise SessionProtectedError(
                        "Archive the conversation before permanently deleting it."
                    )
                return False
            allowed, reason = self.delete_guard(session_id)
            if not allowed:
                raise SessionProtectedError(reason)
            with self.runtime.service.queue.removing_with_session(session_id, pending_revision):
                return self.sessions.delete(session_id, archived=True)

    def session_exists(self, session_id: str) -> bool:
        return self.sessions.exists(session_id)

    def session_available(self, session_id: str) -> bool:
        try:
            self.sessions.load(session_id)
            return True
        except Exception:
            return False

    def reload_current_session(self, session_id: str) -> None:
        if self.runtime.has_session and self.runtime.current.id == session_id:
            self.runtime.session = self.sessions.load(session_id)

    def list_sessions(self, limit: int = 100) -> list[dict]:
        return self.sessions.list(limit, with_tokens=False)

    # ---------- 查询与压缩 ----------

    def status(self) -> dict:
        return self.runtime.status()

    def summary_text(self) -> str:
        return self.runtime.summary_text()

    def context_usage(self) -> int:
        return self.runtime.context_usage()

    def estimate_usage(self, session: Session) -> int:
        return self.runtime.estimate_usage(session)

    def context_limit(self, session_id: str | None) -> int:
        with self._lock:
            if session_id and self.ask_in_flight == session_id and self._turn_settings:
                return self._turn_settings.context.config.context_window
        return self.runtime.turn_settings().context.config.context_window

    def compact_threshold(self) -> float:
        return self.runtime.config.compact_threshold

    def force_compact(
        self, on_compaction: Callable[[CompactionPhase], None] | None = None
    ) -> list[str]:
        return self.runtime.force_compact(on_compaction=on_compaction)
