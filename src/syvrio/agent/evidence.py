"""Bind a proposed write to the facts actually reviewed in a later model request."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from ..infra.transaction import vault_transaction
from ..memory.service import MemoryService, pending_view
from ..session.model import Session


def emotion_sources(session: Session, message_id: str) -> list[str]:
    """Only the active user's words and file pages actually returned for that message."""
    pages = []
    for row in reversed(session.active_records()):
        if row.get("type") == "message" and row.get("role") == "user":
            if row.get("id") == message_id:
                return [row["content"], *pages]
            break
        if row.get("type") == "tool" and row.get("name") == "read_file" and row.get("ok") is True:
            try:
                page = json.loads(row["content"])
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(page, dict) and isinstance(page.get("text"), str):
                pages.append(page["text"])
    return []


@dataclass(frozen=True)
class ReviewedEvidence:
    operation: str
    target_id: str | None
    payload_hash: str
    version: tuple
    generation: int
    model_round: int


class RecallContext:
    def __init__(self, service: MemoryService):
        self.service = service
        self.model_round = 0
        self.generation = 0
        self.reviewed: ReviewedEvidence | None = None

    def begin_model_request(self) -> None:
        self.model_round += 1

    def invalidate(self) -> None:
        self.generation += 1
        self.reviewed = None

    def run(
        self, operation: str, payload: dict, query: str, write: Callable[[], dict],
        *, target_id: str | None = None, include_pending: bool = False,
        related_ids: tuple[str, ...] = (), additional_queries: tuple[str, ...] = (),
    ) -> dict:
        token = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        service = self.service
        with vault_transaction(service.paths):
            version = service.evidence_version(include_pending=include_pending)
            prior = self.reviewed
            if (prior is not None and prior.operation == operation and prior.target_id == target_id
                and prior.payload_hash == token and prior.version == version
                and prior.generation == self.generation and prior.model_round < self.model_round):
                self.reviewed = None
                return write()
        self.reviewed = None
        # Network embeddings must not hold the Vault write lock. Register evidence only
        # if both the search and the directly referenced records came from the same version.
        for _ in range(3):
            facts = [item for question in dict.fromkeys((query, *additional_queries))
                     for item in service.recall(question)]
            with vault_transaction(service.paths):
                now = service.evidence_version(include_pending=include_pending)
                if now != version:
                    version = now
                    continue
                by_id = {fact.id: fact for fact in facts}
                for key in dict.fromkeys(([target_id] if target_id else []) + list(related_ids)):
                    entry = service.store.find(key)
                    if entry is None:
                        raise ValueError(f"Referenced memory does not exist: {key}")
                    by_id[key] = entry.as_recall()
                result = {
                    "status": "review_required",
                    "instruction": "Review these facts, then choose whether to issue the operation again.",
                    "results": [entry.model_dump(exclude_none=True) for entry in by_id.values()],
                }
                if include_pending:
                    result["pending"] = [pending_view(item) for item in service.pending()]
                self.reviewed = ReviewedEvidence(
                    operation, target_id, token, version, self.generation, self.model_round
                )
                return result
        raise ValueError("The facts changed during recall. Request fresh evidence.")
