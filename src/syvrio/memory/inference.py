from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..infra.paths import vault_paths
from .models import Entry, Inference


def evidence_v2(entry: Entry) -> str:
    def nodes(fields):
        return [
            (node.key, node.value, nodes(node.children))
            for node in fields
            if node.key not in {"migration.evidence_v1", "migration.evidence_v2"}
        ]

    payload = json.dumps(
        {
            "id": entry.id,
            "text": entry.text.replace("\r\n", "\n"),
            "status": entry.status,
            "timestamp": entry.timestamp,
            "supersedes": entry.supersedes,
            "metadata": entry.metadata,
            "context": nodes(entry.context_fields),
            "extensions": nodes(entry.extension_fields),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_fingerprint(entry: Entry) -> str:
    digest = evidence_v2(entry)
    markers = {
        node.key: node.value
        for node in entry.extension_fields
        if node.key in {"migration.evidence_v1", "migration.evidence_v2"}
    }
    previous = markers.get("migration.evidence_v1")
    if (
        previous
        and re.fullmatch(r"[0-9a-f]{64}", previous)
        and markers.get("migration.evidence_v2") == digest
    ):
        # A migrated record keeps its old evidence identity until its content changes.
        return previous
    return digest


class InferenceStore:
    """仅追加的独立推断记录；状态确认及来源失效处理由后续流程实现。"""

    def __init__(self, vault: Path) -> None:
        self.path = vault_paths(vault).records_dir / "inferences.jsonl"

    def all(self) -> list[Inference]:
        if not self.path.exists():
            return []
        records: dict[str, Inference] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = Inference.model_validate_json(line)
                records[item.id] = item
        return list(records.values())

    def add(self, item: Inference) -> Inference:
        existing = next((record for record in self.all() if record.id == item.id), None)
        if existing is not None:
            return existing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(item.model_dump_json() + "\n")
        return item
