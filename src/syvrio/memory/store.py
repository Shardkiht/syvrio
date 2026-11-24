from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from ..infra.files import safe_component
from ..infra.paths import SyvrioPaths, vault_paths
from ..infra.transaction import VaultTransaction, vault_transaction
from ..time import parse_timestamp, serialize_timestamp, utc_now
from .models import Entry, MemoryContext, TopicInfo
from .schema import (
    STATUSES,
    Core,
    Document,
    SchemaError,
    _render_fields,
    _span_code,
    append_record,
    context_values,
    parse_document,
    render_record,
    replacements,
)
from .turn_undo import capture_file_write

SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")
STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"


def slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip().lower()).replace("_", "-")
    return re.sub(r"-{2,}", "-", slug).strip("-") or "uncategorized"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().lower()


def tracked_write(tx: VaultTransaction, path: Path, payload: bytes) -> None:
    capture_file_write(path, payload)
    tx.write(path, payload)


def read_utf8(path: Path) -> str:
    if path.resolve() != path.absolute():
        raise SchemaError(f"Redirected Markdown file: {path.name}.")
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{path}: invalid UTF-8. Save the file as UTF-8.") from exc


def _recorded_time(value: str) -> datetime:
    return parse_timestamp(value) if value else datetime.min.replace(tzinfo=UTC)


class MemoryStore:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.vault = self.paths.vault_dir
        self.root = self.paths.memories_dir
        self.changes_path = self.vault / "records" / "memory-changes.jsonl"

    def ensure(self) -> None:
        if self.root.resolve() != self.root.absolute():
            raise SchemaError("Redirected memories directory.")
        self.root.mkdir(parents=True, exist_ok=True)

    def _files(self) -> list[Path]:
        if self.root.resolve() != self.root.absolute():
            raise SchemaError("Redirected memories directory.")
        pending = self.root / "pending.md"
        return sorted(path for path in self.root.rglob("*.md") if path != pending)

    def _topic(self, path: Path) -> str:
        return path.relative_to(self.vault).as_posix()

    def topic_path(self, topic: str) -> Path:
        parts = topic.strip().split("/")
        if not all(safe_component(part) for part in parts):
            raise ValueError("Invalid memory topic.")
        if parts[0] == "memories":
            if len(parts) < 3 or not parts[-1].endswith(".md"):
                raise ValueError("Use a topic such as general/reading.")
            path = self.vault.joinpath(*parts)
        elif len(parts) > 1:
            path = self.root.joinpath(*(slugify(p) for p in parts[:-1]), f"{slugify(parts[-1])}.md")
        else:
            slug = slugify(parts[0])
            matches = [p for p in self._files() if p.stem == slug]
            if len(matches) > 1:
                raise ValueError("Ambiguous topic; use its vault-relative path.")
            path = matches[0] if matches else self.root / "general" / f"{slug}.md"
        self._assert_path(path)
        return path

    def _assert_path(self, path: Path) -> None:
        if path == self.root / "pending.md":
            raise SchemaError("The pending queue is reserved for unreviewed drafts.")
        if (
            path.resolve() != path.absolute()
            or not path.is_relative_to(self.root)
            or not all(safe_component(p) for p in path.relative_to(self.root).parts)
        ):
            raise SchemaError("Invalid or redirected memory path.")

    def fingerprint(self) -> tuple[tuple[str, str], ...]:
        with vault_transaction(self.paths):
            items = []
            for path in self._files():
                self._assert_path(path)
                items.append((self._topic(path), hashlib.sha256(path.read_bytes()).hexdigest()))
            return tuple(items)

    def document(self, path: Path) -> Document:
        with vault_transaction(self.paths):
            self._assert_path(path)
            relative = self._topic(path)
            return parse_document(read_utf8(path), relative, relative)

    @staticmethod
    def new_entry_id(stamp: datetime | None = None) -> str:
        stamp = stamp or utc_now()
        return f"mem-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"

    def _change(self, tx, entry_id, change):
        path = self.changes_path
        row = json.dumps({"id": entry_id, "change": change}, ensure_ascii=False) + "\n"
        if path.resolve() != path.absolute():
            raise SchemaError("Redirected memory change log.")
        old = path.read_bytes() if path.exists() else b""
        if old and not old.endswith(b"\n"):
            raise SchemaError("Incomplete memory change log.")
        tracked_write(tx, path, old + row.encode("utf-8"))

    def append(
        self,
        topic: str,
        text: str,
        *,
        when: datetime | None = None,
        title: str | None = None,
        extra: dict[str, str] | None = None,
        entry_id: str | None = None,
        context: MemoryContext | None = None,
        supersedes: str | None = None,
    ) -> Entry:
        with vault_transaction(self.paths) as tx:
            stamp = when or utc_now()
            existing = {entry.id for entry in self.entries(include_inactive=True)}
            if entry_id is not None and entry_id in existing:
                raise ValueError("duplicate memory id")
            while entry_id is None or entry_id in existing:
                entry_id = self.new_entry_id(stamp)
            path = self.topic_path(topic)
            relative = self._topic(path)
            original = self.document(path).raw_markdown
            nl = "\r\n" if "\r\n" in original else "\n"
            extensions = dict(extra or {})
            extensions["recorded_at"] = serialize_timestamp(stamp)
            if supersedes is not None:
                if supersedes not in existing:
                    raise ValueError("Unknown superseded memory id.")
                extensions["supersedes"] = supersedes
            record = render_record(
                Core(id=entry_id, status=STATUS_ACTIVE, text=text),
                context.markdown_fields() if context else (),
                extensions.items(),
                nl,
            )
            output = append_record(original, record, title or path.stem, nl)
            parsed = parse_document(output, relative, relative)
            entry = parsed.records[-1].entry
            if entry.id != entry_id or entry.text != text:
                raise SchemaError(f"{relative}: failed to round-trip appended memory.")
            tracked_write(tx, path, output.encode("utf-8"))
            self._change(tx, entry_id, "created")
            return entry

    def entries(self, topic: str | None = None, include_inactive: bool = False) -> list[Entry]:
        with vault_transaction(self.paths):
            ids = set()
            result = []
            for path in [self.topic_path(topic)] if topic is not None else self._files():
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

    def update(
        self, entry_id: str, *, context: MemoryContext | None = None, status: str | None = None
    ) -> Entry:
        if status is not None and status not in STATUSES:
            raise ValueError("Invalid memory status.")
        with vault_transaction(self.paths) as tx:
            entry = self.find(entry_id)
            if entry is None:
                raise ValueError("Unknown memory id.")
            path = self.topic_path(entry.topic)
            doc = self.document(path)
            block = next(record.block for record in doc.records if record.entry.id == entry_id)
            raw = doc.raw_markdown
            fields = block.values("Context")
            add = {}
            for key, value in context.markdown_fields() if context else ():
                if key in fields and fields[key] != value:
                    raise ValueError(f"Context field {key} already has a value.")
                if key not in fields:
                    add[key] = value
            edits = []
            change = {}
            if status is not None and status != entry.status:
                left, right = block.positions["Core", "status"]
                edits.append((left, right, _span_code(status)))
                change["status"] = status
            if add:
                MemoryContext.model_validate(context_values(fields | add))
                nl = "\r\n" if "\r\n" in block.raw else "\n"
                pos = block.headings["Extensions"]
                insert = _render_fields(add.items(), "  ", nl) + nl
                if "Context" not in block.sections:
                    insert = "  **Context**" + nl + insert
                edits.append((pos, pos, insert))
                change["context"] = context_values(add)
            if not edits:
                return entry
            output = replacements(raw, edits)
            updated = next(
                r.entry
                for r in parse_document(output, entry.source_path, entry.topic).records
                if r.entry.id == entry_id
            )
            tracked_write(tx, path, output.encode("utf-8"))
            self._change(tx, entry_id, change)
            return updated

    def fill_context(self, entry_id: str, context: MemoryContext) -> Entry:
        return self.update(entry_id, context=context)

    def set_status(self, entry_id: str, status: str) -> bool:
        with vault_transaction(self.paths):
            if self.find(entry_id) is None:
                return False
            self.update(entry_id, status=status)
            return True

    def topics(self) -> list[TopicInfo]:
        with vault_transaction(self.paths):
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
                    TopicInfo(
                        topic=self._topic(path), title=title, entries=active, updated=modified
                    )
                )
            return result

    def topic_directory(self) -> list[dict[str, str]]:
        """只读取网页端所需的标题，无需解码每条记忆。"""
        with vault_transaction(self.paths):
            rows = []
            for path in self._files():
                self._assert_path(path)
                title = path.stem
                try:
                    with path.open(encoding="utf-8") as markdown:
                        for line in markdown:
                            if line.startswith("# "):
                                title = line[2:].strip() or title
                                break
                            if line.strip() and not line.startswith("#"):
                                break
                except UnicodeDecodeError as exc:
                    raise SchemaError(f"{path}: invalid UTF-8. Save the file as UTF-8.") from exc
                modified = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
                rows.append({"topic": self._topic(path), "title": title, "updated": modified})
            return rows

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
