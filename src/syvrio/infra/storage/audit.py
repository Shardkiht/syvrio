"""写入审计并从当前读视图提供可公开的操作和有效事实变更。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Container, Iterator

from ..time import utc_timestamp
from .database import encode

# SQL NULL 表示普通审计。导入历史日志时，JSON null 本身也可以是变更内容。
_NO_CHANGE = object()
_MAX_DELTA = 256


def append_audit(
    db: sqlite3.Connection,
    action: str,
    fields: dict,
    *,
    memory_change: object = _NO_CHANGE,
    reverts: int | None = None,
    at: str | None = None,
) -> int:
    row = db.execute(
        """INSERT INTO audit(at,action,details,reverts,memory_change)
           VALUES(?,?,?,?,?) RETURNING seq""",
        (
            at or utc_timestamp(),
            action,
            encode(fields),
            reverts,
            None if memory_change is _NO_CHANGE else encode(memory_change),
        ),
    ).fetchone()
    return row[0]


def revert_memory_change(db: sqlite3.Connection, original: dict) -> None:
    # 撤销与原操作采用相同的归档可见性，审计导出能同时保留或隐藏它们。
    details = json.loads(original["details"])
    origin = {key: details[key] for key in ("memory_id", "session_id") if key in details}
    append_audit(db, "undo_memory_change", origin, reverts=original["seq"])


def latest_sequence(db: sqlite3.Connection) -> int:
    return db.execute("SELECT coalesce(max(seq),0) FROM audit").fetchone()[0]


def changed_memory_ids(
    db: sqlite3.Connection,
    *,
    from_revision: int | None,
    to_revision: int,
    after_seq: int,
    through_seq: int,
    previous_ids: Container[str],
) -> set[str] | None:
    """在调用方的读视图中核对原始变更；无法覆盖版本区间时返回 None。

    previous_ids 仅用于验证撤销新增导致的删除，按 ID 查询即可。
    """
    if from_revision is None:
        return None
    distance = to_revision - from_revision
    if not 0 < distance <= _MAX_DELTA or through_seq <= after_seq:
        return None
    events = db.execute(
        """
        SELECT a.action, json_extract(a.details,'$.memory_id'),
               json_extract(a.details,'$.revision'), a.reverts,
               json_extract(original.details,'$.memory_id')
        FROM audit a LEFT JOIN audit original ON original.seq=a.reverts
            AND original.memory_change IS NOT NULL
        WHERE a.seq>? AND a.seq<=? AND (a.memory_change IS NOT NULL OR a.reverts IS NOT NULL)
        ORDER BY a.seq
    """,
        (after_seq, through_seq),
    )
    revisions = set()
    added = set()
    undone = set()
    ids = set()
    for action, key, rev, reverts, original_key in events:
        if not isinstance(key, str):
            return None
        ids.add(key)
        if reverts is not None:
            if action != "undo_memory_change" or original_key != key:
                return None
            undone.add(key)
        elif (
            action in {"add", "update", "correct"}
            and type(rev) is int
            and from_revision < rev <= to_revision
            and rev not in revisions
        ):
            revisions.add(rev)
            if action == "add":
                added.add(key)
        else:
            return None
    # 多个补偿事件可能只恢复一行。用恢复后的行版本证明写入；
    # 撤销新增会物理删除，必须证实该 ID 在区间内确实从存在变为缺失。
    deleted = 0
    if undone:
        restored = dict(
            db.execute(
                "SELECT id,revision FROM memories WHERE id IN (SELECT value FROM json_each(?))",
                (encode(sorted(undone)),),
            )
        )
        for key in undone:
            if key in restored:
                if from_revision < restored[key] <= to_revision:
                    revisions.add(restored[key])
            elif key in previous_ids or key in added:
                deleted += 1
    if len(revisions) + deleted != distance:
        return None
    return ids


_VISIBLE = """
    FROM audit a
    LEFT JOIN memories m ON m.id=json_extract(a.details,'$.memory_id')
    LEFT JOIN topics t ON t.name=m.topic
    WHERE coalesce(t.archived,0)=0 AND NOT EXISTS (
        SELECT 1 FROM sessions s WHERE s.archived AND s.id IN
        (json_extract(a.details,'$.session'),json_extract(a.details,'$.session_id')))
"""


def effective_memory_changes(db: sqlite3.Connection) -> Iterator[dict]:
    rows = db.execute(f"""SELECT a.seq,m.id,a.memory_change {_VISIBLE}
        AND a.memory_change IS NOT NULL AND m.id IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM audit undo WHERE undo.reverts=a.seq)
        ORDER BY a.seq""")
    for seq, memory_id, change in rows:
        yield {"seq": seq, "memory_id": memory_id, "change": json.loads(change)}


def public_audit_events(db: sqlite3.Connection) -> Iterator[dict]:
    rows = db.execute(f"SELECT a.seq,a.at,a.action,a.details,a.reverts {_VISIBLE} ORDER BY a.seq")
    for seq, at, action, details, reverts in rows:
        event = {"seq": seq, "at": at, "action": action, "details": json.loads(details)}
        if reverts is not None:
            event["reverts"] = reverts
        yield event
