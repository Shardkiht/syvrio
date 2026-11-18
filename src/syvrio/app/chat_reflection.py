from __future__ import annotations

import threading
import time
from functools import partial

from textual.widgets import Input

from ..infra.diagnostics import record_event
from .chat_widgets import ProfileScreen


class ChatReflectionMixin:
    """回顾 worker 不占聊天 busy；取消令牌在主线程创建，覆盖尚未启动的任务。"""

    def _start_auto_reflection(self) -> None:
        self._reflection_last_activity = time.monotonic()
        self._reflection_next = 0.0
        self._reflection_paused = False
        self._reflection_pending = False
        self._reflection_cancel = None
        self.set_interval(5, self._auto_reflection_tick)

    def _reflection_activity(self) -> None:
        self._reflection_last_activity = time.monotonic()

    def _auto_reflection_tick(self) -> None:
        config = self.application.config.chat
        now = time.monotonic()
        if (
            not config.auto_reflect
            or self._reflection_paused
            or not getattr(self, "_reflection_ready", False)
            or self.busy
            or getattr(self, "_reflection_pending", False)
            or now < self._reflection_next
            or now - self._reflection_last_activity < config.reflect_idle_seconds
        ):
            return
        if self.query_one("#input", Input).value.strip() or len(self.screen_stack) > 1:
            return
        try:
            due = self.application.reflection.auto_check_due()
        except Exception as exc:
            record_event("reflection.schedule_failed", exc)
            return
        if not due:
            return
        self._reflection_next = now + max(5.0, config.reflect_interval_seconds)
        self._reflection_pending = True
        self._reflection_cancel = threading.Event()
        self.run_worker(
            partial(self._auto_reflection_work, self._reflection_cancel),
            thread=True,
            group="reflection",
            exit_on_error=False,
        )

    def _auto_reflection_work(self, token: threading.Event) -> None:
        try:
            mode = self.application.reflection.auto_plan()
            if mode is None:
                self.call_from_thread(self._after_auto_reflection, None, None)
                return
            result = self.application.reflection.run(mode, 1, cancel=token, auto=True)
            self.call_from_thread(self._after_auto_reflection, result, result.error)
        except Exception as exc:
            record_event("reflection.failed", exc)
            self.call_from_thread(
                self._after_auto_reflection,
                None,
                "Automatic reflection failed; progress preserved.",
            )

    def _after_auto_reflection(self, result, error: str | None) -> None:
        self._reflection_pending = False
        self._reflection_next = time.monotonic() + max(
            5.0, self.application.config.chat.reflect_interval_seconds
        )
        if result is not None and result.proposed:
            self.notify(
                f"Background reflection proposed {result.proposed} inferences. Review with /inferences."
            )
        if error:
            self.notify(error)

    def _reflection_command(self, mode: str, argument: str) -> None:
        if argument == "resume":
            self._reflection_paused = False
            self.add_card("Automatic reflection resumed; it will continue when idle.")
            return
        if argument == "cancel":
            self._reflection_paused = True
            token = getattr(self, "_reflection_cancel", None)
            if token is not None:
                token.set()
            self.application.reflection.cancel()
            self.add_card(
                "Cancellation requested; stopping after the active request returns without publishing its candidates."
            )
            return
        if argument == "status":
            running = "running" if getattr(self, "_reflection_pending", False) else "idle"
            enabled = self.application.config.chat.auto_reflect and not getattr(
                self, "_reflection_paused", False
            )
            try:
                overview = self.application.reflection.schedule()
                schedule = (
                    f"next profile ~{overview.profile_next} ({overview.profile_pending}), "
                    f"next dream ~{overview.dream_next} "
                    f"({'pending' if overview.dream_pending else 'no pending changes'})"
                )
            except Exception:
                schedule = "schedule state unavailable"
            self.add_card(
                f"Reflection {running}; automatic reflection {'enabled' if enabled else 'paused/disabled'}; {schedule}."
            )
            return
        if mode == "profile" and not argument:
            self.run_worker(
                self._profile_work, thread=True, group="profile-view", exit_on_error=False
            )
            return
        if mode == "profile" and argument.split()[0] != "refresh":
            self.add_card(
                "Use /profile to view the profile; /profile refresh [0–20] generates proposals."
            )
            return
        value = argument.removeprefix("refresh").strip() if mode == "profile" else argument
        try:
            budget = int(value) if value else 1
            if not 0 <= budget <= 20:
                raise ValueError
        except ValueError:
            self.add_card(
                "Usage: /dream [0–20|cancel|status], /profile [refresh [0–20]|cancel|status]"
            )
            return
        if getattr(self, "_reflection_pending", False):
            self.add_card("Reflection is already running. Use /dream cancel to cancel.")
            return
        self._reflection_cancel = threading.Event()
        self._reflection_pending = True
        self.add_card(
            f"Background reflection started: at most {budget} model requests. You can continue chatting or switch sessions."
        )
        self.run_worker(
            partial(self._reflection_work, mode, budget, self._reflection_cancel),
            thread=True,
            group="reflection",
            exit_on_error=False,
        )

    def _profile_work(self) -> None:
        """只读缓存摘要；事实/推断的完整遍历和模型调用都留给 /profile refresh。"""
        generated_at = None
        fact_count = None
        unique_count = None
        try:
            snapshot = self.application.memory.profile()
            body = snapshot.body
            stale = snapshot.stale
            status = snapshot.status
            generated_at = snapshot.generated_at
            fact_count = snapshot.fact_count
            unique_count = snapshot.unique_count
        except Exception as exc:
            record_event("reflection.failed", exc)
            body = (
                "Could not read the profile summary cache.\n\n"
                "The cache is derived data; retry or run /profile refresh to rebuild it."
            )
            status = "Cache unavailable"
            stale = False
        try:
            self.call_from_thread(
                self._show_profile,
                body,
                status,
                stale,
                generated_at,
                fact_count,
                unique_count,
            )
        except RuntimeError:
            pass  # 界面已退出，不再投递结果。

    def _show_profile(
        self,
        body: str,
        status: str,
        stale: bool,
        generated_at: str | None,
        fact_count: int | None,
        unique_count: int | None,
    ) -> None:
        if len(self.screen_stack) > 1:
            return
        self.push_screen(
            ProfileScreen(body, status, stale, generated_at, fact_count, unique_count),
            lambda _: self.query_one("#input", Input).focus(),
        )

    def _reflection_work(self, mode: str, budget: int, token: threading.Event) -> None:
        try:
            result = self.application.reflection.run(mode, budget, cancel=token)
            message = (
                f"Reflection {'cancelled' if result.cancelled else 'complete'}: requests {result.requests}, "
                f"proposed {result.proposed}, remaining {result.remaining} batches."
            )
            if mode == "profile":
                labels = {
                    "updated": "updated",
                    "fresh": "already up to date",
                    "skipped": "not updated (request budget 0)",
                    "empty": "cleared (no valid facts)",
                    "failed": "generation failed; previous summary kept",
                    "stale": "not published (sources changed during generation)",
                    "cancelled": "not published",
                    "not_requested": "not requested",
                }
                message += f"\nProfile summary: {labels.get(result.summary_status, result.summary_status)}."
            if result.error:
                message += f"\n{result.error}"
            if result.proposed:
                message += "\nView and review with /inferences."
        except Exception as exc:
            record_event("reflection.failed", exc)
            message = (
                "Reflection failed; local progress preserved. Check the job log before retrying."
            )
        self.call_from_thread(self._after_reflection, message)

    def _after_reflection(self, message: str) -> None:
        self._reflection_pending = False
        self.add_card(message)

    def on_unmount(self) -> None:
        token = getattr(self, "_reflection_cancel", None)
        if token is not None:
            token.set()
        self.application.reflection.cancel()
