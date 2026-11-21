"""Read-only session history, listing and original-message lookup."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..infra.attachments import AttachmentBatch
from ..infra.diagnostics import record_event
from ..infra.documents import DocumentError
from ..infra.paths import SyvrioPaths, vault_paths
from ..time import parse_timestamp, serialize_timestamp
from .model import Session

_LOG = logging.getLogger(__name__)

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _session_sort_time(value: str) -> datetime:
    return parse_timestamp(value) if value else datetime.min.replace(tzinfo=UTC)


def _validate_record(record: Any) -> None:
    # Syntactically valid JSON may still be unusable session data. Do not
    # classify such complete records as torn tails or coerce historical data.
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise ValueError("Session record must be an object with a string type")
    for key in ("id", "at", "started_at", "title", "role", "up_to"):
        if key in record and not isinstance(record[key], str):
            raise ValueError(f"Invalid session field: {key}")
    if "original_local_date" in record:
        local = record["original_local_date"]
        if not isinstance(local, str) or date.fromisoformat(local).isoformat() != local:
            raise ValueError("Invalid original message calendar date.")
    for key in ("at", "started_at"):
        if record.get(key):
            record[key] = serialize_timestamp(parse_timestamp(record[key]))
    if "content" in record and not isinstance(record["content"], str):
        if record["type"] != "assistant" or record["content"] is not None:
            raise ValueError("Invalid session content")
    if record["type"] == "message" and "content" not in record:
        raise ValueError("Missing session message content")
    if "covered_records" in record:
        count = record["covered_records"]
        if type(count) is not int or count < 0:
            raise ValueError("Invalid session summary boundary")
    if "distilled_ids" in record:
        ids = record["distilled_ids"]
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError("Invalid session distilled IDs")
    calls = record.get("tool_calls")
    if calls is not None and (
        not isinstance(calls, list)
        or any(
            not isinstance(call, dict) or not isinstance(call.get("name", ""), str)
            for call in calls
        )
    ):
        raise ValueError("Invalid session tool calls")


class SessionQuery:
    def __init__(self, vault: Path | SyvrioPaths):
        paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.root = paths.sessions_dir
        self.archive_root = paths.archived_sessions_dir
        self.attachments_dir = paths.attachments_dir
        self.runtime_dir = paths.runtime_dir
        self._reasoning_root = paths.runtime_dir / "session-reasoning"
        self._listing_cache: dict[str, tuple[tuple[Path, int, int], dict]] = {}
        # 补沉淀扫描只需知道文件还有多少待抽取消息，不必保留完整会话。
        self._undistilled_cache: dict[str, tuple[tuple[Path, int, int], int]] = {}

    def exists(self, session_id: str, *, archived: bool = False) -> bool:
        return self._path(session_id, archived=archived).exists()

    def verify_attachments(self) -> tuple[int, list[str]]:
        """Verify the archive against every original message, including undone turns."""
        checked = 0
        errors = []
        if not self.root.is_dir():
            return 0, ["The selected Vault has no sessions directory."]
        try:
            files = self._files()
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

    def _load_path(self, path: Path, *, include_reasoning: bool) -> Session:
        session_id = path.stem
        self._validate_session_id(session_id)
        if not path.exists():
            raise KeyError(f"会话不存在：{session_id}")
        if path.resolve() != path or not (
            path.is_relative_to(self.root) or path.is_relative_to(self.archive_root)
        ):
            raise ValueError("symlink in session archive")
        lines = path.read_bytes().splitlines()
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
            _validate_record(record)
            record.pop("reasoning", None)  # Only local Runtime attachments can carry replay data.
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
                        continue  # Runtime is local and may be absent after a Vault sync.
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

    def _files(self, *, archived: bool = False) -> list[Path]:
        root = self.archive_root if archived else self.root
        if root.resolve() != root:
            raise ValueError("symlink in session archive")
        files = sorted(root.rglob("*.jsonl"))
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

    def _path(self, session_id: str, *, archived: bool = False) -> Path:
        self._validate_session_id(session_id)
        root = self.archive_root if archived else self.root
        return next(
            (path for path in self._files(archived=archived) if path.stem == session_id),
            root / "legacy" / "undated" / f"{session_id}.jsonl",
        )

    def user_message(self, session_id: str, message_id: str, *, active_only: bool = False) -> dict:
        """Look up exactly one original user message without loading reasoning or other sidecars."""
        if not isinstance(message_id, str) or not SESSION_ID_RE.fullmatch(message_id):
            raise ValueError("Invalid source message ID.")
        session = self.load(session_id, include_reasoning=False)
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
