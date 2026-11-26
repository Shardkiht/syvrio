from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..infra.session.errors import SessionDeletionChanged
from ..infra.storage.database import encode, transaction
from ..infra.storage.paths import SyvrioPaths, vault_paths
from .models import MemoryContext, PendingItem
from .source_links import CANDIDATE_SOURCES
from .undo_store import image, track


class PendingQueue:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)

    @staticmethod
    def _item(db, row):
        return PendingItem(
            **{
                k: row[k]
                for k in (
                    "id",
                    "topic",
                    "text",
                    "question",
                    "created_at",
                    "session_id",
                    "message_id",
                )
            },
            context=json.loads(row["context"]) if row["context"] else None,
            related_ids=[
                r[0]
                for r in db.execute(
                    "SELECT memory_id FROM candidate_related WHERE candidate_id=? ORDER BY position",
                    (row["id"],),
                )
            ],
        )

    def all(self):
        with transaction(self.paths, write=False) as db:
            return [
                self._item(db, row)
                for row in db.execute(
                    "SELECT * FROM candidates WHERE state='pending' ORDER BY rowid"
                )
            ]

    def count(self) -> int:
        with transaction(self.paths, write=False) as db:
            return db.execute("SELECT count(*) FROM candidates WHERE state='pending'").fetchone()[0]

    def get(self, item_id):
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                "SELECT * FROM candidates WHERE id=? AND state='pending'", (item_id,)
            ).fetchone()
            return self._item(db, row) if row else None

    def add_unique(self, item: PendingItem) -> PendingItem:
        with transaction(self.paths) as db:
            rows = db.execute(
                """SELECT * FROM candidates WHERE state='pending' AND session_id IS ?
                   AND message_id IS ? AND topic=? AND text=? AND question=?
                   AND (SELECT count(*) FROM candidate_related WHERE candidate_id=candidates.id)=?
                   AND NOT EXISTS(
                       SELECT memory_id FROM candidate_related WHERE candidate_id=candidates.id
                       EXCEPT SELECT value FROM json_each(?)
                   ) ORDER BY rowid""",
                (
                    item.session_id,
                    item.message_id,
                    item.topic,
                    item.text,
                    item.question,
                    len(item.related_ids),
                    encode(item.related_ids),
                ),
            )
            for row in rows:
                context = (
                    MemoryContext.model_validate_json(row["context"]) if row["context"] else None
                )
                if context == item.context:
                    return self._item(db, row)
            self.add(item)
            return item

    def add(self, item):
        item = PendingItem.model_validate(item.model_dump())
        with transaction(self.paths) as db:
            if self.get(item.id) == item:
                return
            if db.execute("SELECT 1 FROM candidates WHERE id=?", (item.id,)).fetchone():
                raise ValueError("Duplicate pending id.")
            db.execute(
                """INSERT INTO candidates(id,topic,text,question,created_at,state,context,session_id,message_id)
                       VALUES(?,?,?,?,?,'pending',?,?,?)""",
                (
                    item.id,
                    item.topic,
                    item.text,
                    item.question,
                    item.created_at,
                    item.context.model_dump_json(exclude_none=True) if item.context else None,
                    item.session_id,
                    item.message_id,
                ),
            )
            CANDIDATE_SOURCES.add(db, item.id, item.related_ids)
            track(db, "candidates", item.id, before=None)

    def resolve(self, item_id, status):
        if status not in {"approved", "rejected"}:
            raise ValueError("Invalid pending resolution.")
        with transaction(self.paths) as db:
            item = self.get(item_id)
            if item:
                before = image(db, "candidates", item_id)
                if status == "approved":
                    db.execute("DELETE FROM candidates WHERE id=?", (item_id,))
                else:
                    db.execute(
                        "UPDATE candidates SET state='rejected',revision=revision+1 WHERE id=?",
                        (item_id,),
                    )
                track(db, "candidates", item_id, before=before)
            return item

    def deletion_preview(self, session_id):
        with transaction(self.paths, write=False) as db:
            rows = db.execute(
                "SELECT * FROM candidates WHERE state='pending' AND session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [self._item(db, r) for r in rows], hashlib.sha256(
                encode([(r["id"], r["revision"]) for r in rows]).encode()
            ).hexdigest() if rows else ""

    def remove_for_session(self, session_id, revision):
        with transaction(self.paths) as db:
            if self.deletion_preview(session_id)[1] != revision:
                raise SessionDeletionChanged(
                    "The related pending items changed. Review the updated list."
                )
            db.execute(
                "DELETE FROM candidates WHERE state='pending' AND session_id=?", (session_id,)
            )
