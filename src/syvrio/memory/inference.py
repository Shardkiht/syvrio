from __future__ import annotations

from pathlib import Path

from ..infra.storage import evidence_versions
from ..infra.storage.database import transaction
from ..infra.storage.paths import SyvrioPaths, vault_paths
from .models import Entry, Inference
from .undo_store import image, track


def source_version(entry: Entry) -> str:
    return str(entry.revision)


class InferenceStore:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)

    def all(self, *, at: int | None = None):
        with transaction(self.paths, write=False) as db:
            links = {}
            versions = {}
            for source, key, version in db.execute(
                "SELECT inference_id,memory_id,memory_version FROM inference_sources ORDER BY inference_id,position"
            ):
                links.setdefault(source, []).append(key)
                if version is not None:
                    versions.setdefault(source, {})[key] = version
            if at is None:
                rows = db.execute(
                    "SELECT id,kind,status,text,created_at,updated_at FROM inferences ORDER BY kind,id"
                )
            else:
                rows = db.execute(
                    """SELECT v.inference_id AS id, v.kind, v.status, v.text,
                    v.created_at, v.updated_at FROM inference_versions v
                    WHERE v.commit_id=(SELECT max(h.commit_id) FROM inference_versions h
                    WHERE h.inference_id=v.inference_id AND h.commit_id<=?)
                    ORDER BY v.kind,v.inference_id""",
                    (at,),
                )
            return [
                Inference(
                    **dict(row),
                    source_ids=links.get(row["id"], []),
                    source_versions=versions.get(row["id"], {}),
                )
                for row in rows
            ]

    def source_references(self, item_id: str) -> list[tuple[str, int | None]]:
        with transaction(self.paths, write=False) as db:
            if not db.execute("SELECT 1 FROM inferences WHERE id=?", (item_id,)).fetchone():
                raise KeyError(item_id)
            return [
                (row[0], row[1])
                for row in db.execute(
                    """SELECT memory_id,memory_version FROM inference_sources
                       WHERE inference_id=? ORDER BY position""",
                    (item_id,),
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
            evidence_versions.group_changes(db)
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
                        {
                            x["memory_id"]: x["memory_version"]
                            for x in before["_links"]
                            if x["memory_version"] is not None
                        },
                    ) != (
                        item.kind,
                        item.text,
                        item.created_at,
                        item.source_ids,
                        item.source_versions,
                    ):
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
                        """INSERT INTO inference_sources
                        (inference_id,position,memory_id,memory_version) VALUES(?,?,?,?)""",
                        [
                            (item.id, i, key, item.source_versions.get(key))
                            for i, key in enumerate(item.source_ids)
                        ],
                    )
                track(db, "inferences", item.id, before=before)
