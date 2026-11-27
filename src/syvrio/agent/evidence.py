"""将拟写入的内容与后续模型请求实际核对过的事实关联。"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..infra.errors import SyvrioError
from ..infra.session.model import Session
from ..memory.service import MemoryService, pending_view


def emotion_sources(session: Session, message_id: str) -> list[str]:
    """仅使用当前用户的原话，以及为该消息实际返回的文件页。"""
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


class ProposalError(SyvrioError, ValueError):
    pass


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    proposal_id: str = Field(min_length=1)
    decision: Literal["memory", "pending", "skip"]
    question: str | None = Field(default=None, min_length=1)


@dataclass
class ChatProposal:
    id: str
    operation: str
    draft: dict
    target_id: str | None
    include_pending: bool
    read: Callable[[], tuple[tuple, dict]]
    write: Callable[[ProposalDecision], dict]
    version: tuple = ()
    shown_round: int = -1
    evidence: dict | None = None
    receipt: dict | None = None


class RecallContext:
    def __init__(self, service: MemoryService):
        self.service = service
        self.model_round = 0
        self.generation = 0
        self.reviewed: ReviewedEvidence | None = None
        self._by_id: dict[str, ChatProposal] = {}
        self._by_draft: dict[tuple[str, str], str] = {}

    def begin_model_request(self) -> None:
        self.model_round += 1

    def invalidate(self, *, new_message: bool = False) -> None:
        self.generation += 1
        self.reviewed = None

        if new_message:
            self._by_id.clear()
            self._by_draft.clear()
        else:
            # 压缩可能移除已展示的证据；已提交的回执仍用于处理重试。
            for proposal in self._by_id.values():
                proposal.evidence = None

    def propose(
        self,
        operation: str,
        payload: dict,
        query: Callable[[], str],
        write: Callable[[ProposalDecision], dict],
        *,
        target_id: str | None,
        include_pending: bool,
        related_ids: tuple[str, ...],
    ) -> dict:
        key = operation, json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if key in self._by_draft:
            proposal = self._by_id[self._by_draft[key]]
        else:
            proposal = ChatProposal(
                secrets.token_hex(8),
                operation,
                payload,
                target_id,
                include_pending,
                lambda: self.read_evidence(query, target_id, include_pending, related_ids),
                write,
            )
            self._by_id[proposal.id] = proposal
            self._by_draft[key] = proposal.id
        return proposal.receipt or self._show(proposal)

    def _show(self, proposal: ChatProposal) -> dict:
        if proposal.evidence is None or proposal.version != self.service.evidence_version(
            include_pending=proposal.include_pending
        ):
            proposal.version, facts = proposal.read()
            proposal.evidence = {
                **facts,
                "status": "review_required",
                "proposal_id": proposal.id,
                "operation": proposal.operation,
                "draft": proposal.draft,
                "instruction": "Review the evidence, then call resolve_memory_proposals with this ID.",
            }
            proposal.shown_round = self.model_round
        return proposal.evidence

    def resolve(self, decisions: list[ProposalDecision]) -> dict:
        selected = {}
        for decision in decisions:
            key = decision.proposal_id
            if key not in self._by_id:
                raise ProposalError(
                    "Unknown proposal for this message. Submit the draft for review."
                )
            if key in selected and selected[key] != decision:
                raise ProposalError("Choose one decision per proposal.")
            selected[key] = decision
        receipts = {}
        refresh = []
        with self.service.work.write():
            # 一批裁决共同审阅一个提交版本；全部校验后才开始写，失败则整批回滚。
            for key in selected:
                proposal = self._by_id[key]
                if proposal.receipt is not None:
                    receipts[key] = proposal.receipt
                elif (
                    proposal.evidence is None
                    or proposal.shown_round >= self.model_round
                    or proposal.version
                    != self.service.evidence_version(include_pending=proposal.include_pending)
                ):
                    refresh.append(proposal)
            if not refresh:
                changed_targets = set()
                for key, decision in selected.items():
                    if key in receipts:
                        continue
                    proposal = self._by_id[key]
                    if decision.decision == "memory" and proposal.target_id:
                        if proposal.target_id in changed_targets:
                            raise ProposalError(
                                "Resolve competing changes to one fact individually."
                            )
                        changed_targets.add(proposal.target_id)
                    result = {} if decision.decision == "skip" else proposal.write(decision)
                    receipts[key] = {"proposal_id": key, "decision": decision.decision, **result}
        if refresh:
            return {
                "status": "review_required",
                "proposals": [self._show(p) for p in refresh],
                "instruction": "Review the updated evidence, then resubmit the decisions by ID.",
            }
        # 只缓存已提交事务的回执。重复确认、重复提案均不会再次写入。
        for key, receipt in receipts.items():
            self._by_id[key].receipt = receipt
        return {"decisions": [receipts[key] for key in selected]}

    def run(
        self,
        operation: str,
        payload: dict,
        query: str,
        write: Callable[[], dict],
        *,
        target_id: str | None = None,
        include_pending: bool = False,
        related_ids: tuple[str, ...] = (),
        additional_queries: tuple[str, ...] = (),
    ) -> dict:
        token = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        service = self.service
        prior = self.reviewed
        if (
            prior is not None
            and prior.operation == operation
            and prior.target_id == target_id
            and prior.payload_hash == token
            and prior.generation == self.generation
            and prior.model_round < self.model_round
        ):
            with service.work.write():
                version = service.evidence_version(include_pending=include_pending)
                if prior.version == version:
                    self.reviewed = None
                    return write()
        self.reviewed = None
        version, result = self.read_evidence(
            query, target_id, include_pending, related_ids, additional_queries
        )
        self.reviewed = ReviewedEvidence(
            operation, target_id, token, version, self.generation, self.model_round
        )
        return {
            **result,
            "status": "review_required",
            "instruction": "Review these facts, then choose whether to issue the operation again.",
        }

    def read_evidence(
        self,
        query: str | Callable[[], str],
        target_id: str | None,
        include_pending: bool,
        related_ids: tuple[str, ...],
        additional_queries: tuple[str, ...] = (),
    ) -> tuple[tuple, dict]:
        service = self.service
        version = service.evidence_version(include_pending=include_pending)
        # 调用远程嵌入服务时释放 Vault 写锁。只有检索结果与直接引用的记录
        # 来自同一版本时，才登记证据。
        for _ in range(3):
            primary = query() if callable(query) else query
            facts = [
                item
                for question in dict.fromkeys((primary, *additional_queries))
                for item in service.recall(question)
            ]
            with service.work.read():
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
                    "results": [entry.model_dump(exclude_none=True) for entry in by_id.values()],
                }
                if include_pending:
                    result["pending"] = [pending_view(item) for item in service.pending()]
                return version, result
        raise ProposalError("The facts changed during recall. Request fresh evidence.")
