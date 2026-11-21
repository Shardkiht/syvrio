"""Application-owned turn identity, ordered events and receipts shared across clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from ..agent.runtime import ThinkingUpdate
from ..errors import ConversationError
from ..infra.attachments import AttachmentBatch
from ..infra.diagnostics import record_event
from ..infra.files import atomic_write
from .contracts import TurnCallbacks, TurnResult, UndoOutcome

_TURN_IDS = count(1)
TOOL_PREFIX = "Calling tool: "
MODEL_PHASES = frozenset({"Waiting for model", "Generating final answer"})


def next_turn_id() -> int:
    return next(_TURN_IDS)


EventType = Literal[
    "start",
    "message_saved",
    "message_inserted",
    "reasoning_delta",
    "reasoning_end",
    "content_delta",
    "tool_start",
    "tool_end",
    "event",
    "progress",
    "compaction",
    "done",
    "cancelled",
    "failed",
]


class TurnEvent(BaseModel):
    """A provider-independent conversation event. Sequence numbers are local to each turn."""

    seq: int
    type: EventType
    turn_id: int | None = None
    session_id: str | None = None
    message_id: str | None = None
    # Short-lived deltas (content/reasoning), tool display and error messages.
    text: str = ""
    name: str | None = None
    seconds: float | None = None
    # A model-request stage; reasoning from different tool rounds never shares it.
    phase: int | None = None
    steps: int | None = None
    estimated_tokens: int | None = None
    message: str | None = None
    # Tool outcome on ``tool_end``: False means the call failed, None unknown.
    ok: bool | None = None


@dataclass(frozen=True)
class MessageEdit:
    owner: str
    message_id: str
    text: str
    files: tuple[tuple[str, str], ...]


@dataclass
class ConversationTurn:
    session_id: str | None
    line: str
    attachments: AttachmentBatch | None = None
    turn_id: int = field(default_factory=next_turn_id, kw_only=True)
    finished: threading.Event = field(default_factory=threading.Event)
    saved_or_closed: threading.Event = field(default_factory=threading.Event)
    undone: UndoOutcome | None = None
    undo_claimed: bool = False
    _events: list[TurnEvent] = field(default_factory=list)
    message_id: str | None = None
    edit: MessageEdit | None = None
    edit_conflict: str | None = None
    _closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _listeners: dict[asyncio.Event, asyncio.AbstractEventLoop] = field(default_factory=dict)

    def publish(self, event: TurnEvent | None) -> None:
        with self._lock:
            if event is None:
                self._closed = True
                self.saved_or_closed.set()
            else:
                self._events.append(event)
                if event.type == "message_saved" and self.message_id is None:
                    self.message_id = event.message_id
                    self.saved_or_closed.set()
            for signal, loop in self._listeners.items():
                try:
                    loop.call_soon_threadsafe(signal.set)
                except RuntimeError:
                    continue

    async def events(
        self, after: int = 0, heartbeat: float = 15
    ) -> AsyncIterator[TurnEvent | None]:
        """Replay from a cursor, then yield live events and idle heartbeats."""
        loop = asyncio.get_running_loop()
        signal = asyncio.Event()
        with self._lock:
            self._listeners[signal] = loop
        try:
            while True:
                with self._lock:
                    signal.clear()
                    events = self._events[after:]
                    closed = self._closed
                for event in events:
                    yield event
                    after = event.seq
                if closed:
                    return
                try:
                    await asyncio.wait_for(signal.wait(), timeout=heartbeat)
                except TimeoutError:
                    yield None
        finally:
            with self._lock:
                self._listeners.pop(signal, None)

    def receipt(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "text": self.line,
            "message_id": self.message_id,
        }


class TurnRegistry:
    """Keep live receipts plus recent completions for reconnecting consumers."""

    def __init__(self) -> None:
        self._instance = uuid4().hex
        self._delivery_lock = threading.Lock()
        self._lock = threading.Lock()
        self._turns: OrderedDict[int, ConversationTurn] = OrderedDict()

    def remember(self, turn: ConversationTurn) -> None:
        with self._lock:
            self._turns[turn.turn_id] = turn
            completed = [key for key, item in self._turns.items() if item.finished.is_set()]
            for key in completed[:-8]:
                del self._turns[key]

    def get(self, turn_id: int, session_id: str | None) -> ConversationTurn | None:
        with self._lock:
            turn = self._turns.get(turn_id)
            return turn if turn and turn.session_id == session_id else None

    def replacement(self, session_id: str, edit: MessageEdit) -> ConversationTurn | None:
        with self._lock:
            for turn in reversed(self._turns.values()):
                previous = turn.edit
                if (
                    turn.session_id == session_id
                    and turn.message_id
                    and previous
                    and previous.owner == edit.owner
                    and previous.message_id == edit.message_id
                ):
                    return turn
            return None

    def submit_once(
        self,
        state_dir: Path,
        submission_id: str,
        message: str,
        session_id: str | None,
        attachments: AttachmentBatch | None,
        submit: Callable[[], ConversationTurn],
    ) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", submission_id):
            raise ConversationError("invalid_request", "Invalid submission ID.")
        files = [(item.name, item.sha256) for item in attachments.files] if attachments else []
        payload = json.dumps([session_id, message.strip(), files], ensure_ascii=False).encode()
        signature = hashlib.sha256(payload).hexdigest()
        path = state_dir / "chat-receipts" / submission_id[:2] / submission_id[2:]
        with self._delivery_lock:
            try:
                stored = json.loads(path.read_bytes())
            except FileNotFoundError:
                stored = None
            if stored is not None:
                if stored["signature"] != signature:
                    raise ConversationError(
                        "submission_conflict", "This delivery ID belongs to another message."
                    )
                if stored.get("instance") != self._instance or "turn_id" not in stored:
                    raise ConversationError(
                        "delivery_unknown",
                        "Delivery is uncertain after a backend interruption. Check the conversation before sending again.",
                    )
                if attachments:
                    attachments.discard()
                sid = stored["session_id"]
                turn = self.get(stored["turn_id"], sid)
                if turn:
                    return turn.receipt()
                return {
                    "session_id": sid,
                    "turn_id": stored["turn_id"],
                    "text": message,
                    "history_only": True,
                }

            # Reserve before dispatch so a lost acknowledgement cannot start the same turn twice.
            record = {"signature": signature, "instance": self._instance}
            atomic_write(path, json.dumps(record).encode())
            try:
                turn = submit()
            except ConversationError:
                path.unlink()
                raise
            record.update(session_id=turn.session_id, turn_id=turn.turn_id)
            try:
                atomic_write(path, json.dumps(record).encode())
            except OSError as exc:
                record_event("web.submission_receipt_failed", exc)
            return turn.receipt()


class TurnEvents:
    """Turn TurnCallbacks into ordered conversation events (runs in the worker thread).

    ``phase`` advances at each model request so reasoning from different tool
    rounds never merges. Tool starts are paired with their result events in
    call order, which also covers parallel tool batches.
    """

    def __init__(self, turn: ConversationTurn) -> None:
        self._turn = turn
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
        self._turn.publish(TurnEvent(**payload))

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

    def callbacks(self) -> TurnCallbacks:
        return TurnCallbacks(
            on_event=self.on_event,
            on_delta=self.on_delta,
            on_progress=self.on_progress,
            on_thinking=self.on_thinking,
            on_tool=self.on_tool,
            on_compaction=lambda phase: self.send("compaction", text=phase),
            on_session=lambda session: self.on_session(session.id),
            on_user_message=self.on_user_message,
            on_inserted=lambda mid, content: self.send(
                "message_inserted", message_id=mid, text=content
            ),
        )

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

    def finish_result(self, result: TurnResult) -> None:
        if self._turn.undone is not None:
            self.finish("cancelled", session_id=result.session_id or self.session_id)
        elif result.status == "ok":
            reply = result.reply
            self.finish(
                "done",
                text=result.content,
                session_id=result.session_id or self.session_id,
                steps=reply.steps if reply else 0,
                estimated_tokens=reply.estimated_tokens if reply else 0,
            )
        elif result.status in ("cancelled", "undo"):
            self.finish("cancelled", session_id=result.session_id or self.session_id)
        else:
            self.finish(
                "failed",
                session_id=result.session_id or self.session_id,
                message=result.error or "Request failed",
            )

    def close(self) -> None:
        self._turn.publish(None)
