"""Textual 全屏聊天界面：左侧会话列表 + 中部对话流 + 底部输入与状态栏。

界面层只做展示与选择收集；模型/思考/上下文的判定与落盘在 actions.py，
会话与记忆操作走 AgentRuntime / MemoryService。
"""

from __future__ import annotations

import time
from datetime import date
from functools import partial

from rich.markup import escape
from textual.app import App
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Input, Label, ListItem, ListView, Static
from textual.worker import Worker, WorkerState

from ..agent.insertion import InsertionRequest
from ..agent.runtime import AgentRuntime
from ..application.commands import is_command_form, parse_command
from ..application.service import ApplicationService
from ..errors import SessionProtectedError
from ..infra.attachments import AttachmentBatch
from ..infra.diagnostics import record_event
from ..infra.tokens import format_tokens
from ..memory.service import MemoryService
from ..session import Session, SessionStore
from ..time import format_local_timestamp
from .attachment_screen import AttachmentScreen
from .chat_commands import ChatCommandsMixin
from .chat_flow import ChatFlowMixin
from .chat_git import ChatGitMixin
from .chat_input import BarInput
from .chat_theme import APP_CSS
from .chat_view import ChatViewMixin
from .chat_widgets import (
    ChatReply,
    CommandPalette,
    PickScreen,
    SessionItem,
    SessionPickerScreen,
    TranscriptView,
)
from .session_ui import visible_sessions


class ChatApp(ChatViewMixin, ChatGitMixin, ChatFlowMixin, ChatCommandsMixin, App):
    TITLE = "Syvrio — Personal Memory"

    CSS = APP_CSS

    BINDINGS = [
        Binding("ctrl+p", "model_providers", "Manage providers", priority=True, show=False),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+b", "toggle_sidebar", "Toggle sidebar"),
        ("ctrl+o", "attach_files", "Attach files"),
        Binding("ctrl+enter", "insert_message", "Insert message", show=False, priority=True),
    ]

    def __init__(
        self,
        runtime: AgentRuntime,
        service: MemoryService,
        sessions: SessionStore,
    ) -> None:
        super().__init__()
        self.application = ApplicationService(runtime, service)
        # 忙碌三态：导航操作与 LLM 请求分开记账，输入框只在导航期间禁用。
        self.pending_count = 0
        self._progress_phase = ""
        self._progress_started = 0.0
        self._navigation_started = 0.0
        self._model_status = Static("", id="model-status")
        self._context_loading = False
        self._pending_review_busy = False
        self._stream_widget: ChatReply | None = None
        self._stream_buffer = ""
        self._thinking_widget = None
        self._thinking_parts: list[str] = []
        self._stream_stamp = ""
        self._stream_flush_scheduled = False
        self._usage_cache: dict[tuple, int] = {}
        self._usage_request: tuple | None = None
        self._usage_failed: tuple | None = None
        self._pending_undo_restore: dict[str, str] = {}
        self._safe_insert: InsertionRequest | None = None
        self._attachments: dict[str | None, AttachmentBatch] = {}

    # ---------- 应用层兼容访问面（仅供旧 UI 测试与只读视图） ----------

    _STATE_ALIASES = {
        "_queue": "queue",
        "_queue_paused_after_undo": "queue_paused_after_undo",
        "_pending_new": "pending_new",
        "_pending_switch": "pending_switch",
        "_pending_command": "pending_command",
        "_navigation_busy": "_navigation_busy",
        "_ask_active": "ask_active",
        "_ask_in_flight": "ask_in_flight",
        "_ask_user_message_id": "_ask_user_message_id",
        "_cancel_generation": "_cancel",
        "_undo_requested": "_undo_requested",
        "_undo_target_session_id": "_undo_target_session_id",
        "_undo_target_message_id": "_undo_target_message_id",
    }

    @property
    def runtime(self) -> AgentRuntime:
        return self.application.conversation.runtime

    @property
    def service(self) -> MemoryService:
        return self.application._service

    @property
    def sessions(self) -> SessionStore:
        return self.application.conversation.sessions

    def __getattr__(self, name: str):
        alias = type(self)._STATE_ALIASES.get(name)
        application = self.__dict__.get("application")
        if alias is not None and application is not None:
            return getattr(application.conversation, alias)
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        alias = type(self)._STATE_ALIASES.get(name)
        application = self.__dict__.get("application")
        if alias is not None and application is not None:
            if alias == "_navigation_busy":
                application.conversation.set_navigation_busy(value)
            else:
                setattr(application.conversation, alias, value)
            return
        super().__setattr__(name, value)

    # ---------- 布局 ----------

    def on_mount(self) -> None:
        self.set_interval(0.15, self._render_model_status)
        self.refresh_pending()
        self.refresh_sessions()
        self.update_status()
        if self.application.conversation.has_session:
            self.build_transcript()
        else:
            self.add_welcome()
        # 主界面只有聊天输入框接受焦点；侧栏和对话区仍可点击/滚动，但不抢键盘焦点。
        self.query_one("#sessions", ListView).can_focus = False
        self.query_one("#chat", VerticalScroll).can_focus = False
        self.query_one("#input", Input).focus()
        self._set_sidebar_visible(False)
        self._git_screen = None
        self._git_sync_running = False
        self._git_last_notice = None

    def _handle_exception(self, error: Exception) -> None:
        record_event(
            "tui.failed",
            error,
            busy=self.busy,
            streaming=self._ask_active,
            queued=self.application.conversation.queue_snapshot().count,
        )
        super()._handle_exception(error)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            record_event(
                "tui.failed",
                event.worker.error,
                busy=self.busy,
                streaming=self._ask_active,
                queued=self.application.conversation.queue_snapshot().count,
            )

    def _submit_command(self, line: str) -> None:
        if self._ask_active and is_command_form(*parse_command(line)):
            self._submit_during_ask(line)
        else:
            self._command(line)

    # ---------- 会话侧栏 ----------

    def refresh_sessions(self) -> None:
        view = self.query_one("#sessions", ListView)
        view.clear()
        current_id = self.application.conversation.current_id
        last_date = None
        for row in visible_sessions(self.application.conversation.list_sessions(100)):
            stamp = format_local_timestamp(row.get("last_message_at") or row.get("started_at"))
            date = stamp[:10] or "Unknown date"
            if date != last_date:
                view.append(ListItem(Label(date, classes="group-label"), disabled=True))
                last_date = date
            time_text = stamp[11:16] if len(stamp) >= 16 else ""
            meta = (
                f"{time_text} · {row['messages']} msgs" if time_text else f"{row['messages']} msgs"
            )
            item = SessionItem({**row, "meta": meta})
            if row["id"] == current_id:
                item.add_class("current")
            view.append(item)

    def _sync_current_session(self) -> None:
        """切换会话只移动当前标记；侧栏时间/顺序留到下一次常规刷新。"""
        current_id = self.application.conversation.current_id
        found = False
        for item in self.query(SessionItem):
            is_current = item.row["id"] == current_id
            item.set_class(is_current, "current")
            found = found or is_current
        # 目标会话可能尚未出现在当前列表里，这时才退回全量刷新。
        if current_id and not found:
            self.refresh_sessions()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, SessionItem):
            return
        if self._navigation_busy:
            # 导航期间点击会话：记住意图，结束后自动切换，不静默丢弃。
            # LLM 请求进行中不排队：回复由 runtime 绑定原会话写盘，可立即切换。
            self.application.conversation.request_switch(event.item.row["id"])
            return
        row = event.item.row
        if self.application.conversation.current_id == row["id"]:
            self.notify("Already the current session.")
            self.query_one("#input", Input).focus()
            return
        self._switch_session(row["id"])

    def _new_session(self) -> None:
        if self._navigation_busy:
            self.application.conversation.request_new()
            return
        self._begin_navigation()
        self.run_worker(
            partial(self._session_work, None),
            group="session",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _switch_session(self, session_id: str) -> None:
        if self._navigation_busy:
            self.application.conversation.request_switch(session_id)
            return
        self._begin_navigation()
        self.run_worker(
            partial(self._session_work, session_id),
            group="session",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _drain_pending_intent(self) -> None:
        """空闲时接续排队的切换意图与等待期消息，最后一次切换优先，不静默丢弃。"""
        if self.busy:
            return
        navigation = self.application.conversation.take_pending_navigation()
        if navigation is not None:
            kind, session_id = navigation
            if kind == "new":
                self._new_session()
            else:
                self._switch_session(session_id)
            return
        conversation = self.application.conversation
        item = conversation.take_pending_command() or conversation.take_next_command()
        if item is not None:
            self._refresh_queue()
            # 导航期间可能再次切换视图，执行前重新核验完整队列项的目标。
            self._run_queued_command(item)
            if not self.busy:
                self._drain_pending_intent()
            return
        if self._queue_paused_after_undo:
            # 撤销后保留消息，直到用户下一次主动提交。
            return
        self._dispatch_queue()

    def _session_work(self, session_id: str | None) -> None:
        try:
            # 快速路径：切换只做记账（distill=False 不跑 LLM 抽取），UI 立即就绪。
            switched = self.application.conversation.switch(session_id)
            stats = switched.stats
            label = switched.label
        except Exception as exc:
            # 会话文件损坏/被删等异常不能让 busy 卡死：留在原会话并提示。
            record_event("session.failed", exc)
            self.call_from_thread(
                self._finish_navigation_error, f"Session switch failed: {exc}", drop_pending=True
            )
            return
        self.call_from_thread(self._after_session_switch, stats, label)

    def _after_session_switch(self, stats: dict, label: str | None) -> None:
        self._clear_chat()
        message = None
        if label is None:
            self.add_welcome()
            message = "New session · created after your first message"
        else:
            self.build_transcript()
            if (
                self._ask_active
                and self._ask_in_flight is not None
                and self.application.conversation.current_id == self._ask_in_flight
            ):
                # 切回生成中的会话：磁盘重建负责已完成轮次，活跃段从状态重放。
                self._replay_stream()
        if stats.get("admitted") or stats.get("queued"):
            message = f"{message or ''} (previous session distilled: {stats['admitted']} written, {stats['queued']} pending)".strip()
        self._sync_current_session()
        self.refresh_pending()
        self.update_status()
        self._end_navigation()
        if message:
            self.notify(message)
        self._flush_undo_refresh()
        self.query_one("#input", Input).focus()
        self._drain_pending_intent()

    def confirm_delete_session(self, row: dict) -> None:
        if not self._session_deletable(row["id"]):
            return
        self.push_screen(
            PickScreen(
                f'Move "{escape(row["title"])}" to Archive?',
                [("Cancel", False), ("Archive", True)],
                partial(self._delete_session_result, row),
            )
        )

    def _focus_session_picker_or_input(self) -> None:
        for screen in reversed(self.screen_stack):
            if isinstance(screen, SessionPickerScreen):
                screen.query_one("#picker-list", ListView).focus()
                return
        self.query_one("#input", Input).focus()

    def _session_deletable(self, session_id: str) -> bool:
        allowed, reason = self.application.conversation.archive_guard(session_id)
        if not allowed:
            self.notify(reason or "Session cannot be deleted.")
            self._focus_session_picker_or_input()
        return allowed

    def _delete_session_result(self, row: dict, confirmed) -> None:
        if confirmed is not True:
            self._focus_session_picker_or_input()
            return
        # 弹窗期间可能已排队切换/生成：确认时复查，不能删掉受保护的会话。
        if not self._session_deletable(row["id"]):
            return
        # 用弹窗打开前捕获的 row，不读列表当前高亮：模态期间列表可能被刷新重建。
        if self.application.conversation.session_exists(row["id"]):
            try:
                self.application.conversation.archive_session(row["id"])
            except SessionProtectedError as exc:
                self.notify(str(exc), severity="warning")
                self._focus_session_picker_or_input()
                return
            except (OSError, ValueError) as exc:
                record_event("session.delete_failed", exc)
                self.notify("Session could not be archived; the file was kept.", severity="warning")
                self._focus_session_picker_or_input()
                return
            self.refresh_sessions()
            self.refresh_pending()
            self.notify("Session archived.")
        else:
            self.notify("Session no longer exists.")
        # 从会话选择器里删除时，同步刷新底层选择器的列表
        for screen in self.screen_stack:
            if isinstance(screen, SessionPickerScreen):
                screen.set_rows(visible_sessions(self.application.conversation.list_sessions(100)))
        self._focus_session_picker_or_input()

    # ---------- 对话流 ----------

    def build_transcript(self) -> None:
        self._chat().remove_class("home")
        self.query_one("#chat", TranscriptView).show_session(self.application.conversation.current)
        self._scroll_end()

    # ---------- 状态栏与忙碌态 ----------

    def refresh_pending(self) -> None:
        self.pending_count = self.application.pending_count

    def _usage_key(self) -> tuple | None:
        session = (
            self.application.conversation.current
            if self.application.conversation.has_session
            else None
        )
        if session is None:
            return None
        try:
            stat = (self.application.config.vault / "SOUL.md").stat()
            persona = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            persona = None
        return (
            session.id,
            len(session.records),
            self.application.conversation.model_label,
            persona,
            date.today(),
        )

    def _request_usage(self, key: tuple) -> None:
        if key == self._usage_request or key == self._usage_failed:
            return
        session = self.application.conversation.current
        # 固定本次估算的事件快照；切换或继续输入后，旧结果不得覆盖新会话。
        snapshot = Session(session.id, session.started_at, session.path, list(session.records))
        self._usage_request = key
        self.run_worker(
            partial(self._usage_work, key, snapshot),
            group="usage",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _usage_work(self, key: tuple, session: Session) -> None:
        try:
            used = self.application.conversation.estimate_usage(session)
        except Exception as exc:
            record_event("usage.failed", exc)
            used = None
        try:
            self.call_from_thread(self._after_usage, key, used)
        except RuntimeError:
            pass  # 界面已退出，不再投递状态更新。

    def _after_usage(self, key: tuple, used: int | None) -> None:
        if self._usage_request == key:
            self._usage_request = None
        if key != self._usage_key() or (
            self.application.conversation.last_plan is not None and self._plan_is_current()
        ):
            return
        if used is None:
            self._usage_failed = key
            self.notify("Context usage estimation failed")
        else:
            self._usage_cache[key] = used
            while len(self._usage_cache) > 16:
                del self._usage_cache[next(iter(self._usage_cache))]
        self.update_status()

    def update_status(self) -> None:
        conversation = self.application.conversation
        limits = conversation.context_limits(conversation.current_id)
        total = limits.window
        # 后台请求的 plan 属于别的会话时不得污染当前视图的用量显示。
        plan = self.application.conversation.last_plan if self._plan_is_current() else None
        key = self._usage_key()
        used = plan.estimated_tokens if plan is not None else self._usage_cache.get(key, 0)
        estimating = plan is None and key is not None and key not in self._usage_cache
        if estimating:
            self._request_usage(key)
        percent = used / max(total, 1)
        blocks = min(10, max(0, round(percent * 10)))
        # 用量条用制表横线（━ 已用 / ─ 剩余）：块/阴影字符在部分 CJK 字体会画成竖线；
        # 低占用时百分比恒为 0%，直接展示“已用/上限”数字。
        bar = f"[#8a8b90]{'━' * blocks}[/][#2f3035]{'─' * (10 - blocks)}[/]"
        used_text = format_tokens(used) if used else "0"
        if estimating:
            used_text = "Unavailable" if key == self._usage_failed else "Calculating"
        text = f"[#8a8b90]Context[/] {bar} [#8a8b90]{used_text}/{format_tokens(total)}[/]"
        if self.pending_count:
            text += f"   [#b08a6a]Pending {self.pending_count}[/]"
        if used >= limits.trigger_tokens:
            text += " [#b08a6a]⚠ compact next turn[/]"
        self.query_one("#status", Static).update(text)
        self._render_model_status()

    @property
    def busy(self) -> bool:
        """任一操作（导航或 LLM 请求）进行中；等待请求期间输入与切换不再被禁用。"""
        return self._navigation_busy or self._ask_active

    def _begin_navigation(self) -> None:
        self._navigation_busy = True
        self._navigation_started = time.monotonic()
        self.query_one("#input", Input).disabled = True
        self._render_model_status()

    def _end_navigation(self) -> None:
        self._navigation_busy = False
        self._release_input_if_idle()

    def _begin_ask(self, session_id: str | None) -> None:
        record_event(
            "generation.started", queued=self.application.conversation.queue_snapshot().count
        )
        self.application.conversation.begin_turn(session_id)
        self._progress_started = time.monotonic()
        self._progress_phase = "Working"
        self.query_one("#input", BarInput).set_hint("Message or /command · Esc to stop")
        self._render_model_status()

    def _end_ask(self, turn_id: int | None) -> bool:
        if turn_id is None or not self.application.conversation.end_turn(turn_id):
            return False
        record_event(
            "generation.finished",
            queued=self.application.conversation.queue_snapshot().count,
            elapsed_ms=round((time.monotonic() - self._progress_started) * 1000),
        )
        self._stream_widget = None
        self._stream_buffer = ""
        if self._thinking_widget is not None:
            self._thinking_widget.fold_preview()
        self._thinking_widget = None
        self._thinking_parts.clear()
        self._stream_stamp = ""
        self._stream_flush_scheduled = False
        self.query_one("#input", BarInput).set_hint("Message or /command")
        self._release_input_if_idle()
        return True

    def _release_input_if_idle(self) -> None:
        widget = self.query_one("#input", Input)
        # 输入只在导航操作期间禁用；LLM 等待期始终可输入（提交进全局队列）。
        widget.disabled = self._navigation_busy
        if not self.busy:
            self._progress_phase = ""
            widget.focus()
        self._render_model_status()

    def _render_model_status(self) -> None:
        if not self._model_status.is_mounted:
            return
        label = self.application.conversation.model_label
        phase = ""
        started = 0.0
        if self._ask_active and self._ask_in_current_view():
            phase, started = self._progress_phase, self._progress_started
        elif self._navigation_busy:
            phase, started = "Working", self._navigation_started
        if phase:
            elapsed = time.monotonic() - started
            frame = int(elapsed * 5) % 6
            bar = "".join("━" if i == frame else "─" for i in range(6))
            label += f"  {bar} {phase} {elapsed:.0f}s"
        if label != str(self._model_status.content):
            self._model_status.update(label)

    def _progress_update(self, phase: str) -> None:
        self._progress_phase = phase
        self._render_model_status()
        if phase.startswith("Calling tool: "):
            self.add_event(phase)

    # ---------- 输入与回合 ----------

    def action_attach_files(self) -> None:
        if len(self.screen_stack) != 1:
            return
        session_id = self.application.conversation.current_id

        def selected(batch: AttachmentBatch | None) -> None:
            if batch is None:
                return
            if old := self._attachments.get(session_id):
                old.discard()
            self._attachments[session_id] = batch
            self.notify("Attached: " + ", ".join(item.name for item in batch.files))
            self.query_one("#input", Input).focus()

        self.push_screen(AttachmentScreen(self.service.paths.attachments_dir), selected)

    async def action_insert_message(self) -> None:
        if len(self.screen_stack) != 1 or not isinstance(self.focused, BarInput):
            return
        widget = self.focused
        text = widget.value.strip()
        if not text:
            return
        if text.startswith("/"):
            await widget.action_submit()
            return
        conversation = self.application.conversation
        if conversation.active_turn_id and self._attachments.get(conversation.current_id):
            self.notify("Press Enter to queue the message with its attachments.")
            return
        turn_id = conversation.active_turn_id
        if turn_id is None:
            await widget.action_submit()
            return
        if not self._ask_in_current_view():
            self.notify("Insert is only available in the generating session.")
            return
        request = conversation.insert_message(turn_id, text)
        if request is None:
            self.notify("Cannot insert now. Enter queues the message for the next turn.")
            return
        self._safe_insert = request
        self.notify("Message will be inserted at the next safe boundary.")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        session_id = self.application.conversation.current_id
        if not line and session_id in self._attachments:
            line = "Analyze the attached files."
        if self._safe_insert and self._safe_insert.text == line:
            return
        if not line:
            event.input.value = ""
            return
        if line.startswith("/"):
            self._submit_command_input(event)
            return
        self.query_one("#cmd-palette", CommandPalette).close()
        event.input.value = ""
        attachments = self._attachments.pop(session_id, None)
        if self._ask_active:
            self._submit_during_ask(line, attachments)
            return
        # 用户主动开始下一轮消息后，撤销期间暂停的旧队列才恢复接续。
        self.application.conversation.resume_after_undo()
        if attachments:
            self._start_ask(line, attachments=attachments)
        else:
            self._start_ask(line)
