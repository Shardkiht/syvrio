"""已提交的召回快照。按有序页和 ID 桶共享未修改的记录。"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import accumulate, batched, chain
from types import MappingProxyType

from ..infra.storage.database import encode
from .models import MemoryContext, RecallEntry


@dataclass(frozen=True, slots=True)
class RecallRecord:
    id: str
    topic: str
    text: str
    timestamp: str
    index_text: str
    context_json: str | None
    revision: int = 0
    rowid: int = field(default=0, compare=False, repr=False)

    @property
    def order(self) -> tuple[str, int]:
        return self.topic, self.rowid

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RecallRecord:
        key, topic, text, stamp, raw, revision, rowid = row
        context = MemoryContext.model_validate_json(raw) if raw else None
        return cls(
            key,
            topic,
            text,
            stamp,
            context.index_text(text) if context else text,
            raw,
            revision,
            rowid,
        )

    @property
    def embedding_text(self) -> str:
        return self.text

    @property
    def context(self) -> MemoryContext | None:
        # 每个调用方获得自己的上下文对象，无法改动共享快照。
        return MemoryContext.model_validate_json(self.context_json) if self.context_json else None

    def as_recall(self, context: MemoryContext | None) -> RecallEntry:
        return RecallEntry(
            id=self.id, topic=self.topic, text=self.text, context=context, revision=self.revision
        )


_PAGE = 256
_BUCKETS = 256

_ROWS = """SELECT m.id,m.topic,m.text,m.recorded_at,m.context,m.revision,m.rowid
           FROM memories m JOIN topics t ON t.name=m.topic
           WHERE t.archived=0 AND m.status='active'"""


@dataclass(frozen=True, slots=True, eq=False)
class _Records(Sequence[RecallRecord]):
    pages: tuple[tuple[RecallRecord, ...], ...]
    ends: tuple[int, ...]

    @classmethod
    def of(cls, pages: Iterable[tuple[RecallRecord, ...]]) -> _Records:
        pages = tuple(pages)
        return cls(pages, tuple(accumulate(map(len, pages))))

    def __len__(self) -> int:
        return self.ends[-1] if self.ends else 0

    def __iter__(self) -> Iterator[RecallRecord]:
        return chain.from_iterable(self.pages)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        page = bisect_right(self.ends, index)
        return self.pages[page][index - (self.ends[page - 1] if page else 0)]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other, strict=True))

    def updated(self, ids: set[str], previous: _Lookup, rows: tuple[RecallRecord, ...]) -> _Records:
        # 以旧页边界定位新旧位置，跨主题更正涉及两页，旧快照的页不动。
        def page_of(row: RecallRecord) -> int:
            return min(
                bisect_left(self.pages, row.order, key=lambda p: p[-1].order),
                max(0, len(self.pages) - 1),
            )

        changes = defaultdict(list)
        for key in ids:
            if (old := previous.get(key)) is not None:
                changes.setdefault(page_of(old), [])  # 只有删除时也要重建此页。
        for row in rows:
            changes[page_of(row)].append(row)
        pages = list(self.pages)
        for pos in sorted(changes, reverse=True):
            kept = [r for r in pages[pos] if r.id not in ids] if pages else []
            merged = sorted(kept + changes[pos], key=lambda row: row.order)
            # 页可增长到两倍，再均分；避免每次追加都产生单元素页。
            size = len(merged) if len(merged) <= 2 * _PAGE else _PAGE
            pages[pos : pos + 1] = batched(merged, size or 1)
        return self.of(pages)


@dataclass(frozen=True, slots=True, eq=False)
class _Lookup(Mapping[str, RecallRecord]):
    records: _Records
    buckets: tuple[Mapping[str, RecallRecord], ...]

    def __iter__(self) -> Iterator[str]:
        return (r.id for r in self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, key: str) -> RecallRecord:
        return self.buckets[hash(key) % _BUCKETS][key]

    def updated(self, records: _Records, ids: set[str], rows: tuple[RecallRecord, ...]) -> _Lookup:
        buckets = list(self.buckets)
        changed = {hash(key) % _BUCKETS for key in ids}
        copies = {i: dict(buckets[i]) for i in changed}
        for key in ids:
            copies[hash(key) % _BUCKETS].pop(key, None)
        for row in rows:
            copies[hash(row.id) % _BUCKETS][row.id] = row
        for i, bucket in copies.items():
            buckets[i] = MappingProxyType(bucket)
        return _Lookup(records, tuple(buckets))


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    revision: int | None
    records: _Records
    by_id: _Lookup
    audit_seq: int = field(compare=False, repr=False)

    @classmethod
    def read(cls, db: sqlite3.Connection, revision: int | None, audit_seq: int) -> MemorySnapshot:
        rows = db.execute(_ROWS + " ORDER BY m.topic,m.rowid")
        records = _Records.of(batched(map(RecallRecord.from_row, rows), _PAGE))
        buckets = [{} for _ in range(_BUCKETS)]
        for row in records:
            buckets[hash(row.id) % _BUCKETS][row.id] = row
        lookup = _Lookup(records, tuple(map(MappingProxyType, buckets)))
        return cls(revision, records, lookup, audit_seq)

    def refresh(
        self, db: sqlite3.Connection, version: int, audit_seq: int, ids: set[str]
    ) -> MemorySnapshot:
        rows = tuple(
            map(
                RecallRecord.from_row,
                db.execute(
                    _ROWS + " AND m.id IN (SELECT value FROM json_each(?))", (encode(sorted(ids)),)
                ),
            )
        )
        records = self.records.updated(ids, self.by_id, rows)
        return MemorySnapshot(version, records, self.by_id.updated(records, ids, rows), audit_seq)
