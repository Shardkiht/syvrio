"""常驻应用为客户端视图提供的 HTTP 响应。"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from ...infra.session.errors import SessionProtectedError
from .channel import HISTORY_ITEM_LIMIT, TOOL_CONTENT_LIMIT, APIChannel, APIError
from .models import (
    CommandResponse,
    CreateSessionResponse,
    HistoryResponse,
    SwitchSessionHistoryResponse,
    SwitchSessionResponse,
)


@dataclass(frozen=True)
class CachedHistory:
    revision: int
    version: str
    history: HistoryResponse


class ClientViews:
    def __init__(self, owner: APIChannel) -> None:
        self.owner = owner
        self._history_lock = Lock()
        self._histories: OrderedDict[str, CachedHistory] = OrderedDict()

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
            channel = self.owner.directory.select(key, session_id)
            return SwitchSessionResponse(
                session_id=session_id, title=channel.conversation.sessions.session_title(session_id)
            )
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
                self.owner.directory.select(key, session_id)
            channel = self.owner.directory.channel(key)
            store = channel.conversation.sessions
            current = select or channel.conversation.current_id == session_id
            with self._history_lock:
                cached = self._histories.get(session_id)
            revision, page = store.latest_history_page(
                session_id,
                limit=HISTORY_ITEM_LIMIT,
                tool_preview=TOOL_CONTENT_LIMIT,
                known_revision=cached.revision if cached else None,
            )
            if page is None:
                assert cached is not None
                version, history = cached.version, cached.history
            else:
                history = self.owner.project_history(page, current=True)
                content = history.model_dump_json(exclude={"current"}).encode()
                version = hashlib.sha256(content).hexdigest()
                if revision is not None:
                    with self._history_lock:
                        latest = self._histories.get(session_id)
                        if latest is None or latest.revision < revision:
                            self._histories[session_id] = CachedHistory(revision, version, history)
                            self._histories.move_to_end(session_id)
                            if len(self._histories) > 16:
                                self._histories.popitem(last=False)
        except KeyError as exc:
            raise APIError("session_not_found", "Session not found.", 404) from exc
        except (ValueError, OSError) as exc:
            raise APIError("session_unreadable", "Session history could not be read.", 500) from exc
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
