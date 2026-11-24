"""只读的会话历史、会话列表与原始消息查询。"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..observability.diagnostics import record_event
from ..storage.documents import DocumentError
from ..storage.locking import session_file_lock
from ..storage.paths import SESSION_ID_RE, SyvrioPaths, vault_paths
from ..time import parse_timestamp
from .attachment_index import attachment_metadata
from .attachments import Attachment, AttachmentBatch
from .model import Session, validate_record

_LOG = logging.getLogger(__name__)


def _session_sort_time(value: str) -> datetime:
    return parse_timestamp(value) if value else datetime.min.replace(tzinfo=UTC)


class SessionQuery:
    def __init__(self, vault: Path | SyvrioPaths, *, offline: bool = False):
        paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.paths = paths
        self._offline = offline
        self.root = paths.sessions_dir
        self.archive_root = paths.archived_sessions_dir
        self.attachments_dir = paths.attachments_dir
        self.runtime_dir = paths.runtime_dir
        self._reasoning_root = paths.runtime_dir / "session-reasoning"
        self._listing_cache: dict[str, tuple[tuple[Path, int, int], dict]] = {}
        # 补沉淀扫描只需知道文件还有多少待抽取消息，不必保留完整会话。
        self._undistilled_cache: dict[str, tuple[tuple[Path, int, int], int]] = {}

    def exists(self, session_id: str, *, archived: bool = False) -> bool:
        return self._find(session_id, archived=archived) is not None

    _attachment_metadata = staticmethod(attachment_metadata)

    def copy_message_attachment(
        self,
        session_id: str,
        message_id: str,
        attachment_id: str,
        destination: Callable[[bytes], object],
        *,
        archived: bool = False,
    ) -> tuple[Attachment, os.stat_result] | None:
        self._validate_session_id(session_id)
        root = self.archive_root if archived else self.root
        # 生成的 ID 已包含归档年月，常见下载不必枚举整个会话目录。
        match = re.fullmatch(r"s-(\d{4})(\d{2})\d{2}-\d{6}-[a-f0-9]{4}", session_id)
        path = root / match[1] / match[2] / f"{session_id}.jsonl" if match else None
        if path is None or not path.is_file():
            path = self._path(session_id, archived=archived)
        if path.resolve() != path:
            raise ValueError("Redirected session path.")
        try:
            meta = self._attachment_metadata(path, message_id, attachment_id)
        except FileNotFoundError as exc:
            raise KeyError(session_id) from exc
        if meta is None:
            return None
        return AttachmentBatch.copy_original(
            self.attachments_dir, session_id, message_id, meta, destination
        )

    def verify_attachments(self) -> tuple[int, list[str]]:
        """对照所有原始消息核验归档，包括已撤回的回合。"""
        checked = 0
        errors = []
        if not self.root.is_dir() and not self.archive_root.is_dir():
            return 0, ["The selected Vault has no sessions directory."]
        try:
            files = self._files() + self._files(archived=True)
        except (OSError, ValueError) as exc:
            return 0, [str(exc)]
        for path in files:
            try:
                session = self._load_path(path, include_reasoning=False)
            except (KeyError, OSError, ValueError) as exc:
                errors.append(f"{path.stem}: {exc}")
                continue
            if session.damaged_tail_line is not None:
                errors.append(
                    f"{session.id}: incomplete session record on line {session.damaged_tail_line}"
                )
            for record in session.records:
                attachments = record.get("attachments", [])
                if not isinstance(attachments, list):
                    errors.append(f"{session.id}/{record.get('id')}: invalid attachment list")
                    continue
                for meta in attachments:
                    try:
                        AttachmentBatch.restore(
                            self.attachments_dir, session.id, record["id"], [meta]
                        )
                    except (DocumentError, KeyError, OSError, TypeError, ValueError) as exc:
                        name = meta.get("name", "?") if isinstance(meta, dict) else "?"
                        errors.append(f"{session.id}/{record.get('id')}/{name}: {exc}")
                    else:
                        checked += 1
        return checked, errors

    def load(
        self, session_id: str, *, include_reasoning: bool = True, archived: bool = False
    ) -> Session:
        return self._load_path(
            self._path(session_id, archived=archived), include_reasoning=include_reasoning
        )

    def _read_log(self, path: Path) -> bytes:
        # 捕获已提交字节后立即放锁，JSON 校验、页面投影和附件摘要均在锁外处理。
        if self._offline:
            # 调用方持有物理 Vault 的维护租约，不接触可能属于其他 Vault 的 Runtime。
            return path.read_bytes()
        if not self.paths.lock_dir.exists():
            # 单独复制来的 Vault 没有 Runtime，离线核验不能创建目录。如果写者
            # 同时启动，锁目录一定先于日志修改出现，此时重新在会话锁内读取。
            raw = path.read_bytes()
            if not self.paths.lock_dir.exists():
                return raw
        with session_file_lock(self.paths, path.stem):
            return path.read_bytes()

    def _load_path(self, path: Path, *, include_reasoning: bool) -> Session:
        session_id = path.stem
        self._validate_session_id(session_id)
        if path.resolve() != path or not (
            path.is_relative_to(self.root) or path.is_relative_to(self.archive_root)
        ):
            raise ValueError("symlink in session archive")
        try:
            lines = self._read_log(path).splitlines()
        except FileNotFoundError as exc:
            raise KeyError(f"会话不存在：{session_id}") from exc
        records: list[dict] = []
        errors: dict[int, ValueError] = {}
        recovered: set[int] = set()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors[number] = exc
                continue
            validate_record(record)
            record.pop("reasoning", None)  # 只有本地 Runtime 的附属文件可以携带重放数据。
            records.append(record)
            if record.get("type") == "recovery":
                ignored = record.get("ignored_line")
                if type(ignored) is int and ignored < number:
                    recovered.add(ignored)
        damaged_tail = None
        for number, error in errors.items():
            if number in recovered:
                continue
            if number == len(lines):
                if (
                    isinstance(error, UnicodeDecodeError)
                    and error.reason != "unexpected end of data"
                ):
                    raise error
                damaged_tail = number
            else:
                raise error
        meta = records[0] if records and records[0].get("type") == "meta" else {}
        if meta.get("id", session_id) != session_id:
            raise ValueError("Session ID differs from archive filename")
        if include_reasoning:
            for record in records:
                if record.get("type") == "assistant" and record.get("reasoning_local") is True:
                    record_id = record.get("id")
                    if not isinstance(record_id, str) or not SESSION_ID_RE.fullmatch(record_id):
                        continue
                    try:
                        attachment = self._reasoning_path(session_id, record_id)
                        reasoning = json.loads(attachment.read_text(encoding="utf-8"))
                        if (
                            not isinstance(reasoning, dict)
                            or not isinstance(reasoning.get("text"), str)
                            or not isinstance(reasoning.get("source", ""), str)
                            or not isinstance(reasoning.get("fragments", []), list)
                            or reasoning.get("seconds") is not None
                            and type(reasoning["seconds"]) not in (float, int)
                        ):
                            raise ValueError("Invalid local session reasoning")
                    except FileNotFoundError:
                        continue  # Runtime 保存在本机，同步 Vault 后可能缺失。
                    except (OSError, ValueError, RecursionError) as exc:
                        _LOG.warning(
                            "Skipping unreadable reasoning attachment for %s/%s (%s)",
                            session_id,
                            record_id,
                            type(exc).__name__,
                        )
                        continue
                    record["reasoning"] = reasoning
        return Session(
            id=session_id,
            started_at=meta.get("started_at", ""),
            path=path,
            records=records,
            damaged_tail_line=damaged_tail,
        )

    def _reasoning_path(self, session_id: str, record_id: str) -> Path:
        if not SESSION_ID_RE.fullmatch(session_id) or not SESSION_ID_RE.fullmatch(record_id):
            raise ValueError("Invalid local session reasoning identifier")
        path = self._reasoning_root / session_id / f"{record_id}.json"
        if any(
            p.is_symlink() for p in (self._reasoning_root, path.parent, path)
        ) or not path.resolve().is_relative_to(self._reasoning_root.parent.resolve()):
            raise ValueError("Local session reasoning cannot redirect outside Runtime")
        return path

    def _listing_row(self, path: Path, with_tokens: bool) -> dict | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (path, stat.st_mtime_ns, stat.st_size)
        cached = self._listing_cache.get(path.stem)
        if cached is None or cached[0] != signature:
            try:
                session = self._load_path(path, include_reasoning=False)
            except (ValueError, KeyError, OSError) as exc:
                _LOG.warning("Unable to list session %s (%s)", path.stem, type(exc).__name__)
                record_event("session.scan_failed", exc)
                return None
            row = {
                "id": session.id,
                "started_at": session.started_at,
                "updated_at": session.updated_at,
                "last_message_at": session.last_message_at,
                "messages": len(session.messages),
                # token 估算要遍历全部记录，按需计算后缓存在同一行里。
                "tokens": session.estimated_tokens() if with_tokens else None,
                "summaries": sum(
                    1 for record in session.records if record.get("type") == "summary"
                ),
                "title": session.title(),
            }
            self._listing_cache[path.stem] = (signature, row)
        else:
            row = cached[1]
            if with_tokens and row.get("tokens") is None:
                try:
                    row["tokens"] = self._load_path(
                        path, include_reasoning=False
                    ).estimated_tokens()
                except (ValueError, KeyError, OSError) as exc:
                    _LOG.warning("Unable to list session %s (%s)", path.stem, type(exc).__name__)
                    record_event("session.scan_failed", exc)
                    return None
        result = dict(row)
        if not with_tokens:
            result["tokens"] = 0
        return result

    def list(
        self, limit: int = 20, with_tokens: bool = True, *, archived: bool = False, offset: int = 0
    ) -> list[dict]:
        sessions = []
        seen: set[str] = set()
        for path in self._files(archived=archived):
            row = self._listing_row(path, with_tokens)
            if row is None:
                continue
            seen.add(path.stem)
            sessions.append(row)
        root = self.archive_root if archived else self.root
        for stale, (signature, _) in tuple(self._listing_cache.items()):
            if stale not in seen and signature[0].is_relative_to(root):
                self._listing_cache.pop(stale, None)
        sessions.sort(key=lambda item: _session_sort_time(item["last_message_at"]), reverse=True)
        return sessions[offset : offset + limit]

    def unconsolidated(
        self, limit: int = 3, exclude: str | None = None, min_new_messages: int = 1
    ) -> list[Session]:
        """先筛选达到沉淀阈值的会话，再按最近更新限量返回。"""
        if not self.root.exists():
            return []
        needed = max(1, min_new_messages)
        found: list[tuple[str, Session]] = []
        seen: set[str] = set()
        for path in self._files():
            if exclude and path.stem == exclude:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            seen.add(path.stem)
            signature = (path, stat.st_mtime_ns, stat.st_size)
            cached = self._undistilled_cache.get(path.stem)
            if cached is not None and cached[0] == signature:
                if cached[1] < needed:
                    continue
                try:
                    session = self._load_path(path, include_reasoning=False)
                except (ValueError, KeyError, OSError) as exc:
                    _LOG.warning("Unable to scan session %s (%s)", path.stem, type(exc).__name__)
                    record_event("session.scan_failed", exc)
                    continue
                found.append((session.updated_at, session))
                continue
            try:
                session = self._load_path(path, include_reasoning=False)
            except (ValueError, KeyError, OSError) as exc:
                _LOG.warning("Unable to scan session %s (%s)", path.stem, type(exc).__name__)
                record_event("session.scan_failed", exc)
                continue
            count = len(session.user_texts_after_distilled())
            self._undistilled_cache[path.stem] = (signature, count)
            if count >= needed:
                found.append((session.updated_at, session))
        for stale in set(self._undistilled_cache) - seen:
            self._undistilled_cache.pop(stale, None)
        found.sort(key=lambda item: _session_sort_time(item[0]), reverse=True)
        return [session for _, session in found[:limit]]

    @staticmethod
    def _chat_messages(session: Session):
        for record in session.active_records():
            kind = record["type"]
            if kind in {"assistant", "message"}:
                if record.get("content"):
                    yield {
                        "message_id": record["id"],
                        "role": record.get("role", "assistant"),
                        "content": record["content"],
                        "at": record.get("at", ""),
                    }

    def search_chats(self, query: str, limit: int = 20, *, exclude_session: str = "") -> dict:
        keyword = query.strip()
        if not 1 <= len(keyword) <= 160 or not 1 <= limit <= 50:
            raise ValueError("Enter 1–160 characters and request 1–50 matches.")
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        hits = []
        for path in reversed(self._files()):
            if path.stem == exclude_session:
                continue
            try:
                session = self._load_path(path, include_reasoning=False)
            except (KeyError, FileNotFoundError):
                continue  # 列举完成后，会话仍可能被归档。
            title = session.title()
            origin = {"session_id": session.id, "title": title}
            if pattern.search(title):
                hits.append({**origin, "message_id": "", "role": "title", "excerpt": title})
            for message in reversed(list(self._chat_messages(session))):
                if len(hits) >= limit:
                    break
                content = message["content"]
                match = pattern.search(content)
                if match:
                    start = max(0, match.start() - 48)
                    end = min(len(content), match.end() + 80)
                    excerpt = (
                        ("…" if start else "")
                        + content[start:end]
                        + ("…" if end < len(content) else "")
                    )
                    hits.append(
                        {
                            **origin,
                            "message_id": message["message_id"],
                            "role": message["role"],
                            "excerpt": excerpt,
                        }
                    )
            if len(hits) >= limit:
                break
        return {"results": hits}

    def search_message(self, session_id: str, message_id: str) -> dict:
        session = self.load(session_id, include_reasoning=False)
        messages = list(self._chat_messages(session))
        for i, message in enumerate(messages):
            if message["message_id"] == message_id:
                return {"title": session.title(), "messages": messages[max(0, i - 1) : i + 2]}
        raise KeyError(message_id)

    def _files(self, *, archived: bool = False) -> list[Path]:
        root = self.archive_root if archived else self.root
        if root.resolve() != root:
            raise ValueError("symlink in session archive")
        files = sorted(
            path
            for path in root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.jsonl")
            if 1 <= int(path.parent.name) <= 12
        )
        seen: set[str] = set()
        for path in files:
            if path.resolve() != path or path.stem in seen:
                raise ValueError("duplicate session ID or symlink in session archive")
            seen.add(path.stem)
        return files

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise KeyError(f"非法会话 ID：{session_id}")

    def _find(self, session_id: str, *, archived: bool = False) -> Path | None:
        self._validate_session_id(session_id)
        return next((p for p in self._files(archived=archived) if p.stem == session_id), None)

    def _path(self, session_id: str, *, archived: bool = False) -> Path:
        path = self._find(session_id, archived=archived)
        if path is None and not self._offline:
            # 删除事务可能在发布前回滚；未知 ID 不应因此在 Runtime 新建锁文件。
            lock = session_file_lock(self.paths, session_id)
            if lock.path.exists():
                with lock:
                    path = self._find(session_id, archived=archived)
        if path is None:
            raise KeyError(f"会话不存在：{session_id}")
        return path

    def user_message(self, session_id: str, message_id: str, *, active_only: bool = False) -> dict:
        """精确查找一条原始用户消息，无需加载思考记录或其他附属文件。"""
        session = self.load(session_id, include_reasoning=False)
        return self._user_message(session, message_id, active_only=active_only)

    def review_source(self, session_id: str, message_id: str) -> dict:
        """显式审核时可沿着已保存的引用查阅归档会话。"""
        archived = False
        try:
            session = self.load(session_id, include_reasoning=False)
        except KeyError:
            session = self.load(session_id, include_reasoning=False, archived=True)
            archived = True
        return {
            **self._user_message(session, message_id),
            "title": session.title(),
            "archived": archived,
        }

    @staticmethod
    def _user_message(session: Session, message_id: str, *, active_only: bool = False) -> dict:
        if not isinstance(message_id, str) or not SESSION_ID_RE.fullmatch(message_id):
            raise ValueError("Invalid source message ID.")
        records = session.active_records() if active_only else session.records
        matches = [record for record in records if record.get("id") == message_id]
        if (
            len(matches) != 1
            or matches[0].get("type") != "message"
            or matches[0].get("role") != "user"
        ):
            raise ValueError("Source does not identify a unique user message.")
        message = matches[0]
        result = {"content": message["content"], "at": message.get("at", "")}
        if "original_local_date" in message:
            result["original_local_date"] = message["original_local_date"]
        return result
