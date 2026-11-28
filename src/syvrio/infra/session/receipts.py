"""持久化消息送达回执，预留与读取已有回执是同一原子操作。"""

from __future__ import annotations

from dataclasses import dataclass

from ..storage.database import transaction
from ..storage.paths import SyvrioPaths


@dataclass(frozen=True)
class DeliveryReceipt:
    request_digest: str
    instance: str
    session_id: str | None
    turn_id: int | None


class ReceiptStore:
    def __init__(self, paths: SyvrioPaths):
        self.paths = paths

    def reserve(self, key: str, digest: str, instance: str) -> DeliveryReceipt | None:
        with transaction(self.paths) as db:
            row = db.execute(
                "SELECT request_digest,instance,session_id,turn_id FROM receipts WHERE submission_id=?",
                (key,),
            ).fetchone()
            if row is not None:
                return DeliveryReceipt(*row)
            db.execute(
                "INSERT INTO receipts(submission_id,request_digest,instance) VALUES(?,?,?)",
                (key, digest, instance),
            )
            return None

    def release(self, key: str) -> None:
        with transaction(self.paths) as db:
            db.execute("DELETE FROM receipts WHERE submission_id=?", (key,))

    def complete(self, key: str, session_id: str, turn_id: int) -> None:
        with transaction(self.paths) as db:
            updated = db.execute(
                """UPDATE receipts SET session_id=?,turn_id=? WHERE submission_id=?
                   AND session_id IS NULL AND turn_id IS NULL""",
                (session_id, turn_id, key),
            )
            if updated.rowcount != 1:
                raise ValueError("A pending delivery reservation is required.")
