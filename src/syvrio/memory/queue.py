from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..infra.session.errors import SessionDeletionChanged
from ..infra.storage.paths import SyvrioPaths, vault_paths
from ..infra.storage.transaction import vault_transaction
from .models import PendingItem
from .schema import (
    SchemaError,
    append_record,
    context_values,
    parse_blocks,
    render_sections,
    replacements,
    validate_file_model,
)
from .store import read_utf8, tracked_write


class PendingQueue:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.vault = self.paths.vault_dir
        self.pending_path = self.paths.memories_dir / "pending.md"
        self.resolved_path = self.paths.archived_dir / "pending" / "rejected.md"

    @staticmethod
    def _records(raw: str, name: str):
        _, blocks = parse_blocks(raw, name)
        records = []
        ids = set()
        for block in blocks:
            core = block.values("Core")
            if set(core) - {"id", "topic", "text", "question", "related_ids"}:
                raise SchemaError(f"{name}:{block.line}: invalid pending Core.")
            fields = block.values("Context")
            extras = block.extensions({"created_at", "session_id", "message_id"})
            if "created_at" not in extras or "id" not in core:
                raise SchemaError(f"{name}:{block.line}: missing pending identifier or timestamp.")
            item = validate_file_model(
                PendingItem,
                {**core, **extras, "context": context_values(fields) or None},
                name,
                block.line,
            )
            if item.id in ids:
                raise SchemaError(f"{name}:{block.line}: duplicate pending id.")
            ids.add(item.id)
            records.append((item, block))
        return records

    @classmethod
    def decode(cls, payload: bytes) -> list[PendingItem]:
        return [item for item, _ in cls._records(payload.decode("utf-8"), "pending.md")]

    @staticmethod
    def _render(item: PendingItem, nl: str) -> str:
        core = [(k, getattr(item, k)) for k in ("id", "text", "topic", "question", "related_ids")]
        extensions = [
            (k, getattr(item, k))
            for k in ("created_at", "session_id", "message_id")
            if getattr(item, k) is not None
        ]
        return render_sections(
            core, item.context.markdown_fields() if item.context else (), extensions, nl
        )

    def _active(self):
        raw = read_utf8(self.pending_path)
        return raw, self._records(raw, "memories/pending.md")

    @staticmethod
    def _revision(records) -> str:
        if not records:
            return ""
        rows = [block.raw.rstrip("\r\n") for _, block in records]
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()

    def deletion_preview(self, session_id: str) -> tuple[list[PendingItem], str]:
        with vault_transaction(self.paths):
            _, records = self._active()
            related = [(i, block) for i, block in records if i.session_id == session_id]
            return [i for i, _ in related], self._revision(related)

    def remove_for_session(self, session_id: str, revision: str) -> None:
        with vault_transaction(self.paths) as tx:
            raw, records = self._active()
            related = [(item, block) for item, block in records if item.session_id == session_id]
            if revision != self._revision(related):
                raise SessionDeletionChanged(
                    "The related pending items changed. Review the updated list."
                )
            if related:
                prepared = replacements(raw, [(b.start, b.end, "") for _, b in related])
                tx.write(self.pending_path, prepared.encode("utf-8"))

    def all(self) -> list[PendingItem]:
        with vault_transaction(self.paths):
            return [item for item, _ in self._active()[1]]

    def get(self, item_id: str) -> PendingItem | None:
        return next((item for item in self.all() if item.id == item_id), None)

    def add(self, item: PendingItem) -> None:
        with vault_transaction(self.paths) as tx:
            raw, records = self._active()
            item = PendingItem.model_validate(item.model_dump())
            existing = next((i for i, _ in records if i.id == item.id), None)
            if existing == item:
                return
            archived = self._records(read_utf8(self.resolved_path), "archived/pending/rejected.md")
            if existing or any(i.id == item.id for i, _ in archived):
                raise ValueError("Duplicate pending id.")
            nl = "\r\n" if "\r\n" in raw else "\n"
            output = append_record(raw, self._render(item, nl), "Pending", nl)
            if self._records(output, "memories/pending.md")[-1][0] != item:
                raise SchemaError("Pending item failed to round-trip.")
            tracked_write(tx, self.pending_path, output.encode("utf-8"))

    def resolve(self, item_id: str, status: str) -> PendingItem | None:
        if status not in {"approved", "rejected"}:
            raise ValueError("Invalid pending resolution.")
        with vault_transaction(self.paths) as tx:
            raw, records = self._active()
            result = next(((item, block) for item, block in records if item.id == item_id), None)
            if result is None:
                return None
            item, block = result
            if status == "rejected":
                archive = read_utf8(self.resolved_path)
                if any(
                    i.id == item_id
                    for i, _ in self._records(archive, "archived/pending/rejected.md")
                ):
                    raise SchemaError("Pending id already exists in the archive.")
                nl = "\r\n" if "\r\n" in (archive or raw) else "\n"
                output = append_record(archive, block.raw, "Rejected pending items", nl)
                tracked_write(tx, self.resolved_path, output.encode("utf-8"))
            output = raw[: block.start] + raw[block.end :]
            tracked_write(tx, self.pending_path, output.encode("utf-8"))
            return item
