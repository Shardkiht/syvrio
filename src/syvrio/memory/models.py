from __future__ import annotations

import calendar
import re
import secrets
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..infra.time import parse_timestamp, serialize_timestamp, utc_timestamp


class MemoryEmotion(BaseModel):
    """按感受发生的时点分别保留用户原话。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, str_min_length=1)

    event: str | None = None
    recollection: str | None = None
    unspecified: str | None = None


class MemoryContext(BaseModel):
    """可选的事实上下文；日期字符串自身保留年、月、日精度。"""

    model_config = ConfigDict(extra="forbid")

    event_time: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    location: str | None = None
    emotion: MemoryEmotion | None = None

    @property
    def emotion_texts(self) -> tuple[str, ...]:
        if self.emotion is None:
            return ()
        return tuple(self.emotion.model_dump(exclude_none=True).values())

    def index_text(self, text: str) -> str:
        return "\n".join(value for value in (text, self.location, *self.emotion_texts) if value)

    @field_validator("event_time", "valid_from", "valid_to")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?", value):
            raise ValueError("事实日期须为 YYYY、YYYY-MM 或 YYYY-MM-DD")
        parts = [int(part) for part in value.split("-")]
        try:
            date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
        except ValueError as exc:
            raise ValueError("事实日期不存在") from exc
        return value

    @field_validator("location")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("上下文文本不能为空")
        return value

    @model_validator(mode="after")
    def validate_context(self) -> MemoryContext:
        if self.emotion is not None and not self.emotion_texts:
            self.emotion = None
        if self.valid_from and self.valid_to:
            start = date.fromisoformat(
                self.valid_from + "-01" * (3 - len(self.valid_from.split("-")))
            )
            end_parts = [int(part) for part in self.valid_to.split("-")]
            year = end_parts[0]
            month = end_parts[1] if len(end_parts) > 1 else 12
            day = end_parts[2] if len(end_parts) > 2 else calendar.monthrange(year, month)[1]
            if start > date(year, month, day):
                raise ValueError("事实生效结束时间不能早于开始时间")
        return self


MemoryStatus = Literal["active", "deprecated"]


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str | None = None
    end: str | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> TimeRange:
        if self.start is None and self.end is None:
            raise ValueError("Provide a start or an end for a time range.")
        MemoryContext(valid_from=self.start, valid_to=self.end)
        return self


class MemoryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    context: MemoryContext | None = None

    @field_validator("topic")
    @classmethod
    def topic_name(cls, value: str) -> str:
        if (
            "\\" in value
            or ":" in value
            or any(not part or part.startswith(".") for part in value.split("/"))
        ):
            raise ValueError("Use a topic name or a relative topic such as general/reading.")
        return value


class RecallEntry(MemoryDraft):
    id: str = Field(min_length=1)
    status: MemoryStatus = "active"


class MemoryEntry(RecallEntry):
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("recorded_at needs an explicit UTC offset.")
        return value


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    context: MemoryContext | None = None
    status: MemoryStatus | None = None

    @model_validator(mode="after")
    def has_change(self) -> MemoryUpdate:
        if self.status is None and not (
            self.context and self.context.model_dump(exclude_none=True)
        ):
            raise ValueError("A memory update needs context or status.")
        return self


class Candidate(MemoryDraft):
    """从文档提取的草稿，可能需要进一步澄清。"""

    question: str | None = Field(default=None, min_length=1)
    related_ids: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    memories: list[Candidate] = Field(default_factory=list)


InferenceKind = str
InferenceStatus = Literal["hypothesis", "confirmed", "deprecated"]


class Inference(BaseModel):
    """独立假设记录；不属于事实条目，也不能作为另一推断的事实证据。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    created_at: str
    updated_at: str
    status: InferenceStatus = "hypothesis"

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return serialize_timestamp(parse_timestamp(value))

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("推断来源 ID 不能为空")
        return sorted({value.strip() for value in values})


class ReflectionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=600)
    source_ids: list[str] = Field(min_length=1, max_length=12)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("回顾候选不能为空。")
        return value.strip()

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, values: list[str]) -> list[str]:
        return Inference.unique_sources(values)


class ReflectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposals: list[ReflectionProposal] = Field(max_length=12)


class ProfileSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def nonempty_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("画像摘要不能为空。")
        return value.strip()


class ProfileItem(BaseModel):
    id: str
    text: str
    kind: Literal["fact", "inference"]
    source_ids: list[str]
    status: str
    updated_at: str


class PendingItem(MemoryDraft):
    id: str = Field(default_factory=lambda: f"p-{secrets.token_hex(12)}", min_length=1)
    created_at: str = Field(default_factory=utc_timestamp)
    question: str = Field(min_length=1)
    related_ids: list[str] = Field(default_factory=list)
    session_id: str | None = None
    message_id: str | None = None

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, stamp: str) -> str:
        return serialize_timestamp(parse_timestamp(stamp))

    @field_validator("related_ids")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("A related memory ID cannot be empty.")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def source_pair(self) -> PendingItem:
        if (self.session_id is None) != (self.message_id is None):
            raise ValueError("A session source needs both a session and a message ID.")
        return self


class Entry(BaseModel):
    id: str
    topic: str
    text: str
    timestamp: str
    status: str = "active"
    supersedes: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    revision: int = 0
    context: MemoryContext | None = None
    topic_title: str = ""

    @property
    def source_session_id(self) -> str | None:
        return self.metadata.get("session_id")

    @property
    def source_message_id(self) -> str | None:
        return self.metadata.get("message_id")

    def as_recall(self) -> RecallEntry:
        return RecallEntry(
            id=self.id, status=self.status, topic=self.topic, text=self.text, context=self.context
        )

    def as_memory(self) -> MemoryEntry:
        return MemoryEntry(
            **self.as_recall().model_dump(), recorded_at=parse_timestamp(self.timestamp)
        )

    @property
    def index_text(self) -> str:
        return self.context.index_text(self.text) if self.context else self.text

    @property
    def embedding_text(self) -> str:
        return self.text


class TopicInfo(BaseModel):
    topic: str
    title: str
    entries: int
    updated: str
