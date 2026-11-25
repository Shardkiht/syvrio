from __future__ import annotations

from pathlib import Path

from ..infra.storage.database import transaction
from ..infra.storage.paths import SyvrioPaths, vault_paths
from .models import Entry, Inference
from .turn_undo import image, track


def source_version(entry: Entry) -> str:
    return str(entry.revision)


class InferenceStore:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)

    def all(self):
        with transaction(self.paths, write=False) as db:
            links = {}
            for source, key in db.execute(
                "SELECT inference_id,memory_id FROM inference_sources ORDER BY inference_id,position"
            ):
                links.setdefault(source, []).append(key)
            return [
                Inference(**dict(row), source_ids=links.get(row["id"], []))
                for row in db.execute(
                    "SELECT id,kind,status,text,created_at,updated_at FROM inferences ORDER BY kind,id"
                )
            ]

    def add(self, item):
        with transaction(self.paths):
            old = next((x for x in self.all() if x.id == item.id), None)
            if old is not None and old != item:
                raise ValueError("Duplicate inference id.")
            self.record(item)
            return item

    def record(self, *items):
        with transaction(self.paths) as db:
            seen = set()
            for item in items:
                item = Inference.model_validate(item.model_dump())
                if item.id in seen:
                    raise ValueError("Duplicate inference id in one revision.")
                seen.add(item.id)
                before = image(db, "inferences", item.id)
                if before:
                    if (
                        before["kind"],
                        before["text"],
                        before["created_at"],
                        sorted(x["memory_id"] for x in before["_links"]),
                    ) != (item.kind, item.text, item.created_at, item.source_ids):
                        raise ValueError("Editing an inference's content requires a new id.")
                    if (item.status, item.updated_at) == (before["status"], before["updated_at"]):
                        continue
                    db.execute(
                        "UPDATE inferences SET status=?,updated_at=?,revision=revision+1 WHERE id=?",
                        (item.status, item.updated_at, item.id),
                    )
                else:
                    if any(
                        not db.execute("SELECT 1 FROM memories WHERE id=?", (key,)).fetchone()
                        for key in item.source_ids
                    ):
                        raise ValueError("Unknown source memory.")
                    db.execute(
                        "INSERT INTO inferences(id,kind,text,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (
                            item.id,
                            item.kind,
                            item.text,
                            item.status,
                            item.created_at,
                            item.updated_at,
                        ),
                    )
                    db.executemany(
                        "INSERT INTO inference_sources(inference_id,memory_id,position) VALUES(?,?,?)",
                        [(item.id, key, i) for i, key in enumerate(item.source_ids)],
                    )
                track(db, "inferences", item.id, before=before)
