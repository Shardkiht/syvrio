"""Single-flight Web adapter over the existing Application layer.

One process owns one ApplicationService / AgentRuntime. Only one turn may run at
a time: a second chat request gets an explicit ``busy`` error instead of two
streams writing the same session. Callbacks from the worker thread are converted
into ordered structured events and posted onto the SSE connection's asyncio
queue. Nothing here re-implements the agent loop, memory, sessions or LLM calls.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ... import __version__
from ...agent.runtime import AgentRuntime, ThinkingUpdate
from ...agent.session import SESSION_ID_RE, SessionStore
from ...application.commands import CommandUseCases, parse_command
from ...application.contracts import TurnCallbacks
from ...application.service import ApplicationService
from ...infra.diagnostics import record_event
from ...infra.locking import LockTimeout, VaultLock, client_instance_lock
from ...llm import LiteLLMChatModel
from ...memory.service import MemoryService, get_service
from .models import (
    CommandCatalogResponse,
    CommandInfo,
    ActionResponse,
    CommandResponse,
    CreateSessionResponse,
    DeleteSessionResponse,
    HealthResponse,
    ProviderActionRequest,
    ProviderInfo,
    ProviderListResponse,
    HistoryItem,
    HistoryResponse,
    ReasoningBlock,
    SessionInfo,
    SessionListResponse,
    StreamEvent,
    SwitchSessionResponse,
    ToolCallInfo,
    sse_frame,
)

HEARTBEAT_SECONDS = 15.0
TOOL_PREFIX = "Calling tool: "
MODEL_PHASES = frozenset({"Waiting for model", "Generating final answer"})
HISTORY_ITEM_LIMIT = 300
TOOL_CONTENT_LIMIT = 300


class WebError(Exception):
    """Expected failure with a stable, browser-safe error code."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class ActiveTurn:
    turn_id: int
    session_id: str | None
    queue: asyncio.Queue[StreamEvent | None]
    loop: asyncio.AbstractEventLoop
    finished: threading.Event = field(default_factory=threading.Event)


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
        pass  # Bind stays internal (used by undo and cancellation).

    def on_progress(self, text: str) -> None:
        if text.startswith(TOOL_PREFIX):
            name = text[len(TOOL_PREFIX):].strip() or "tool"
            self._pending_tools.append(name)
            self.send("tool_start", name=name)
            return
        self._flush_tools()
        if text in MODEL_PHASES:
            self._phase += 1
        self.send("progress", text=text)

    def on_event(self, text: str) -> None:
        if self._pending_tools:
            name = self._pending_tools.pop(0)
            self.send("tool_end", name=name, text=text)
            return
        self.send("event", text=text)

    def on_delta(self, delta: str) -> None:
        self._flush_tools()
        if delta:
            self.send("content_delta", text=delta)

    def on_thinking(self, update: ThinkingUpdate) -> None:
        self._flush_tools()
        if update.record_id is not None:
            self.send("reasoning_end", text=update.text, seconds=update.seconds,
                      phase=self._phase)
        elif update.text:
            self.send("reasoning_delta", text=update.text, phase=self._phase)

    def _flush_tools(self) -> None:
        while self._pending_tools:
            self.send("tool_end", name=self._pending_tools.pop(0))

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
    ) -> None:
        self._service = service or get_service(config_path, vault_path)
        if not (self._service.config.vault / ".git").exists():
            self._service.init_vault()
        self._sessions = SessionStore(self._service.paths)
        self._runtime = AgentRuntime(
            self._service, LiteLLMChatModel(self._service.config.llm), self._sessions
        )
        self._runtime.start()
        self._app_service = ApplicationService(self._runtime, self._service)
        self._conversation = self._app_service.conversation
        self._commands = CommandUseCases(self._app_service)
        self._lock = threading.RLock()
        # Serializes mutating commands/session ops; held across slow LLM/Git work
        # so ``_lock`` itself stays free for health/cancel/turn bookkeeping.
        self._command_lock = threading.Lock()
        self._operation: str | None = None
        self._active: ActiveTurn | None = None
        self._client_lock: VaultLock = client_instance_lock(self._service.paths)
        self._started = False
        self._closed = False

    # ---------- lifecycle ----------

    def startup(self) -> None:
        try:
            self._client_lock.acquire(timeout=0)
        except (LockTimeout, OSError) as exc:
            raise WebError(
                "client_busy",
                "Another Syvrio client (TUI or Web) is already using this Vault. "
                "Close it before starting the Web server.",
                503,
            ) from exc
        self._started = True
        # Reuse the Application layer's own startup/shutdown hooks; failures
        # must not prevent the server from starting.
        for hook in (self._app_service.startup_catchup, self._app_service.warm_search):
            try:
                hook()
            except Exception as exc:
                record_event("web.startup_hook_failed", exc)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._active
            if active is not None:
                self._conversation.request_cancel()
        if active is not None:
            active.finished.wait(timeout=5.0)
        try:
            self._runtime.close(distill=True)
        except Exception as exc:
            record_event("web.shutdown_distill_failed", exc)
        if self._started:
            self._client_lock.release()
            self._started = False

    # ---------- queries ----------

    def health(self, static_ready: bool = True) -> HealthResponse:
        with self._lock:
            active = self._active
            operating = self._operation is not None
        llm = self._service.config.llm
        return HealthResponse(
            version=__version__,
            provider=llm.provider,
            model=llm.model,
            busy=active is not None or operating,
            active_turn_id=active.turn_id if active else None,
            static_ready=static_ready,
        )

    # ---------- slash commands ----------

    def command_catalog(self) -> CommandCatalogResponse:
        return CommandCatalogResponse(items=[
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
        ])

    def execute_command(
        self, line: str, session_id: str | None, confirmed: bool
    ) -> CommandResponse:
        if session_id is not None:
            self._validate_session_id(session_id)
        name, argument = parse_command(line)
        # Read-only and interrupt forms (e.g. /dream cancel) must stay reachable
        # while a slow command runs, so they never take the command mutex.
        if not self._commands.is_mutating(name, argument):
            with self._lock:
                self._check_session_match(session_id)
                busy = self._active is not None
            result = self._commands.execute(line, busy=busy, confirmed=confirmed)
            return self._command_response(result)
        if not self._command_lock.acquire(blocking=False):
            raise WebError("busy", "Another command is still running. Wait or cancel it.", 409)
        try:
            with self._lock:
                self._check_session_match(session_id)
                busy = self._active is not None
                self._operation = name or "command"
            try:
                result = self._commands.execute(line, busy=busy, confirmed=confirmed)
            finally:
                with self._lock:
                    self._operation = None
        finally:
            self._command_lock.release()
        return self._command_response(result)

    def _check_session_match(self, session_id: str | None) -> None:
        current = self._conversation.current_id
        # A stale tab must not act on another tab's session.
        if session_id and current and session_id != current:
            raise WebError("session_mismatch",
                           "This tab is viewing a different session. Refresh first.", 409)

    def _command_response(self, result) -> CommandResponse:
        with self._lock:
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
        if not self._command_lock.acquire(blocking=False):
            raise WebError("busy", "Another operation is still running. Wait or cancel it.", 409)
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

    def list_sessions(self, limit: int = 100) -> SessionListResponse:
        rows = self._conversation.list_sessions(max(1, min(limit, 500)))
        # SessionStore.list() sorts by updated_at, which also advances on
        # control records (rename / summary / distill / undo). The sidebar
        # follows the last real message instead, so an older conversation never
        # jumps ahead just because it was renamed or distilled.
        rows = sorted(
            rows,
            key=lambda row: str(row.get("last_message_at") or row.get("updated_at") or ""),
            reverse=True,
        )
        current = self._conversation.current_id
        return SessionListResponse(items=[
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
        ])

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
                items.append(HistoryItem(role="user", content=str(record.get("content") or ""), **base))
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
                items.append(HistoryItem(
                    role="assistant",
                    content=str(record.get("content") or ""),
                    reasoning=block,
                    tool_calls=calls,
                    interrupted=bool(record.get("interrupted")),
                    **base,
                ))
            elif role == "tool":
                items.append(HistoryItem(
                    role="tool",
                    content=str(record.get("content") or "")[:TOOL_CONTENT_LIMIT],
                    tool_name=str(record.get("name") or ""),
                    tool_ok=bool(record.get("ok")),
                    **base,
                ))
            elif role == "summary":
                items.append(HistoryItem(role="summary", content=str(record.get("content") or ""), **base))
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
        with self._operation_scope("new"):
            self._runtime.close(distill=True)
            self._conversation.switch(None)
            session = self._runtime.ensure_session()
        return CreateSessionResponse(session_id=session.id, title=session.title())

    def switch_session(self, session_id: str) -> SwitchSessionResponse:
        self._validate_session_id(session_id)
        with self._operation_scope("switch"):
            if not self._conversation.session_exists(session_id):
                raise WebError("session_not_found", "Session not found.", 404)
            self._runtime.close(distill=True)
            try:
                result = self._conversation.switch(session_id)
            except (KeyError, ValueError) as exc:
                record_event("web.switch_failed", exc)
                raise WebError("session_unreadable", "Session could not be restored.", 500) from exc
        session = result.session
        return SwitchSessionResponse(session_id=session.id, title=session.title())

    def delete_session(self, session_id: str, confirmed: bool) -> DeleteSessionResponse:
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
            allowed, reason = self._conversation.delete_guard(session_id)
            if not allowed:
                raise WebError(
                    "session_protected",
                    reason or "Session cannot be deleted.",
                    409,
                )
            try:
                if not self._conversation.delete_session(session_id):
                    raise WebError("session_not_found", "Session not found.", 404)
            except OSError as exc:
                record_event("session.delete_failed", exc)
                raise WebError(
                    "delete_failed",
                    "Session could not be deleted; the file was kept.",
                    500,
                ) from exc
        return DeleteSessionResponse(session_id=session_id)

    # ---------- settings / providers (TUI Ctrl+P equivalent) ----------

    def providers(self) -> ProviderListResponse:
        settings = self._app_service.settings
        return ProviderListResponse(items=[
            ProviderInfo(
                key=option.key,
                label=option.label,
                configured=option.configured,
                current=option.current,
                endpoint=settings.provider_endpoint(option.key),
            )
            for option in settings.provider_options()
        ])

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
        self, message: str, session_id: str | None, loop: asyncio.AbstractEventLoop
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
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        with self._lock:
            self._require_idle()
            self._check_session_match(session_id)
            token = self._conversation.begin_turn(session_id)
            turn_id = self._conversation.active_turn_id
            if turn_id is None:
                raise WebError("turn_unavailable", "Could not start a request.", 503)
            turn = ActiveTurn(turn_id, session_id, queue, loop)
            self._active = turn

        pump = EventPump(turn, self._post)
        pump.start()
        try:
            worker = threading.Thread(
                target=self._worker,
                args=(turn, text, token, pump),
                daemon=True,
                name=f"syvrio-web-turn-{turn_id}",
            )
            worker.start()
        except Exception as exc:
            self.release(turn_id)
            pump.close()
            record_event("web.worker_failed", exc)
            raise WebError("turn_unavailable", "Could not start a request.", 503) from exc
        return turn

    def _worker(
        self, turn: ActiveTurn, text: str, token, pump: EventPump
    ) -> None:
        callbacks = TurnCallbacks(
            on_event=pump.on_event,
            on_delta=pump.on_delta,
            on_progress=pump.on_progress,
            on_thinking=pump.on_thinking,
            on_session=lambda session: pump.on_session(session.id),
            on_user_message=pump.on_user_message,
        )
        try:
            result = self._conversation.run_turn(text, callbacks, token, turn.session_id)
        except Exception as exc:  # run_turn normally funnels errors into a TurnResult
            record_event("web.turn_failed", exc)
            pump.finish("failed", message="Request failed unexpectedly.")
        else:
            if result.status == "ok":
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
            self.release(turn.turn_id)
            turn.finished.set()
            pump.close()

    async def stream(self, turn: ActiveTurn):
        """Yield SSE frames until the terminal event; clean up on disconnect."""
        try:
            while True:
                try:
                    event = await asyncio.wait_for(turn.queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                yield sse_frame(event)
        finally:
            if not turn.finished.is_set():
                # Browser went away mid-generation: cancel this turn, but let the
                # worker release request state in its own finally. Releasing here
                # while runtime.ask still runs would let a new turn overlap it.
                self.cancel(turn.turn_id)

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
        try:
            turn.loop.call_soon_threadsafe(turn.queue.put_nowait, event)
        except RuntimeError:
            pass  # Loop closed during shutdown; the worker still finishes cleanly.

    def release(self, turn_id: int) -> None:
        with self._lock:
            if self._active is not None and self._active.turn_id == turn_id:
                self._active = None
            self._conversation.end_turn(turn_id)

    def _require_idle(self) -> None:
        if self._active is not None or self._operation is not None:
            raise WebError(
                "busy",
                "A reply or command is still running. Stop it or wait before starting another request.",
                409,
            )

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise WebError("invalid_session_id", "Invalid session ID.", 422)
