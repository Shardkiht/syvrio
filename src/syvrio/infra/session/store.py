"""会话 JSONL 归档及其附属文件的可靠写入和恢复。"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from threading import Lock
from typing import Any

from ..security.secrets import redact_secrets
from ..storage.files import FileVersion, atomic_write, file_version
from ..storage.locking import session_file_lock, vault_write_lock
from ..storage.paths import TURN_UNDO_DIR, SyvrioPaths, vault_paths
from ..storage.transaction import vault_transaction
from ..time import serialize_timestamp, timestamp_calendar_date, utc_now
from .attachment_objects import release_message, upgrade_originals
from .errors import ConversationError
from .model import Session, new_message_id, now_iso, validate_record
from .query import SessionQuery


def _reasoning_versions(folder: Path) -> dict[str, FileVersion]:
    if folder.resolve() != folder:
        raise ValueError("Redirected local reasoning directory.")
    try:
        with os.scandir(folder) as entries:
            return {
                entry.name: file_version(entry.stat(follow_symlinks=False))
                for entry in entries
                if entry.name.endswith(".json")
            }
    except FileNotFoundError:
        return {}


class HistoryRevision:
    """为投影缓存标识版本，仅追加关闭标记时沿用原标识。"""


@dataclass(frozen=True)
class SessionSnapshot:
    """多个读取方共享的只读日志版本，Runtime 另取独立副本。"""

    session: Session
    version: FileVersion
    # None 表示对话记录完全没有引用本地思考内容。
    reasoning: dict[str, FileVersion] | None
    history_revision: HistoryRevision = field(default_factory=HistoryRevision)

    @cached_property
    def attachments(self) -> dict:
        return {
            (row["id"], item["id"]): item
            for row in self.session.active_records()
            if row.get("role") == "user"
            for item in row.get("attachments", [])
        }


def _sidecar_directory(path: Path) -> Path:
    if path.resolve() != path or path.exists() and not path.is_dir():
        raise ValueError("Invalid or redirected session sidecar directory.")
    return path


def _finish_sidecar_deletion(folder: Path, *, attachments: bool) -> None:
    try:
        if attachments:
            for message in folder.iterdir():
                if message.is_dir():
                    release_message(folder.parent.parent, message, folder.name)
        shutil.rmtree(folder)
    except OSError as exc:
        # Windows 上查看器可能仍打开着文件，重启后会再次尝试恢复。
        logging.getLogger(__name__).warning("Session sidecar cleanup deferred: %s", exc)


@contextmanager
def _stage_sidecars(root: Path, session_id: str, *, attachments: bool) -> Iterator[None]:
    source = _sidecar_directory(root / session_id)
    if not source.exists():
        yield
        return
    staged = _sidecar_directory(root / ".deleting" / session_id)
    if staged.exists():
        raise ValueError("An earlier session deletion needs recovery.")
    staged.parent.mkdir(exist_ok=True)
    source.rename(staged)
    try:
        yield
    except BaseException:
        staged.rename(source)
        raise
    _finish_sidecar_deletion(staged, attachments=attachments)


def _redact_record(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_record(item) for item in value]
    return value


def _stamp_event(record: dict) -> None:
    if record.get("type") in {"message", "assistant"} and "id" not in record:
        record["id"] = new_message_id()
    supplied = record.get("at")
    record.pop("original_local_date", None)
    if "at" in record:
        if record.get("type") == "message" and record.get("role") == "user":
            record["original_local_date"] = timestamp_calendar_date(supplied).isoformat()
    else:
        record["at"] = now_iso()
    validate_record(record)


class SessionStore(SessionQuery):
    def __init__(self, vault: Path | SyvrioPaths):
        paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        super().__init__(paths)
        self._lock = vault_write_lock(paths)
        self._turn_undo_root = paths.runtime_dir.resolve() / TURN_UNDO_DIR
        self._sidecar_roots = (self.attachments_dir, self._reasoning_root, self._turn_undo_root)
        self._cache_lock = Lock()
        self._snapshots: OrderedDict[Path, SessionSnapshot] = OrderedDict()

    def _attachment_metadata(self, path: Path, message_id: str, attachment_id: str) -> dict | None:
        with session_file_lock(self.paths, path.stem):
            snapshot = self._snapshots.get(path)
            if snapshot is not None and snapshot.version == file_version(path.stat()):
                return snapshot.attachments.get((message_id, attachment_id))
            return super()._attachment_metadata(path, message_id, attachment_id)

    def load_snapshot(self, session_id: str) -> Session:
        """返回独立副本，供 Runtime 追加或撤回。"""
        return deepcopy(self.read_snapshot(session_id).session)

    def read_snapshot(self, session_id: str) -> SessionSnapshot:
        """核验已保存的版本，在只读投影之间共享其记录。"""
        self._validate_session_id(session_id)
        with session_file_lock(self.paths, session_id):
            path = self._path(session_id)
            try:
                version = file_version(path.stat())
            except FileNotFoundError as exc:
                raise KeyError(f"会话不存在：{session_id}") from exc
            cached = self._snapshots.get(path)
            try:
                reasoning = None
                if cached is None or cached.version != version or cached.reasoning is not None:
                    reasoning = _reasoning_versions(self._reasoning_root / session_id)
            except (OSError, ValueError, RuntimeError):
                # 常规读取时跳过单个缺失或被重定向的附属文件。
                return SessionSnapshot(self._load_path(path, include_reasoning=True), version, None)
            if cached is None or cached.version != version or cached.reasoning != reasoning:
                session = self._load_path(path, include_reasoning=True)
                has_reasoning = any(item.get("reasoning_local") is True for item in session.records)
                cached = SessionSnapshot(session, version, reasoning if has_reasoning else None)
            self._cache_snapshot(path, cached)
            return cached

    def _cache_snapshot(self, path: Path, snapshot: SessionSnapshot) -> None:
        with self._cache_lock:
            self._snapshots[path] = snapshot
            self._snapshots.move_to_end(path)
            if len(self._snapshots) > 8:
                self._snapshots.popitem(last=False)

    def create(self) -> Session:
        stamp = utc_now()
        with self._lock:
            archive = self.root / f"{stamp.year:04d}" / f"{stamp.month:02d}"
            if not archive.resolve().is_relative_to(self.root.resolve()) or self.root.is_symlink():
                raise ValueError("session archive cannot redirect outside sessions")
            archive.mkdir(parents=True, exist_ok=True)
            session_id = f"s-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
            path = archive / f"{session_id}.jsonl"
            known = {item.stem for item in self._files()}
            # 独占创建还能防止进程间同时创建文件时发生冲突。
            while True:
                if (
                    session_id in known
                    or (self.archive_root / path.relative_to(self.root)).exists()
                ):
                    session_id = f"s-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
                    path = archive / f"{session_id}.jsonl"
                    continue
                meta = {
                    "type": "meta",
                    "id": session_id,
                    "started_at": serialize_timestamp(stamp.replace(microsecond=0)),
                    "cwd": str(Path.cwd()),
                }
                try:
                    with (
                        session_file_lock(self.paths, session_id),
                        path.open("x", encoding="utf-8") as handle,
                    ):
                        handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
                    break
                except FileExistsError:
                    session_id = f"s-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
                    path = archive / f"{session_id}.jsonl"

        return Session(id=session_id, started_at=meta["started_at"], path=path, records=[meta])

    def recover_deletions(self) -> None:
        with self._lock:
            for root in self._sidecar_roots:
                pending = _sidecar_directory(root / ".deleting")
                if not pending.exists():
                    continue
                for item in pending.iterdir():
                    staged = _sidecar_directory(item)
                    if self.exists(item.name) or self.exists(item.name, archived=True):
                        original = _sidecar_directory(root / item.name)
                        if original.exists():
                            raise ValueError("Cannot restore over an existing session directory.")
                        staged.rename(original)
                    else:
                        _finish_sidecar_deletion(staged, attachments=root == self.attachments_dir)
            upgrade_originals(self.attachments_dir)

    def append(self, session: Session, record: dict) -> dict:
        raw_reasoning = record.get("reasoning")
        record = _redact_record(record)
        reasoning = record.pop("reasoning", None)
        if reasoning and reasoning != raw_reasoning:
            # 已脱敏的思考片段不能作为原始协议数据重放。
            reasoning.pop("source", None)
        _stamp_event(record)
        # 损坏尾行原样保留：只追加换行和恢复标记，后续加载据此跳过该行。
        with self._lock, session_file_lock(self.paths, session.id):
            if not session.path.resolve().is_relative_to(self.root):
                raise ValueError("Only active sessions can be modified.")
            cached = None
            with session.path.open("r+b") as handle:
                if (
                    record.keys() == {"type", "at"}
                    and record["type"] == "closed"
                    and session.damaged_tail_line is None
                ):
                    previous = self._snapshots.get(session.path)
                    if (
                        previous is not None
                        and previous.session.damaged_tail_line is None
                        and previous.version == file_version(os.fstat(handle.fileno()))
                    ):
                        cached = previous
                if reasoning:
                    attachment = self._reasoning_path(session.id, record["id"])
                    atomic_write(
                        attachment, json.dumps(reasoning, ensure_ascii=False).encode("utf-8")
                    )
                    record["reasoning_local"] = True
                handle.seek(0, 2)
                if handle.tell():
                    handle.seek(-1, 2)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
                if session.damaged_tail_line is not None:
                    recovery = {
                        "type": "recovery",
                        "ignored_line": session.damaged_tail_line,
                        "at": now_iso(),
                    }
                    handle.write((json.dumps(recovery) + "\n").encode("utf-8"))
                    session.records.append(recovery)
                    session.damaged_tail_line = None
                handle.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
                if cached is not None:
                    handle.flush()
                    version = file_version(os.fstat(handle.fileno()))
            if cached is not None:
                # 为 Runtime 保留已保存的关闭事件，界面显示的历史保持一致。
                saved = replace(cached.session, records=[*cached.session.records, record.copy()])
                self._cache_snapshot(session.path, replace(cached, session=saved, version=version))
            if reasoning:
                record["reasoning"] = reasoning
            session.records.append(record)
        return record

    def append_event_transactionally(self, session: Session, event: dict, *following: dict) -> dict:
        """在调用方的事务中一次替换文件，一并发布关联事件。"""
        with vault_transaction(self.paths) as tx:
            tx.guard(session_file_lock(self.paths, session.id))
            current = self.load(session.id, include_reasoning=False)
            records = []
            if current.damaged_tail_line is not None:
                records.append(
                    {"type": "recovery", "ignored_line": current.damaged_tail_line, "at": now_iso()}
                )
            for raw in (event, *following):
                record = _redact_record(raw)
                _stamp_event(record)
                records.append(record)
            prefix = b""
            with current.path.open("rb") as file:
                if file.seek(0, 2):
                    file.seek(-1, 2)
                    if file.read(1) != b"\n":
                        prefix = b"\n"
            payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
            tx.append(current.path, prefix + payload.encode("utf-8"))
        return records[-1]

    def advance_distilled(self, session: Session, message_id: str) -> None:
        """将下一个游标与从该消息提取的所有事实一同提交。"""
        with vault_transaction(self.paths):
            current = self.load(session.id, include_reasoning=False)
            pending = current.undistilled_messages()
            if not pending or pending[0]["id"] != message_id:
                raise ValueError("Distillation cursor no longer points at this message.")
            self.append_event_transactionally(current, {"type": "distilled", "up_to": message_id})

    def rename(self, session: Session, title: str) -> None:
        if not isinstance(title, str) or not (title := title.strip()) or len(title) > 80:
            raise ConversationError("invalid_title", "Enter a session title of 1 to 80 characters.")
        record = self.append_event_transactionally(session, {"type": "title", "title": title})
        session.records.append(record)

    def set_archived(self, session_id: str, archived: bool) -> None:
        """仅移动对话记录，保留来源 ID 及所有原始附属文件。"""
        self._validate_session_id(session_id)
        with self._lock, session_file_lock(self.paths, session_id):
            source = self._find(session_id, archived=not archived)
            existing = self._find(session_id, archived=archived)
            if source is None:
                if existing is not None:
                    return
                raise KeyError(session_id)
            root, destination = (
                (self.root, self.archive_root) if archived else (self.archive_root, self.root)
            )
            target = destination / source.relative_to(root)
            if existing is not None or target.exists() or target.resolve() != target:
                raise ValueError("Session archive destination is occupied or redirected.")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            self._snapshots.pop(source, None)
            self._listing_cache.pop(session_id, None)
            self._undistilled_cache.pop(session_id, None)

    def delete(
        self,
        session_id: str,
        *,
        archived: bool = False,
        before_delete: Callable[[], None] | None = None,
    ) -> bool:
        self._validate_session_id(session_id)
        with self._lock, session_file_lock(self.paths, session_id):
            path = self._find(session_id, archived=archived)
            if path is None:
                return False
            with ExitStack() as sidecars:
                for root in self._sidecar_roots:
                    sidecars.enter_context(
                        _stage_sidecars(root, session_id, attachments=root == self.attachments_dir)
                    )
                # 事务提交后才清理暂存的附件；中断时先恢复事务，再按会话是否存在恢复附件。
                with vault_transaction(self.paths) as tx:
                    if before_delete is not None:
                        before_delete()
                    tx.write(path, None)
            self._snapshots.pop(path, None)
            self._listing_cache.pop(session_id, None)
            self._undistilled_cache.pop(session_id, None)
        return True
