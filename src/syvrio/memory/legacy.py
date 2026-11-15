from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote

from .models import Entry

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
ENTRY_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (.*)$")
COMMENT_LINE_RE = re.compile(r"^- <!-- ([^>]+) --> (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (.*)$")
TAG_AT_END_RE = re.compile(r"\s+`([A-Za-z_][\w-]*)=([^`]*)`$")
KNOWN_TAG_KEYS = frozenset(
    {
        "id",
        "status",
        "supersedes",
        "source",
        "confidence",
        "note",
        "session",
        "message",
        "context",
        "change_kind",
    }
)
ESCAPED_TAG_PREFIX = "~syvrio-escaped~"

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_DELETED = "deleted"


def _split_metadata(rest: str) -> tuple[str, dict[str, str]]:
    if rest.endswith(")") and "  (" in rest:
        idx = rest.rfind("  (")
        text = rest[:idx].rstrip()
        raw = rest[idx + 3 : -1]
        metadata: dict[str, str] = {}
        for part in raw.split(", "):
            if not part:
                continue
            if ": " in part:
                key, value = part.split(": ", 1)
                metadata[key.strip()] = value.strip()
            else:
                metadata[part.strip()] = "true"
        return text, metadata
    return rest, {}


def _parse_comment(comment: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in comment.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def _split_tags(rest: str) -> tuple[str, dict[str, str]]:
    # 只从行尾逐个剥离已知键；生成的标签以 id 开头，剥到 id 即停，
    # 否则正文末尾同形的 `status=...`/`id=...` 会被当成元数据覆盖真实值。
    tags: dict[str, str] = {}
    text = rest.rstrip()
    while match := TAG_AT_END_RE.search(text):
        key = match.group(1)
        value = match.group(2)
        tags[key] = (
            unquote(value[len(ESCAPED_TAG_PREFIX) :])
            if value.startswith(ESCAPED_TAG_PREFIX)
            else value
        )
        text = text[: match.start()].rstrip()
        if key == "id":
            break
    if "id" not in tags and any(key not in KNOWN_TAG_KEYS for key in tags):
        return rest, {}
    return text, dict(reversed(list(tags.items())))


def parse_legacy_line(line: str, topic: str) -> Entry | None:
    stripped = line.strip()
    comment = COMMENT_LINE_RE.match(stripped)
    if comment:
        fields = _parse_comment(comment.group(1))
        text, metadata = _split_metadata(comment.group(3))
        return Entry(
            id=fields.get("id") or _legacy_id(comment.group(2), text),
            topic=topic,
            text=text,
            timestamp=comment.group(2),
            status=fields.get("status", STATUS_ACTIVE),
            supersedes=fields.get("supersedes"),
            metadata={
                **{
                    key: value
                    for key, value in fields.items()
                    if key not in ("id", "status", "supersedes")
                },
                **metadata,
            },
        )
    match = ENTRY_LINE_RE.match(stripped)
    if match is None:
        return None
    text, tags = _split_tags(match.group(2))
    if tags:
        metadata = {
            key: value for key, value in tags.items() if key not in ("id", "status", "supersedes")
        }
        entry_id = tags.get("id") or _legacy_id(match.group(1), text)
        return Entry(
            id=entry_id,
            topic=topic,
            text=text,
            timestamp=match.group(1),
            status=tags.get("status", STATUS_ACTIVE),
            supersedes=tags.get("supersedes"),
            metadata=metadata,
        )
    text, metadata = _split_metadata(text)
    return Entry(
        id=_legacy_id(match.group(1), text),
        topic=topic,
        text=text,
        timestamp=match.group(1),
        status=STATUS_ACTIVE,
        metadata=metadata,
    )


def _legacy_id(timestamp: str, text: str) -> str:
    digest = hashlib.sha1(f"{timestamp}{text}".encode("utf-8")).hexdigest()[:8]
    return f"mem-legacy-{digest}"
