"""写入审计并从当前读视图提供可公开的操作和有效事实变更。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

from ..time import utc_timestamp
from .database import encode

# SQL NULL 表示普通审计。导入历史日志时，JSON null 本身也可以是变更内容。
_NO_CHANGE = object()


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
