from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..infra.diagnostics import record_event
from ..infra.locking import vault_write_lock
from ..infra.paths import SyvrioPaths, vault_paths
from ..infra.tokens import estimate_tokens
from ..memory.detect import redact_secrets
from ..memory.store import atomic_write
from ..time import (
    local_timestamp,
    parse_timestamp,
    serialize_timestamp,
    utc_now,
    utc_timestamp,
)

_LOG = logging.getLogger(__name__)

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def now_iso() -> str:
    return utc_timestamp()


def _session_sort_time(value: str) -> datetime:
    return parse_timestamp(value) if value else datetime.min.replace(tzinfo=UTC)


def new_message_id() -> str:
    stamp = utc_now()
    return f"m-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def _redact_record(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_record(item) for item in value]
    return value


def _validate_record(record: Any) -> None:
    # Syntactically valid JSON may still be unusable session data. Do not
    # classify such complete records as torn tails or coerce historical data.
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise ValueError("Session record must be an object with a string type")
    for key in ("id", "at", "started_at", "title", "role", "up_to"):
        if key in record and not isinstance(record[key], str):
            raise ValueError(f"Invalid session field: {key}")
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


@dataclass
class Session:
    id: str
    started_at: str
    path: Path
    records: list[dict] = field(default_factory=list)
    damaged_tail_line: int | None = None

    def active_records(self) -> list[dict]:
        """按 undo 事件投影有效会话；撤销同时令其后的摘要失效。"""
        active: list[dict] = []
        for record in self.records:
            record_type = record.get("type")
            if record_type == "undo":
                target_id = record.get("message_id")
                target = next(
                    (
                        index
                        for index, item in enumerate(active)
                        if item.get("id") == target_id
                        and item.get("type") == "message"
                        and item.get("role") == "user"
                    ),
                    None,
                )
                if target is not None:
                    del active[target:]
            elif record_type in ("message", "assistant", "tool", "summary"):
                active.append(record)
        return active

    @property
    def messages(self) -> list[dict]:
        return [record for record in self.active_records() if record.get("type") == "message"]

    @property
    def updated_at(self) -> str:
        stamps = [record["at"] for record in self.records if record.get("at")]
        return stamps[-1] if stamps else self.started_at

    @property
    def last_message_at(self) -> str:
        """最后一条有效消息的时间；重命名/摘要/沉淀等控制记录不得改变会话顺序。"""
        for record in reversed(self.active_records()):
            if record.get("type") == "message" and record.get("at"):
                return str(record["at"])
        return self.started_at

    def user_texts(self) -> list[str]:
        return [
            record["content"]
            for record in self.active_records()
            if record.get("type") == "message" and record.get("role") == "user"
        ]

    def last_user_message_id(self) -> str | None:
        for record in reversed(self.active_records()):
            if record.get("type") == "message" and record.get("role") == "user":
                return record.get("id")
        return None

    def title(self, width: int = 40) -> str:
        for record in reversed(self.records):
            if record.get("type") == "title":
                return record.get("title", "")[:width]
        texts = self.user_texts()
        if not texts:
            return "(空会话)"
        title = texts[0].replace("\n", " ").strip()
        return title[:width] + ("…" if len(title) > width else "")

    def latest_summary(self) -> dict | None:
        for record in reversed(self.active_records()):
            if record.get("type") == "summary":
                return record
        return None

    def summary_text(self) -> str:
        summary = self.latest_summary()
        return summary.get("content", "") if summary else ""

    def covered_records(self) -> int:
        summary = self.latest_summary()
        return int(summary.get("covered_records", 0)) if summary else 0

    def records_after_summary(self) -> list[dict]:
        active_ids = {id(record) for record in self.active_records()}
        return [
            record
            for record in self.records[self.covered_records() :]
            if id(record) in active_ids and record.get("type") in ("message", "assistant", "tool")
        ]

    def needs_consolidation(self) -> bool:
        """以沉淀游标为准：游标后还有用户消息就需要补记。

        正常退出/补记成功会推进游标；异常退出、抽取失败或恢复后再次崩溃时
        游标不动，因此不依赖 closed/consolidated 标记的历史顺序。
        """
        return bool(self.user_texts_after_distilled())

    def user_texts_after_summary(self) -> list[str]:
        return [
            record["content"]
            for record in self.records_after_summary()
            if record.get("type") == "message" and record.get("role") == "user"
        ]

    def undistilled_messages(self) -> list[dict]:
        positions = {
            record["id"]: index for index, record in enumerate(self.records) if record.get("id")
        }
        covered = 0
        distilled_ids: set[str] = set()
        # 沉淀是不可由 undo 撤回的写入结果，确认信息须读取原始流而非有效会话投影。
        for record in self.records:
            if record.get("type") == "distilled":
                covered = max(covered, positions.get(record.get("up_to"), -1) + 1)
            elif record.get("type") == "summary":
                if "distilled_ids" in record:
                    distilled_ids.update(record["distilled_ids"])
                else:
                    # 旧摘要没有逐消息确认信息，沿用原有边界；新摘要的空列表不代表已沉淀。
                    covered = max(covered, int(record.get("covered_records", 0)))
        active_ids = {id(record) for record in self.active_records()}
        return [
            record
            for record in self.records[covered:]
            if id(record) in active_ids
            and record.get("type") == "message"
            and record.get("role") == "user"
            and record.get("id") not in distilled_ids
        ]

    def user_texts_after_distilled(self) -> list[str]:
        return [record["content"] for record in self.undistilled_messages()]

    def distillation_texts(self) -> list[str]:
        return [dated_message(record) for record in self.undistilled_messages()]

    def estimated_tokens(self) -> int:
        return sum(
            estimate_tokens(str(record.get("content") or ""))
            for record in self.active_records()
            if record.get("type") in ("message", "assistant", "summary")
        )


class SessionStore:
    def __init__(self, vault: Path | SyvrioPaths):
        paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.root = paths.sessions_dir
        self._reasoning_root = paths.runtime_dir / "session-reasoning"
        # 会话追加与 Git 快照共用 Vault 写锁，避免提交到写了一半的 JSONL。
        self._lock = vault_write_lock(paths)
        self._listing_cache: dict[str, tuple[tuple[Path, int, int], dict]] = {}
        # 补沉淀扫描只需知道文件还有多少待抽取消息，不必保留完整会话。
        self._undistilled_cache: dict[str, tuple[tuple[Path, int, int], int]] = {}

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
            # Exclusive creation also protects the collision window between processes.
            while True:
                if session_id in known:
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
                    with path.open("x", encoding="utf-8") as handle:
                        handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
                    break
                except FileExistsError:
                    session_id = f"s-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
                    path = archive / f"{session_id}.jsonl"

        return Session(id=session_id, started_at=meta["started_at"], path=path, records=[meta])

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def load(self, session_id: str, *, include_reasoning: bool = True) -> Session:
        return self._load_path(self._path(session_id), include_reasoning=include_reasoning)

    def _load_path(self, path: Path, *, include_reasoning: bool) -> Session:
        session_id = path.stem
        self._validate_session_id(session_id)
        if not path.exists():
            raise KeyError(f"会话不存在：{session_id}")
        if (
            self.root.is_symlink()
            or path.is_symlink()
            or not path.resolve().is_relative_to(self.root.resolve())
            or any(
                parent.is_symlink() for parent in path.parents if parent.is_relative_to(self.root)
            )
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
                    attachment = self._reasoning_path(session_id, record["id"])
                    try:
                        reasoning = json.loads(attachment.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        continue  # Runtime is local and may be absent after a Vault sync.
                    if (
                        not isinstance(reasoning, dict)
                        or not isinstance(reasoning.get("text"), str)
                        or not isinstance(reasoning.get("source", ""), str)
                        or reasoning.get("seconds") is not None
                        and type(reasoning["seconds"]) not in (float, int)
                    ):
                        raise ValueError("Invalid local session reasoning")
                    record["reasoning"] = reasoning
        return Session(
            id=session_id,
            started_at=meta.get("started_at", ""),
            path=path,
            records=records,
            damaged_tail_line=damaged_tail,
        )

    def append(self, session: Session, record: dict) -> dict:
        raw_reasoning = record.get("reasoning")
        record = _redact_record(record)
        reasoning = record.pop("reasoning", None)
        if reasoning and reasoning != raw_reasoning:
            # A scrubbed reasoning fragment must never be replayed as original protocol data.
            reasoning.pop("source", None)
        record["at"] = (
            serialize_timestamp(parse_timestamp(record["at"])) if "at" in record else now_iso()
        )
        # 损坏尾行原样保留：只追加换行和恢复标记，后续加载据此跳过该行。
        with self._lock:
            if reasoning:
                attachment = self._reasoning_path(session.id, record["id"])
                atomic_write(attachment, json.dumps(reasoning, ensure_ascii=False).encode("utf-8"))
                record["reasoning_local"] = True
            with session.path.open("a+b") as handle:
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
            if reasoning:
                record["reasoning"] = reasoning
            session.records.append(record)
        return record

    def _reasoning_path(self, session_id: str, record_id: str) -> Path:
        if not SESSION_ID_RE.fullmatch(session_id) or not SESSION_ID_RE.fullmatch(record_id):
            raise ValueError("Invalid local session reasoning identifier")
        path = self._reasoning_root / session_id / f"{record_id}.json"
        if any(
            p.is_symlink() for p in (self._reasoning_root, path.parent, path)
        ) or not path.resolve().is_relative_to(self._reasoning_root.parent.resolve()):
            raise ValueError("Local session reasoning cannot redirect outside Runtime")
        return path

    def rename(self, session: Session, title: str) -> None:
        self.append(session, {"type": "title", "title": title.strip()[:80]})

    def delete(self, session_id: str) -> bool:
        with self._lock:
            path = self._path(session_id)
            if not path.exists():
                return False
            attachments = self._reasoning_path(session_id, "attachment").parent
            for attachment in attachments.glob("*.json"):
                attachment.unlink()
            path.unlink()
            self._listing_cache.pop(session_id, None)
            self._undistilled_cache.pop(session_id, None)
        return True

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

    def list(self, limit: int = 20, with_tokens: bool = True) -> list[dict]:
        if not self.root.exists():
            return []
        sessions = []
        seen: set[str] = set()
        for path in self._files():
            row = self._listing_row(path, with_tokens)
            if row is None:
                continue
            seen.add(path.stem)
            sessions.append(row)
        for stale in set(self._listing_cache) - seen:
            self._listing_cache.pop(stale, None)
        sessions.sort(key=lambda item: _session_sort_time(item["last_message_at"]), reverse=True)
        return sessions[:limit]

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

    def _files(self) -> list[Path]:
        files = sorted(self.root.rglob("*.jsonl"))
        seen: set[str] = set()
        for path in files:
            if self.root.is_symlink() or path.is_symlink() or path.stem in seen:
                raise ValueError("duplicate session ID or symlink in session archive")
            seen.add(path.stem)
        return files

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise KeyError(f"非法会话 ID：{session_id}")

    def _path(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return next(
            (path for path in self._files() if path.stem == session_id),
            self.root / "legacy" / "undated" / f"{session_id}.jsonl",
        )


def dated_message(record: dict) -> str:
    raw = record.get("at")
    if not raw:
        return record.get("content", "")
    stamp = local_timestamp(raw)
    return f"[原始消息时间：{stamp}]\n{record.get('content', '')}"


def record_label(record: dict) -> str:
    record_type = record.get("type")
    if record_type == "message":
        return f"用户：{record.get('content', '')}"
    if record_type == "assistant":
        content = (record.get("content") or "").strip()
        tools = [call.get("name", "") for call in record.get("tool_calls") or []]
        suffix = f"（调用工具：{', '.join(tools)}）" if tools else ""
        return f"助手：{content}{suffix}"
    if record_type == "tool":
        return f"工具 {record.get('name')}：{record.get('content', '')}"
    if record_type == "summary":
        return f"此前摘要：{record.get('content', '')}"
    return ""


def transcript(records: list[dict]) -> str:
    return "\n".join(label for label in map(record_label, records) if label)
