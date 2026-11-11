"""Stable HTTP/SSE contracts for the Web adapter.

The browser only ever sees these shapes; provider-specific payloads stay in the
Agent/LLM layers. SSE events use one flat schema so the front end can dispatch
on ``type`` without understanding any provider protocol.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "start",
    "reasoning_delta",
    "reasoning_end",
    "content_delta",
    "tool_start",
    "tool_end",
    "event",
    "progress",
    "done",
    "cancelled",
    "failed",
]


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
    # True when another TUI/Web client is blocked from writing the same Vault.
    single_client: bool = True
    static_ready: bool = True


class SessionInfo(BaseModel):
    id: str
    title: str
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


class ProviderInfo(BaseModel):
    key: str
    label: str
    configured: bool
    current: bool
    endpoint: str = ""


class ProviderListResponse(BaseModel):
    items: list[ProviderInfo]


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
    role: Literal["user", "assistant", "tool", "summary"]
    content: str = ""
    at: str = ""
    reasoning: ReasoningBlock | None = None
    tool_calls: list[ToolCallInfo] = Field(default_factory=list)
    tool_name: str | None = None
    tool_ok: bool | None = None
    interrupted: bool = False


class HistoryResponse(BaseModel):
    session_id: str
    title: str
    items: list[HistoryItem]
    summary: str | None = None
    has_earlier: bool = False
    current: bool = False


class StreamEvent(BaseModel):
    """One SSE frame. ``seq`` is monotonic per turn and doubles as the SSE id."""

    seq: int
    type: EventType
    turn_id: int | None = None
    session_id: str | None = None
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


def sse_frame(event: StreamEvent) -> str:
    """Serialize as UTF-8 text; Starlette encodes the response body."""
    payload = event.model_dump_json(exclude_none=True)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"
