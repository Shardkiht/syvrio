from __future__ import annotations

from collections.abc import Hashable, Sequence
from pathlib import Path
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
    def source_path(self) -> str: ...

    @property
    def line(self) -> int: ...

    @property
    def end_line(self) -> int: ...

    @property
    def raw_markdown(self) -> str: ...

    @property
    def index_text(self) -> str: ...

    @property
    def embedding_text(self) -> str: ...


class RecordSource[Record: SearchRecord](Protocol):
    """检索只读快照，来源自己决定记录范围、修订标识和路径。"""

    def entries(self) -> Sequence[Record]: ...

    def fingerprint(self) -> Hashable: ...

    def topic_path(self, topic: str) -> Path: ...


class SearchHit(BaseModel):
    id: str = ""
    file: str = ""
    line: int = 0
    end_line: int = 0
    source_path: str = ""
    raw_markdown: str = ""
    index_text: str = ""
    text: str
    topic: str
    timestamp: str = ""
    score: float = 0.0
    status: str = "active"
    context: dict[str, str | dict[str, str]] | None = None
    matched_field: str = Field(default="", exclude=True)

    @classmethod
    def from_record(
        cls, record: SearchRecord, file: Path, score: float, matched_field: str = ""
    ) -> SearchHit:
        return cls(
            id=record.id,
            file=str(file),
            source_path=record.source_path,
            line=record.line,
            end_line=record.end_line,
            raw_markdown=record.raw_markdown,
            index_text=record.index_text,
            text=record.text,
            topic=record.topic,
            timestamp=record.timestamp,
            score=round(score, 4),
            matched_field=matched_field,
        )


class Retriever(Protocol):
    """检索抽象：默认 hybrid；关键字与向量实现共用同一个 search 接口。"""

    def search(self, query: str, limit: int = 10) -> list[SearchHit]: ...


class Embedder(Protocol):
    """文本向量化接口；embed 用于条目入库，embed_query 用于查询侧。"""

    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
