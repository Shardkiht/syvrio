"""Session channel owned by the resident backend and shared across clients."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from ... import __version__
from ...agent.runtime import AgentRuntime, ThinkingUpdate
from ...application.commands import CommandUseCases, parse_command
from ...application.contracts import TurnCallbacks
from ...application.conversation import next_turn_id
from ...application.service import ApplicationService
from ...backend.ownership import BackendLease
from ...background_memory import BackgroundMemory
from ...errors import SessionDeletionChanged, SessionProtectedError, SyvrioError
from ...infra.attachments import AttachmentBatch
from ...infra.diagnostics import record_event
from ...infra.locking import LockTimeout
from ...llm import LiteLLMChatModel
from ...memory.service import MemoryService, get_service
from ...session import SESSION_ID_RE, SessionStore
from .models import (
    ActionResponse,
    CommandCatalogResponse,
    CommandInfo,
    CommandResponse,
    ContextUsageResponse,
    CreateSessionResponse,
    DeleteSessionResponse,
    DraftAttachment,
    HealthResponse,
    HistoryItem,
    HistoryResponse,
    MessageDraft,
    ModelSearchResponse,
    PendingDeletionItem,
    ProviderActionRequest,
    ProviderInfo,
    ProviderListResponse,
    ReasoningBlock,
    SessionDeletionPreview,
    SessionInfo,
    SessionListResponse,
    StreamEvent,
    SwitchSessionResponse,
    ToolCallInfo,
)
from .scheduler import GitScheduler
from .turns import ActiveTurn, TurnRegistry

HEARTBEAT_SECONDS = 15.0
TOOL_PREFIX = "Calling tool: "
MODEL_PHASES = frozenset({"Waiting for model", "Generating final answer"})
HISTORY_ITEM_LIMIT = 300
TOOL_CONTENT_LIMIT = 300


class WebError(SyvrioError, Exception):
    """Expected failure with a stable, browser-safe error code."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class EventPump:
    """Turn TurnCallbacks into ordered SSE frames (runs in the worker thread).

    ``phase`` advances at each model request so reasoning from different tool
    rounds never merges. Tool starts are paired with their result events in
    call order, which also covers parallel tool batches.
    """

    def __init__(self, turn: ActiveTurn, post) -> None:
        self._turn = turn
        self._post = post
        self._seq = 0
        self._phase = 0
        self._session_id = turn.session_id
        self._pending_tools: list[str] = []
        # Outcomes line up with ``_pending_tools``: the runtime reports a tool's
        # result right before the event text that belongs to it.
        self._pending_ok: list[bool] = []

    def send(self, event_type: str, **fields: Any) -> None:
        self._seq += 1
        payload: dict[str, Any] = {
            "seq": self._seq,
            "type": event_type,
            "turn_id": self._turn.turn_id,
            "session_id": self._session_id,
        }
        payload.update(fields)
        self._post(self._turn, StreamEvent(**payload))

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def start(self) -> None:
        self.send("start")

    def on_session(self, session_id: str) -> None:
        # The first message creates the session; later frames carry its real ID.
        self._session_id = session_id

    def on_user_message(self, message_id: str) -> None:
        self.send("message_saved", message_id=message_id)

    def on_progress(self, text: str) -> None:
        if text.startswith(TOOL_PREFIX):
            name = text[len(TOOL_PREFIX) :].strip() or "tool"
            self._pending_tools.append(name)
            self.send("tool_start", name=name)
            return
        self._flush_tools()
        if text in MODEL_PHASES:
            self._phase += 1
        self.send("progress", text=text)

    def on_tool(self, ok: bool) -> None:
        self._pending_ok.append(ok)

    def on_event(self, text: str) -> None:
        if self._pending_tools:
            name = self._pending_tools.pop(0)
            ok = self._pending_ok.pop(0) if self._pending_ok else None
            self.send("tool_end", name=name, text=text, ok=ok)
            return
        self.send("event", text=text)

    def on_delta(self, delta: str) -> None:
        self._flush_tools()
        if delta:
            self.send("content_delta", text=delta)

    def on_thinking(self, update: ThinkingUpdate) -> None:
        self._flush_tools()
        if update.record_id is not None:
            self.send("reasoning_end", text=update.text, seconds=update.seconds, phase=self._phase)
        elif update.text:
            self.send("reasoning_delta", text=update.text, phase=self._phase)

    def _flush_tools(self) -> None:
        while self._pending_tools:
            self.send("tool_end", name=self._pending_tools.pop(0))
        # Any outcome left here belongs to a call that never emitted text; drop
        # it so the next tool cannot inherit the wrong state.
        self._pending_ok.clear()

    def finish(self, event_type: str, **fields: Any) -> None:
        self._flush_tools()
        self.send(event_type, **fields)

    def close(self) -> None:
        self._post(self._turn, None)


class WebBackend:
    """Owns the process-wide runtime and the active chat turn."""

    def __init__(
        self,
        config_path: Path | None = None,
        vault_path: Path | None = None,
        service: MemoryService | None = None,
        *,
        parent: WebBackend | None = None,
    ) -> None:
        self._parent = parent
        self._service = parent._service if parent else service or get_service(config_path, vault_path)
        self._sessions = parent._sessions if parent else SessionStore(self._service.paths)
        self._runtime = AgentRuntime(
            self._service, LiteLLMChatModel(self._service.config.llm) if parent is None else parent._runtime.model,
            self._sessions
        )
        self._runtime.start()
        self._runtime.allow_local_paths = False
        self.session_busy = lambda session_id: self._conversation.ask_in_flight == session_id
        self._background = parent._background if parent else BackgroundMemory(
            self._service, session_busy=lambda session_id: self.session_busy(session_id)
        )
        self._runtime.on_distillation = self._background.request_distillation
        self._app_service = ApplicationService(self._runtime, self._service, self._background)
        self._turns = parent._turns if parent else TurnRegistry()
        self._git_scheduler = parent._git_scheduler if parent else GitScheduler(self._app_service.git)
        self._conversation = self._app_service.conversation
        self._commands = CommandUseCases(self._app_service)
        self._model_config = self._service.config.llm
        self._lock = threading.RLock()
        # Serializes mutating commands/session ops; held across slow LLM/Git work
        # so ``_lock`` itself stays free for health/cancel/turn bookkeeping.
        self._command_lock = threading.Lock()
        self._navigation_lock = threading.Lock()
        self._operation: str | None = None
        self._active: ActiveTurn | None = None
        self._waiting: deque[tuple[ActiveTurn, str]] = deque()
        self._queue_paused = False
        self._revision = 0
        self._client_lock = BackendLease(self._service.paths)
        self._started = False
        self._closed = False
        self.on_idle = None

    # ---------- lifecycle ----------

    def startup(self, ownership: BackendLease | None = None) -> None:
        try:
            if ownership is None:
                self._client_lock.acquire(timeout=0)
            else:
                self._client_lock = ownership
        except (LockTimeout, OSError) as exc:
            raise WebError(
                "client_busy",
                "A resident Syvrio backend already owns this Vault.",
                503,
            ) from exc
        try:
            from ...infra.transaction import recover_vault_transaction

            recover_vault_transaction(self._service.paths)
            if not (self._service.config.vault / ".git").exists():
                self._service.init_vault()
            self._background.start()
            self._git_scheduler.start()
            self._started = True
        except BaseException:
            self._background.stop()
            if ownership is None:
                self._client_lock.release()
            raise
        for hook in (self._service.commit_pending_changes, self._app_service.warm_search):
            try:
                hook()
            except Exception as exc:
                record_event("backend.startup_hook_failed", exc)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while self._waiting:
                turn, _ = self._waiting.popleft()
                if turn.attachments:
                    turn.attachments.discard()
                pump = EventPump(turn, self._post)
                pump.finish("cancelled")
                pump.close()
                turn.finished.set()
            active = self._active
            if active is not None:
                self._conversation.request_cancel()
        if active is not None:
            active.finished.wait()
        # Keep the client lock until every writer has stopped. Shutdown records
        # the remaining work for startup, without launching another model call.
        with self._command_lock:
            try:
                self._runtime.close(distill=False)
            except Exception as exc:
                record_event("web.shutdown_distill_failed", exc)
            finally:
                if self._started:
                    self._git_scheduler.stop()
                    self._background.stop()
                    self._client_lock.release()
                    self._started = False

    # ---------- queries ----------

    def health(self, static_ready: bool = True) -> HealthResponse:
        with self._lock:
            active = self._active
            operating = self._operation is not None or bool(self._waiting)
        llm = self._service.config.llm
        return HealthResponse(
            version=__version__,
            provider=llm.provider,
            model=llm.model,
            busy=active is not None or operating,
            active_turn_id=active.turn_id if active else None,
            static_ready=static_ready,
        )

    def context_usage(self, session_id: str | None) -> ContextUsageResponse:
        used = 0
        if session_id is not None:
            self._validate_session_id(session_id)
            current = self._runtime.session
            if current is not None and current.id == session_id:
                # Estimate from a snapshot while the turn may append new records.
                session = replace(current, records=list(current.records))
            else:
                try:
                    session = self._sessions.load(session_id, include_reasoning=False)
                except KeyError as exc:
                    raise WebError("not_found", "Session not found.", 404) from exc
            used = self._conversation.estimate_usage(session)
        return ContextUsageResponse(
            session_id=session_id,
            used=used,
            limit=self._app_service.settings.context_window,
            compact_threshold=self._conversation.compact_threshold(),
        )

    # ---------- slash commands ----------

    def command_catalog(self) -> CommandCatalogResponse:
        return CommandCatalogResponse(
            items=[
                CommandInfo(
                    name=row.name,
                    usage=row.usage,
                    description=row.description,
                    support=row.support,
                    intent=row.intent,
                    confirm=row.confirm,
                    note=row.note,
                    shortcut=row.shortcut,
                    subcommands=[
                        {"name": name, "description": description}
                        for name, description in row.subcommands
                    ],
                )
                for row in self._commands.catalog()
            ]
        )

    def execute_command(
        self, line: str, session_id: str | None, confirmed: bool, expected_message_id: str | None = None
    ) -> CommandResponse:
        if session_id is not None:
            self._validate_session_id(session_id)
        name, argument = parse_command(line)
        if expected_message_id is not None and (name != "undo" or not session_id):
            raise WebError("invalid_request", "A message target requires /undo and a session.", 422)
        target = {"expected_message_id": expected_message_id} if expected_message_id is not None else {}
        # Read-only and interrupt forms (e.g. /dream cancel) must stay reachable
        # while a slow command runs, so they never take the command mutex.
        if not self._commands.is_mutating(name, argument):
            with self._lock:
                self._check_session_match(session_id)
                busy = self._active is not None
            result = self._commands.execute(line, busy=busy, confirmed=confirmed)
            return self._command_response(result)
        with self._lock:
            self._require_open()
            self._check_session_match(session_id)
            if name == "new" and self._waiting:
                raise WebError("busy", "This session still has queued messages.", 409)
            active = self._active
            interrupt = name == "undo" and active is not None
            if interrupt:
                if active.undo_claimed:
                    return CommandResponse(
                        status="busy",
                        command="undo",
                        intent="undo",
                        message="Undo already in progress.",
                    )
                result = self._commands.execute(line, busy=True, confirmed=confirmed, **target)
                active.undo_claimed = bool((result.data or {}).get("undo_pending"))
            else:
                if not self._command_lock.acquire(blocking=False):
                    raise WebError(
                        "busy", "Another command is still running. Wait for it to finish.", 409
                    )
                self._operation = name or "command"
        if interrupt:
            return self._finish_active_undo(active, result)
        try:
            result = self._commands.execute(line, busy=active is not None, confirmed=confirmed, **target)
            return self._command_response(result)
        finally:
            with self._lock:
                self._operation = None
            self._command_lock.release()

    def _finish_active_undo(self, active: ActiveTurn, result) -> CommandResponse:
        if not (result.data or {}).get("undo_pending"):
            return self._command_response(result)
        active.finished.wait()
        outcome = active.undone
        restored = outcome.restored if outcome else None
        if outcome is None or outcome.failed:
            status = "error"
            message = (
                outcome.public_error
                if outcome and outcome.public_error
                else "Unable to undo the last turn."
            )
        else:
            status = "ok"
            message = "Last turn undone." if restored is not None else "Nothing to undo."
        return CommandResponse(
            status=status,
            command="undo",
            intent="undo",
            message=message,
            data={"restored": restored},
            session_id=outcome.target_session_id if outcome else None,
            refresh_history=restored is not None,
            refresh_sessions=restored is not None,
        )

    def _check_session_match(self, session_id: str | None, *, exact: bool = False) -> None:
        current = self._conversation.current_id
        # Chat binds even the empty view. Commands can omit the session for
        # compatibility with API callers issuing global commands.
        if (exact or session_id is not None) and session_id != current:
            raise WebError(
                "session_mismatch", "This tab is viewing a different session. Refresh first.", 409
            )

    def _command_response(self, result) -> CommandResponse:
        with self._lock:
            if result.refresh_history:
                self._revision += 1
            current = result.session_id or self._conversation.current_id
        return CommandResponse(
            status=result.status,
            command=result.command,
            intent=result.intent,
            message=result.message,
            data=result.data,
            session_id=current,
            refresh_sessions=result.refresh_sessions,
            refresh_history=result.refresh_history,
        )

    @contextmanager
    def _operation_scope(self, name: str):
        """Serialize a mutating session/provider op and mark the backend busy.

        Slow work runs outside ``_lock`` so health/cancel never block; the command
        mutex keeps two mutating ops from overlapping.
        """
        self._require_open()
        if not self._command_lock.acquire(blocking=False):
            raise WebError(
                "busy", "Another operation is still running. Wait for it to finish.", 409
            )
        try:
            with self._lock:
                self._require_idle()
                self._operation = name
            try:
                yield
            finally:
                with self._lock:
                    self._operation = None
        finally:
            self._command_lock.release()

    def edit_draft(self, session_id: str, message_id: str) -> MessageDraft:
        self._validate_session_id(session_id)
        with self._lock:
            self._check_session_match(session_id, exact=True)
        with self._service.write_lock:
            session = self._sessions.load(session_id, include_reasoning=False)
            inputs = session.latest_turn_inputs()
        if not inputs or inputs[-1]["id"] != message_id:
            raise WebError("stale_message", "Only the latest user message can be edited.", 409)
        return MessageDraft(
            text="\n\n".join(item["content"] for item in inputs),
            attachments=[
                DraftAttachment(message_id=item["id"], **file)
                for item in inputs for file in item.get("attachments", [])
            ],
        )

    def list_sessions(self, limit: int = 100) -> SessionListResponse:
        rows = self._conversation.list_sessions(max(1, min(limit, 500)))
        current = self._conversation.current_id
        return SessionListResponse(
            items=[
                SessionInfo(
                    id=str(row["id"]),
                    title=str(row["title"]),
                    messages=int(row["messages"]),
                    started_at=str(row["started_at"]),
                    updated_at=str(row["updated_at"]),
                    last_message_at=str(row.get("last_message_at") or ""),
                    current=str(row["id"]) == current,
                )
                for row in rows
            ]
        )

    def history(self, session_id: str, limit: int = HISTORY_ITEM_LIMIT) -> HistoryResponse:
        self._validate_session_id(session_id)
        try:
            session = self._sessions.load(session_id)
        except KeyError as exc:
            raise WebError("session_not_found", "Session not found.", 404) from exc
        except (ValueError, OSError) as exc:
            record_event("web.history_unreadable", exc)
            raise WebError("session_unreadable", "Session history could not be read.", 500) from exc
        records = session.active_records()
        bounded = max(1, min(limit, HISTORY_ITEM_LIMIT))
        has_earlier = len(records) > bounded
        visible = records[-bounded:] if has_earlier else records
        items: list[HistoryItem] = []
        for record in visible:
            role = record.get("type")
            base = {
                "id": str(record.get("id") or ""),
                "at": str(record.get("at") or ""),
            }
            if role == "message" and record.get("role") == "user":
                items.append(
                    HistoryItem(role="user", content=str(record.get("content") or ""),
                                attachments=record.get("attachments", []), **base)
                )
            elif role == "assistant":
                reasoning = record.get("reasoning")
                block = None
                if isinstance(reasoning, dict) and reasoning.get("text"):
                    seconds = reasoning.get("seconds")
                    block = ReasoningBlock(
                        text=str(reasoning["text"]),
                        seconds=float(seconds) if isinstance(seconds, (int, float)) else None,
                    )
                calls = [
                    ToolCallInfo(id=str(call.get("id") or ""), name=str(call.get("name") or ""))
                    for call in (record.get("tool_calls") or [])
                    if isinstance(call, dict)
                ]
                items.append(
                    HistoryItem(
                        role="assistant",
                        content=str(record.get("content") or ""),
                        reasoning=block,
                        tool_calls=calls,
                        interrupted=bool(record.get("interrupted")),
                        **base,
                    )
                )
            elif role == "tool":
                items.append(
                    HistoryItem(
                        role="tool",
                        content=str(record.get("content") or "")[:TOOL_CONTENT_LIMIT],
                        tool_name=str(record.get("name") or ""),
                        tool_ok=bool(record.get("ok")),
                        **base,
                    )
                )
            elif role == "summary":
                items.append(
                    HistoryItem(role="summary", content=str(record.get("content") or ""), **base)
                )
        return HistoryResponse(
            session_id=session.id,
            title=session.title(),
            items=items,
            summary=session.summary_text() or None,
            has_earlier=has_earlier,
            current=self._conversation.current_id == session.id,
        )

    # ---------- session lifecycle ----------

    def create_session(self) -> CreateSessionResponse:
        """Detach to a fresh conversation without touching the disk.

        The runtime already defers ``sessions.create()`` to the first message
        (``AgentRuntime.start``), so creating a file here would only produce an
        empty session — the exact behaviour users see as clutter in the sidebar.
        """
        with self._navigation_lock, self._operation_scope("new"):
            self._conversation.switch(None)
        return CreateSessionResponse(session_id=None, title="")

    def switch_session(self, session_id: str) -> SwitchSessionResponse:
        self._validate_session_id(session_id)
        with self._navigation_lock, self._operation_scope("switch"):
            if not self._conversation.session_exists(session_id):
                raise WebError("session_not_found", "Session not found.", 404)
            try:
                result = self._conversation.switch(session_id)
            except (KeyError, ValueError, OSError) as exc:
                record_event("web.switch_failed", exc)
                raise WebError("session_unreadable", "Session could not be restored.", 500) from exc
        session = result.session
        return SwitchSessionResponse(session_id=session.id, title=session.title())

    def deletion_preview(self, session_id: str) -> SessionDeletionPreview:
        self._validate_session_id(session_id)
        try:
            items, revision = self._conversation.deletion_preview(session_id)
        except KeyError as exc:
            raise WebError("session_not_found", "Session not found.", 404) from exc
        except SessionProtectedError as exc:
            raise WebError("session_protected", str(exc), 409) from exc
        except (ValueError, OSError) as exc:
            record_event("session.delete_failed", exc)
            raise WebError("delete_failed", "The related pending items could not be read.", 500) from exc
        return SessionDeletionPreview(
            pending=[PendingDeletionItem(id=item.id, text=item.text) for item in items],
            revision=revision,
        )

    def delete_session(self, session_id: str, confirmed: bool, pending_revision: str = "") -> DeleteSessionResponse:
        """Delete a non-live session. Mirrors the TUI guard plus server-side confirmation.

        A browser dialog is not an authorization boundary: the server validates
        the ID, requires an explicit confirmation flag, re-checks that the
        session is not live / in flight, and only then removes the JSONL file.
        """
        self._validate_session_id(session_id)
        with self._operation_scope("delete"):
            if not self._conversation.session_exists(session_id):
                raise WebError("session_not_found", "Session not found.", 404)
            if not confirmed:
                raise WebError(
                    "confirm_required",
                    "Deleting a session is permanent; confirm the action first.",
                    409,
                )
            try:
                if not self._conversation.delete_session(session_id, pending_revision):
                    raise WebError("session_not_found", "Session not found.", 404)
            except SessionProtectedError as exc:
                raise WebError("session_protected", str(exc), 409) from exc
            except SessionDeletionChanged as exc:
                raise WebError("pending_changed", str(exc), 409) from exc
            except (OSError, ValueError) as exc:
                record_event("session.delete_failed", exc)
                raise WebError(
                    "delete_failed",
                    "Session could not be deleted; the file was kept.",
                    500,
                ) from exc
        return DeleteSessionResponse(session_id=session_id)

    # ---------- settings / providers (TUI Ctrl+P equivalent) ----------

    def search_models(self, query: str, refresh: bool = False) -> ModelSearchResponse:
        items, unavailable = self._app_service.settings.search_models(query, refresh=refresh)
        return ModelSearchResponse(items=items, unavailable_providers=unavailable)

    def providers(self) -> ProviderListResponse:
        settings = self._app_service.settings
        return ProviderListResponse(
            items=[
                ProviderInfo(
                    key=option.key,
                    label=option.label,
                    configured=option.configured,
                    current=option.current,
                    endpoint=settings.provider_endpoint(option.key),
                )
                for option in settings.provider_options()
            ]
        )

    def provider_action(self, provider: str, body: ProviderActionRequest) -> ActionResponse:
        """Provider credentials never leave the server; only booleans are returned."""
        settings = self._app_service.settings
        if provider not in {option.key for option in settings.provider_options()}:
            raise WebError("unknown_provider", "Unknown provider.", 404)
        with self._operation_scope("provider"):
            if body.action == "remove":
                if not body.confirmed:
                    raise WebError(
                        "confirm_required",
                        "Removing a provider deletes its saved API key. Confirm first.",
                        409,
                    )
                result = settings.remove_provider(provider)
            elif body.action == "set_endpoint":
                result = settings.set_provider_endpoint(provider, body.endpoint)
            elif body.action == "set_key":
                result = settings.configure_provider(provider, body.api_key)
            elif body.action == "update_key":
                result = settings.update_provider_key(provider, body.api_key)
            else:
                raise WebError("invalid_action", "Unknown provider action.", 422)
        return ActionResponse(
            ok=result.ok,
            message=result.message,
            refresh_health=result.ok and result.config_changed,
        )

    # ---------- chat streaming ----------

    def start_turn(
        self, message: str, session_id: str | None, loop: asyncio.AbstractEventLoop,
        attachments: AttachmentBatch | None = None,
    ) -> ActiveTurn:
        text = message.strip()
        if not text:
            raise WebError("empty_message", "Message must not be empty.", 422)
        # Defence in depth: even a stale or hand-crafted client must never send
        # an unrecognised slash line to the LLM. The browser routes every "/"
        # line to /api/commands; the server enforces the same boundary.
        if text.startswith("/"):
            raise WebError(
                "command_required",
                "Slash commands must be sent to /api/commands.",
                422,
            )
        if session_id is not None:
            self._validate_session_id(session_id)
            if not self._conversation.session_exists(session_id):
                raise WebError("session_not_found", "Session not found.", 404)
        with self._lock:
            self._require_open()
            if self._operation or (self._active and session_id is None):
                raise WebError("busy", "Another operation is still running.", 409)
            self._check_session_match(session_id, exact=True)
            if session_id is None:
                session_id = self._runtime.ensure_session().id
            turn = ActiveTurn(next_turn_id(), session_id, text, attachments=attachments)
            self._turns.remember(turn)
            if self._active is not None or (self._waiting and not self._queue_paused):
                self._waiting.append((turn, text))
            else:
                self._queue_paused = False
                self._launch(turn, text)
        return turn

    def _launch(self, turn: ActiveTurn, text: str) -> None:
        if self._model_config is not self._service.config.llm:
            self._runtime.reload_llm(self._service.config)
            self._model_config = self._service.config.llm
        token = self._conversation.begin_turn(turn.session_id, turn_id=turn.turn_id)
        self._active = turn
        pump = EventPump(turn, self._post)
        pump.start()
        try:
            worker = threading.Thread(
                target=self._worker,
                args=(turn, text, token, pump),
                daemon=True,
                name=f"syvrio-web-turn-{turn.turn_id}",
            )
            worker.start()
        except Exception as exc:
            self.release(turn.turn_id)
            pump.finish("failed", message="Could not start a request.")
            pump.close()
            turn.finished.set()
            record_event("web.worker_failed", exc)
            raise WebError("turn_unavailable", "Could not start a request.", 503) from exc

    def _worker(self, turn: ActiveTurn, text: str, token, pump: EventPump) -> None:
        callbacks = TurnCallbacks(
            on_event=pump.on_event,
            on_delta=pump.on_delta,
            on_progress=pump.on_progress,
            on_thinking=pump.on_thinking,
            on_tool=pump.on_tool,
            on_compaction=lambda phase: pump.send("compaction", text=phase),
            on_session=lambda session: pump.on_session(session.id),
            on_user_message=pump.on_user_message,
            on_inserted=lambda mid, content: pump.send("message_inserted", message_id=mid, text=content),
        )
        try:
            result = self._conversation.run_turn(text, callbacks, token, turn.session_id, **(
                {"attachments": turn.attachments} if turn.attachments else {}
            ))
        except Exception as exc:  # run_turn normally funnels errors into a TurnResult
            record_event("web.turn_failed", exc)
            pump.finish("failed", message="Request failed unexpectedly.")
        else:
            with self._lock:
                turn.undone = result.undo
                if turn.undone is None and self._conversation.undo_requested:
                    # An undo can arrive after run_turn has returned but before
                    # the Web worker has released the active turn.
                    turn.undone = self._conversation.perform_undo(
                        result.session_id, result.user_message_id, result.error
                    )
                if turn.undone is not None and turn.undone.restored is not None:
                    self._queue_paused = bool(self._waiting)
                    target = turn.undone.target_session_id
                    if target is not None:
                        try:
                            self._conversation.reload_current_session(target)
                        except (KeyError, OSError, ValueError) as exc:
                            record_event("web.history_unreadable", exc)
                self.release(turn.turn_id)
            if turn.undone is not None:
                pump.finish("cancelled", session_id=result.session_id or pump.session_id)
            elif result.status == "ok":
                reply = result.reply
                pump.finish(
                    "done",
                    text=result.content,
                    session_id=result.session_id or pump.session_id,
                    steps=reply.steps if reply else 0,
                    estimated_tokens=reply.estimated_tokens if reply else 0,
                )
            elif result.status in ("cancelled", "undo"):
                pump.finish("cancelled", session_id=result.session_id or pump.session_id)
            else:
                pump.finish(
                    "failed",
                    session_id=result.session_id or pump.session_id,
                    message=result.error or "Request failed",
                )
        finally:
            with self._lock:
                self.release(turn.turn_id)
                self._revision += 1
                turn.finished.set()
                pump.close()
                while self._active is None and self._waiting and not self._queue_paused and not self._closed:
                    pending, message = self._waiting.popleft()
                    try:
                        self._launch(pending, message)
                    except WebError:
                        continue  # The failed receipt was settled; advance the next one.
            if self.on_idle and not self._closed:
                self.on_idle(self)

    def stream(self, turn: ActiveTurn, after: int = 0):
        return turn.stream(after, heartbeat=HEARTBEAT_SECONDS)

    def queue_state(self) -> dict:
        with self._lock:
            return {
                "session_id": self._conversation.current_id,
                "active": self._active.receipt() if self._active else None,
                "waiting": [turn.receipt() for turn, _ in self._waiting],
                "paused": self._queue_paused,
                "revision": self._revision,
            }

    def remove_queued(self, turn_id: int) -> None:
        with self._lock:
            for turn, text in self._waiting:
                if turn.turn_id == turn_id:
                    self._waiting.remove((turn, text))
                    if turn.attachments:
                        turn.attachments.discard()
                    pump = EventPump(turn, self._post)
                    pump.finish("cancelled")
                    pump.close()
                    turn.finished.set()
                    self._queue_paused = bool(self._waiting) and self._queue_paused
                    return
        raise WebError("inactive_turn", "This message is no longer queued.", 409)

    def insert(self, turn_id: int, session_id: str | None, message: str) -> None:
        with self._lock:
            self._require_open()
            active = self._active
            if active is None or active.turn_id != turn_id:
                raise WebError("inactive_turn", "This reply has already finished.", 409)
            self._check_session_match(session_id, exact=True)
            request = self._conversation.insert_message(turn_id, message)
        if request is None:
            raise WebError(
                "insertion_unavailable", "Cannot insert now. Enter queues the message.", 409
            )
        request.finished.wait()
        if not request.applied:
            raise WebError("insertion_unavailable", "The message was not inserted.", 409)

    def cancel(self, turn_id: int) -> bool:
        with self._lock:
            active = self._active
            if active is None or active.turn_id != turn_id:
                return False
            return self._conversation.request_cancel()

    def active_turn_id(self) -> int | None:
        with self._lock:
            return self._active.turn_id if self._active else None

    # ---------- internals ----------

    @staticmethod
    def _post(turn: ActiveTurn, event: StreamEvent | None) -> None:
        turn.publish(event)

    def release(self, turn_id: int) -> None:
        with self._lock:
            if self._active is not None and self._active.turn_id == turn_id:
                self._active = None
            self._conversation.end_turn(turn_id)

    def _require_open(self) -> None:
        if self._closed:
            raise WebError("shutting_down", "The Web service is shutting down.", 503)

    def _require_idle(self) -> None:
        self._require_open()
        if self._active is not None:
            raise WebError("busy", "A reply is being generated. Stop it or wait.", 409)
        if self._waiting:
            raise WebError("busy", "This session still has queued messages.", 409)
        if self._operation is not None:
            raise WebError(
                "busy", "Another operation is still running. Wait for it to finish.", 409
            )

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise WebError("invalid_session_id", "Invalid session ID.", 422)
