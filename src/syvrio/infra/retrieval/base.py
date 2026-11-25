from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, Field


class SearchRecord(Protocol):
    """检索记录只提供展示、排序和向量化所需的只读字段。"""

    @property
    def id(self) -> str: ...

    @property
    def topic(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def timestamp(self) -> str: ...

    @property
    def index_text(self) -> str: ...

    @property
    def embedding_text(self) -> str: ...


class RecordSnapshot[Record: SearchRecord](Protocol):
    """一次读取的内容与递增版本号始终对应。"""

    @property
    def revision(self) -> int: ...

    @property
    def records(self) -> Sequence[Record]: ...


class RecordSource[Record: SearchRecord](Protocol):
    def snapshot(self) -> RecordSnapshot[Record]: ...


class SearchHit(BaseModel):
    id: str = ""
    index_text: str = ""
    text: str
    topic: str
    timestamp: str = ""
    score: float = 0.0
    status: str = "active"
    context: dict[str, str | dict[str, str]] | None = None
    matched_field: str = Field(default="", exclude=True)

    @classmethod
    def from_record(cls, record: SearchRecord, score: float, matched_field: str = "") -> SearchHit:
        return cls(
            id=record.id,
            index_text=record.index_text,
            text=record.text,
            topic=record.topic,
            timestamp=record.timestamp,
            score=round(score, 4),
            matched_field=matched_field,
        )


class Retriever(Protocol):
    """检索抽象：默认 hybrid；关键字与向量实现共用同一个 search 接口。"""

    def search(
        self, query: str, limit: int = 10, *, snapshot: RecordSnapshot | None = None
    ) -> list[SearchHit]: ...


class Embedder(Protocol):
    """文本向量化接口；embed 用于条目入库，embed_query 用于查询侧。"""

    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
