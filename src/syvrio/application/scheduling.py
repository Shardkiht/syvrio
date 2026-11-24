"""管理单个对话的任务准入、排序和完成。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from ..infra.observability.diagnostics import record_event
from ..infra.session import SESSION_ID_RE
from ..infra.session.attachments import AttachmentBatch
from ..infra.session.errors import ConversationError
from ..memory.turn_undo import TurnUndoError
from .commands import CommandResult, CommandUseCases, parse_command
from .contracts import TurnResult
from .turns import ConversationTurn, MessageEdit, TurnEvents, TurnRegistry

if TYPE_CHECKING:
    from .conversation import ConversationUseCases


class ConversationScheduler:
    def __init__(
        self,
        conversation: ConversationUseCases,
        lock: threading.RLock,
        registry: TurnRegistry | None = None,
        settings_lock: threading.RLock | None = None,
    ) -> None:
        self.conversation = conversation
        self._lock = lock
        self.registry = registry if registry is not None else TurnRegistry()
        self.settings_lock = settings_lock if settings_lock is not None else threading.RLock()
        self.command_lock = threading.Lock()
        self.on_idle: Callable[[], None] | None = None
        self._editing = False
        self._operation: str | None = None
        self._closed = False
        self._revision = 0

    def _notify_idle(self) -> None:
        if self.on_idle and not self._closed:
            self.on_idle()

    @property
    def busy(self) -> bool:
        return bool(
            self.conversation.active_turn
            or self.conversation.queue
            or self._editing
            or self._operation
        )

    def session_busy(self, session_id: str) -> bool:
        # 后台任务查询时已持有 Vault 锁。此处再获取准入锁，
        # 会与启动回合或归档时的加锁顺序相反。
        return self.conversation.ask_in_flight == session_id or (
            self.conversation.current_id == session_id and self.busy
        )

    def activity(self) -> tuple[bool, int | None]:
        with self._lock:
            return self.busy, self.conversation.active_turn_id

    def history_changed(self) -> None:
        with self._lock:
            self._revision += 1

    def check_session(self, session_id: str | None, *, exact: bool = False) -> None:
        with self._lock:
            if (exact or session_id is not None) and session_id != self.conversation.current_id:
                raise ConversationError(
                    "session_mismatch", "A different session is selected. Refresh first."
                )

    def _require_open(self) -> None:
        if self._editing:
            raise ConversationError("busy", "A message edit is in progress.")
        if self._closed:
            raise ConversationError("shutting_down", "The service is shutting down.")

    @contextmanager
    def operation(self, name: str, *, require_idle: bool = True) -> Iterator[None]:
        with self._lock:
            self._require_open()
        if not self.command_lock.acquire(blocking=False):
            raise ConversationError(
                "busy", "Another operation is still running. Wait for it to finish."
            )
        try:
            with self._lock:
                self._require_open()
                if require_idle and self.conversation.active_turn:
                    raise ConversationError("busy", "A reply is being generated. Stop it or wait.")
                if require_idle and self.conversation.queue:
                    raise ConversationError("busy", "This session still has queued messages.")
                if require_idle:
                    self._operation = name
            try:
                yield
            finally:
                if require_idle:
                    with self._lock:
                        self._operation = None
        finally:
            self.command_lock.release()

    def execute_command(
        self,
        commands: CommandUseCases,
        line: str,
        session_id: str | None,
        confirmed: bool,
        expected_message_id: str | None = None,
    ) -> CommandResult:
        result = self._execute_command(commands, line, session_id, confirmed, expected_message_id)
        if result.refresh_history:
            self.history_changed()
        return result

    def _execute_command(
        self,
        commands: CommandUseCases,
        line: str,
        session_id: str | None,
        confirmed: bool,
        expected_message_id: str | None,
    ) -> CommandResult:
        name, argument = parse_command(line)
        if expected_message_id is not None and (name != "undo" or not session_id):
            raise ConversationError(
                "invalid_request", "A message target requires /undo and a session."
            )
        target = (
            {"expected_message_id": expected_message_id} if expected_message_id is not None else {}
        )
        if not commands.is_mutating(name, argument):
            with self._lock:
                self.check_session(session_id)
                busy = self.conversation.ask_active
            return commands.execute(line, busy=busy, confirmed=confirmed)
        turn_setting = commands.changes_turn_settings(name, argument)
        with self._lock:
            self._require_open()
            self.check_session(session_id)
            if name == "new" and self.conversation.queue:
                raise ConversationError("busy", "This session still has queued messages.")
            active = self.conversation.active_turn
            if name == "undo" and active is not None:
                if active.undo_claimed:
                    return CommandResult("busy", "undo", "undo", "Undo already in progress.")
                result = commands.execute(line, busy=True, confirmed=confirmed, **target)
                active.undo_claimed = bool((result.data or {}).get("undo_pending"))
            else:
                if not self.command_lock.acquire(blocking=False):
                    raise ConversationError(
                        "busy", "Another command is still running. Wait for it to finish."
                    )
                if not turn_setting:
                    self._operation = name or "command"
        if name == "undo" and active is not None:
            if not active.undo_claimed:
                return result
            active.finished.wait()
            outcome = active.undone
            restored = outcome.restored if outcome else None
            if outcome is None or outcome.failed:
                message = (
                    outcome.public_error if outcome else None
                ) or "Unable to undo the last turn."
                return CommandResult(
                    "error",
                    "undo",
                    "undo",
                    message,
                    {"restored": None},
                    session_id=outcome.target_session_id if outcome else None,
                )
            return CommandResult(
                "ok",
                "undo",
                "undo",
                "Last turn undone." if restored is not None else "Nothing to undo.",
                {"restored": restored},
                session_id=outcome.target_session_id,
                refresh_sessions=restored is not None,
                refresh_history=restored is not None,
            )
        try:
            with self.settings_lock if turn_setting else nullcontext():
                return commands.execute(
                    line, busy=active is not None, confirmed=confirmed, **target
                )
        finally:
            with self._lock:
                self._operation = None
            self.command_lock.release()

    @staticmethod
    def _chat_text(message: str) -> str:
        text = message.strip()
        if not text:
            raise ConversationError("empty_message", "Message must not be empty.")
        if text.startswith("/"):
            raise ConversationError(
                "command_required", "Submit slash commands through the command interface."
            )
        return text

    def submit(
        self, message: str, session_id: str | None, attachments: AttachmentBatch | None = None
    ) -> ConversationTurn:
        text = self._chat_text(message)
        with self._lock:
            self._require_open()
            if session_id is not None:
                if not SESSION_ID_RE.fullmatch(session_id):
                    raise ConversationError("invalid_session_id", "Invalid session ID.")
                if not self.conversation.session_exists(session_id):
                    raise ConversationError("session_not_found", "Session not found.")
            if self._operation or (self.conversation.active_turn and session_id is None):
                raise ConversationError("busy", "Another operation is still running.")
            self.check_session(session_id, exact=True)
            if session_id is None:
                session_id = self.conversation.runtime.ensure_session().id
            turn = ConversationTurn(session_id, text, attachments)
            if self.conversation.active_turn or (
                self.conversation.queue and not self.conversation.queue_paused_after_undo
            ):
                self.conversation.queue.append(turn)
            else:
                self._launch(turn)
                self.conversation.resume_after_undo()
            self.registry.remember(turn)
            return turn

    def submit_once(
        self,
        submission_id: str,
        message: str,
        session_id: str | None,
        attachments: AttachmentBatch | None = None,
    ) -> dict:
        return self.registry.submit_once(
            self.conversation.runtime.service.paths.state_dir,
            submission_id,
            message,
            session_id,
            attachments,
            lambda: self.submit(message, session_id, attachments),
        )

    def replace(
        self,
        receipt_owner: str,
        message_id: str,
        message: str,
        session_id: str | None,
        attachments: AttachmentBatch | None,
    ) -> ConversationTurn:
        text = self._chat_text(message)
        if not session_id:
            raise ConversationError("invalid_request", "Editing a message requires its session.")
        files = tuple((item.name, item.sha256) for item in attachments.files) if attachments else ()
        edit = MessageEdit(receipt_owner, message_id, text, files)
        with self._lock:
            self._require_open()
            self.check_session(session_id, exact=True)
            prior = self.registry.replacement(session_id, edit)
            if prior is not None:
                if prior.edit != edit:
                    raise ConversationError(
                        "edit_conflict", "This message already has a different replacement."
                    )
                if attachments:
                    attachments.discard()
                return prior
            if self._operation:
                raise ConversationError("busy", "Another operation is still running.")
            active = self.conversation.active_turn
            if active and active.undo_claimed:
                raise ConversationError("busy", "Undo already in progress.")
            latest = (
                self.conversation.ask_user_message_id
                if active
                else self.conversation.current.last_user_message_id()
            )
            if latest != message_id:
                raise ConversationError(
                    "edit_conflict",
                    "This message is no longer the latest. Refresh the conversation.",
                )
            self._editing = True
            self.conversation.pause_after_undo(True)
            if active:
                self.conversation.request_cancel()
        try:
            if active:
                active.finished.wait()
            turn = ConversationTurn(session_id, text, attachments, edit=edit)
            try:
                with self._lock:
                    if self._closed:
                        raise ConversationError("shutting_down", "The service is shutting down.")
                    self._launch(turn)
                    self.registry.remember(turn)
                    self.conversation.resume_after_undo()
            except Exception as exc:
                record_event("web.edit_admission_failed", exc)
                raise ConversationError(
                    "edit_not_sent", "The original turn was kept. Retry this edit."
                ) from exc
            turn.saved_or_closed.wait()
            if turn.message_id is None:
                if turn.edit_conflict:
                    raise ConversationError("edit_conflict", turn.edit_conflict)
                raise ConversationError(
                    "edit_not_sent", "The original turn was kept. Retry this edit."
                )
            return turn
        finally:
            with self._lock:
                self._editing = False
                self._start_next()
            self._notify_idle()

    def _launch(self, turn: ConversationTurn) -> None:
        events = TurnEvents(turn)
        try:
            with self.settings_lock:
                runtime = self.conversation.runtime
                if runtime.model_config is not runtime.service.config.llm:
                    runtime.reload_llm(runtime.service.config)
                token = self.conversation.begin_turn(turn.session_id, turn=turn)
            events.start()
            worker = threading.Thread(
                target=self._worker,
                args=(turn, token, events),
                daemon=True,
                name=f"syvrio-turn-{turn.turn_id}",
            )
            worker.start()
        except Exception as exc:
            self.conversation.end_turn(turn.turn_id)
            events.finish("failed", message="Could not start a request.")
            self._close_turn(turn, events)
            record_event("web.worker_failed", exc)
            raise ConversationError("turn_unavailable", "Could not start a request.") from exc

    @staticmethod
    def _close_turn(turn: ConversationTurn, events: TurnEvents) -> None:
        try:
            if turn.message_id is None and turn.attachments and turn.attachments.folder.is_dir():
                turn.attachments.discard()
        except OSError as exc:
            record_event("web.turn_failed", exc)
        finally:
            events.close()
            turn.finished.set()

    def _worker(self, turn: ConversationTurn, token: threading.Event, events: TurnEvents) -> None:
        try:
            result = self.conversation.run_turn(
                turn.line,
                events.callbacks(),
                token,
                turn.session_id,
                **({"attachments": turn.attachments} if turn.attachments else {}),
                **({"replace_message_id": turn.edit.message_id} if turn.edit else {}),
            )
        except Exception as exc:
            record_event("web.turn_failed", exc)
            result = TurnResult(status="failed", error="Request failed unexpectedly.")

        with self._lock:
            try:
                self._finish_result(turn, result, events)
            except Exception as exc:
                record_event("web.turn_failed", exc)
                events.finish("failed", message="Request failed unexpectedly.")
            finally:
                self.conversation.end_turn(turn.turn_id)
                self._revision += 1
                if turn.edit and turn.message_id is None:
                    self.conversation.pause_after_undo(True)
                self._close_turn(turn, events)
                self._start_next()
        self._notify_idle()

    def _finish_result(
        self, turn: ConversationTurn, result: TurnResult, events: TurnEvents
    ) -> None:
        if turn.edit and isinstance(result.exception, TurnUndoError):
            turn.edit_conflict = str(result.exception)
        turn.undone = result.undo
        if turn.undone is None and self.conversation.undo_requested:
            turn.undone = self.conversation.perform_undo(
                result.session_id, result.user_message_id, result.error
            )
        if turn.undone and turn.undone.restored is not None and turn.undone.target_session_id:
            try:
                self.conversation.reload_current_session(turn.undone.target_session_id)
            except (KeyError, OSError, ValueError) as exc:
                record_event("web.history_unreadable", exc)
        events.finish_result(result)

    def _start_next(self) -> None:
        if (
            self._closed
            or self._editing
            or self.conversation.active_turn
            or self.conversation.queue_paused_after_undo
        ):
            return
        while pending := self.conversation.take_next():
            try:
                self._launch(pending)
            except ConversationError:
                continue
            break

    def queue_state(self) -> dict:
        with self._lock:
            active = self.conversation.active_turn
            return {
                "session_id": self.conversation.current_id,
                "active": active.receipt() if active else None,
                "waiting": [turn.receipt() for turn in self.conversation.queue],
                "paused": self.conversation.queue_paused_after_undo,
                "revision": self._revision,
            }

    def remove_queued(self, turn_id: int) -> None:
        with self._lock:
            for turn in self.conversation.queue:
                if turn.turn_id == turn_id:
                    self.conversation.queue.remove(turn)
                    self._cancel_queued(turn)
                    if not self.conversation.queue:
                        self.conversation.resume_after_undo()
                    return
        raise ConversationError("inactive_turn", "This message is no longer queued.")

    def _cancel_queued(self, turn: ConversationTurn) -> None:
        events = TurnEvents(turn)
        events.finish("cancelled")
        self._close_turn(turn, events)

    def insert(self, turn_id: int, session_id: str | None, message: str) -> None:
        with self._lock:
            self._require_open()
            if self.conversation.active_turn_id != turn_id:
                raise ConversationError("inactive_turn", "This reply has already finished.")
            self.check_session(session_id, exact=True)
            request = self.conversation.insert_message(turn_id, message)
        if request is None:
            raise ConversationError(
                "insertion_unavailable", "Cannot insert now. Enter queues the message."
            )
        request.finished.wait()
        if not request.applied:
            raise ConversationError("insertion_unavailable", "The message was not inserted.")

    def cancel(self, turn_id: int) -> bool:
        with self._lock:
            return (
                self.conversation.active_turn_id == turn_id and self.conversation.request_cancel()
            )

    def shutdown(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            while turn := self.conversation.take_next():
                self._cancel_queued(turn)
            active = self.conversation.active_turn
            if active:
                self.conversation.request_cancel()
        if active:
            active.finished.wait()
        return True
