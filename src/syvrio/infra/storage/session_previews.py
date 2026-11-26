"""把历史页需要的工具预览与大段原文分开读取。"""

from __future__ import annotations

import sqlite3

PREVIEW_SIZE = 300


def save(db: sqlite3.Connection, seq: int, text: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO tool_previews VALUES(?,?,?)", (seq, text[:PREVIEW_SIZE], len(text))
    )


def install(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE tool_previews(
        event_id INTEGER PRIMARY KEY REFERENCES session_events ON DELETE CASCADE,
        preview TEXT NOT NULL, length INTEGER NOT NULL)""")
    # 直接用 SQL 修改原文时也维护常见预览。SQLite 文本函数遇 NUL 停止，
    # 这类正文由写入入口按 Python 字符处理；外部写入则在查询时惰性计算。
    for event in ("insert", "update"):
        update = " OF kind,message_id,content" if event == "update" else ""
        clear = "DELETE FROM tool_previews WHERE event_id=old.seq;" if event == "update" else ""
        db.execute(f"""CREATE TRIGGER tool_preview_{event} AFTER {event}{update} ON session_events
            WHEN new.kind='tool' {"OR old.kind='tool'" if event == "update" else ""} BEGIN {clear}
            INSERT INTO tool_previews SELECT new.seq,substr(coalesce(new.content,''),1,{PREVIEW_SIZE}),length(coalesce(new.content,''))
            WHERE new.kind='tool' AND new.message_id IS NOT NULL AND instr(coalesce(new.content,''),char(0))=0; END""")
