"""Render a reproducible public Git projection from a single read transaction."""

import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from ..session.query import SessionQuery
from ..storage.database import encode, transaction


def _jsonl(rows):
    return "".join(encode(row) + "\n" for row in rows)


def render(paths):
    files = {}
    with transaction(paths, write=False) as db:
        for topic, title in db.execute(
            "SELECT name,title FROM topics WHERE archived=0 ORDER BY name"
        ):
            body = [f"# {title}\n"]
            for row in db.execute(
                "SELECT * FROM memories WHERE topic=? ORDER BY recorded_at,id", (topic,)
            ):
                body.extend(
                    [
                        f"## {row['id']}\n",
                        row["text"],
                        "",
                        f"- Status: {row['status']}",
                        f"- Recorded: {row['recorded_at']}",
                    ]
                )
                for name in ("context", "supersedes", "session_id", "message_id"):
                    if row[name]:
                        body.append(f"- {name}: {row[name]}")
                extensions = json.loads(row["extensions"])
                if extensions:
                    body.append(f"- Metadata: {encode(extensions)}")
                body.append("")
            files[f"memories/{topic}.md"] = "\n".join(body)
        pending = []
        for row in db.execute(
            "SELECT id,topic,text,question,created_at,context,session_id,message_id FROM candidates WHERE state='pending' ORDER BY id"
        ):
            item = dict(row)
            if item["context"]:
                item["context"] = json.loads(item["context"])
            item["related_ids"] = [
                r[0]
                for r in db.execute(
                    "SELECT memory_id FROM candidate_related WHERE candidate_id=? ORDER BY position",
                    (row["id"],),
                )
            ]
            pending.append(item)
        files["pending.jsonl"] = _jsonl(pending)
        inferences = []
        for row in db.execute(
            "SELECT id,kind,text,status,created_at,updated_at FROM inferences ORDER BY id"
        ):
            item = dict(row)
            item["source_ids"] = [
                r[0]
                for r in db.execute(
                    "SELECT memory_id FROM inference_sources WHERE inference_id=? ORDER BY position",
                    (row["id"],),
                )
            ]
            inferences.append(item)
        files["inferences.jsonl"] = _jsonl(inferences)
        profile = db.execute("SELECT body FROM profile").fetchone()
        if profile:
            files["profile.md"] = profile[0].rstrip() + "\n"
        changes = db.execute("""SELECT c.* FROM memory_changes c
            JOIN memories m ON m.id=c.memory_id JOIN topics t ON t.name=m.topic
            WHERE t.archived=0 ORDER BY c.seq""")
        files["records/memory_changes.jsonl"] = _jsonl(
            {**dict(row), "change": json.loads(row["change"])} for row in changes
        )
        audit = db.execute("""SELECT a.* FROM audit a WHERE NOT EXISTS (
            SELECT 1 FROM sessions s WHERE s.id=json_extract(a.details,'$.session') AND s.archived)
            AND NOT EXISTS (SELECT 1 FROM memories m JOIN topics t ON t.name=m.topic
                WHERE m.id=json_extract(a.details,'$.memory_id') AND t.archived)
            ORDER BY a.seq""")
        files["records/audit.jsonl"] = _jsonl(
            {**dict(row), "details": json.loads(row["details"])} for row in audit
        )
        sessions = SessionQuery(paths)
        for (sid,) in db.execute("SELECT id FROM sessions WHERE archived=0 ORDER BY id"):
            session = sessions.load(sid, include_reasoning=False)
            files[f"sessions/{sid}.jsonl"] = _jsonl(
                {
                    k: v
                    for k, v in r.items()
                    if k not in {"reasoning", "reasoning_local", "turn_undo_recorded"}
                }
                for r in session.records
            )
    soul = paths.vault_dir / "SOUL.md"
    if soul.is_file() and not soul.is_symlink():
        files["SOUL.md"] = soul.read_text(encoding="utf-8")
    return files


def publish_export(paths, repo):
    """Git coordination is held by the caller. Only complete trees enter the index."""
    dest = repo / "content"
    saved = repo.parent / ".syvrio-previous-export"
    if any(p.resolve() != p for p in (repo, dest, saved)):
        raise ValueError("Invalid export directory.")
    if saved.exists():
        if not dest.exists():
            saved.rename(dest)
        else:
            shutil.rmtree(saved)
    repo.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".syvrio-export-", dir=repo.parent) as temp:
        prepared = Path(temp) / "content"
        prepared.mkdir()
        for relative, body in render(paths).items():
            path = prepared / relative
            if not path.resolve().is_relative_to(prepared):
                raise ValueError("Invalid export entry.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))
        if dest.exists():
            dest.rename(saved)
        try:
            prepared.rename(dest)
        except BaseException:
            if saved.exists():
                saved.rename(dest)
            raise
        if saved.exists():
            shutil.rmtree(saved)
