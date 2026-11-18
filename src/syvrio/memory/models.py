from __future__ import annotations

import calendar
import re
import secrets
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..time import parse_timestamp, serialize_timestamp, utc_now

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


class MemoryContext(BaseModel):
    """可选的事实上下文；日期字符串自身保留年、月、日精度。"""

    model_config = ConfigDict(extra="forbid")

    event_time: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    time_text: str | None = None
    location: str | None = None
    location_kind: Literal["event", "residence"] | None = None
    emotion: str | None = None
    emotion_at: Literal["event", "expression"] | None = None
    emotion_source: Literal["explicit"] | None = None

    def markdown_fields(self) -> list[tuple[str, str]]:
        values = self.model_dump(exclude_none=True)
        return [(key, values[name]) for key, name in _CONTEXT_KEYS.items() if name in values]

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

    @field_validator("time_text", "location", "emotion")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("上下文文本不能为空")
        return value

    @model_validator(mode="after")
    def validate_context(self) -> MemoryContext:
        if (self.location is None) != (self.location_kind is None):
            raise ValueError("地点与地点类型须同时提供")
        if self.emotion is None:
            if self.emotion_at is not None or self.emotion_source is not None:
                raise ValueError("情绪标注缺少情绪原文")
        elif self.emotion_source != "explicit":
            raise ValueError("第一版仅保存用户明确表达的情绪")
        elif self.emotion_at is None:
            # 无法区分时，只记录当前表达，不能倒推为事件当时的情绪。
            self.emotion_at = "expression"
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


_CONTEXT_NAMESPACES = {
    "temporal": ("event_time", "valid_from", "valid_to", "time_text"),
    "spatial": ("location", "location_kind"),
    "affective": ("emotion", "emotion_at", "emotion_source"),
}
_CONTEXT_KEYS = {f"{ns}.{key}": key for ns, keys in _CONTEXT_NAMESPACES.items() for key in keys}


class Candidate(BaseModel):
    text: str
    topic: str = "uncategorized"
    title: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    sensitive: bool = False
    kind: Literal["add", "update", "conflict"] = "add"
    reason: str = ""
    context: MemoryContext | None = None


class ExtractionResult(BaseModel):
    memories: list[Candidate] = Field(default_factory=list)


InferenceKind = Literal["profile", "emotion", "reflection"]
InferenceStatus = Literal["hypothesis", "confirmed", "rejected", "superseded"]


class Inference(BaseModel):
    """独立假设记录；不属于事实条目，也不能作为另一推断的事实证据。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(min_length=1)
    kind: InferenceKind
    source_ids: list[str] = Field(min_length=1)
    created_at: str
    updated_at: str
    status: InferenceStatus = "hypothesis"
    source_fingerprints: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return serialize_timestamp(parse_timestamp(value))

    @model_validator(mode="after")
    def validate_fingerprints(self) -> Inference:
        if self.source_fingerprints:
            if set(self.source_fingerprints) != set(self.source_ids) or any(
                not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in self.source_fingerprints.values()
            ):
                raise ValueError("推断证据指纹须覆盖全部来源并使用有效摘要。")
        return self

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("推断来源 ID 不能为空")
        return sorted({value.strip() for value in values})


class ReflectionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


class PendingItem(BaseModel):
    id: str
    created_at: str
    source: str
    text: str
    topic: str
    title: str | None = None
    confidence: float
    sensitive: bool = False
    kind: str = "add"
    reason: str = ""
    session_id: str | None = None
    message_id: str | None = None
    conflict_with: str | None = None
    context: MemoryContext | None = None

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return serialize_timestamp(parse_timestamp(value))

    @classmethod
    def from_candidate(
        cls,
        candidate: Candidate,
        source: str,
        reason: str,
        session_id: str | None = None,
        message_id: str | None = None,
        conflict_with: str | None = None,
    ) -> "PendingItem":
        stamp = utc_now()
        return cls(
            id=f"p-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}",
            created_at=serialize_timestamp(stamp.replace(microsecond=0)),
            source=source,
            text=candidate.text,
            topic=candidate.topic,
            title=candidate.title,
            confidence=candidate.confidence,
            sensitive=candidate.sensitive,
            kind=candidate.kind,
            reason=reason,
            session_id=session_id,
            message_id=message_id,
            conflict_with=conflict_with,
            context=candidate.context,
        )


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
    def context(self) -> MemoryContext | None:
        if self.context_fields:
            values: dict[str, str] = {}
            for node in self.context_fields:
                name = _CONTEXT_KEYS.get(node.key)
                if name and node.value is not None:
                    values.setdefault(name, node.value)
            # A manually written v2 record can omit companion fields. Project only
            # understood scalar values without inventing type or provenance.
            for key in ("event_time", "valid_from", "valid_to"):
                try:
                    MemoryContext.validate_date(values.get(key))
                except ValueError:
                    values.pop(key, None)
            return MemoryContext.model_construct(**values) if values else None
        raw = self.metadata.get("context")
        return MemoryContext.model_validate_json(raw) if raw else None

    @property
    def index_text(self) -> str:
        return "\n".join(
            [
                self.text,
                *(
                    f.raw_markdown
                    for f in (*self.context_fields, *self.extension_fields)
                    if f.key
                    not in {
                        "syvrio.recorded_at",
                        "syvrio.supersedes",
                        "syvrio.session",
                        "syvrio.message",
                        "syvrio.change_kind",
                        "migration.evidence_v1",
                        "migration.evidence_v2",
                    }
                ),
            ]
        )

    @property
    def embedding_text(self) -> str:
        indexed = self.index_text
        field = short_field(self.text)
        if field is None or self.text.rstrip().endswith("。"):
            # A field whose value already forms a sentence needs no vector rewrite.
            return indexed
        label, value = field
        if not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", label):
            return indexed
        context = f"\n主题：{self.topic_title}" if 1 <= len(self.topic_title) <= 64 else ""
        return f"{label}是{value}。{context}{indexed[len(self.text) :]}"


class TopicInfo(BaseModel):
    topic: str
    title: str
    entries: int
    updated: str
