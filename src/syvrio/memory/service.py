from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from pathlib import Path
from uuid import uuid4

from ..infra.config import (
    Config,
    LLMConfig,
    RetrievalConfig,
    default_config_path,
    load_config,
)
from ..infra.errors import SyvrioError
from ..infra.llm import LiteLLMEmbedder
from ..infra.retrieval import (
    Embedder,
    HybridRetriever,
    KeywordRetriever,
    LocalEmbedder,
    Retriever,
    VectorRetriever,
)
from ..infra.security.secrets import contains_secret
from ..infra.session.query import SessionQuery
from ..infra.storage.work import VaultWork
from ..infra.time import utc_timestamp
from .audit import AuditLog
from .background.jobs import enqueue_context_fill
from .extractor import ImportExtractor
from .inference import InferenceStore, source_version
from .models import (
    Candidate,
    Entry,
    Inference,
    InferenceKind,
    MemoryContext,
    MemoryDraft,
    MemoryUpdate,
    PendingItem,
    RecallEntry,
    TimeRange,
)
from .queue import PendingQueue
from .reflection import ProfileReader
from .store import (
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    MemorySnapshot,
    MemoryStore,
    normalize_text,
)
from .temporal import date_bounds, matches_context


def _build_embedder(config: RetrievalConfig) -> Embedder | None:
    """按配置构造 Embedder；后端不可用返回 None，由调用方回退 keyword。"""
    embedding = config.embedding
    if embedding.backend == "api":
        llm = LLMConfig(
            provider=embedding.provider,
            base_url=embedding.base_url,
            model=embedding.model,
            api_key_env=embedding.api_key_env,
        )
        if llm.requires_api_key() and not llm.api_key():
            return None
        return LiteLLMEmbedder(llm)
    if not LocalEmbedder.available():
        return None
    return LocalEmbedder(embedding.model)


def build_retriever(config: Config, store: MemoryStore) -> tuple[Retriever, str]:
    """按 [retrieval] 装配检索器；返回检索器和模式标签，回退时标签注明。"""
    method = (config.retrieval.method or "hybrid").strip().lower()
    keyword = KeywordRetriever(store)
    if method == "keyword":
        return keyword, "keyword"
    embedder = _build_embedder(config.retrieval)
    if embedder is None:
        return keyword, f"{method}-fallback-keyword"
    vector = VectorRetriever(store, embedder, paths=store.paths, current_revision=store.revision)
    if method == "vector":
        return vector, "vector"
    return HybridRetriever(keyword, vector), "hybrid"


@dataclass(slots=True)
class MemorizeResult:
    admitted: list[Entry]
    queued: list[PendingItem]
    duplicates: list[str]
    blocked: list[str]

    def to_dict(self) -> dict:
        return {
            "admitted": [
                entry.as_recall().model_dump(exclude_none=True) for entry in self.admitted
            ],
            "queued": [pending_view(item) for item in self.queued],
            "duplicates": self.duplicates,
            "blocked": self.blocked,
        }


def pending_view(item: PendingItem) -> dict:
    return item.model_dump(exclude={"created_at", "session_id", "message_id"}, exclude_none=True)


class MemoryTargetError(SyvrioError, ValueError):
    """目标记忆不存在或当前状态不允许本次操作。"""


class MemoryService:
    def __init__(
        self,
        config: Config,
        *,
        retriever: Retriever | None = None,
        config_path: Path | None = None,
    ):
        self.config = config
        self.paths = config.paths(config_path)
        self.paths.validate()
        self.work = VaultWork(self.paths)
        self.store = MemoryStore(self.paths)
        self.session_query = SessionQuery(self.paths)
        self.queue = PendingQueue(self.paths)
        self.inferences = InferenceStore(self.paths)
        self.audit = AuditLog(self.paths)
        if retriever is not None:
            self.retriever: Retriever = retriever
            self.retrieval_mode = "custom"
        else:
            self.retriever, self.retrieval_mode = build_retriever(config, self.store)
        # 热更新（切模型/上下文）写回配置的路径：记住加载来源，避免写错文件或被读回旧值。
        self.config_path = config_path
        self.profile = ProfileReader(self.paths)

    def initialize_files(self) -> None:
        self.store.ensure()

    def add(
        self,
        draft: MemoryDraft,
        *,
        session_id: str | None = None,
        message_id: str | None = None,
        entry_id: str | None = None,
    ) -> Entry:
        return self.apply(draft, session_id=session_id, message_id=message_id, entry_id=entry_id)[0]

    def apply(
        self,
        add: MemoryDraft | None = None,
        updates: list[MemoryUpdate] | None = None,
        *,
        session_id: str | None = None,
        message_id: str | None = None,
        entry_id: str | None = None,
        supersedes: str | None = None,
    ) -> list[Entry]:
        draft = MemoryDraft.model_validate(add) if add is not None else None
        changes = [MemoryUpdate.model_validate(update) for update in (updates or [])]
        if draft is None and not changes:
            raise ValueError("Provide a memory draft or at least one update.")
        if len({change.id for change in changes}) != len(changes):
            raise ValueError("Combine updates for the same memory ID.")
        with self.work.write():
            source = self._source_fields(session_id, message_id)
            result = []
            if draft is not None:
                self.validate_draft(draft)
                if supersedes is not None:
                    self.require_entry(supersedes)
                if entry_id is None and session_id is not None:
                    identity = [
                        session_id,
                        message_id,
                        self.store.topic_name(draft.topic),
                        draft.model_dump(exclude={"topic"}, exclude_none=True),
                        supersedes,
                    ]
                    key = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    entry_id = "mem-" + hashlib.sha256(key).hexdigest()[:24]
                existing = self.store.find(entry_id) if entry_id is not None else None
                if existing is None:
                    new = self.store.append(
                        draft.topic,
                        draft.text,
                        context=draft.context,
                        extra=source,
                        entry_id=entry_id,
                        supersedes=supersedes,
                    )
                    self.audit.write("add", memory_id=new.id)
                else:
                    if (
                        existing.text != draft.text
                        or existing.context != draft.context
                        or self.store.topic_name(existing.topic)
                        != self.store.topic_name(draft.topic)
                        or existing.source_session_id != session_id
                        or existing.source_message_id != message_id
                        or existing.supersedes != supersedes
                    ):
                        raise ValueError("The memory ID already identifies a different fact.")
                    new = existing
                result.append(new)
            for change in changes:
                if change.context and contains_secret(change.context.model_dump_json()):
                    raise ValueError("Credentials cannot be stored in memory context.")
                old = self.store.find(change.id)
                if old is None:
                    raise KeyError(f"Memory not found: {change.id}")
                updated = self.store.update(change.id, context=change.context, status=change.status)
                if updated.context != old.context or updated.status != old.status:
                    self.audit.write("update", memory_id=updated.id)
                result.append(updated)
            return result

    def validate_draft(self, draft: MemoryDraft) -> None:
        self.store.topic_name(draft.topic)
        if contains_secret(draft.model_dump_json(exclude_none=True)):
            raise ValueError("检测到凭据，已拦截写入。")

    def require_entry(self, entry_id: str, *, include_deprecated: bool = False) -> Entry:
        entry = self.store.find(entry_id)
        if entry is not None and (include_deprecated or entry.status == STATUS_ACTIVE):
            return entry
        if include_deprecated:
            raise MemoryTargetError("The target memory does not exist.")
        raise MemoryTargetError("The target must be an active memory.")

    def memorize(self, draft: MemoryDraft, **source) -> Entry:
        return self.add(draft, **source)

    def correct(self, entry_id: str, draft: MemoryDraft, **source) -> Entry:
        with self.work.write():
            old = self.require_entry(entry_id)
            draft = MemoryDraft.model_validate(draft)
            if draft.text == old.text:
                raise ValueError("A correction requires a changed fact.")
            return self.apply(
                draft,
                [MemoryUpdate(id=entry_id, status=STATUS_DEPRECATED)],
                supersedes=entry_id,
                **source,
            )[0]

    def transition(
        self,
        entry_id: str,
        draft: MemoryDraft,
        *,
        previous_valid_to: str | None = None,
        **source,
    ) -> Entry:
        with self.work.write():
            self.require_entry(entry_id)
            changes = []
            if previous_valid_to is not None:
                changes.append(
                    MemoryUpdate(id=entry_id, context=MemoryContext(valid_to=previous_valid_to))
                )
            return self.apply(draft, changes, supersedes=entry_id, **source)[0]

    def deprecate(self, entry_id: str, **source) -> Entry:
        return self.apply(updates=[MemoryUpdate(id=entry_id, status=STATUS_DEPRECATED)], **source)[
            0
        ]

    def note(self, text: str, topic: str = "uncategorized") -> Entry:
        return self.add(MemoryDraft(text=text, topic=topic))

    def create_pending(
        self,
        draft: MemoryDraft,
        question: str,
        related_ids: list[str],
        *,
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> PendingItem:
        draft = MemoryDraft.model_validate(draft)
        self.validate_draft(draft)
        item = PendingItem(
            **draft.model_dump(),
            question=question,
            related_ids=related_ids,
            session_id=session_id,
            message_id=message_id,
        )
        if contains_secret(item.question):
            raise ValueError("Credentials cannot be stored in a pending question.")
        with self.work.write():
            self._source_fields(session_id, message_id)
            if any(self.store.find(key) is None for key in item.related_ids):
                raise ValueError("Pending references must identify existing memories.")
            return self.queue.add_unique(item)

    def prepare_import(
        self, text: str, extractor=None, *, llm_config: LLMConfig | None = None
    ) -> tuple[tuple, list[Candidate]]:
        if not text.strip():
            raise ValueError("导入内容为空。")
        if extractor is None:
            extractor = ImportExtractor(
                llm_config or self.config.llm, topics=[item.topic for item in self.store.topics()]
            )
        # 提取过程可能调用远程供应商，接受草稿前再次核验证据。
        for _ in range(3):
            version = self.evidence_version(include_pending=True)
            evidence = {
                "memories": [item.model_dump(exclude_none=True) for item in self.recall(text)],
                "pending": [pending_view(item) for item in self.pending()],
            }
            candidates = extractor.extract(text, json.dumps(evidence, ensure_ascii=False)).memories
            if self.evidence_version(include_pending=True) == version:
                return version, candidates
        raise ValueError("Memory changed during document review. Retry the import.")

    def import_text(
        self,
        text: str,
        *,
        source: str = "import",
        dry_run: bool = False,
        extractor=None,
    ) -> MemorizeResult:
        version, candidates = self.prepare_import(text, extractor)
        return self.apply_import(candidates, version, dry_run=dry_run)

    def apply_import(
        self,
        candidates: list[Candidate],
        version: tuple,
        *,
        dry_run: bool = False,
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> MemorizeResult:
        with self.work.read() if dry_run else self.work.write():
            if self.evidence_version(include_pending=True) != version:
                raise ValueError("Memory changed after document review. Request fresh evidence.")
            return self._import_candidates(candidates, dry_run, session_id, message_id)

    def _import_candidates(
        self,
        candidates: list[Candidate],
        dry_run: bool,
        session_id: str | None,
        message_id: str | None,
    ) -> MemorizeResult:
        result = MemorizeResult([], [], [], [])
        existing = {
            (entry.text, entry.context.model_dump_json() if entry.context else None)
            for entry in self.store.entries()
        }
        for item in candidates:
            draft = MemoryDraft(**item.model_dump(include={"text", "topic", "context"}))
            try:
                self.validate_draft(draft)
            except ValueError as exc:
                result.blocked.append(str(exc))
                continue
            identity = (draft.text, draft.context.model_dump_json() if draft.context else None)
            if identity in existing:
                result.duplicates.append(draft.text)
                continue
            if item.question:
                if contains_secret(item.question) or any(
                    self.store.find(key) is None for key in item.related_ids
                ):
                    result.blocked.append("Invalid pending question or memory reference.")
                    continue
                pending = (
                    PendingItem(
                        **draft.model_dump(), question=item.question, related_ids=item.related_ids
                    )
                    if dry_run
                    else self.create_pending(
                        draft,
                        item.question,
                        item.related_ids,
                        session_id=session_id,
                        message_id=message_id,
                    )
                )
                result.queued.append(pending)
            else:
                preview = (
                    Entry(
                        id="preview",
                        text=draft.text,
                        topic=draft.topic,
                        timestamp=utc_timestamp(),
                        context=draft.context,
                    )
                    if dry_run
                    else self.add(draft, session_id=session_id, message_id=message_id)
                )
                result.admitted.append(preview)
            existing.add(identity)
        return result

    def dream_version(self) -> tuple[int, ...]:
        return self.store.revisions("inferences", "memories")

    def evidence_version(self, *, include_pending: bool = False) -> tuple:
        return (
            self.store.revisions("memories", "candidates")
            if include_pending
            else self.store.revisions("memories")
        )

    def propose_inference(self, text: str, source_ids: list[str], kind: InferenceKind) -> Inference:
        text = text.strip()
        if not text or contains_secret(text) or contains_secret(kind):
            raise ValueError("Inference text and kind must be nonempty and free of credentials.")
        stamp = utc_timestamp()
        item = Inference(
            id="inf-" + uuid4().hex,
            text=text,
            kind=kind.strip(),
            source_ids=source_ids,
            created_at=stamp,
            updated_at=stamp,
        )
        with self.work.write():
            for key in item.source_ids:
                self.require_entry(key)
            for record in self.inferences.all():
                if (record.kind, normalize_text(record.text), record.source_ids) == (
                    item.kind,
                    normalize_text(item.text),
                    item.source_ids,
                ):
                    return record
            self.inferences.add(item)
            return item

    def list_inferences(self) -> list[Inference]:
        return self.inferences.all()

    def review_inference(
        self, inference_id: str, action: str, text: str | None = None
    ) -> Inference:
        if action not in {"confirm", "reject", "correct"}:
            raise ValueError("操作须为 confirm、reject 或 correct。")
        with self.work.write():
            old = next((item for item in self.inferences.all() if item.id == inference_id), None)
            if old is None:
                raise KeyError("推断不存在。")
            target = "confirmed" if action == "confirm" else "deprecated"
            if action != "correct" and old.status == target:
                return old
            if old.status == "deprecated":
                raise ValueError("该推断已弃用。")
            stamp = utc_timestamp()
            updated = old.model_copy(update={"status": target, "updated_at": stamp})
            if action == "correct":
                content = (text or "").strip()
                if not content or contains_secret(content):
                    raise ValueError("更正内容为空或包含凭据。")
                if content == old.text:
                    return old
                if any(self.store.find(key) is None for key in old.source_ids):
                    raise ValueError("An inference source is unavailable.")
                replacement = old.model_copy(
                    update={
                        "id": "inf-" + uuid4().hex,
                        "text": content,
                        "status": "confirmed",
                        "created_at": stamp,
                        "updated_at": stamp,
                    }
                )
                self.inferences.record(updated, replacement)
                return replacement
            self.inferences.record(updated)
            return updated

    def current_field(
        self, query: str, *, snapshot: MemorySnapshot | None = None
    ) -> RecallEntry | None:
        if len(query) > 24:
            return None
        keyword = (
            self.retriever.keyword
            if isinstance(self.retriever, HybridRetriever)
            else self.retriever
        )
        if not isinstance(keyword, KeywordRetriever):
            return None
        entry = keyword.unique_field(query, snapshot=snapshot)
        if entry is None:
            return None
        context = entry.context
        today = date.today()
        if context and (
            (context.valid_from and date_bounds(context.valid_from)[0] > today)
            or (context.valid_to and date_bounds(context.valid_to)[1] < today)
        ):
            return None
        return entry.as_recall(context)

    def recall(
        self,
        query: str,
        limit: int | None = None,
        *,
        time_range: TimeRange | dict | None = None,
        location: str | None = None,
        emotion: str | None = None,
        original_query: str = "",
    ) -> list[RecallEntry]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Provide a nonempty recall query.")
        if limit is None:
            limit = self.config.retrieval.default_limit
        if type(limit) is not int or limit < 1:
            raise ValueError("Recall limit must be a positive integer.")
        period = TimeRange.model_validate(time_range) if time_range is not None else None
        snapshot = self.store.snapshot()
        if not (period or location or emotion or original_query):
            if exact := self.current_field(query, snapshot=snapshot):
                return [exact]
        entries = snapshot.by_id
        depth = max(len(entries), limit)
        hits = self.retriever.search(query, depth, snapshot=snapshot)
        if original_query and original_query != query:
            original = self.retriever.search(original_query, depth, snapshot=snapshot)
            combined = {hit.id: hit for hit in original if hit.matched_field}
            for pair in zip_longest(hits, original):
                for hit in pair:
                    if hit is not None:
                        combined.setdefault(hit.id, hit)
            hits = list(combined.values())
        results = []
        for hit in hits:
            entry = entries.get(hit.id)
            if entry is None:
                continue
            context = entry.context
            if (period or location or emotion) and not matches_context(
                context,
                period.start if period else None,
                period.end if period else None,
                location,
                emotion,
            ):
                continue
            results.append(entry.as_recall(context))
            if len(results) == limit:
                break
        return results

    def current_facts(self) -> list[Entry]:
        return self.store.entries()

    def read_topic(self, topic: str) -> dict:
        logical, matches = self.store.read_topic(topic)
        return {
            "topic": logical,
            "entries": [entry.as_recall().model_dump(exclude_none=True) for entry in matches],
        }

    def pending(self) -> list[PendingItem]:
        return self.queue.all()

    def approve(self, item_id: str, text_override: str | None = None) -> Entry:
        entry_id = "mem-" + hashlib.sha256(("pending:" + item_id).encode("utf-8")).hexdigest()[:24]
        with self.work.write():
            item = self.queue.get(item_id)
            if item is None:
                entry = self.store.find(entry_id)
                if entry is None:
                    raise KeyError(f"待确认项不存在：{item_id}")
                if text_override is not None and text_override.strip() != entry.text:
                    raise ValueError("This pending item was already approved with different text.")
                return entry
            text = item.text if text_override is None else text_override.strip()
            edited = text != item.text
            draft = MemoryDraft(
                topic=item.topic, text=text, context=None if edited else item.context
            )
            entry = self.add(
                draft, session_id=item.session_id, message_id=item.message_id, entry_id=entry_id
            )
            self.queue.resolve(item_id, "approved")
            if edited:
                enqueue_context_fill(self.paths, entry.id, source_version(entry))
            return entry

    def reject(self, item_id: str) -> PendingItem:
        with self.work.write():
            item = self.queue.resolve(item_id, "rejected")
            if item is None:
                raise KeyError(f"待确认项不存在：{item_id}")
            return item

    def topics(self) -> list[dict]:
        return [topic.model_dump() for topic in self.store.topics()]

    def memory_referencing_session(self, session_id: str) -> str | None:
        return self.store.first_source_reference(session_id)

    def _source_fields(self, session_id: str | None, message_id: str | None) -> dict[str, str]:
        if not session_id and not message_id:
            return {}
        if not session_id or not message_id:
            raise ValueError("A session source needs both a session and a user message.")
        try:
            self.session_query.user_message(session_id, message_id, active_only=True)
        except (KeyError, ValueError, OSError) as exc:
            raise ValueError(
                "The source user message is unavailable. No memory was written."
            ) from exc
        return {"session_id": session_id, "message_id": message_id}

    def read_memory_source(self, memory_id: str) -> dict:
        if not isinstance(memory_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]*", memory_id
        ):
            raise ValueError("Invalid memory ID.")
        with self.work.read():
            entry = self.store.find(memory_id)
            if entry is None:
                raise KeyError("Memory not found.")
            session_id, message_id = entry.source_session_id, entry.source_message_id
            if not session_id and not message_id:
                return {
                    "status": "no_session_source",
                    "memory_id": memory_id,
                    "message": "无对应会话来源",
                }
            if not session_id or not message_id:
                raise ValueError("Memory has an incomplete session source.")
            try:
                message = self.session_query.user_message(session_id, message_id)
            except (KeyError, ValueError, OSError) as exc:
                raise ValueError("The original source message is unavailable.") from exc
            return {"status": "found", "memory_id": memory_id, "message": message}

    def topic_entries(self, topic: str) -> list[Entry]:
        return self.store.entries(topic)

    def status(self) -> dict:
        return {
            "vault": str(self.config.vault),
            **self.store.counts(),
            "retrieval": self.retrieval_mode,
        }


def get_service(
    config_path: Path | None = None,
    *,
    retriever: Retriever | None = None,
) -> MemoryService:
    resolved = Path(config_path).expanduser() if config_path else default_config_path()
    config = load_config(config_path)
    return MemoryService(config, retriever=retriever, config_path=resolved)
