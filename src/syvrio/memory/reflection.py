"""Latest published profile. Only Dream publishes new evidence snapshots."""

import json
import sqlite3
from dataclasses import dataclass

from ..infra.storage.database import DatabaseError, encode, transaction


@dataclass(frozen=True)
class ProfileSummaryView:
    text: str
    generated_at: str
    fact_count: int
    unique_count: int


class ProfileReader:
    def __init__(self, paths):
        self.paths = paths

    def _published(self):
        try:
            with transaction(self.paths, write=False) as db:
                row = db.execute("SELECT body,metadata FROM profile WHERE singleton=1").fetchone()
                return (row[0], json.loads(row[1])) if row else ("", {})
        except sqlite3.DatabaseError as exc:
            raise DatabaseError("Could not read the published profile.") from exc

    def read_text(self):
        return self._published()[0]

    def metadata(self):
        return self._published()[1]

    def publish(self, body, metadata):
        with transaction(self.paths) as db:
            db.execute(
                "INSERT INTO profile VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET body=excluded.body, metadata=excluded.metadata",
                (body, encode(metadata)),
            )

    def profile_summary(self):
        body, meta = self._published()
        return (
            ProfileSummaryView(
                body,
                meta.get("generated_at", ""),
                meta.get("fact_count", 0),
                meta.get("unique_count", 0),
            )
            if body
            else None
        )
