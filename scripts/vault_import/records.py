"""按新到旧的顺序合并源快照。冲突会话只接受同一日志的前缀。"""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import PurePosixPath

from syvrio.infra.session.model import validate_record
from syvrio.memory.models import Inference, MemoryContext

from .legacy_lines import FRONTMATTER_RE, parse_legacy_line
from .markdown import document
from .snapshot import fingerprint


def instant(value, zone):
    time = datetime.fromisoformat(value)
    return (time if time.tzinfo else time.replace(tzinfo=zone)).astimezone(UTC).isoformat()


def topic(name):
    parts = name.removeprefix("memories/").removesuffix(".md")
    return parts if "/" in parts else "general/" + parts


def context(fields):
    fields = dict(fields)
    emotion = {k[8:]: fields.pop(k) for k in list(fields) if k.startswith("emotion.")}
    if emotion:
        fields["emotion"] = emotion
    return MemoryContext.model_validate(fields).model_dump(exclude_none=True) if fields else None


class Records:
    def __init__(self, snapshot, zone):
        self.snapshot = snapshot
        self.zone = zone
        self.sessions = {}
        self.memories = {}
        self.topics = {}
        self.inferences = {}
        self.candidates = {}
        self.reasons = {}
        self.logs = {}
        self.profile = None
        self.soul = None
        self.deferred = []
        for i, path in snapshot.paths:
            if path.startswith("runtime/session-reasoning/") and path.endswith(".json"):
                p = PurePosixPath(path)
                self.reasons.setdefault((p.parent.name, p.stem), (i, path))
        for i in sorted({i for i, _ in snapshot.paths}):
            self.read_source(i)

    def read_source(self, i):
        paths = [p for n, p in self.snapshot.paths if n == i]
        for p in paths:
            local = p.removeprefix("vault/")
            if (
                p.startswith("vault/")
                and local.startswith(("sessions/", "archived/sessions/"))
                and p.endswith(".jsonl")
            ):
                self.session(i, p, local.startswith("archived/"))
            elif p.endswith(".md") and local.startswith(
                ("memories/", "archived/memories/", "archived/pending/", "inferences/")
            ):
                self.markdown(i, p, local)
        for p in paths:
            name = PurePosixPath(p).name
            if p.startswith(("vault/records/", "vault/.syvrio/")) and name in (
                "pending.jsonl",
                "resolved.jsonl",
                "inferences.jsonl",
                "audit.jsonl",
                "memory-changes.jsonl",
            ):
                self.log(i, p, name)
        summary = next(
            (
                p
                for p in (
                    "runtime/cache/profile/profile-summary.json",
                    "vault/.syvrio/profile-summary.json",
                )
                if p in paths
            ),
            None,
        )
        if not self.profile:
            data = json.loads(self.snapshot.text(i, summary)) if summary else {}
            body = self.snapshot.text(i, "vault/profile.md") if "vault/profile.md" in paths else ""
            if body.strip() or data.get("text"):
                self.profile = (
                    body if body.strip() else data["text"],
                    {k: v for k, v in data.items() if k != "text"},
                )
        if self.soul is None and "vault/SOUL.md" in paths:
            self.soul = self.snapshot.paths[i, "vault/SOUL.md"]

    def session(self, i, path, archived):
        events = [
            json.loads(line) for line in self.snapshot.text(i, path).splitlines() if line.strip()
        ]
        sid = PurePosixPath(path).stem
        if not events or events[0].get("type") != "meta" or events[0].get("id") != sid:
            raise ValueError(f"会话头无效: {path}")
        for event in events:
            for key in ("at", "started_at"):
                if key in event:
                    event[key] = instant(event[key], self.zone)
            if event["type"] == "message" and event.get("role") == "user":
                event.setdefault(
                    "original_local_date",
                    datetime.fromisoformat(event["at"]).astimezone(self.zone).date().isoformat(),
                )
            if event.get("reasoning_local") and event.get("type") == "assistant":
                location = self.reasons.get((sid, event["id"]))
                if location:
                    event["reasoning"] = json.loads(self.snapshot.text(*location))
                elif not event.get("reasoning"):
                    self.deferred.append(("缺少思考原件", sid, event["id"]))
                    event.pop("reasoning_local")
            validate_record(event)
        if sid in self.sessions:
            kept, prior_archived = self.sessions[sid]
            length = min(len(kept), len(events))
            if kept[:length] != events[:length]:
                raise ValueError(f"同一会话存在分叉历史: {sid}")
            if len(events) > len(kept):
                self.sessions[sid] = events, prior_archived
        else:
            self.sessions[sid] = events, archived

    def markdown(self, i, path, local):
        raw = self.snapshot.text(i, path)
        archived = local.startswith("archived/memories/")
        name = topic(local.removeprefix("archived/"))
        front = FRONTMATTER_RE.match(raw.replace("\r\n", "\n"))
        if front:
            meta = dict(line.split(": ", 1) for line in front[1].splitlines() if ": " in line)
            self.topics.setdefault(name, (meta.get("title", name), archived))
            for line in raw.splitlines():
                if not line.startswith("- "):
                    continue
                item = parse_legacy_line(line, name)
                if item is None:
                    raise ValueError(f"无法读取旧记忆条目: {path}")
                if item.id in self.memories:
                    continue
                ext = dict(item.metadata)
                sid, mid = ext.get("session"), ext.get("message")
                if item.status not in ("active", "deleted", "superseded", "deprecated"):
                    raise ValueError(f"未知记忆状态: {item.status}")
                self.memories[item.id] = (
                    name,
                    "active" if item.status == "active" else "deprecated",
                    item.text,
                    instant(item.timestamp, self.zone),
                    None,
                    item.supersedes,
                    sid,
                    mid,
                    ext,
                )
            return
        title, blocks = document(raw)
        candidate = local == "memories/pending.md" or local.startswith("archived/pending/")
        inference = local.startswith("inferences/")
        if not candidate and not inference:
            self.topics.setdefault(name, (title or name, archived))
        for block in blocks:
            core = dict(block["Core"])
            ext = dict(block.get("Extensions", {}))
            ctx = context(block.get("Context", {}))
            key = core["id"]
            if inference:
                self.inference(
                    dict(core, created_at=ext["created_at"], updated_at=ext["updated_at"])
                )
            elif candidate:
                self.candidates.setdefault(
                    key,
                    (
                        topic(core["topic"]),
                        core["text"],
                        core["question"],
                        instant(ext["created_at"], self.zone),
                        "rejected" if local.startswith("archived/") else "pending",
                        ctx,
                        ext.get("session_id"),
                        ext.get("message_id"),
                        core.get("related_ids", []),
                    ),
                )
            else:
                at = ext.pop("recorded_at", None) or ext.get("syvrio.recorded_at")
                if not at:
                    raise ValueError(f"缺少记忆时间: {key}")
                sid, mid = ext.pop("session_id", None), ext.pop("message_id", None)
                self.memories.setdefault(
                    key,
                    (
                        name,
                        core["status"],
                        core["text"],
                        instant(at, self.zone),
                        ctx,
                        ext.pop("supersedes", None),
                        sid,
                        mid,
                        ext,
                    ),
                )

    def inference(self, row):
        key = row["id"]
        if key in self.inferences:
            return
        fields = {k: row[k] for k in Inference.model_fields if k in row}
        for k in ("created_at", "updated_at"):
            fields[k] = instant(fields[k], self.zone)
        item = Inference.model_validate(fields)
        self.inferences[key] = (
            item.kind,
            item.text,
            item.status,
            item.created_at,
            item.updated_at,
            item.source_ids,
        )

    def log(self, i, path, name):
        count = Counter()
        for line in self.snapshot.text(i, path).splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if name == "inferences.jsonl":
                self.inference(row)
                continue
            if name in ("pending.jsonl", "resolved.jsonl"):
                if row.get("status", "pending") in ("pending", "rejected"):
                    self.candidates.setdefault(
                        row["id"],
                        (
                            topic(row["topic"]),
                            row["text"],
                            row.get("question") or row.get("reason") or row["text"],
                            instant(row["created_at"], self.zone),
                            row.get("status", "pending"),
                            context(row.get("context") or {}),
                            row.get("session_id"),
                            row.get("message_id"),
                            row.get("related_ids", []),
                        ),
                    )
                if name == "pending.jsonl":
                    continue
            for k in ("at", "created_at", "resolved_at"):
                if row.get(k):
                    row[k] = instant(row[k], self.zone)

            digest = fingerprint(row)
            count[digest] += 1
            self.logs.setdefault((name, digest, count[digest]), row)
