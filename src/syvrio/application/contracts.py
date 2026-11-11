"""应用层稳定数据契约：不依赖 Textual/Rich，也不重复领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..agent.runtime import AgentReply, ThinkingUpdate
from ..agent.session import Session
from ..memory.models import Inference, PendingItem


@dataclass(frozen=True)
class SessionSnapshot:
    id: str
    title: str
    messages: int
    updated_at: str
    started_at: str
    current: bool


@dataclass(frozen=True)
class QueuedItem:
    session_id: str
    line: str
    index: int = 0


@dataclass(frozen=True)
class QueueSnapshot:
    items: tuple[QueuedItem, ...] = ()
    paused: bool = False

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class Submission:
    """忙碌期提交的路由结果；界面据此决定排入队列、直接执行或取消。"""

    kind: str  # queued / command / undo / undo_busy / message
    session_id: str | None = None
    item: QueuedItem | None = None
    message: str | None = None


@dataclass
class TurnCallbacks:
    on_event: Callable[[str], None] | None = None
    on_delta: Callable[[str], None] | None = None
    on_progress: Callable[[str], None] | None = None
    on_session: Callable[[Session], None] | None = None
    on_user_message: Callable[[str], None] | None = None
    on_thinking: Callable[[ThinkingUpdate], None] | None = None
    # Outcome of the tool call whose display text arrives through ``on_event``
    # next; the Web stream needs it to label failures without a history reload.
    on_tool: Callable[[bool], None] | None = None


@dataclass
class UndoOutcome:
    restored: str | None = None
    target_session_id: str | None = None
    failed: bool = False
    error: str | None = None
    queued_count: int = 0
    paused: bool = False


@dataclass
class TurnResult:
    status: str  # ok / cancelled / failed / undo
    session_id: str | None = None
    user_message_id: str | None = None
    turn_id: int | None = None
    reply: AgentReply | None = None
    content: str = ""
    error: str | None = None
    exception: Exception | None = None
    undo: UndoOutcome | None = None


@dataclass
class SwitchResult:
    session: Session | None
    label: str | None
    stats: dict = field(default_factory=dict)


@dataclass
class InferenceReview:
    items: list[Inference]
    sources: dict[str, str]
    action: str | None = None
    updated_id: str | None = None
    feedback: str | None = None


@dataclass
class ProfileSnapshot:
    body: str
    status: str
    stale: bool = False
    generated_at: str | None = None
    fact_count: int | None = None
    unique_count: int | None = None
    available: bool = True


@dataclass
class ImportResult:
    files: int = 0
    chunks: int = 0
    saved: int = 0
    queued: int = 0
    duplicates: int = 0
    blocked: int = 0
    errors: list[str] = field(default_factory=list)
    apply: bool = False


@dataclass
class ImportChunkProgress:
    file: str
    chunk: int
    total_chunks: int
    file_index: int
    total_files: int
    apply: bool
    saved: int = 0
    queued: int = 0
    duplicates: int = 0
    blocked: int = 0
    admitted: list[tuple[str, str]] = field(default_factory=list)
    pending: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None


@dataclass
class SettingsResult:
    ok: bool
    message: str
    config_changed: bool = False


@dataclass
class ProviderOption:
    key: str
    label: str
    configured: bool
    current: bool


@dataclass
class ModelOption:
    provider: str
    model: str
    label: str
    current: bool = False
    recent: bool = False


@dataclass
class ReflectionSchedule:
    running: bool
    enabled: bool
    profile_next: str | None = None
    dream_next: str | None = None
    profile_pending: str | None = None
    dream_pending: bool = False


@dataclass
class PendingSnapshot:
    items: list[PendingItem]
