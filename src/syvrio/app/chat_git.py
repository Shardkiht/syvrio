"""TUI Git 管理入口、后台同步 Worker 与每日调度。

Git 同步不占用聊天 busy；锁和任务生命周期在 GitSyncService，界面只负责展示。
"""

from __future__ import annotations

from functools import partial

from ..infra.diagnostics import record_event
from ..infra.gitsync import SyncResult, SyncStage
from .git_screen import GitScreen


class ChatGitMixin:
    def _start_git_scheduler(self) -> None:
        self._git_screen: GitScreen | None = None
        self._git_sync_running = False
        self._git_last_notice: str | None = None
        # 轻量进程内调度：TUI 不运行时不会主动执行，启动补执行负责错过的计划。
        self.set_interval(30, self._git_scheduler_tick)

    def _git_scheduler_tick(self) -> None:
        if not getattr(self, "_reflection_ready", False):
            return
        if getattr(self, "_git_sync_running", False) or self.application.git.running:
            return
        try:
            due = self.application.git.due()
        except Exception as exc:
            record_event("git_sync.scheduler_failed", exc)
            return
        if due:
            self.start_git_sync(None, trigger="scheduled")

    # ---------- /git 界面 ----------

    def open_git_screen(self) -> None:
        screen = GitScreen()
        self._git_screen = screen
        self.push_screen(screen)
        self._refresh_git_screen(screen)

    def _refresh_git_screen(self, screen: GitScreen) -> None:
        self.run_worker(
            partial(self._git_status_work, screen),
            group="git-status",
            thread=True,
            exit_on_error=False,
        )

    def _git_status_work(self, screen: GitScreen) -> None:
        try:
            status = self.application.git.status()
        except Exception as exc:
            record_event("git_sync.status_failed", exc)
            try:
                self.call_from_thread(self._git_status_failed, screen, str(exc))
            except RuntimeError:
                pass
            return
        try:
            self.call_from_thread(self._apply_git_status, screen, status)
        except RuntimeError:
            pass

    def _apply_git_status(self, screen: GitScreen, status: dict) -> None:
        if self._git_screen is screen and screen.is_mounted:
            screen.apply_status(status)

    def _git_status_failed(self, screen: GitScreen, message: str) -> None:
        if self._git_screen is screen and screen.is_mounted:
            screen.show_error(message)

    # ---------- 同步 Worker ----------

    def start_git_sync(self, screen: GitScreen | None, trigger: str = "manual") -> bool:
        if getattr(self, "_git_sync_running", False) or self.application.git.running:
            if screen is not None:
                screen.sync_unavailable("A Git synchronization is already running.")
            return False
        self._git_sync_running = True
        if screen is not None:
            screen.set_syncing()
        self.run_worker(
            partial(self._git_sync_work, screen, trigger),
            group="git",
            thread=True,
            exit_on_error=False,
        )
        return True

    def _git_sync_work(self, screen: GitScreen | None, trigger: str) -> None:
        def progress(stages: list[SyncStage]) -> None:
            try:
                self.call_from_thread(self._git_progress, screen, stages)
            except RuntimeError:
                pass

        try:
            result = self.application.git.sync(
                trigger, progress, wait_for_git=(trigger == "manual")
            )
        except Exception as exc:
            record_event("git_sync.failed", exc)
            result = SyncResult(status="failed", trigger=trigger)
            result.error = f"Synchronization failed: {exc}"
            result.error_kind = "other"
        try:
            self.call_from_thread(self._git_finished, screen, result, trigger)
        except RuntimeError:
            pass

    def _git_progress(self, screen: GitScreen | None, stages: list[SyncStage]) -> None:
        target = screen if screen is not None and screen.is_mounted else self._git_screen
        if target is not None and target.is_mounted:
            target.update_progress(stages)

    def _git_finished(self, screen: GitScreen | None, result: SyncResult, trigger: str) -> None:
        self._git_sync_running = False
        target = screen if screen is not None and screen.is_mounted else self._git_screen
        if target is not None and target.is_mounted:
            target.show_result(result)
            if result.snapshot is None:
                self._refresh_git_screen(target)
        elif trigger == "manual":
            self._notify_manual_result(result)
        if trigger == "scheduled":
            self._notify_scheduled_result(result)

    def _notify_manual_result(self, result: SyncResult) -> None:
        if result.status == "success":
            self.notify("Git synchronization completed.")
        else:
            detail = result.error or result.status
            self.notify(
                f"Git synchronization {result.status}: {detail}", severity="warning", timeout=8
            )

    def _notify_scheduled_result(self, result: SyncResult) -> None:
        if result.status == "success":
            if self._git_last_notice is not None:
                self.notify("Automatic Git synchronization recovered.")
            self._git_last_notice = None
            return
        kind = result.error_kind or result.status
        if kind == self._git_last_notice:
            return
        self._git_last_notice = kind
        detail = result.error or "See /git for details."
        self.notify(f"Automatic Git sync: {detail}", severity="warning", timeout=8)
