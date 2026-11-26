"""Vault Git 同步：Commit/Push 解耦、每日调度判断与本地执行状态。

状态写入数据库 `git_sync` 表，只记录本机实际执行结果；
Git 不保存 Push 时间，因此“最近成功 Push”只来自这里的成功记录。
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable

from ..config import Config
from ..storage.database import encode, transaction, transaction_mode
from ..storage.locking import LockTimeout, git_operation_lock
from ..storage.paths import SyvrioPaths
from ..system.gitops import GitError
from ..time import parse_timestamp, serialize_timestamp, utc_now
from .commit_summary import summarize_diff
from .export import ExportSnapshot, publish_export
from .git import VaultGitRepo, ensure_vault_gitignore

STAGE_CHECK = "Checking working tree"
STAGE_COMMIT = "Creating commit"
STAGE_PUSH = "Pushing to remote"
STAGE_REFRESH = "Refreshing status"
STAGES = (STAGE_CHECK, STAGE_COMMIT, STAGE_PUSH, STAGE_REFRESH)

RETRYABLE = frozenset({"network", "timeout", "busy", "vault_busy"})


def _now() -> datetime:
    return utc_now().astimezone()


def _parse_stamp(value: str | None) -> datetime | None:
    return parse_timestamp(value) if value is not None else None


@dataclass
class SyncStage:
    name: str
    state: str = "waiting"  # 同步状态可取 waiting、running、done、skipped 或 failed。


@dataclass
class SyncResult:
    status: str  # 同步结果可取 success、partial 或 failed。
    trigger: str = "manual"
    committed: bool = False
    commit_hash: str | None = None
    pushed: bool = False
    push_skipped: bool = False
    error: str | None = None
    error_kind: str | None = None
    stages: list[SyncStage] = field(default_factory=list)
    snapshot: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


class GitSyncService:
    def __init__(self, config: Config, paths: SyvrioPaths) -> None:
        self.config = config
        self.paths = paths
        paths.validate()
        self.repo = VaultGitRepo(paths.vault_dir / "export")
        self._export: ExportSnapshot | None = None
        self._running = threading.Lock()
        self._git_lock = git_operation_lock(paths)

    def _publish_export(self, *, check_only: bool = False) -> None:
        published = publish_export(self.paths, self.repo.path, self._export if check_only else None)
        # 回滚后，同一版号可能对应另一批内容，不能缓存未提交的导出。
        self._export = None if transaction_mode() == "write" else published

    def ensure_ignore_rules(self) -> None:
        with self._git_lock:
            if (self.repo.path / ".git").exists() and self.repo.is_repo:
                ensure_vault_gitignore(self.repo.path)

    def initialize(self, prepare_files: Callable[[], None]) -> dict:
        with self._git_lock:
            created = self.repo.ensure_init()
            prepare_files()
            self._publish_export()
            ensure_vault_gitignore(self.repo.path)
            self.repo.stage_all_checked()
            commit_hash = self.repo.commit("memory: 初始化记忆库")
        return {"vault": str(self.paths.vault_dir), "git_created": created, "commit": commit_hash}

    def is_dirty(self) -> bool:
        with self._git_lock:
            if not self.repo.is_repo:
                return False
            self._publish_export(check_only=True)
            return self.repo.has_changes()

    def history(self, limit: int = 10) -> list[dict]:
        return self.repo.history(limit)

    def commit_pending_changes(self) -> str | None:
        """启动检查用：自动提交 vault 未保存变更（不 push、不调 LLM 生成总结）。"""
        with self._git_lock:
            self.repo.ensure_init()
            self._publish_export()
            self.repo.stage_all_checked()
            return self.repo.commit("memory: 自动提交未保存变更（启动检查）")

    def commit(self, push: bool | None = None) -> dict:
        with self._git_lock:
            return self._commit_locked(push)

    def _commit_locked(self, push: bool | None) -> dict:
        self.repo.ensure_init()
        self._publish_export()
        self.repo.stage_all_checked()
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
                        self.record_push_success()
                    except GitError as exc:
                        result["push_error"] = str(exc)
            return result
        # LLM 摘要与 Push 都不占 Vault 写锁；Git 锁仍持有，其他 Git 操作不会改 Index。
        summary = summarize_diff(diff, self.config.llm, stat, self.config.chat.context_window)
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
                self.record_push_success()
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

    # ---------- 状态 ----------

    @property
    def running(self) -> bool:
        return self._running.locked()

    def _read_state(self):
        with transaction(self.paths, write=False) as db:
            row = db.execute("SELECT state FROM git_sync WHERE singleton=1").fetchone()
            return json.loads(row[0]) if row else {}

    def _update_state(self, changes: dict) -> None:
        with transaction(self.paths) as db:
            row = db.execute("SELECT state FROM git_sync WHERE singleton=1").fetchone()
            state = json.loads(row[0]) if row else {}
            state.update(changes)
            db.execute("INSERT OR REPLACE INTO git_sync VALUES(1,?)", (encode(state),))

    def record_push_success(self, now: datetime | None = None) -> None:
        """CLI 等入口实际 push 成功后记录时间；不能从 Commit 时间推断。"""
        state = {"version": 1}
        state["last_push_at"] = serialize_timestamp((now or utc_now()).replace(microsecond=0))
        try:
            self._update_state(state)
        except OSError as exc:
            # 推送已成功，时间戳丢失只会多触发一次同步。
            # 如果状态文件持续无法写入，仍需报告。
            from ..observability.diagnostics import record_once

            record_once("degraded.gitsync_state", exc)

    def _schedule_time(self) -> time:
        try:
            hour, minute = self.config.git.sync_time.split(":")
            return time(int(hour), int(minute))
        except (AttributeError, ValueError):
            return time(3, 0)

    def next_sync_at(self, now: datetime | None = None) -> datetime | None:
        if not self.config.git.auto_sync:
            return None
        now = now or _now()
        target = now.replace(
            hour=self._schedule_time().hour,
            minute=self._schedule_time().minute,
            second=0,
            microsecond=0,
        )
        state = self._read_state()
        if target > now:
            return target
        today = now.date().isoformat()
        if (
            state.get("last_daily_success_date") == today
            or state.get("last_daily_blocked_date") == today
        ):
            return target + timedelta(days=1)
        return now

    def due(self, now: datetime | None = None) -> bool:
        """每日计划是否到期；失败重试受 retry_after 限制。"""
        if not self.config.git.auto_sync:
            return False
        now = now or _now()
        state = self._read_state()
        today = now.date().isoformat()
        if (
            state.get("last_daily_success_date") == today
            or state.get("last_daily_blocked_date") == today
        ):
            return False
        if now.time() < self._schedule_time():
            return False
        retry_after = _parse_stamp(state.get("retry_after"))
        return retry_after is None or now >= retry_after

    def status(self, now: datetime | None = None) -> dict:
        now = now or _now()
        state = self._read_state()
        initialized = self.repo.is_repo
        if initialized and not self.running:
            try:
                self._git_lock.acquire(timeout=0)
            except LockTimeout:
                pass
            else:
                try:
                    self._publish_export(check_only=True)
                finally:
                    self._git_lock.release()
        branch = self.repo.branch() if initialized else None
        upstream = self.repo.upstream() if initialized else None
        configured_remote = self.repo.remote_name() if initialized else None
        remote = upstream.split("/", 1)[0] if upstream and "/" in upstream else configured_remote
        counts = self.repo.ahead_behind(upstream) if upstream else None
        last_commit = self.repo.last_commit_info() if initialized else None
        next_sync = self.next_sync_at(now) if self.config.git.auto_sync else None
        return {
            "initialized": initialized,
            "branch": branch,
            "remote": remote,
            "upstream": upstream,
            "modified": self.repo.modified_count() if initialized else 0,
            "unpushed": counts[0] if counts is not None else None,
            "behind": counts[1] if counts is not None else None,
            "last_commit_hash": last_commit["hash"] if last_commit else None,
            "last_commit_at": last_commit["at"] if last_commit else None,
            "last_push_at": state.get("last_push_at"),
            "last_sync_at": state.get("last_sync_at"),
            "last_result": state.get("last_result"),
            "last_error": state.get("last_error"),
            "last_error_kind": state.get("last_error_kind"),
            "auto_sync": self.config.git.auto_sync,
            "schedule": self.config.git.sync_time,
            "retry_interval_minutes": self.config.git.retry_interval_minutes,
            "next_sync": serialize_timestamp(next_sync.replace(microsecond=0))
            if next_sync
            else None,
            "running": self.running,
        }

    # ---------- 同步 ----------

    def sync(
        self,
        trigger: str = "manual",
        progress: Callable[[list[SyncStage]], None] | None = None,
        *,
        wait_for_git: bool = True,
        now: datetime | None = None,
    ) -> SyncResult:
        stages = [SyncStage(name) for name in STAGES]
        result = SyncResult(status="failed", trigger=trigger, stages=stages)

        def emit(name: str, state: str) -> None:
            for stage in stages:
                if stage.name == name:
                    stage.state = state
            if progress is not None:
                with contextlib.suppress(Exception):
                    progress([SyncStage(item.name, item.state) for item in stages])

        if not self._running.acquire(blocking=False):
            result.error = "A Git synchronization is already running."
            result.error_kind = "busy"
            result.status = "failed"
            return result

        lock_timeout = 5.0 if wait_for_git else 0.0
        try:
            emit(STAGE_CHECK, "running")
            try:
                self._git_lock.acquire(timeout=lock_timeout)
            except LockTimeout:
                emit(STAGE_CHECK, "failed")
                result.error = "Another Git operation is running; try again later."
                result.error_kind = "busy"
                return result

            try:
                return self._sync_locked(trigger, result, emit, now)
            finally:
                self._git_lock.release()
        finally:
            self._running.release()

    def _sync_locked(
        self, trigger: str, result: SyncResult, emit, now: datetime | None = None
    ) -> SyncResult:
        now = now or _now()

        def skip_remaining() -> None:
            for stage in result.stages:
                if stage.state == "waiting":
                    emit(stage.name, "skipped")

        if not self.repo.is_repo:
            emit(STAGE_CHECK, "failed")
            skip_remaining()
            foreign = self.repo.foreign_repo_root()
            if foreign is not None:
                result.error = (
                    f"Vault is inside another Git repository ({foreign}); refusing to "
                    "commit or push. Make the Vault its own repository or move it out."
                )
                result.error_kind = "foreign_repo"
            else:
                result.error = "Git repository is not initialized in this vault."
                result.error_kind = "not_repo"
            self._record_state(result, now, commit_at=None)
            return result

        commit_hash: str | None = None
        modified = 0
        try:
            self._publish_export()
            modified = self.repo.modified_count()
            sensitive = self.repo.sensitive_paths()
            if sensitive:
                emit(STAGE_CHECK, "failed")
                skip_remaining()
                result.error = (
                    "Refusing to commit sensitive paths: "
                    + ", ".join(sensitive[:5])
                    + ". Move them outside the vault or add them to .gitignore."
                )
                result.error_kind = "sensitive"
                self._record_state(result, now, commit_at=None)
                return result
            emit(STAGE_CHECK, "done")
            if modified:
                emit(STAGE_COMMIT, "running")
                with self.repo.stage_checked():
                    commit_hash = self.repo.commit(self._commit_message(trigger, now))
                result.committed = commit_hash is not None
                result.commit_hash = commit_hash
                emit(STAGE_COMMIT, "done" if result.committed else "skipped")
            else:
                emit(STAGE_COMMIT, "skipped")

        except (GitError, OSError, ValueError) as exc:
            emit(STAGE_COMMIT, "failed")
            skip_remaining()
            result.error = str(exc)
            result.error_kind = getattr(exc, "kind", "git")
            self._record_state(result, now, commit_at=None)
            return result

        remote = self.repo.remote_name()
        upstream = self.repo.upstream()
        try:
            if remote is None:
                emit(STAGE_PUSH, "skipped")
                result.push_skipped = True
                result.error = (
                    "Remote is not configured; the local commit is preserved. "
                    "Add a remote with: git remote add origin <url>"
                )
                result.error_kind = "no_remote"
            elif upstream is None:
                emit(STAGE_PUSH, "skipped")
                result.push_skipped = True
                result.error = (
                    "Upstream is not configured; the local commit is preserved. "
                    f"Run: git push -u {remote} {self.repo.branch() or 'main'}"
                )
                result.error_kind = "no_upstream"
            else:
                counts = self.repo.ahead_behind(upstream)
                ahead, behind = counts if counts is not None else (0, 0)
                if counts is None:
                    emit(STAGE_PUSH, "skipped")
                    result.push_skipped = True
                    result.error = "Unable to read the local/remote branch relationship."
                    result.error_kind = "other"
                elif ahead == 0:
                    emit(STAGE_PUSH, "skipped")
                    result.push_skipped = True
                elif behind > 0:
                    emit(STAGE_PUSH, "failed")
                    result.push_skipped = True
                    result.error = (
                        "Remote branch has diverged; automatic push stopped. "
                        "Resolve it manually (Pull/Merge/Rebase are not run automatically)."
                    )
                    result.error_kind = "diverged"
                else:
                    emit(STAGE_PUSH, "running")
                    try:
                        self.repo.push()
                        result.pushed = True
                        emit(STAGE_PUSH, "done")
                    except GitError as exc:
                        emit(STAGE_PUSH, "failed")
                        result.error = str(exc)
                        result.error_kind = exc.kind if exc.kind != "other" else "git"
        except GitError as exc:
            emit(STAGE_PUSH, "failed")
            result.error = str(exc)
            result.error_kind = exc.kind if exc.kind != "other" else "git"

        emit(STAGE_REFRESH, "done")

        if result.error_kind is None:
            result.status = "success"
        elif result.error_kind in {"no_remote", "no_upstream"}:
            result.status = "partial"
        elif (
            result.error_kind == "git"
            and result.committed
            and not result.pushed
            and upstream is None
        ):
            result.status = "partial"
        else:
            result.status = "partial" if result.committed else "failed"

        commit_at = self.repo.last_commit_info()
        self._record_state(result, now, commit_at=commit_at["at"] if commit_at else None)
        try:
            result.snapshot = self.status(now)
            # 本任务即将返回；快照不应继续显示“运行中”。
            if result.snapshot is not None:
                result.snapshot["running"] = False
        except Exception:
            result.snapshot = None
        return result

    def _commit_message(self, trigger: str, now: datetime) -> str:
        if trigger == "scheduled":
            return f"memory: daily sync {now.date().isoformat()}"
        return f"memory: manual sync {now:%Y-%m-%d %H:%M}"

    def _record_state(
        self,
        result: SyncResult,
        now: datetime,
        *,
        commit_at: str | None,
    ) -> None:
        state = {"version": 1}
        state["last_sync_at"] = serialize_timestamp(now.replace(microsecond=0))
        state["last_sync_trigger"] = result.trigger
        state["last_result"] = result.status
        state["last_error"] = result.error
        state["last_error_kind"] = result.error_kind
        if result.commit_hash:
            state["last_commit_hash"] = result.commit_hash
        if commit_at:
            state["last_commit_at"] = serialize_timestamp(parse_timestamp(commit_at))
        if result.pushed:
            # 只有实际 push 成功才更新；不能用 Commit 时间代替。
            state["last_push_at"] = serialize_timestamp(now.replace(microsecond=0))
        if result.trigger == "scheduled":
            state["last_daily_attempt_date"] = now.date().isoformat()
            # 成功与不可重试失败分别记录；不可重试只暂停当日重复尝试。
            if result.status == "success":
                state["last_daily_success_date"] = now.date().isoformat()
                state["last_daily_blocked_date"] = None
            elif result.error_kind not in RETRYABLE:
                state["last_daily_blocked_date"] = now.date().isoformat()
            else:
                state["last_daily_blocked_date"] = None
            if result.error_kind in RETRYABLE:
                retry = now + timedelta(minutes=self.config.git.retry_interval_minutes)
                state["retry_after"] = serialize_timestamp(retry.replace(microsecond=0))
            else:
                state["retry_after"] = None
        elif result.status == "success":
            state["retry_after"] = None
        try:
            self._update_state(state)
        except OSError:
            # 状态文件不可写不能影响已完成或已失败的 Git 操作。
            pass


__all__ = [
    "GitSyncService",
    "SyncResult",
    "SyncStage",
    "STAGES",
    "STAGE_CHECK",
    "STAGE_COMMIT",
    "STAGE_PUSH",
    "STAGE_REFRESH",
    "RETRYABLE",
]
