"""常驻后端管理、多个客户端共享的会话通道。"""

from __future__ import annotations

from dataclasses import replace

from ... import __version__
from ...application.turns import ConversationTurn
from ...errors import ConversationError, SessionDeletionChanged, SessionProtectedError, SyvrioError
from ...infra.attachments import AttachmentBatch
from ...infra.diagnostics import record_event
from ...session import SESSION_ID_RE, Session
from ..channel import ResidentChannel
from ..ownership import BackendLease
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
    SwitchSessionResponse,
    ToolCallInfo,
)
from .turns import stream_frames

HISTORY_ITEM_LIMIT = 300
TOOL_CONTENT_LIMIT = 300


class APIError(SyvrioError):
    """为 API 客户端提供稳定错误码的预期异常。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    @classmethod
    def from_conversation(cls, exc: ConversationError) -> APIError:
        if exc.code in {
            "invalid_request",
            "invalid_title",
            "invalid_session_id",
            "empty_message",
            "command_required",
        }:
            status = 422
        elif exc.code == "session_unreadable":
            status = 500
        elif exc.code == "session_not_found":
            status = 404
        elif exc.code in {"turn_unavailable", "edit_not_sent", "shutting_down", "client_busy"}:
            status = 503
        else:
            status = 409
        return cls(exc.code, str(exc), status)


class APIChannel(ResidentChannel):
    """将常驻对话及其共享服务接入统一 HTTP 协议。"""

    def startup(self, ownership: BackendLease | None = None) -> None:
        try:
            super().startup(ownership)
        except ConversationError as exc:
            raise APIError.from_conversation(exc) from exc

    # 查询

    def health(self, static_ready: bool = False) -> HealthResponse:
        busy, turn_id = self.scheduling.activity()
        llm = self._service.config.llm
        return HealthResponse(
            version=__version__,
            provider=llm.provider,
            model=llm.model,
            busy=busy,
            active_turn_id=turn_id,
            static_ready=static_ready,
        )

    def context_usage(self, session_id: str | None) -> ContextUsageResponse:
        used = 0
        if session_id is not None:
            self._validate_session_id(session_id)
            current = self._runtime.session
            if current is not None and current.id == session_id:
                # 从快照估算，因为活动回合可能同时追加记录。
                session = replace(current, records=list(current.records))
            else:
                try:
                    session = self._sessions.load(session_id, include_reasoning=False)
                except KeyError as exc:
                    raise APIError("not_found", "Session not found.", 404) from exc
            used = self._conversation.estimate_usage(session)
        limits = self._conversation.context_limits(session_id)
        return ContextUsageResponse(
            session_id=session_id,
            used=used,
            limit=limits.window,
            compact_threshold=limits.compact_threshold,
            compaction_trigger_tokens=limits.trigger_tokens,
        )

    # 斜杠命令

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
        self,
        line: str,
        session_id: str | None,
        confirmed: bool,
        expected_message_id: str | None = None,
    ) -> CommandResponse:
        if session_id is not None:
            self._validate_session_id(session_id)
        result = self.scheduling.execute_command(
            self._commands, line, session_id, confirmed, expected_message_id
        )
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

    def edit_draft(self, session_id: str, message_id: str) -> MessageDraft:
        self._validate_session_id(session_id)
        self.scheduling.check_session(session_id, exact=True)
        with self._service.write_lock:
            session = self._sessions.load(session_id, include_reasoning=False)
            inputs = session.latest_turn_inputs()
        if not inputs or inputs[-1]["id"] != message_id:
            raise APIError("stale_message", "Only the latest user message can be edited.", 409)
        return MessageDraft(
            text="\n\n".join(item["content"] for item in inputs),
            attachments=[
                DraftAttachment(message_id=item["id"], **file)
                for item in inputs
                for file in item.get("attachments", [])
            ],
        )

    def list_sessions(
        self, limit: int = 100, *, archived: bool = False, offset: int = 0
    ) -> SessionListResponse:
        # 先读取任务，即使列举期间刚发布标题，也能在结束时刷新。
        pending = set() if archived else self._background.pending_titles()
        with self._service.write_lock:
            rows = self._sessions.list(
                max(1, min(limit, 500)), with_tokens=False, archived=archived, offset=offset
            )
        current = self._conversation.current_id
        return SessionListResponse(
            items=[
                SessionInfo(
                    id=str(row["id"]),
                    title=str(row["title"]),
                    title_pending=row["id"] in pending,
                    messages=int(row["messages"]),
                    started_at=str(row["started_at"]),
                    updated_at=str(row["updated_at"]),
                    last_message_at=str(row.get("last_message_at") or ""),
                    current=str(row["id"]) == current,
                )
                for row in rows
            ]
        )

    def history(
        self, session_id: str, limit: int = HISTORY_ITEM_LIMIT, *, archived: bool = False
    ) -> HistoryResponse:
        self._validate_session_id(session_id)
        try:
            with self._service.write_lock:
                if archived:
                    session = self._sessions.load(session_id, archived=True)
                else:
                    session = self._sessions.read_snapshot(session_id).session
        except KeyError as exc:
            raise APIError("session_not_found", "Session not found.", 404) from exc
        except (ValueError, OSError) as exc:
            record_event("web.history_unreadable", exc)
            raise APIError("session_unreadable", "Session history could not be read.", 500) from exc
        return self.project_history(
            session, current=self._conversation.current_id == session.id, limit=limit
        )

    @staticmethod
    def project_history(
        session: Session, *, current: bool, limit: int = HISTORY_ITEM_LIMIT
    ) -> HistoryResponse:
        records = session.active_records(include_failures=True)
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
                    HistoryItem(
                        role="user",
                        content=str(record.get("content") or ""),
                        attachments=record.get("attachments", []),
                        **base,
                    )
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
            elif role == "turn_failed":
                items.append(
                    HistoryItem(
                        role="error",
                        content=str(record.get("content") or "Request failed."),
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
            current=current,
        )

    # 会话生命周期

    def create_session(self) -> CreateSessionResponse:
        self.conversation.navigate(None)
        return CreateSessionResponse()

    def switch_session(self, session_id: str) -> SwitchSessionResponse:
        self._validate_session_id(session_id)
        session = self.conversation.navigate(session_id).session
        return SwitchSessionResponse(session_id=session.id, title=session.title())

    def deletion_preview(self, session_id: str) -> SessionDeletionPreview:
        self._validate_session_id(session_id)
        try:
            items, revision = self._conversation.deletion_preview(session_id, archived=True)
        except KeyError as exc:
            raise APIError("session_not_found", "Session not found.", 404) from exc
        except SessionProtectedError as exc:
            raise APIError("session_protected", str(exc), 409) from exc
        except (ValueError, OSError) as exc:
            record_event("session.delete_failed", exc)
            raise APIError(
                "delete_failed", "The related pending items could not be read.", 500
            ) from exc
        return SessionDeletionPreview(
            pending=[PendingDeletionItem(id=item.id, text=item.text) for item in items],
            revision=revision,
        )

    def delete_session(
        self, session_id: str, confirmed: bool, pending_revision: str = ""
    ) -> DeleteSessionResponse:
        """在用户明确确认后永久删除已归档的对话记录。"""
        self._validate_session_id(session_id)
        with self._service.write_lock:
            if not self._sessions.exists(session_id, archived=True):
                if self._sessions.exists(session_id):
                    raise APIError(
                        "archive_required",
                        "Archive this conversation before permanently deleting it.",
                        409,
                    )
                raise APIError("session_not_found", "Archived session not found.", 404)
            if not confirmed:
                raise APIError(
                    "confirm_required",
                    "Deleting a session is permanent; confirm the action first.",
                    409,
                )
            try:
                if not self._conversation.delete_session(session_id, pending_revision):
                    raise APIError("session_not_found", "Session not found.", 404)
            except SessionProtectedError as exc:
                raise APIError("session_protected", str(exc), 409) from exc
            except SessionDeletionChanged as exc:
                raise APIError("pending_changed", str(exc), 409) from exc
            except (OSError, ValueError) as exc:
                record_event("session.delete_failed", exc)
                raise APIError(
                    "delete_failed",
                    "Session could not be deleted; the file was kept.",
                    500,
                ) from exc
        return DeleteSessionResponse(session_id=session_id)

    # 设置与供应商，对应 TUI 的 Ctrl+P

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
        """供应商凭据只保存在服务器端，接口仅返回是否已配置。"""
        settings = self._app_service.settings
        if provider not in {option.key for option in settings.provider_options()}:
            raise APIError("unknown_provider", "Unknown provider.", 404)
        with (
            self.scheduling.operation("provider", require_idle=body.action != "set_endpoint"),
            self.scheduling.settings_lock,
        ):
            if body.action == "remove":
                if not body.confirmed:
                    raise APIError(
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
                raise APIError("invalid_action", "Unknown provider action.", 422)
        return ActionResponse(
            ok=result.ok,
            message=result.message,
            refresh_health=result.ok and result.config_changed,
        )

    # 聊天流式传输

    def start_turn(
        self,
        message: str,
        session_id: str | None,
        attachments: AttachmentBatch | None = None,
    ) -> ConversationTurn:
        try:
            return self.scheduling.submit(message, session_id, attachments)
        except ConversationError as exc:
            raise APIError.from_conversation(exc) from exc

    def stream(self, turn: ConversationTurn, after: int = 0):
        return stream_frames(turn, after)

    def active_turn_id(self) -> int | None:
        return self._conversation.active_turn_id

    def cancel(self, turn_id: int) -> bool:
        return self.scheduling.cancel(turn_id)

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise APIError("invalid_session_id", "Invalid session ID.", 422)
