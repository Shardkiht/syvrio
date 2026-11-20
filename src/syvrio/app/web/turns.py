"""Replayable turn streams shared by every client attached to a session."""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from ...application.contracts import UndoOutcome
from ...infra.attachments import AttachmentBatch
from .models import StreamEvent, sse_frame


@dataclass
class ActiveTurn:
    turn_id: int
    session_id: str
    text: str
    attachments: AttachmentBatch | None = None
    finished: threading.Event = field(default_factory=threading.Event)
    undone: UndoOutcome | None = None
    undo_claimed: bool = False
    _events: list[StreamEvent] = field(default_factory=list)
    message_id: str | None = None
    _closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _listeners: dict[asyncio.Event, asyncio.AbstractEventLoop] = field(default_factory=dict)

    def publish(self, event: StreamEvent | None) -> None:
        with self._lock:
            if event is None:
                self._closed = True
            else:
                self._events.append(event)
                if event.type == "message_saved" and self.message_id is None:
                    self.message_id = event.message_id
            for signal, loop in self._listeners.items():
                try:
                    loop.call_soon_threadsafe(signal.set)
                except RuntimeError:
                    continue

    async def stream(self, after: int = 0, heartbeat: float = 15):
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
                    yield sse_frame(event)
                    after = event.seq
                if closed:
                    return
                try:
                    await asyncio.wait_for(signal.wait(), timeout=heartbeat)
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            with self._lock:
                self._listeners.pop(signal, None)

    def receipt(self) -> dict:
        return {"turn_id": self.turn_id, "session_id": self.session_id, "text": self.text, "message_id": self.message_id}


class TurnRegistry:
    """Keep live receipts plus recent completions for reconnecting consumers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: OrderedDict[int, ActiveTurn] = OrderedDict()

    def remember(self, turn: ActiveTurn) -> None:
        with self._lock:
            self._turns[turn.turn_id] = turn
            completed = [key for key, item in self._turns.items() if item.finished.is_set()]
            for key in completed[:-8]:
                del self._turns[key]

    def get(self, turn_id: int, session_id: str | None) -> ActiveTurn | None:
        with self._lock:
            turn = self._turns.get(turn_id)
            return turn if turn and turn.session_id == session_id else None
