"""Request, response and event shapes shared by resident HTTP clients."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictBool

from ...application.contracts import ModelOption
from ...application.turns import TurnEvent
from ...infra.update import UpdateProgress


class ErrorDetail(BaseModel):
    code: str
    message: str


class ApiErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    provider: str
    model: str
    busy: bool = False
    active_turn_id: int | None = None
    # Multiple clients share the resident owner and its per-session queue.
    single_client: bool = False
    static_ready: bool = False


class UpdateJobResponse(BaseModel):
    state: Literal["idle", "running", "failed", "completed"]
    message: str
    progress: UpdateProgress | None = None


class UpdateCheckResponse(UpdateJobResponse):
    current: str
    latest: str
    available: bool
    managed: bool


class UpdateRequest(BaseModel):
    version: str
    confirmed: StrictBool


class ContextUsageResponse(BaseModel):
    session_id: str | None
    used: int
    limit: int
    compact_threshold: float
    compaction_trigger_tokens: int


class SessionInfo(BaseModel):
    id: str
    title: str
    title_pending: bool = False
    messages: int = 0
    started_at: str = ""
    updated_at: str = ""
    last_message_at: str = ""
    current: bool = False


class SessionListResponse(BaseModel):
    items: list[SessionInfo]


class CreateSessionResponse(BaseModel):
    """A fresh conversation. ``session_id`` stays null until the first message."""

    session_id: str | None = None
    title: str = ""
    draft: bool = True


class SwitchSessionResponse(BaseModel):
    session_id: str
    title: str


class DeleteSessionResponse(BaseModel):
    session_id: str
    deleted: bool = True


class PendingDeletionItem(BaseModel):
    id: str
    text: str


class SessionDeletionPreview(BaseModel):
    pending: list[PendingDeletionItem]
    revision: str


class ProviderInfo(BaseModel):
    key: str
    label: str
    configured: bool
    current: bool
    endpoint: str = ""


class ProviderListResponse(BaseModel):
    items: list[ProviderInfo]


class ModelSearchResponse(BaseModel):
    items: list[ModelOption]
    unavailable_providers: list[str]


class ProviderActionRequest(BaseModel):
    action: Literal["set_key", "update_key", "set_endpoint", "remove"]
    api_key: str = Field(default="", max_length=4096)
    endpoint: str = Field(default="", max_length=512)
    confirmed: bool = False


class ActionResponse(BaseModel):
    ok: bool
    message: str
    refresh_health: bool = False


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None
    submission_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")


class InsertRequest(ChatRequest):
    turn_id: int


class CancelRequest(BaseModel):
    turn_id: int


class CancelResponse(BaseModel):
    cancelled: bool
    turn_id: int
    active_turn_id: int | None = None


class ToolCallInfo(BaseModel):
    id: str = ""
    name: str
    arguments: dict | None = None


class ReasoningBlock(BaseModel):
    text: str = ""
    seconds: float | None = None


class HistoryItem(BaseModel):
    id: str = ""
    role: Literal["user", "assistant", "tool", "summary", "error"]
    content: str = ""
    attachments: list[dict[str, str | int]] = Field(default_factory=list)
    at: str = ""
    reasoning: ReasoningBlock | None = None
    tool_calls: list[ToolCallInfo] = Field(default_factory=list)
    tool_name: str | None = None
    tool_ok: bool | None = None
    interrupted: bool = False


class DraftAttachment(BaseModel):
    message_id: str
    id: str
    name: str
    media_type: str


class MessageDraft(BaseModel):
    text: str
    attachments: list[DraftAttachment]


class HistoryResponse(BaseModel):
    session_id: str
    title: str
    items: list[HistoryItem]
    summary: str | None = None
    has_earlier: bool = False
    current: bool = False


class SwitchSessionHistoryResponse(SwitchSessionResponse):
    history_version: str
    history: HistoryResponse | None


class CommandInfo(BaseModel):
    """One row of the shared TUI/Web command catalog."""

    name: str
    usage: str
    description: str
    support: str  # server / client / unsupported
    intent: str = ""
    confirm: bool = False
    note: str = ""
    shortcut: str = ""
    subcommands: list[dict[str, str]] = Field(default_factory=list)


class CommandCatalogResponse(BaseModel):
    items: list[CommandInfo]


class CommandRequest(BaseModel):
    line: str = Field(min_length=1, max_length=2000)
    expected_message_id: str | None = Field(None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    pending_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    inference_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    session_id: str | None = None
    confirmed: bool = False


class CommandResponse(BaseModel):
    status: str  # ok / client / unsupported / unknown / busy / confirm_required / error
    command: str
    intent: str = ""
    message: str = ""
    data: dict | None = None
    session_id: str | None = None
    refresh_sessions: bool = False
    refresh_history: bool = False


def sse_frame(event: TurnEvent) -> str:
    """Serialize as UTF-8 text; Starlette encodes the response body."""
    payload = event.model_dump_json(exclude_none=True)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"
