from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import tempfile
from datetime import UTC, date, datetime, time
from pathlib import Path, PurePosixPath

from ..infra.paths import SyvrioPaths, vault_paths
from ..time import parse_timestamp, serialize_timestamp, utc_now
from .models import Entry, MemoryContext, TopicInfo
from .schema import (
    STATUSES,
    Core,
    Document,
    SchemaError,
    _span_code,
    parse_document,
    render_record,
)

SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_DELETED = "deleted"


def slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip().lower()).replace("_", "-")
    return re.sub(r"-{2,}", "-", slug).strip("-") or "uncategorized"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().lower()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            if path.exists():
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _recorded_time(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    if len(value) == 10:
        # Day-precision records have no instant. Use their date as a sort bucket only.
        return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)
    return parse_timestamp(value)


class MemoryStore:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.vault = self.paths.vault_dir
        self.root = self.paths.memories_dir

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _files(self) -> list[Path]:
        if self.root.is_symlink():
            raise SchemaError("memories directory cannot be a symlink")
        return sorted(self.root.rglob("*.md"))

    def _topic(self, path: Path) -> str:
        return path.relative_to(self.vault).as_posix()

    def topic_path(self, topic: str) -> Path:
        parts = PurePosixPath(topic.strip()).parts
        if (
            not parts
            or topic.startswith("/")
            or "\\" in topic
            or ".." in parts
            or any(part.startswith(".") for part in parts)
        ):
            raise ValueError("invalid memory topic")
        if parts[0] == "memories" and len(parts) >= 3 and parts[-1].endswith(".md"):
            path = self.vault.joinpath(*parts)
        elif len(parts) > 1:
            path = self.root.joinpath(*(slugify(p) for p in parts[:-1]), f"{slugify(parts[-1])}.md")
        else:
            slug = slugify(parts[0])
            matches = [p for p in self._files() if p.stem == slug]
            if len(matches) > 1:
                raise ValueError("ambiguous topic; use its vault-relative path")
            path = matches[0] if matches else self.root / "general" / f"{slug}.md"
        self._assert_path(path)
        return path

    def _assert_path(self, path: Path) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise SchemaError("memory path points outside memories or is a symlink")

    def fingerprint(self) -> tuple[tuple[str, str], ...]:
        items = []
        for path in self._files():
            self._assert_path(path)
            items.append((self._topic(path), hashlib.sha256(path.read_bytes()).hexdigest()))
        return tuple(items)

    def document(self, path: Path) -> Document:
        self._assert_path(path)
        relative = self._topic(path)
        try:
            raw = path.read_bytes().decode("utf-8") if path.exists() else ""
        except UnicodeDecodeError as exc:
            raise SchemaError(
                f"{relative}: invalid UTF-8. Save the memory file as UTF-8 and try again."
            ) from exc
        document = parse_document(raw, relative, relative)
        for record in document.records:
            try:
                _recorded_time(record.entry.timestamp)
            except ValueError as exc:
                raise SchemaError(
                    f"{relative}:{record.entry.line}: invalid syvrio.recorded_at"
                ) from exc
        return document

    @staticmethod
    def new_entry_id(stamp: datetime | None = None) -> str:
        stamp = stamp or utc_now()
        return f"mem-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"

    def append(
        self,
        topic: str,
        text: str,
        *,
        source: str = "chat",
        confidence: float = 1.0,
        note: str | None = None,
        when: datetime | None = None,
        title: str | None = None,
        extra: dict[str, str] | None = None,
        entry_id: str | None = None,
        supersedes: str | None = None,
        known_ids: set[str] | None = None,
        context: MemoryContext | None = None,
    ) -> Entry:
        stamp = when or utc_now()
        if known_ids is None:
            known_ids = {entry.id for entry in self.entries(include_inactive=True)}
        if entry_id is not None and entry_id in known_ids:
            raise ValueError("duplicate memory id")
        while entry_id is None or entry_id in known_ids:
            entry_id = self.new_entry_id(stamp)
        path = self.topic_path(topic)
        relative = self._topic(path)
        original = self.document(path).raw_markdown
        newline = "\r\n" if "\r\n" in original else "\n"
        extensions = {
            "recorded_at": serialize_timestamp(stamp.replace(microsecond=0)),
            **{key: value for key, value in (extra or {}).items() if key != "recorded_at"},
            "supersedes": supersedes,
            "note": note,
        }
        record = render_record(
            text.strip(),
            Core(id=entry_id, status=STATUS_ACTIVE, source=source, confidence=confidence),
            context.markdown_fields() if context else (),
            [(f"syvrio.{key}", value) for key, value in extensions.items() if value],
            newline,
        )
        prefix = (
            original + (newline if original.endswith(("\n", "\r")) else newline * 2)
            if original
            else (f"# {' '.join((title or path.stem).splitlines())}{newline}{newline}")
        )
        output = prefix + record
        parsed = parse_document(output, relative, relative)
        if not parsed.records or parsed.records[-1].entry.id != entry_id:
            raise SchemaError(f"{relative}: appended record is not a complete Markdown list item")
        atomic_write(path, output.encode("utf-8"))
        return parsed.records[-1].entry

    def entries(self, topic: str | None = None, include_inactive: bool = False) -> list[Entry]:
        if not self.root.exists():
            return []
        paths = [self.topic_path(topic)] if topic else self._files()
        ids: set[str] = set()
        result = []
        for path in paths:
            for record in self.document(path).records:
                entry = record.entry
                if entry.id in ids:
                    raise SchemaError(
                        f"{entry.source_path}:{entry.line}: duplicate memory id across topics"
                    )
                ids.add(entry.id)
                if include_inactive or entry.status == STATUS_ACTIVE:
                    result.append(entry)
        return result

    def find(self, entry_id: str) -> Entry | None:
        return next((e for e in self.entries(include_inactive=True) if e.id == entry_id), None)

    def set_status(self, entry_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError("invalid memory status")
        entry = self.find(entry_id)
        if entry is None:
            return False
        path = self.topic_path(entry.topic)
        document = self.document(path)
        record = next(r for r in document.records if r.entry.id == entry_id)
        if record.entry.status != status:
            raw = document.raw_markdown
            atomic_write(
                path,
                (raw[: record.status_start] + _span_code(status) + raw[record.status_end :]).encode(
                    "utf-8"
                ),
            )
        return True

    def topics(self) -> list[TopicInfo]:
        result = []
        for path in self._files():
            document = self.document(path)
            title = next(
                (
                    line[2:].strip()
                    for line in document.raw_markdown.splitlines()
                    if line.startswith("# ")
                ),
                path.stem,
            )
            active = sum(r.entry.status == STATUS_ACTIVE for r in document.records)
            modified = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
            result.append(
                TopicInfo(topic=self._topic(path), title=title, entries=active, updated=modified)
            )
        return result

    def has_text(self, text: str) -> bool:
        target = normalize_text(text)
        return any(normalize_text(entry.text) == target for entry in self.entries())

    def recent_context(self, limit: int = 60) -> str:
        entries = sorted(
            self.entries(), key=lambda entry: _recorded_time(entry.timestamp), reverse=True
        )[:limit]
        return "\n".join(
            f"[{entry.topic}] {entry.timestamp} {entry.text}" for entry in reversed(entries)
        )
