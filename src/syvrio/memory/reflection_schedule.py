"""自动回顾的跨重启调度：低频周期、变更判断、冷却与失败退避。

只读取状态和文件元信息供 UI 轻量检查；完整事实扫描只在 Worker 中执行。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..time import parse_timestamp, serialize_timestamp, utc_now
from .detect import contains_secret
from .inference import source_fingerprint
from .models import Entry

if TYPE_CHECKING:
    from .reflection import ReflectionEngine, ReflectionResult

REFLECTION_STATE_VERSION = 1
PROFILE_IMPORTANT_TOPICS = frozenset(
    {
        "preferences",
        "goals",
        "constraints",
        "schedule",
        "career",
        "work",
        "偏好",
        "喜好",
        "目标",
        "约束",
        "习惯",
        "日程",
        "职业",
        "工作",
    }
)
PROFILE_IMPORTANT_KEYWORDS = (
    "长期",
    "目标",
    "职业",
    "工作",
    "转行",
    "计划",
    "方向",
    "goal",
    "career",
    "long-term",
    "long term",
)
MAX_AUTO_RETRY_HOURS = 24.0


@dataclass
class FactChanges:
    count: int
    important: bool
    digest: str
    fingerprints: dict[str, str]
    important_ids: list[str]


class ReflectionSchedule:
    def __init__(self, engine: ReflectionEngine) -> None:
        self.engine = engine
        self.service = engine.service
        self.path = self.service.paths.state_dir / "reflection.json"
        self.fingerprints_path = self.service.paths.state_dir / "fingerprints.json"
        self._vault_lock = engine._vault_lock

    def default_state(self) -> dict:
        return {
            "version": REFLECTION_STATE_VERSION,
            "last_seen_facts_signature": "",
            "last_checked_at": None,
            "profile": {
                "last_attempt_at": None,
                "last_success_at": None,
                "next_retry_at": None,
                "fact_digest": "",
                "pending_changes": 0,
                "pending_important": False,
                "pending_inferences": False,
                "consecutive_failures": 0,
                "last_error_kind": None,
            },
            "dream": {
                "last_attempt_at": None,
                "last_success_at": None,
                "next_retry_at": None,
                "fact_digest": "",
                "pending": False,
                "remaining_batches": 0,
                "consecutive_failures": 0,
                "last_error_kind": None,
            },
        }

    def load(self) -> dict:
        """缺失或损坏时安全回退到首次运行，不阻断自动调度。"""
        default = self.default_state()
        if not self.path.exists():
            return default
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return default
        if not isinstance(data, dict) or data.get("version") != REFLECTION_STATE_VERSION:
            return default
        if isinstance(data.get("last_seen_facts_signature"), str):
            default["last_seen_facts_signature"] = data["last_seen_facts_signature"]
        if (checked_at := data.get("last_checked_at")) is not None:
            default["last_checked_at"] = serialize_timestamp(parse_timestamp(checked_at))
        for section in ("profile", "dream"):
            raw = data.get(section)
            if not isinstance(raw, dict):
                continue
            for key in default[section]:
                if key in raw:
                    value = raw[key]
                    if key.endswith("_at") and value is not None:
                        value = serialize_timestamp(parse_timestamp(value))
                    default[section][key] = value
        for section in ("profile", "dream"):
            for key in ("pending_changes", "consecutive_failures", "remaining_batches"):
                if key in default[section] and type(default[section][key]) is not int:
                    default[section][key] = 0
            if default[section].get("remaining_batches", 0) < 0:
                default[section]["remaining_batches"] = 0
            for key in ("pending", "pending_important", "pending_inferences"):
                if key in default[section] and type(default[section][key]) is not bool:
                    default[section][key] = False
        return default

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with self._vault_lock:
            temporary.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(self.path)

    @staticmethod
    def parse_time(value) -> datetime | None:
        return parse_timestamp(value) if value is not None else None

    @staticmethod
    def stamp(now: datetime) -> str:
        return serialize_timestamp(now.replace(microsecond=0))

    def elapsed_hours(self, value, now: datetime) -> float | None:
        parsed = self.parse_time(value)
        if parsed is None or parsed > now + timedelta(hours=24):
            return None
        return max(0.0, (now - parsed).total_seconds() / 3600)

    def retry_due(self, section: dict, now: datetime) -> bool:
        retry = self.parse_time(section.get("next_retry_at"))
        return retry is not None and now >= retry

    def retry_blocked(self, section: dict, now: datetime) -> bool:
        retry = self.parse_time(section.get("next_retry_at"))
        return retry is not None and now < retry

    def summary_needs_refresh(self) -> bool:
        """轻量判断摘要是否需要重建：只读缓存与文件元信息，不扫描事实正文。"""
        summary = self.engine.profile_summary()
        if summary is not None:
            return summary.stale
        try:
            return bool(self.service.store.fingerprint())
        except OSError:
            return False

    @staticmethod
    def important_entry(entry: Entry) -> bool:
        if Path(entry.topic).stem.casefold() in PROFILE_IMPORTANT_TOPICS:
            return True
        text = entry.text.casefold()
        return any(keyword in text for keyword in PROFILE_IMPORTANT_KEYWORDS)

    def load_fingerprints(self) -> tuple[dict[str, str], set[str]]:
        if not self.fingerprints_path.exists():
            return {}, set()
        try:
            data = json.loads(self.fingerprints_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}, set()
        if not isinstance(data, dict):
            return {}, set()
        raw = data.get("fingerprints")
        fingerprints = (
            {
                key: value
                for key, value in raw.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if isinstance(raw, dict)
            else {}
        )
        raw_important = data.get("important_ids")
        important = (
            {value for value in raw_important if isinstance(value, str)}
            if isinstance(raw_important, list)
            else set()
        )
        return fingerprints, important

    def write_fingerprints(self, fingerprints: dict[str, str], important_ids: list[str]) -> None:
        self.fingerprints_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.fingerprints_path.with_name(self.fingerprints_path.name + ".tmp")
        payload = {
            "version": REFLECTION_STATE_VERSION,
            "fingerprints": fingerprints,
            "important_ids": important_ids,
        }
        with self._vault_lock:
            temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(self.fingerprints_path)

    def facts_changes(
        self, entries: list[Entry], previous: dict[str, str], remembered_important: set[str]
    ) -> FactChanges:
        current = {entry.id: source_fingerprint(entry) for entry in entries}
        new_ids = set(current) - set(previous)
        changed_ids = {
            key for key, value in current.items() if key in previous and previous[key] != value
        }
        removed_ids = set(previous) - set(current)
        important_current = [entry.id for entry in entries if self.important_entry(entry)]
        # 只有本轮新增、更正或删除的事实才参与“重要变化”判断；
        # 历史中已存在的重要事实不能把之后的普通变更升级为重要变更。
        important_new = any(self.important_entry(entry) for entry in entries if entry.id in new_ids)
        important_changed = any(
            entry.id in remembered_important or self.important_entry(entry)
            for entry in entries
            if entry.id in changed_ids
        )
        important_removed = any(key in remembered_important for key in removed_ids)
        important_ids = sorted(
            set(important_current) | set(key for key in removed_ids if key in remembered_important)
        )
        digest = hashlib.sha256(
            "|".join(f"{key}:{current[key]}" for key in sorted(current)).encode("utf-8")
        ).hexdigest()
        return FactChanges(
            count=len(new_ids) + len(changed_ids) + len(removed_ids),
            important=bool(important_new or important_changed or important_removed),
            digest=digest,
            fingerprints=current,
            important_ids=important_ids,
        )

    def check_due(self, now: datetime | None = None) -> bool:
        """UI 线程可调用的轻量检查：只读状态和文件元信息，不扫描记忆内容。"""
        now = now or utc_now()
        config = self.service.config.chat
        state = self.load()
        changed = self.engine._facts_signature() != state["last_seen_facts_signature"]
        profile, dream = state["profile"], state["dream"]
        profile_interval = max(0.1, config.profile_auto_interval_hours)
        profile_cooldown = max(0.1, config.profile_min_cooldown_hours)
        dream_interval = max(0.1, config.dream_auto_interval_hours)
        profile_elapsed = self.elapsed_hours(profile.get("last_attempt_at"), now)
        dream_elapsed = self.elapsed_hours(dream.get("last_attempt_at"), now)
        profile_due = profile_elapsed is None or profile_elapsed >= profile_interval
        profile_cool = profile_elapsed is None or profile_elapsed >= profile_cooldown
        dream_due = dream_elapsed is None or dream_elapsed >= dream_interval
        profile_retry = self.retry_due(profile, now)
        dream_retry = self.retry_due(dream, now)
        profile_blocked = self.retry_blocked(profile, now)
        dream_blocked = self.retry_blocked(dream, now)
        pending_changes = int(profile.get("pending_changes") or 0)
        pending_important = bool(profile.get("pending_important"))
        pending_inferences = bool(profile.get("pending_inferences"))
        dream_pending = bool(dream.get("pending"))
        summary_needs = self.summary_needs_refresh()
        dream_ready = (
            not dream_blocked and (changed or dream_pending) and (dream_due or dream_retry)
        )
        profile_ready = not profile_blocked and (
            (changed and (profile_due or profile_cool or profile_retry))
            or (pending_important and (profile_cool or profile_retry))
            or (
                pending_changes >= max(1, config.profile_min_changes)
                and (profile_cool or profile_retry)
            )
            or (pending_changes > 0 and (profile_due or profile_retry))
            or (summary_needs and (profile_due or profile_retry))
            or (pending_inferences and (profile_due or profile_retry))
        )
        return dream_ready or profile_ready

    def plan(self, now: datetime | None = None) -> str | None:
        """在 Worker 中扫描一次事实变化，决定本轮自动任务；不调用模型。"""
        if self.engine.running:
            return None
        now = now or utc_now()
        config = self.service.config.chat
        state = self.load()
        with self.service.write_lock:
            entries = self.service.current_facts()
            inferences = self.service.list_inferences()
        entries = sorted(
            (item for item in entries if not contains_secret(item.model_dump_json())),
            key=lambda item: item.id,
        )
        previous, remembered_important = self.load_fingerprints()
        changes = self.facts_changes(entries, previous, remembered_important)
        profile, dream = state["profile"], state["dream"]
        profile_elapsed = self.elapsed_hours(profile.get("last_attempt_at"), now)
        dream_elapsed = self.elapsed_hours(dream.get("last_attempt_at"), now)
        profile_due = profile_elapsed is None or profile_elapsed >= max(
            0.1, config.profile_auto_interval_hours
        )
        profile_cool = profile_elapsed is None or profile_elapsed >= max(
            0.1, config.profile_min_cooldown_hours
        )
        dream_due = dream_elapsed is None or dream_elapsed >= max(
            0.1, config.dream_auto_interval_hours
        )
        profile_retry = self.retry_due(profile, now)
        dream_retry = self.retry_due(dream, now)
        profile_blocked = self.retry_blocked(profile, now)
        dream_blocked = self.retry_blocked(dream, now)

        summary = self.engine.profile_summary()
        cached = self.engine._load_summary()
        if summary is None:
            summary_stale = bool(entries)
        else:
            stored = cached.get("evidence_signature") if cached else None
            current = self.engine._summary_evidence(entries, inferences)
            if stored == current:
                if summary.stale:
                    self.engine._resign_summary()
                summary_stale = False
            else:
                summary_stale = True

        # 失败遗留的待办不能因本轮没有新变更而被清掉；计划阶段只累加，不重置。
        pending_changes = max(int(profile.get("pending_changes") or 0), changes.count)
        pending_important = bool(profile.get("pending_important")) or changes.important
        pending_inferences = bool(profile.get("pending_inferences"))
        profile_pending = (
            pending_changes > 0 or pending_important or summary_stale or pending_inferences
        )
        valuable_now = pending_important or pending_changes >= max(1, config.profile_min_changes)
        profile_candidate = (
            not profile_blocked
            and profile_pending
            and (profile_due or profile_retry or (profile_cool and valuable_now))
        )
        baseline = dream.get("fact_digest")
        dream_pending = bool(dream.get("pending")) or (
            bool(entries) if not baseline else baseline != changes.digest
        )
        dream_candidate = not dream_blocked and dream_pending and (dream_due or dream_retry)

        state["last_seen_facts_signature"] = self.engine._facts_signature()
        state["last_checked_at"] = self.stamp(now)
        profile["pending_changes"] = pending_changes
        profile["pending_important"] = pending_important
        profile["pending_inferences"] = pending_inferences
        dream["pending"] = dream_pending
        if not dream_pending:
            dream["remaining_batches"] = 0
        self.save(state)

        if profile_candidate and dream_candidate:
            profile_last = self.parse_time(profile.get("last_attempt_at"))
            dream_last = self.parse_time(dream.get("last_attempt_at"))
            if profile_last is not None and dream_last is not None and dream_last < profile_last:
                return "dream"
            return "profile"
        if profile_candidate:
            return "profile"
        if dream_candidate:
            return "dream"
        return None

    @staticmethod
    def error_kind(error: str) -> str:
        if "超时" in error:
            return "timeout"
        if "来源" in error:
            return "stale"
        if "摘要" in error:
            return "summary"
        if "校验" in error:
            return "validation"
        return "request"

    def record_run(
        self, mode: str, result: ReflectionResult, entries: list[Entry] | None, auto: bool
    ) -> None:
        # 无新请求但发布了已校验缓存时也算进展，需要同步自动调度状态。
        if result.requests == 0 and result.completed == 0 and not auto:
            return
        now = utc_now()
        state = self.load()
        section = state["profile" if mode == "profile" else "dream"]
        if result.cancelled:
            if result.requests:
                section["last_attempt_at"] = self.stamp(now)
            if mode == "dream" and result.remaining > 0:
                section["pending"] = True
                section["remaining_batches"] = result.remaining
            elif mode == "profile" and result.remaining > 0:
                section["pending_inferences"] = True
        elif result.error:
            failures = int(section.get("consecutive_failures") or 0) + 1
            section["consecutive_failures"] = failures
            section["last_error_kind"] = self.error_kind(result.error)
            # 失败不等于已处理：保留待办标记，手动失败后自动调度仍可接续。
            if mode == "profile":
                section["pending_changes"] = max(1, int(section.get("pending_changes") or 0))
                if result.remaining > 0:
                    section["pending_inferences"] = True
            else:
                section["pending"] = True
                section["remaining_batches"] = max(1, result.remaining)
            if auto:
                section["last_attempt_at"] = self.stamp(now)
                hours = min(MAX_AUTO_RETRY_HOURS, 2 ** (failures - 1))
                section["next_retry_at"] = self.stamp(now + timedelta(hours=hours))
        else:
            section["last_attempt_at"] = self.stamp(now)
            section["last_success_at"] = self.stamp(now)
            section["next_retry_at"] = None
            section["consecutive_failures"] = 0
            section["last_error_kind"] = None
            if entries is not None:
                previous, remembered_important = self.load_fingerprints()
                changes = self.facts_changes(entries, previous, remembered_important)
                if mode == "profile":
                    self.write_fingerprints(changes.fingerprints, changes.important_ids)
                    section["fact_digest"] = changes.digest
                    section["pending_changes"] = 0
                    section["pending_important"] = False
                    # 摘要完成不等于推断全部完成；剩余批次留待下一周期继续。
                    section["pending_inferences"] = result.remaining > 0
                elif result.remaining > 0:
                    # 只完成部分批次，保留旧基线以便下一次到期继续处理。
                    section["pending"] = True
                    section["remaining_batches"] = result.remaining
                else:
                    section["fact_digest"] = changes.digest
                    section["pending"] = False
                    section["remaining_batches"] = 0
        state["last_seen_facts_signature"] = self.engine._facts_signature()
        self.save(state)

    def overview(self, now: datetime | None = None) -> dict:
        now = now or utc_now()
        state = self.load()
        config = self.service.config.chat
        result = {}
        for mode, interval, cooldown in (
            ("profile", config.profile_auto_interval_hours, config.profile_min_cooldown_hours),
            ("dream", config.dream_auto_interval_hours, 0.0),
        ):
            section = state[mode]
            last = self.parse_time(section.get("last_attempt_at"))
            next_at = last + timedelta(hours=max(0.1, interval)) if last else now
            retry = self.parse_time(section.get("next_retry_at"))
            blocked = retry is not None and retry > now
            if retry is not None:
                next_at = retry if blocked else now
            count = int(section.get("pending_changes") or 0)
            if (
                mode == "profile"
                and not blocked
                and last
                and (
                    section.get("pending_important") or count >= max(1, config.profile_min_changes)
                )
            ):
                early = last + timedelta(hours=max(0.1, cooldown))
                if early < next_at:
                    next_at = early
            result[mode] = {
                "next_attempt_at": self.stamp(next_at),
                "pending": count if mode == "profile" else bool(section.get("pending")),
                "pending_inferences": bool(section.get("pending_inferences")),
                "remaining_batches": int(section.get("remaining_batches") or 0),
                "failures": int(section.get("consecutive_failures") or 0),
            }
        return result
