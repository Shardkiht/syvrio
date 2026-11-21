"""Bind client views to the single running conversation for each open session."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Protocol

from ..errors import ConversationError
from ..infra.attachments import AttachmentBatch
from ..session import Session, SessionSnapshot
from .conversation import ConversationUseCases
from .turns import ConversationTurn


class ConversationOwner(Protocol):
    @property
    def conversation(self) -> ConversationUseCases: ...


class ConversationDirectory[C: ConversationOwner]:
    def __init__(self, owner: C, create: Callable[[], C]) -> None:
        self.owner = owner
        self._create = create
        self._lock = RLock()
        self._views: dict[str, C] = {}
        self._archived: dict[str, str] = {}
        self._channels = (owner,)
        self._first_view = True
        self._closed = False
        owner.conversation.scheduling.on_idle = lambda: self._retire(owner)

    def close(self) -> tuple[C, ...]:
        with self._lock:
            self._closed = True
            return self._channels

    def _require_open(self) -> None:
        if self._closed:
            raise ConversationError("shutting_down", "The service is shutting down.")

    def session_busy(self, session_id: str) -> bool:
        # Background callers hold the Vault lock. Use the immutable channel
        # snapshot so they never acquire the navigation lock in reverse order.
        return any(
            channel.conversation.scheduling.session_busy(session_id) for channel in self._channels
        )

    def live_session(self, session_id: str) -> Session | None:
        channel = self._bound(session_id)
        return channel.conversation.runtime.session if channel is not None else None

    def _bound(self, session_id: str) -> C | None:
        return next(
            (item for item in self._channels if item.conversation.current_id == session_id), None
        )

    def _fresh(self) -> C:
        for channel in self._channels:
            self._retire(channel)
        for channel in self._channels:
            if (
                channel not in self._views.values()
                and channel.conversation.current_id is None
                and not channel.conversation.scheduling.activity()[0]
            ):
                return channel
        channel = self._create()
        channel.conversation.scheduling.on_idle = lambda: self._retire(channel)
        self._channels = (*self._channels, channel)
        return channel

    def channel(self, view: str) -> C:
        with self._lock:
            self._require_open()
            if view not in self._views:
                self._views[view] = self.owner if self._first_view else self._fresh()
                self._first_view = False
            return self._views[view]

    def submit(
        self,
        view: str,
        message: str,
        session_id: str | None,
        attachments: AttachmentBatch | None = None,
    ) -> tuple[C, ConversationTurn]:
        with self._lock:
            channel = self.channel(view)
            turn = channel.conversation.scheduling.submit(message, session_id, attachments)
            self._archived.pop(view, None)
            return channel, turn

    def submit_once(
        self,
        view: str,
        submission_id: str,
        message: str,
        session_id: str | None,
        attachments: AttachmentBatch | None = None,
    ) -> dict:
        with self._lock:
            channel = self.channel(view)
            receipt = channel.conversation.scheduling.submit_once(
                submission_id, message, session_id, attachments
            )
            sid = receipt["session_id"]
            if channel.conversation.current_id is None:
                self.select(view, sid)
            elif channel.conversation.current_id != sid:
                raise ConversationError(
                    "session_mismatch", "Reopen the original conversation to recover this message."
                )
            self._archived.pop(view, None)
            return receipt

    def _navigation_channel(self, view: str, previous: C) -> C:
        # A turn or paused queue owns its conversation even after its last reader leaves.
        if previous.conversation.scheduling.activity()[0] or any(
            key != view and owner is previous for key, owner in self._views.items()
        ):
            return self._fresh()
        return previous

    def new[T](self, view: str, action: Callable[[C], T], session_id: str | None = None) -> T:
        with self._lock:
            previous = self.channel(view)
            previous.conversation.scheduling.check_session(session_id)
            channel = self._navigation_channel(view, previous)
            result = action(channel)
            self._views[view] = channel
            self._archived.pop(view, None)
            self._retire(previous)
            return result

    def select(self, view: str, session_id: str) -> Session:
        with self._lock:
            previous = self.channel(view)
            channel = self._bound(session_id)
            if channel is None:
                channel = self._navigation_channel(view, previous)
                channel.conversation.navigate(session_id)
            self._views[view] = channel
            self._archived.pop(view, None)
            self._retire(previous)
            return channel.conversation.current

    def select_snapshot(self, view: str, session_id: str) -> SessionSnapshot:
        """Bind this view and return the saved revision, including writes from other clients."""
        with self._lock:
            self.select(view, session_id)
            return self.owner.conversation.sessions.read_snapshot(session_id)

    def rename(self, view: str, session_id: str, title: str) -> None:
        with self._lock:
            self._require_open()
            conversation = (self._bound(session_id) or self.channel(view)).conversation
            with conversation.scheduling.operation("rename", require_idle=False):
                if not conversation.session_exists(session_id):
                    raise ConversationError("session_not_found", "Session not found.")
                conversation.rename_session(session_id, title)

    def set_archived(self, session_id: str, archived: bool) -> None:
        with self._lock:
            self._require_open()
            bound = self._bound(session_id)
            (bound or self.owner).conversation.set_archived(session_id, archived)
            if bound is not None and archived:
                for key in [key for key, channel in self._views.items() if channel is bound]:
                    self._archived[key] = session_id
                    del self._views[key]
                self._retire(bound)

    def queue_state(self, view: str) -> dict:
        with self._lock:
            queue = self.channel(view).conversation.scheduling.queue_state()
            queue["replying_session_ids"] = [
                sid
                for channel in self._channels
                if (sid := channel.conversation.ask_in_flight) is not None
            ]
            if view in self._archived and queue["session_id"] is None:
                queue["archived_session_id"] = self._archived[view]
            return queue

    def disconnect(self, view: str) -> None:
        with self._lock:
            self._archived.pop(view, None)
            channel = self._views.pop(view, None)
            if channel is not None:
                self._retire(channel)

    def _retire(self, channel: C) -> None:
        with self._lock:
            if (
                self._closed
                or channel in self._views.values()
                or channel.conversation.scheduling.activity()[0]
            ):
                return
            if channel.conversation.current_id:
                try:
                    channel.conversation.navigate(None)
                except ConversationError as exc:
                    if exc.code in {"busy", "shutting_down"}:
                        return
                    raise
            if channel is not self.owner:
                self._channels = tuple(item for item in self._channels if item is not channel)
                channel.conversation.scheduling.on_idle = None
