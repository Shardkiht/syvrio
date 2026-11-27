"""保存证据的已提交版本；后台任务只持有逻辑提交号，不占用跨批次的读事务。"""

from __future__ import annotations

import sqlite3

from .database import before_commit


def group_changes(db: sqlite3.Connection) -> None:
    """同一业务事务的记忆、主题和推断共用一次提交号。外层提交时清除预留。"""
    if db.execute(
        "UPDATE revisions SET reserved=version+1 WHERE domain='evidence' AND reserved IS NULL"
    ).rowcount:
        before_commit(_finish_changes)


def _finish_changes(db: sqlite3.Connection) -> None:
    db.execute("UPDATE revisions SET reserved=NULL WHERE domain='evidence'")


def forget_new_memory(db: sqlite3.Connection, memory_id: str) -> None:
    """无依赖的新事实被本回合撤回时，同时作废其版本并推进证据提交号。"""
    group_changes(db)
    db.execute("UPDATE revisions SET version=reserved WHERE domain='evidence'")
    db.execute("DELETE FROM memory_versions WHERE memory_id=?", (memory_id,))


def current(db: sqlite3.Connection) -> int:
    return db.execute("SELECT version FROM revisions WHERE domain='evidence'").fetchone()[0]


# 读取和保护读视图共用可见性规则。先按记忆 ID 查切点对应的版本，避免扫描全部历史。
_VISIBLE_FACTS = """
    FROM memories m CROSS JOIN cuts c
    CROSS JOIN memory_versions v ON v.memory_id=m.id AND v.commit_id=(
        SELECT max(h.commit_id) FROM memory_versions h
        WHERE h.memory_id=m.id AND h.commit_id<=c.commit_id)
    WHERE v.status='active' AND (SELECT t.archived FROM topic_versions t
        WHERE t.name=v.topic AND t.commit_id<=c.commit_id ORDER BY t.commit_id DESC LIMIT 1)=0
"""


def visible_facts(db: sqlite3.Connection, commit: int) -> list[sqlite3.Row]:
    return db.execute(
        f"""WITH cuts(commit_id) AS (VALUES(?))
            SELECT v.memory_id AS id,v.topic,v.text,v.context,v.status
            {_VISIBLE_FACTS} ORDER BY m.id""",
        (commit,),
    ).fetchall()


def is_fact_pinned(db: sqlite3.Connection, memory_id: str) -> bool:
    """未结束任务持有固定读视图；完成行仅保留出处，放弃任务时删除其检查点。"""
    return (
        db.execute(
            f"""WITH cuts(commit_id) AS (
            SELECT evidence_commit FROM jobs WHERE evidence_commit IS NOT NULL AND state IS NOT 'completed')
            SELECT 1 {_VISIBLE_FACTS} AND m.id=? LIMIT 1""",
            (memory_id,),
        ).fetchone()
        is not None
    )
