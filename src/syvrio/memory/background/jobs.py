"""按任务类型和目标保存调度状态及任务自己的检查点。"""

import json
from dataclasses import dataclass
from typing import Literal

from ...infra.storage.database import encode, transaction
from ...infra.storage.paths import SyvrioPaths

JobKind = Literal["context", "distillation", "dream", "title", "maintenance"]


@dataclass(frozen=True)
class JobKey:
    paths: SyvrioPaths
    kind: JobKind
    target: str


def job_key(paths, kind, target):
    return JobKey(paths, kind, target)


def list_jobs(paths, kind):
    with transaction(paths, write=False) as db:
        return [
            JobKey(paths, kind, r[0])
            for r in db.execute("SELECT target FROM jobs WHERE kind=? ORDER BY target", (kind,))
        ]


def pending_title_ids(paths: SyvrioPaths, session_ids: list[str]) -> set[str]:
    if not session_ids:
        return set()
    with transaction(paths, write=False) as db:
        return {
            row[0]
            for row in db.execute(
                """SELECT target FROM jobs WHERE kind='title'
                AND target IN (SELECT value FROM json_each(?))""",
                (encode(session_ids),),
            )
        }


_SHARED_FIELDS = ("state", "retry_at", "evidence_commit")


def read_job(key):
    with transaction(key.paths, write=False) as db:
        row = db.execute(
            "SELECT state,retry_at,evidence_commit,checkpoint FROM jobs WHERE kind=? AND target=?",
            (key.kind, key.target),
        ).fetchone()
        if row is None:
            return None
        state = json.loads(row["checkpoint"])
        state.update({name: row[name] for name in _SHARED_FIELDS if row[name] is not None})
        return state


def save_job(key, state):
    with transaction(key.paths) as db:
        if state is None:
            db.execute("DELETE FROM jobs WHERE kind=? AND target=?", (key.kind, key.target))
            return
        checkpoint = {name: value for name, value in state.items() if name not in _SHARED_FIELDS}
        db.execute(
            """INSERT INTO jobs(kind,target,state,retry_at,evidence_commit,checkpoint)
               VALUES(?,?,?,?,?,?) ON CONFLICT(kind,target) DO UPDATE SET
               state=excluded.state,retry_at=excluded.retry_at,
               evidence_commit=excluded.evidence_commit,checkpoint=excluded.checkpoint""",
            (
                key.kind,
                key.target,
                *(state.get(name) for name in _SHARED_FIELDS),
                encode(checkpoint),
            ),
        )


def enqueue_context_fill(paths, memory_id, version):
    key = JobKey(paths, "context", memory_id)
    with transaction(paths):
        if (read_job(key) or {}).get("version") != version:
            save_job(key, {"id": memory_id, "version": version})


def defer_maintenance(db, memory_id, commit):
    db.execute(
        """INSERT INTO maintenance_rechecks VALUES(?,?) ON CONFLICT(memory_id)
                  DO UPDATE SET after_commit=max(after_commit,excluded.after_commit)""",
        (memory_id, commit),
    )


def next_maintenance_recheck(db, commit):
    row = db.execute(
        "SELECT memory_id FROM maintenance_rechecks WHERE after_commit<? LIMIT 1", (commit,)
    ).fetchone()
    return row[0] if row else None


def finish_maintenance_recheck(db, memory_id, commit):
    db.execute(
        "DELETE FROM maintenance_rechecks WHERE memory_id=? AND after_commit<?", (memory_id, commit)
    )
