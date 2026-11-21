from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..infra.files import safe_component
from ..infra.paths import SyvrioPaths, vault_paths
from ..infra.transaction import vault_transaction
from .models import Entry, Inference
from .schema import (
    SchemaError,
    _span_code,
    append_record,
    parse_blocks,
    render_sections,
    replacements,
    validate_file_model,
)
from .store import MemoryStore, read_utf8, tracked_write


def source_fingerprint(entry: Entry) -> str:
    """Detect changes to a snapshot used for an asynchronous job."""
    record = entry.topic + "\x00" + entry.raw_markdown.rstrip("\r\n")
    return hashlib.sha256(record.encode("utf-8")).hexdigest()


class InferenceStore:
    def __init__(self, vault: Path | SyvrioPaths) -> None:
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.root = self.paths.vault_dir / "inferences"

    def path_for(self, kind: str, item_id: str) -> Path:
        if (
            not safe_component(kind)
            or not safe_component(item_id)
            or not re.fullmatch(r"[\w.-]+", item_id)
        ):
            raise ValueError("Invalid inference kind or id.")
        path = self.root / kind / (item_id + ".md")
        if path.resolve() != path.absolute():
            raise SchemaError("Redirected inference path.")
        return path

    def _paths(self) -> list[Path]:
        if self.root.resolve() != self.root.absolute():
            raise SchemaError("Redirected inferences directory.")
        paths = sorted(self.root.rglob("*.md"))
        if any(path.resolve() != path.absolute() for path in paths):
            raise SchemaError("Redirected inference path.")
        return paths

    def _read(self, path: Path):
        raw = read_utf8(path)
        return self._parse(path, raw)

    def decode(self, path: Path, payload: bytes) -> Inference:
        return self._parse(path, payload.decode("utf-8"))[0]

    def _parse(self, path: Path, raw: str):
        _, blocks = parse_blocks(raw, str(path.relative_to(self.paths.vault_dir)))
        if len(blocks) != 1:
            raise SchemaError("An inference file must contain exactly one record.")
        block = blocks[0]
        fields = block.values("Core")
        if set(fields) != {"id", "kind", "status", "text", "source_ids"} or block.sections.get(
            "Context"
        ):
            raise SchemaError("Invalid inference Core or Context.")
        extensions = block.extensions({"created_at", "updated_at"})
        item = validate_file_model(Inference, {**fields, **extensions}, str(path), block.line)
        expected = self.path_for(item.kind, item.id)
        if path.parts != expected.parts:
            raise SchemaError("Inference path must agree with its kind and id.")
        return item, block, raw

    def all(self) -> list[Inference]:
        with vault_transaction(self.paths):
            result = [self._read(path)[0] for path in self._paths()]
            if len({item.id for item in result}) != len(result):
                raise SchemaError("Duplicate inference id.")
            return result

    def fingerprint(self) -> str:
        with vault_transaction(self.paths):
            digest = hashlib.sha256()
            for path in self._paths():
                data = read_utf8(path).encode("utf-8")
                digest.update(path.relative_to(self.root).as_posix().encode("utf-8") + b"\x00")
                digest.update(hashlib.sha256(data).digest())
            return digest.hexdigest()

    @staticmethod
    def _render(item: Inference) -> str:
        core = [(k, getattr(item, k)) for k in ("id", "status", "text", "kind", "source_ids")]
        extensions = [("created_at", item.created_at), ("updated_at", item.updated_at)]
        return append_record("", render_sections(core, extensions=extensions), item.kind, "\n")

    def add(self, item: Inference) -> Inference:
        with vault_transaction(self.paths):
            old = next((i for i in self.all() if i.id == item.id), None)
            if old is not None and old != item:
                raise ValueError("Duplicate inference id.")
            self.record(item)
            return item

    def record(self, *items: Inference) -> None:
        with vault_transaction(self.paths) as tx:
            existing = {i.id: i for i in self.all()}
            facts = {entry.id for entry in MemoryStore(self.paths).entries(include_inactive=True)}
            seen = set()
            for item in items:
                item = Inference.model_validate(item.model_dump())
                path = self.path_for(item.kind, item.id)
                if item.id in seen:
                    raise ValueError("Duplicate inference id in one revision.")
                seen.add(item.id)
                if missing := set(item.source_ids) - facts:
                    raise ValueError(f"Unknown source memory: {', '.join(sorted(missing))}.")
                old = existing.get(item.id)
                if old == item:
                    continue
                if old is None:
                    if path.exists():
                        raise SchemaError("An inference already occupies this path.")
                    output = self._render(item)
                else:
                    mutable = {"status", "updated_at"}
                    if item.model_dump(exclude=mutable) != old.model_dump(exclude=mutable):
                        raise ValueError("Editing an inference's content requires a new id.")
                    _, block, raw = self._read(path)
                    changes = []
                    for section, key in (("Core", "status"), ("Extensions", "updated_at")):
                        if getattr(old, key) != getattr(item, key):
                            start, end = block.positions[section, key]
                            changes.append((start, end, _span_code(getattr(item, key))))
                    output = replacements(raw, changes)
                tracked_write(tx, path, output.encode("utf-8"))
                if self._read(path)[0] != item:
                    raise SchemaError("Inference failed to round-trip.")
