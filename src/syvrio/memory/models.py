from __future__ import annotations

import calendar
import re
import secrets
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..time import parse_timestamp, serialize_timestamp, utc_timestamp

_SHORT_FIELD = re.compile(
    r"(?P<label>[\u3400-\u4dbf\u4e00-\u9fffA-Za-z][\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9 _-]{1,17})"
    r"\s*[:：]\s*(?P<value>[^\n\r。！？]{1,64})。?"
)


def short_field(text: str) -> tuple[str, str] | None:
    """Recognize a bounded, standalone field without guessing its meaning."""
    match = _SHORT_FIELD.fullmatch(text.strip())
    if match and (value := match["value"].strip()):
        return match["label"].strip(), value
    return None


class MemoryEmotion(BaseModel):
    """The user's own wording, separated by when the feeling was experienced."""

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

    def markdown_fields(self) -> list[tuple[str, str]]:
        values = self.model_dump(exclude_none=True)
        fields = [(key, values[name]) for key, name in _CONTEXT_KEYS.items() if name in values]
        fields.extend(
            (f"emotion.{phase}", text) for phase, text in values.get("emotion", {}).items()
        )
        return fields

    @property
    def emotion_texts(self) -> tuple[str, ...]:
        if self.emotion is None:
            return ()
        return tuple(self.emotion.model_dump(exclude_none=True).values())

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


_CONTEXT_KEYS = {key: key for key in ("event_time", "valid_from", "valid_to", "location")}
_EMOTION_KEYS = {f"emotion.{phase}": phase for phase in MemoryEmotion.model_fields}
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
        if self.status is None and not (self.context and self.context.markdown_fields()):
            raise ValueError("A memory update needs context or status.")
        return self


class Candidate(MemoryDraft):
    """A draft returned by document extraction, optionally requiring clarification."""

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


class FieldNode(BaseModel):
    key: str
    value: str | None = None
    children: tuple[FieldNode, ...] = ()
    raw_markdown: str


class Entry(BaseModel):
    id: str
    topic: str
    text: str
    timestamp: str
    status: str = "active"
    supersedes: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    raw_markdown: str = ""
    source_path: str = ""
    line: int = 0
    end_line: int = 0
    confidence: float | None = None
    context_fields: tuple[FieldNode, ...] = ()
    extension_fields: tuple[FieldNode, ...] = ()
    topic_title: str = Field(default="", exclude=True)

    @property
    def source_session_id(self) -> str | None:
        return self.metadata.get("session_id")

    @property
    def source_message_id(self) -> str | None:
        return self.metadata.get("message_id")

    @property
    def context(self) -> MemoryContext | None:
        if self.context_fields:
            values: dict[str, str] = {}
            emotions: dict[str, str] = {}
            for node in self.context_fields:
                if node.value is None:
                    continue
                if name := _CONTEXT_KEYS.get(node.key):
                    values.setdefault(name, node.value)
                elif phase := _EMOTION_KEYS.get(node.key):
                    emotions.setdefault(phase, node.value)
            # A manually written v2 record can omit companion fields. Project only
            # understood scalar values without inventing type or provenance.
            for key in ("event_time", "valid_from", "valid_to"):
                try:
                    MemoryContext.validate_date(values.get(key))
                except ValueError:
                    values.pop(key, None)
            if not values and not emotions:
                return None
            emotion = MemoryEmotion.model_validate(emotions) if emotions else None
            return MemoryContext.model_construct(**values, emotion=emotion)
        raw = self.metadata.get("context")
        return MemoryContext.model_validate_json(raw) if raw else None

    def as_recall(self) -> RecallEntry:
        topic = self.topic
        if topic.startswith("memories/") and topic.endswith(".md"):
            topic = PurePosixPath(topic.removeprefix("memories/")).with_suffix("").as_posix()
        return RecallEntry(
            id=self.id, status=self.status, topic=topic, text=self.text, context=self.context
        )

    def as_memory(self) -> MemoryEntry:
        return MemoryEntry(
            **self.as_recall().model_dump(), recorded_at=parse_timestamp(self.timestamp)
        )

    @property
    def index_text(self) -> str:
        context = self.context
        if context is None:
            return self.text
        return "\n".join(
            value for value in (self.text, context.location, *context.emotion_texts) if value
        )

    @property
    def embedding_text(self) -> str:
        return self.text


class TopicInfo(BaseModel):
    topic: str
    title: str
    entries: int
    updated: str
