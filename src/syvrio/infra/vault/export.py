"""在同一只读快照中生成公开的 Git 文件。"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from ..session.query import SessionQuery
from ..storage.audit import effective_memory_changes, public_audit_events
from ..storage.database import committed_read, encode, transaction


def _jsonl(rows):
    return "".join(encode(row) + "\n" for row in rows)


def _related(rows):
    links = {}
    for owner, memory_id in rows:
        links.setdefault(owner, []).append(memory_id)
    return links


def _soul(paths) -> str | None:
    soul = paths.vault_dir / "SOUL.md"
    if soul.is_file() and not soul.is_symlink():
        return soul.read_text(encoding="utf-8")
    return None


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
        history = db.execute("""SELECT v.* FROM memory_versions v JOIN memories m ON m.id=v.memory_id
            JOIN topics now ON now.name=m.topic JOIN topics then_topic ON then_topic.name=v.topic
            WHERE now.archived=0 AND then_topic.archived=0
            AND NOT EXISTS(SELECT 1 FROM sessions s WHERE s.id=v.session_id AND s.archived)
            ORDER BY v.memory_id,v.commit_id""")
        files["records/memory_versions.jsonl"] = _jsonl(dict(row) for row in history)
        related = _related(
            db.execute(
                """SELECT r.candidate_id,r.memory_id FROM candidate_related r
               JOIN candidates c ON c.id=r.candidate_id WHERE c.state='pending'
               ORDER BY r.candidate_id,r.position"""
            )
        )
        pending = []
        for row in db.execute(
            "SELECT id,topic,text,question,created_at,context,session_id,message_id FROM candidates WHERE state='pending' ORDER BY id"
        ):
            item = dict(row)
            if item["context"]:
                item["context"] = json.loads(item["context"])
            item["related_ids"] = related.get(row["id"], [])
            pending.append(item)
        files["pending.jsonl"] = _jsonl(pending)
        sources = {}
        versions = {}
        for owner, memory, commit in db.execute("""SELECT inference_id,memory_id,memory_version
                FROM inference_sources ORDER BY inference_id,position"""):
            sources.setdefault(owner, []).append(memory)
            if commit is not None:
                versions.setdefault(owner, {})[memory] = commit
        inferences = []
        for row in db.execute(
            "SELECT id,kind,text,status,created_at,updated_at FROM inferences ORDER BY id"
        ):
            item = dict(row)
            item["source_ids"] = sources.get(row["id"], [])
            item["source_versions"] = versions.get(row["id"], {})
            inferences.append(item)
        files["inferences.jsonl"] = _jsonl(inferences)
        profile = db.execute("SELECT body FROM profile").fetchone()
        if profile:
            files["profile.md"] = profile[0].rstrip() + "\n"
        files["records/memory_changes.jsonl"] = _jsonl(effective_memory_changes(db))
        files["records/audit.jsonl"] = _jsonl(public_audit_events(db))
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
    if (soul := _soul(paths)) is not None:
        files["SOUL.md"] = soul
    return files


@dataclass(frozen=True)
class ExportSnapshot:
    revision: int
    soul: str | None
    tree: tuple


def _tree_stamp(root: Path) -> tuple | None:
    # 取文件标识和改动时间以识别外部 Git 检出、替换、重命名或手工修改。
    # 不跟随链接；提交仍从数据库重新生成整个公开视图。
    if not root.is_dir():
        return None
    directories = [root]
    entries = []
    try:
        while directories:
            folder = directories.pop()
            with os.scandir(folder) as children:
                for child in children:
                    info = child.stat(follow_symlinks=False)
                    if (
                        child.is_symlink()
                        or getattr(info, "st_file_attributes", 0)
                        & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        return None
                    path = Path(child.path)
                    if child.is_dir(follow_symlinks=False):
                        directories.append(path)
                        # NTFS 可能稍后才更新父目录时间；子文件分别核对。
                        entries.append((str(path.relative_to(root)), info.st_mode))
                        continue
                    entries.append(
                        (
                            str(path.relative_to(root)),
                            info.st_mode,
                            info.st_ino,
                            info.st_size,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                        )
                    )
    except FileNotFoundError:
        return None
    return tuple(sorted(entries))


def publish_export(paths, repo, previous: ExportSnapshot | None = None) -> ExportSnapshot:
    """调用方持有 Git 锁，只有完整树可供暂存。"""
    dest = repo / "content"
    saved = repo.parent / ".syvrio-previous-export"
    if any(p.resolve() != p for p in (repo, dest, saved)):
        raise ValueError("Invalid export directory.")
    if saved.exists():
        if not dest.exists():
            saved.rename(dest)
        else:
            shutil.rmtree(saved)
    with committed_read(paths) as db:
        revision = db.execute("SELECT version FROM revisions WHERE domain='git_export'").fetchone()[
            0
        ]
        if (
            previous is not None
            and previous.revision == revision
            and previous.soul == _soul(paths)
            and previous.tree == _tree_stamp(dest)
        ):
            return previous
        files = render(paths)
    repo.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".syvrio-export-", dir=repo.parent) as temp:
        prepared = Path(temp) / "content"
        prepared.mkdir()
        for relative, body in files.items():
            path = prepared / relative
            if not path.resolve().is_relative_to(prepared):
                raise ValueError("Invalid export entry.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))
        tree = _tree_stamp(prepared)
        if tree is None:
            raise ValueError("Incomplete export directory.")
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
    return ExportSnapshot(revision, files.get("SOUL.md"), tree)
