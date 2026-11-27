"""跟踪公开导出的输入，使派生文件能够按事务提交的版本刷新。"""

from __future__ import annotations

import sqlite3


def install(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO revisions(domain,version) VALUES('git_export',0)")
    # 只登记渲染器实际读取的表；模型调用、配额、缓存、推理正文和后台检查点均不触发导出。
    sources = (
        "topics",
        "memories",
        "candidates",
        "candidate_related",
        "inferences",
        "inference_sources",
        "profile",
        "audit",
        "sessions",
        "session_events",
        "attachments",
    )
    # 会话计数、搜索索引、沉淀标记以及画像元信息都不会写入这些表的导出字段。
    exported_columns = {
        "sessions": "id,archived,started_at",
        "session_events": "seq,session_id,kind,message_id,role,at,content,payload",
        "topics": "name,archived,title",
        "profile": "body",
    }
    for table in sources:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            event = operation
            if operation == "UPDATE" and table in exported_columns:
                event = f"UPDATE OF {exported_columns[table]}"
            db.execute(f"""CREATE TRIGGER export_{table}_{operation} AFTER {event} ON {table}
                BEGIN UPDATE revisions SET version=version+1 WHERE domain='git_export'; END""")
    # 推断记录的正文和来源分别落表，独立改动来源同样会改变 Dream 使用的证据。
    for event in ("INSERT", "UPDATE", "DELETE"):
        db.execute(f"""CREATE TRIGGER rev_inference_sources_{event} AFTER {event} ON inference_sources
            BEGIN UPDATE revisions SET version=version+1 WHERE domain='inferences'; END""")
