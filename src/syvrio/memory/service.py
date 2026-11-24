from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from pathlib import Path
from uuid import uuid4

from ..infra.archive_layout import migrate_archive
from ..infra.config import (
    Config,
    LLMConfig,
    RetrievalConfig,
    default_config_path,
    load_config,
)
from ..infra.files import atomic_write
from ..infra.gitops import GitError, GitRepo
from ..infra.gitsync import GitSyncService
from ..infra.jobs import enqueue_context_fill
from ..infra.locking import git_operation_lock, vault_write_lock
from ..infra.tokens import truncate_to_tokens
from ..infra.transaction import recover_vault_transaction, vault_transaction
from ..llm import LiteLLMEmbedder
from ..llm.commit_summary import summarize_diff
from ..retrieval import (
    Embedder,
    HybridRetriever,
    KeywordRetriever,
    LocalEmbedder,
    Retriever,
    VectorRetriever,
)
from ..session.query import SessionQuery
from ..time import utc_timestamp
from .audit import AuditLog
from .detect import contains_secret
from .extractor import ImportExtractor, LLMExtractor
from .inference import InferenceStore, source_fingerprint
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
from .schema import parse_document
from .store import (
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    MemoryStore,
    normalize_text,
)
from .temporal import date_bounds, matches_context

VAULT_README = """# Syvrio vault

这个目录由 [Syvrio](https://github.com/Shardkiht/Syvrio) 管理。

- `memories/<category>/<topic>.md`：Markdown v2，是唯一事实来源。
- `sessions/YYYY/MM/`：会话 JSONL，保留原始对话与工具轨迹。
- `archived/sessions/YYYY/MM/<id>.jsonl`：归档会话，仅供人工查看、还原或删除。
- `attachments/`：聊天上传的原件，由 Vault 的 .gitignore 排除；迁移时随整个 Vault 复制。
- `memories/pending.md` 保存待确认项，拒绝的候选移至 `archived/pending/rejected.md`。
- `inferences/` 保存推断，`records/` 保存正式记忆的变化与审计。
- Runtime 与 Vault 分离；派生索引、缓存和锁不会进入 Vault。
- `SOUL.md`（可选）：自定义人设，替代系统提示词里的默认人设段。
- Git 跟踪会话和记忆的变更。
"""

VAULT_GITIGNORE = """__pycache__/
*.pyc
.DS_Store
Thumbs.db
.idea/
.vscode/
*~
/attachments/
/archived/
"""


def _ensure_vault_gitignore(vault: Path) -> None:
    path = vault / ".gitignore"
    if path.is_symlink():
        raise ValueError("Vault .gitignore must be a regular file")
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        atomic_write(path, VAULT_GITIGNORE.encode("utf-8"))
        return
    # 后续的否定规则可能覆盖目录规则，须保留用户规则的位置。
    required = [b"/attachments/", b"/archived/"]
    for rule in reversed(original.splitlines()):
        if rule in required:
            required.remove(rule)
        if rule.startswith(b"!"):
            break
    if not required:
        return
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    separator = newline if original and not original.endswith(b"\n") else b""
    atomic_write(path, original + separator + newline.join(required) + newline)


# 默认人设兼作 init 写入 SOUL.md 的模板；放 memory 层是因为 agent/prompts.py 可以引 memory，
# 反向不行（分层：app → agent → memory → retrieval → infra）。
DEFAULT_PERSONA = "你是用户的个人记忆助手，运行在本地终端，拥有跨会话的长期记忆。"

SOUL_FILENAME = "SOUL.md"
SOUL_MAX_TOKENS = 2000


def load_persona(vault: Path) -> str:
    """读 vault/SOUL.md 自定义人设；缺失或为空返回空串，超长按 token 截断。

    用户可能用记事本以 ANSI/GBK 保存，UTF-8 失败时按本机编码兜底，避免炸掉整轮对话。
    """
    path = vault / SOUL_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        # 记事本 ANSI（中文系统即 GBK）保存的场景；gbk 编码全平台内置，
        # 不用 Windows 专属的 mbcs——英文系统的 cp1252 解不了 GBK。
        try:
            text = path.read_text(encoding="gbk")
        except (UnicodeDecodeError, OSError):
            return ""
    text = text.strip()
    if not text:
        return ""
    return truncate_to_tokens(text, SOUL_MAX_TOKENS)


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
    vector = VectorRetriever(store, embedder)
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


class MemoryService:
    def __init__(
        self,
        config: Config,
        extractor=None,
        retriever: Retriever | None = None,
        config_path: Path | None = None,
    ):
        self.config = config
        self.paths = config.paths(config_path)
        self.paths.validate()
        if (self.paths.vault_dir / ".syvrio").exists() or any(
            self.paths.sessions_dir.glob("*.jsonl")
        ):
            raise ValueError(
                "Legacy Vault layout. Use a new development Vault or migrate your data separately."
            )
        self.store = MemoryStore(self.paths)
        self.session_query = SessionQuery(self.paths)
        self.queue = PendingQueue(self.paths)
        self.inferences = InferenceStore(self.paths)
        self.repo = GitRepo(self.paths.vault_dir)
        self.audit = AuditLog(self.paths)
        self.extractor = extractor or LLMExtractor(config.llm)
        if retriever is not None:
            self.retriever: Retriever = retriever
            self.retrieval_mode = "custom"
        else:
            self.retriever, self.retrieval_mode = build_retriever(config, self.store)
        # 热更新（切模型/上下文）写回配置的路径：记住加载来源，避免写错文件或被读回旧值。
        self.config_path = config_path
        # 后台补沉淀与前台对话可能并发写 vault：所有变更操作在这把锁内串行。
        # 组合写入流程可安全复用公开服务方法，调用方不再绕过接口调用私有实现。
        # 锁同时通过文件锁覆盖 CLI/TUI/MCP 多个进程，进程崩溃后由内核释放。
        self.write_lock = vault_write_lock(self.paths)
        self._git_lock = git_operation_lock(self.paths)
        with self._git_lock, self.write_lock:
            self.paths.bind()
            recover_vault_transaction(self.paths)
            migrate_archive(self.paths.vault_dir)
            self.queue.recover_session_deletion()
            if (self.paths.vault_dir / ".git").exists() and self.repo.is_repo:
                _ensure_vault_gitignore(self.paths.vault_dir)
        self.profile = ProfileReader(self.paths)
        self.git_sync = GitSyncService(config, repo=self.repo)

    def init_vault(self) -> dict:
        with self._git_lock:
            with self.write_lock:
                # 先确认仓库归属，避免在父仓库子目录里写出 Vault 文件后再失败。
                created = self.repo.ensure_init()
                self.store.ensure()
                self.paths.records_dir.mkdir(parents=True, exist_ok=True)
                self.paths.sessions_dir.mkdir(parents=True, exist_ok=True)
                readme = self.paths.vault_dir / "README.md"
                if not readme.exists():
                    readme.write_text(VAULT_README, encoding="utf-8")
                soul = self.paths.vault_dir / SOUL_FILENAME
                if not soul.exists():
                    soul.write_text(DEFAULT_PERSONA + "\n", encoding="utf-8")
                _ensure_vault_gitignore(self.paths.vault_dir)
                self._stage_all_checked()
                commit_hash = self.repo.commit("memory: 初始化记忆库")
        return {"vault": str(self.config.vault), "git_created": created, "commit": commit_hash}

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
        with vault_transaction(self.paths):
            source = self._source_fields(session_id, message_id)
            result = []
            if draft is not None:
                self.validate_draft(draft)
                if supersedes is not None:
                    self._active_entry(supersedes)
                if entry_id is None and session_id is not None:
                    identity = [
                        session_id,
                        message_id,
                        self.store.topic_path(draft.topic)
                        .relative_to(self.paths.memories_dir)
                        .as_posix(),
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
                        or self.store.topic_path(existing.topic)
                        != self.store.topic_path(draft.topic)
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
        self.store.topic_path(draft.topic)
        if contains_secret(draft.model_dump_json(exclude_none=True)):
            raise ValueError("检测到凭据，已拦截写入。")

    def _active_entry(self, entry_id: str) -> Entry:
        entry = self.store.find(entry_id)
        if entry is None or entry.status != STATUS_ACTIVE:
            raise ValueError("The target must be an active memory.")
        return entry

    def memorize(self, draft: MemoryDraft, **source) -> Entry:
        return self.add(draft, **source)

    def correct(self, entry_id: str, draft: MemoryDraft, **source) -> Entry:
        with vault_transaction(self.paths):
            old = self._active_entry(entry_id)
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
        with vault_transaction(self.paths):
            self._active_entry(entry_id)
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
        with vault_transaction(self.paths):
            self._source_fields(session_id, message_id)
            if any(self.store.find(key) is None for key in item.related_ids):
                raise ValueError("Pending references must identify existing memories.")
            for existing in self.queue.all():
                if (
                    existing.text,
                    existing.topic,
                    existing.context,
                    existing.question,
                    set(existing.related_ids),
                    existing.session_id,
                    existing.message_id,
                ) == (
                    item.text,
                    item.topic,
                    item.context,
                    item.question,
                    set(item.related_ids),
                    item.session_id,
                    item.message_id,
                ):
                    return existing
            self.queue.add(item)
        return item

    def prepare_import(self, text: str, extractor=None) -> tuple[tuple, list[Candidate]]:
        if not text.strip():
            raise ValueError("导入内容为空。")
        if extractor is None:
            extractor = ImportExtractor(
                self.config.llm, topics=[item.topic for item in self.store.topics()]
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
        with vault_transaction(self.paths):
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
                        metadata={"context": draft.context.model_dump_json()}
                        if draft.context
                        else {},
                    )
                    if dry_run
                    else self.add(draft, session_id=session_id, message_id=message_id)
                )
                result.admitted.append(preview)
            existing.add(identity)
        return result

    def evidence_version(self, *, include_pending: bool = False) -> tuple:
        with self.write_lock:
            facts = self.store.fingerprint()
            if not include_pending:
                return (facts,)
            pending = tuple(item.model_dump_json() for item in self.queue.all())
            return facts, pending

    def memory_references(
        self, ids: set[str], restored: dict[Path, bytes | None]
    ) -> dict[str, set[str]]:
        """找出恢复文件之后仍然存在、且指向这些文件内容的结构化引用。

        应在 Vault 写锁内调用，已弃用的记录也保留其来源。
        """
        result: dict[str, set[str]] = {key: set() for key in ids}
        surviving = [
            item
            for item in self.inferences.all()
            if self.inferences.path_for(item.kind, item.id) not in restored
        ]
        for path, payload in restored.items():
            if payload and path.is_relative_to(self.inferences.root):
                surviving.append(self.inferences.decode(path, payload))
        for inference in surviving:
            for key in ids.intersection(inference.source_ids):
                result[key].add(f"inference:{inference.id}")
        pending_path = self.queue.pending_path
        if pending_path in restored:
            pending = self.queue.decode(restored[pending_path] or b"")
        else:
            pending = self.queue.all()
        for item in pending:
            for key in ids.intersection(item.related_ids):
                result[key].add(f"pending:{item.id}")
        entries = [
            entry
            for entry in self.store.entries(include_inactive=True)
            if self.paths.vault_dir / entry.source_path not in restored
        ]
        for path, original in restored.items():
            if path != pending_path and path.is_relative_to(self.paths.memories_dir) and original:
                relative = path.relative_to(self.paths.vault_dir).as_posix()
                document = parse_document(original.decode("utf-8"), relative, relative)
                entries.extend(record.entry for record in document.records)
        for entry in entries:
            if entry.supersedes in result:
                result[entry.supersedes].add(f"memory:{entry.id}")
        return result

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
        with vault_transaction(self.paths):
            for key in item.source_ids:
                self._active_entry(key)
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
        with vault_transaction(self.paths):
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

    def current_field(self, query: str) -> RecallEntry | None:
        if len(query) > 24:
            return None
        keyword = (
            self.retriever.keyword
            if isinstance(self.retriever, HybridRetriever)
            else self.retriever
        )
        if not isinstance(keyword, KeywordRetriever):
            return None
        entry = keyword.unique_field(query)
        if entry is None:
            return None
        context = entry.context
        today = date.today()
        if context and (
            (context.valid_from and date_bounds(context.valid_from)[0] > today)
            or (context.valid_to and date_bounds(context.valid_to)[1] < today)
        ):
            return None
        return entry.as_recall()

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
        if not (period or location or emotion or original_query):
            if exact := self.current_field(query):
                return [exact]
        entries = {entry.id: entry for entry in self.store.entries()}
        depth = max(len(entries), limit)
        hits = self.retriever.search(query, depth)
        if original_query and original_query != query:
            original = self.retriever.search(original_query, depth)
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
            if (period or location or emotion) and not matches_context(
                entry.context,
                period.start if period else None,
                period.end if period else None,
                location,
                emotion,
            ):
                continue
            results.append(entry.as_recall())
            if len(results) == limit:
                break
        return results

    def current_facts(self) -> list[Entry]:
        return self.store.entries()

    def read_topic(self, topic: str) -> dict:
        with vault_transaction(self.paths):
            wanted = self.store.topic_path(topic)
            if not wanted.is_file():
                raise KeyError(f"主题不存在：{topic}")
            matches = self.store.entries(wanted.relative_to(self.paths.vault_dir).as_posix())
            logical = wanted.relative_to(self.paths.memories_dir).with_suffix("").as_posix()
        return {
            "topic": logical,
            "entries": [entry.as_recall().model_dump(exclude_none=True) for entry in matches],
        }

    def pending(self) -> list[PendingItem]:
        return self.queue.all()

    def approve(self, item_id: str, text_override: str | None = None) -> Entry:
        entry_id = "mem-" + hashlib.sha256(("pending:" + item_id).encode("utf-8")).hexdigest()[:24]
        with vault_transaction(self.paths):
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
                enqueue_context_fill(self.paths, entry.id, source_fingerprint(entry))
            return entry

    def reject(self, item_id: str) -> PendingItem:
        with vault_transaction(self.paths):
            item = self.queue.resolve(item_id, "rejected")
            if item is None:
                raise KeyError(f"待确认项不存在：{item_id}")
            return item

    def topics(self) -> list[dict]:
        return [topic.model_dump() for topic in self.store.topics()]

    def memory_referencing_session(self, session_id: str) -> str | None:
        with self.write_lock:
            return next(
                (
                    entry.id
                    for entry in self.store.entries(include_inactive=True)
                    if entry.source_session_id == session_id
                ),
                None,
            )

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
        with self.write_lock:
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

    def history(self, limit: int = 10) -> list[dict]:
        return self.repo.history(limit)

    def status(self) -> dict:
        return {
            "vault": str(self.config.vault),
            "topics": len(self.store.topics()),
            "entries": len(self.store.entries()),
            "pending": len(self.pending()),
            "dirty": self.repo.has_changes() if self.repo.is_repo else False,
            "retrieval": self.retrieval_mode,
        }

    def _refuse_sensitive(self) -> None:
        sensitive = self.repo.sensitive_paths()
        if sensitive:
            paths = "、".join(sensitive[:5])
            raise GitError(
                f"拒绝提交疑似敏感文件：{paths}。请移出 vault 或加入 .gitignore。", kind="sensitive"
            )

    def _stage_all_checked(self) -> None:
        """首次初始化与后续提交共用：暂存前后都检查，覆盖索引里已有的文件。"""
        self._refuse_sensitive()
        with self.repo.stage_checked():
            self._refuse_sensitive()

    def commit_pending_changes(self) -> str | None:
        """启动检查用：自动提交 vault 未保存变更（不 push、不调 LLM 生成总结）。"""
        with self._git_lock:
            with self.write_lock:
                self.repo.ensure_init()
                self._stage_all_checked()
                if not self.repo.has_staged():
                    return None
                return self.repo.commit("memory: 自动提交未保存变更（启动检查）")

    def commit(self, push: bool | None = None) -> dict:
        with self._git_lock:
            return self._commit_locked(push)

    def _commit_locked(self, push: bool | None) -> dict:
        with self.write_lock:
            self.repo.ensure_init()
            self._stage_all_checked()
            staged = self.repo.has_staged()
            if staged:
                stat = self.repo.staged_stat()
                diff = self.repo.staged_diff()
        should_push = self.config.auto_push if push is None else push
        if not staged:
            result = {
                "committed": False,
                "summary": "没有需要提交的变更。",
                "hash": None,
                "pushed": False,
                "push_output": "",
                "push_error": "",
            }
            # 没有新提交时仍允许补推历史未推送提交；缺少 remote/upstream 时保持静默。
            if should_push and self.repo.has_remote() and self.repo.upstream():
                if self.repo.unpushed_count():
                    try:
                        result["push_output"] = self.repo.push()
                        result["pushed"] = True
                        self.git_sync.record_push_success()
                    except GitError as exc:
                        result["push_error"] = str(exc)
            return result
        # LLM 摘要与 Push 都不占 Vault 写锁；Git 锁仍持有，其他 Git 操作不会改 Index。
        summary = summarize_diff(diff, self.config.llm, stat)
        lines = summary.splitlines()
        subject = lines[0] if lines else "memory: 更新记忆库"
        if not subject.startswith("memory:"):
            subject = f"memory: {subject}"
        message = subject[:120]
        body = "\n".join(lines[1:]).strip()
        if body:
            message += f"\n\n{body}"
        commit_hash = self.repo.commit(message)
        # 本地提交已成功；push 失败只记录原因，不掩盖已完成的提交。
        pushed = False
        push_output = ""
        push_error = ""
        if should_push and commit_hash:
            try:
                push_output = self.repo.push()
                pushed = True
                self.git_sync.record_push_success()
            except GitError as exc:
                push_error = str(exc)
        return {
            "committed": commit_hash is not None,
            "summary": summary,
            "hash": commit_hash,
            "pushed": pushed,
            "push_output": push_output,
            "push_error": push_error,
        }


def get_service(
    config_path: Path | None = None,
    vault: Path | None = None,
    extractor=None,
    retriever: Retriever | None = None,
) -> MemoryService:
    resolved = Path(config_path).expanduser() if config_path else default_config_path()
    config = load_config(config_path)
    if vault is not None:
        config.vault = Path(vault).expanduser().resolve()
        if config._baseline is not None:
            config._baseline["vault"] = config.vault.as_posix()
    return MemoryService(config, extractor=extractor, retriever=retriever, config_path=resolved)
