"""常驻应用为客户端视图提供的 HTTP 响应。"""

from __future__ import annotations

import hashlib
from threading import Lock
from weakref import WeakKeyDictionary

from ...errors import SessionProtectedError
from ...session import HistoryRevision
from .channel import APIChannel, APIError
from .models import (
    CommandResponse,
    CreateSessionResponse,
    HistoryResponse,
    SwitchSessionHistoryResponse,
    SwitchSessionResponse,
)


class ClientViews:
    def __init__(self, owner: APIChannel) -> None:
        self.owner = owner
        self._history_lock = Lock()
        self._histories: WeakKeyDictionary[HistoryRevision, tuple[str, HistoryResponse]] = (
            WeakKeyDictionary()
        )

    def create_session(self, key: str) -> CreateSessionResponse:
        return self.owner.directory.new(key, APIChannel.create_session)

    def new_command(self, key: str, session_id: str | None) -> CommandResponse:
        return self.owner.directory.new(
            key,
            lambda channel: channel.execute_command("/new", channel.conversation.current_id, False),
            session_id,
        )

    def switch(
        self,
        key: str,
        session_id: str,
        include_history: bool = False,
        known_version: str | None = None,
    ) -> SwitchSessionResponse:
        if not include_history:
            self.owner._validate_session_id(session_id)
            session = self.owner.directory.select(key, session_id)
            return SwitchSessionResponse(session_id=session.id, title=session.title())
        version, history = self.versioned_history(key, session_id, select=True)
        return SwitchSessionHistoryResponse(
            session_id=history.session_id,
            title=history.title,
            history_version=version,
            history=None if known_version == version else history,
        )

    def versioned_history(
        self, key: str, session_id: str, *, select: bool = False
    ) -> tuple[str, HistoryResponse]:
        self.owner._validate_session_id(session_id)
        try:
            if select:
                snapshot = self.owner.directory.select_snapshot(key, session_id)
                current = True
            else:
                channel = self.owner.directory.channel(key)
                snapshot = channel.conversation.sessions.read_snapshot(session_id)
                current = channel.conversation.current_id == session_id
        except KeyError as exc:
            raise APIError("session_not_found", "Session not found.", 404) from exc
        except (ValueError, OSError) as exc:
            raise APIError("session_unreadable", "Session history could not be read.", 500) from exc
        with self._history_lock:
            projection = self._histories.get(snapshot.history_revision)
            if projection is None:
                history = self.owner.project_history(snapshot.session, current=True)
                # 关闭聊天和提炼记忆会追加内部记录，界面显示的历史保持相同。
                content = history.model_dump_json(exclude={"current"}).encode()
                version = hashlib.sha256(content).hexdigest()
                projection = (version, history)
                self._histories[snapshot.history_revision] = projection
        version, history = projection
        return version, history if current else history.model_copy(update={"current": False})

    def archive(self, session_id: str, archived: bool) -> dict:
        self.owner._validate_session_id(session_id)
        try:
            self.owner.directory.set_archived(session_id, archived)
        except KeyError as exc:
            raise APIError("session_not_found", "Session not found.", 404) from exc
        except SessionProtectedError as exc:
            raise APIError("session_protected", str(exc), 409) from exc
        except (OSError, ValueError) as exc:
            raise APIError(
                "archive_failed", "Could not move the conversation. The original was kept.", 409
            ) from exc
        return {"session_id": session_id, "archived": archived}
