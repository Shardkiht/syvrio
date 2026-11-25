"""Durable background requests, keyed by lane and business entity."""

import json
from dataclasses import dataclass
from typing import Literal

from ...infra.storage.database import encode, transaction
from ...infra.storage.paths import SyvrioPaths

JobKind = Literal["context", "distillation", "dream", "title"]


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


# Common scheduling fields and lane checkpoints live in typed columns.
_COLUMNS = (
    "id",
    "state",
    "phase",
    "retry_at",
    "generation",
    "failures",
    "version",
    "through",
    "partial",
    "cursor",
    "remaining_requests",
    "requests",
    "batch",
    "proposed",
    "updated_at",
)
_FIELDS = ",".join(_COLUMNS)
_PARAMS = ",".join("?" for _ in _COLUMNS)
_UPDATES = ",".join(f"{name}=excluded.{name}" for name in _COLUMNS)


def read_job(key):
    with transaction(key.paths, write=False) as db:
        row = db.execute(
            "SELECT * FROM jobs WHERE kind=? AND target=?", (key.kind, key.target)
        ).fetchone()
        if row is None:
            return None
        state = json.loads(row["extra"])
        state.update({name: row[name] for name in _COLUMNS if row[name] is not None})
        if "cursor" in state:
            state["cursor"] = json.loads(state["cursor"])
        return state


def save_job(key, state):
    with transaction(key.paths) as db:
        if state is None:
            db.execute("DELETE FROM jobs WHERE kind=? AND target=?", (key.kind, key.target))
            return
        values = [state.get(name) for name in _COLUMNS]
        if "cursor" in state:
            values[_COLUMNS.index("cursor")] = encode(state["cursor"])
        extra = {key: value for key, value in state.items() if key not in _COLUMNS}
        db.execute(
            f"""INSERT INTO jobs(kind,target,{_FIELDS},extra) VALUES(?,?,{_PARAMS},?)
             ON CONFLICT(kind,target) DO UPDATE SET {_UPDATES},extra=excluded.extra""",
            (key.kind, key.target, *values, encode(extra)),
        )


def enqueue_context_fill(paths, memory_id, version):
    key = JobKey(paths, "context", memory_id)
    with transaction(paths):
        if (read_job(key) or {}).get("version") != version:
            save_job(key, {"id": memory_id, "version": version})
