"""Bind each client view to a shared conversation for its selected session."""

from __future__ import annotations

import asyncio
from threading import RLock
from uuid import UUID

from ...infra.attachments import AttachmentBatch
from .backend import ActiveTurn, WebBackend, WebError
from .models import (
    CommandResponse,
    CreateSessionResponse,
    DeleteSessionResponse,
    SwitchSessionResponse,
)


class ClientViews:
    def __init__(self, owner: WebBackend) -> None:
        self.owner = owner
        self._lock = RLock()
        self._views: dict[str, WebBackend] = {}
        self._channels = (owner,)
        self._first_view = True
        # This callback runs under the Vault write lock. Read the tuple snapshot
        # without taking the navigation lock, which may itself be loading a session.
        owner.session_busy = lambda sid: any(
            item._conversation.ask_in_flight == sid or item._conversation.current_id == sid
            and (item._active is not None or item._operation is not None or bool(item._waiting))
            for item in self._channels
        )
        owner.on_idle = self._channel_idle
        owner._runtime.external_session_in_use = self._session_in_use

    def _session_in_use(self, session_id: str) -> bool:
        return any(
            channel._conversation.current_id == session_id
            or channel._conversation.ask_in_flight == session_id
            for channel in self._channels
        )

    def _channel_idle(self, channel: WebBackend) -> None:
        with self._lock:
            self._retire(channel)

    def _fresh(self) -> WebBackend:
        for channel in self._channels:
            self._retire(channel)
        for channel in self._channels:
            if channel not in self._views.values() and channel._conversation.current_id is None:
                return channel
        channel = WebBackend(parent=self.owner)
        channel.on_idle = self._channel_idle
        channel._runtime.external_session_in_use = self._session_in_use
        self._channels = (*self._channels, channel)
        return channel

    def channel(self, key: str) -> WebBackend:
        if key:
            try:
                UUID(key)
            except ValueError as exc:
                raise WebError("invalid_view", "Invalid client view.", 422) from exc
        with self._lock:
            if key not in self._views:
                self._views[key] = self.owner if self._first_view else self._fresh()
                self._first_view = False
            return self._views[key]

    def start_turn(
        self, key: str, message: str, session_id: str | None, loop: asyncio.AbstractEventLoop,
        attachments: AttachmentBatch | None = None,
    ) -> tuple[WebBackend, ActiveTurn]:
        # Navigation and first-message ID assignment must publish one session owner.
        with self._lock:
            channel = self.channel(key)
            turn = channel.start_turn(message, session_id, loop, attachments)
            return channel, turn

    def _shared(self, key: str, channel: WebBackend) -> bool:
        return any(other != key and value is channel for other, value in self._views.items())

    def new(self, key: str) -> CreateSessionResponse:
        with self._lock:
            current = self.channel(key)
            if self._shared(key, current):
                self._views[key] = self._fresh()
                return CreateSessionResponse()
            return current.create_session()

    def new_command(self, key: str, session_id: str | None) -> CommandResponse:
        with self._lock:
            current = self.channel(key)
            current._check_session_match(session_id)
            if not self._shared(key, current):
                return current.execute_command("/new", session_id, False)
            self.new(key)
            return CommandResponse(
                status="ok", command="new", intent="new_session",
                message="Started a new session. It is saved with your first message.",
                refresh_sessions=True, refresh_history=True,
            )

    def switch(self, key: str, session_id: str) -> SwitchSessionResponse:
        with self._lock:
            current = self.channel(key)
            for channel in self._channels:
                if channel._conversation.current_id == session_id:
                    session = channel._runtime.current
                    self._views[key] = channel
                    self._retire(current)
                    return SwitchSessionResponse(session_id=session.id, title=session.title())
            channel = self._fresh() if self._shared(key, current) else current
            result = channel.switch_session(session_id)
            self._views[key] = channel
            return result

    def rename(self, key: str, session_id: str, title: str) -> None:
        self.owner._validate_session_id(session_id)
        with self._lock:
            current = self.channel(key)
            selected = next((item for item in self._channels if item._conversation.current_id == session_id), current)
            with selected._operation_scope("rename"):
                if not selected._conversation.session_exists(session_id):
                    raise WebError("session_not_found", "Session not found.", 404)
                selected._conversation.rename_session(session_id, title)

    def delete(self, key: str, session_id: str, confirmed: bool, pending_revision: str = "") -> DeleteSessionResponse:
        with self._lock:
            if self._session_in_use(session_id):
                raise WebError("session_protected", "A client is using this session.", 409)
            return self.channel(key).delete_session(session_id, confirmed, pending_revision)

    def disconnect(self, key: str) -> None:
        with self._lock:
            channel = self._views.pop(key, None)
            if channel is not None:
                self._retire(channel)

    def _retire(self, channel: WebBackend) -> None:
        if channel in self._views.values() or channel.health().busy:
            return
        if channel._conversation.current_id:
            channel.create_session()
        if channel is not self.owner:
            self._channels = tuple(item for item in self._channels if item is not channel)

    def shutdown(self) -> None:
        for channel in self._channels:
            if channel is not self.owner:
                channel.shutdown()
        self.owner.shutdown()
