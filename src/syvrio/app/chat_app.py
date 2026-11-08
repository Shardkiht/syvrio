"""Textual 全屏聊天界面：左侧会话列表 + 中部对话流 + 底部输入与状态栏。

界面层只做展示与选择收集；模型/思考/上下文的判定与落盘在 actions.py，
会话与记忆操作走 AgentRuntime / MemoryService。
"""
from __future__ import annotations

import json
import time
from datetime import date
from functools import partial
from urllib.parse import urlsplit

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import MouseDown, MouseUp
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Label, ListView, ListItem, Markdown, Static
from textual.worker import Worker, WorkerState

from ..agent.runtime import AgentRuntime
from ..agent.session import Session, SessionStore
from ..application.service import ApplicationService
from ..infra.tokens import format_tokens
from ..infra.diagnostics import record_event
from ..llm import warm_model_metadata
from ..memory.service import MemoryService
from .chat_commands import ChatCommandsMixin
from .chat_git import ChatGitMixin
from .chat_reflection import ChatReflectionMixin
from .chat_flow import ChatFlowMixin
from .chat_input import BarInput
from .chat_theme import APP_CSS
from .chat_widgets import (
    ASSISTANT_NAME,
    CommandPalette,
    HomePanel,
    NewSessionButton,
    PickScreen,
    ModelPickerScreen,
    SessionItem,
    SessionPickerScreen,
    SidebarToggle,
    ChatReply,
    TranscriptView,
)
from .session_ui import visible_sessions
from .command_catalog import SLASH_COMMANDS, SLASH_SUBCOMMANDS


class ChatApp(ChatGitMixin, ChatReflectionMixin, ChatFlowMixin, ChatCommandsMixin, App):
    TITLE = "Syvrio — Personal Memory"

    CSS = APP_CSS

    BINDINGS = [
        Binding("ctrl+p", "model_providers", "Manage providers", priority=True, show=False),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+b", "toggle_sidebar", "Toggle sidebar"),
    ]

    def action_model_providers(self) -> None:
        # Textual 的全局命令面板也使用 Ctrl+P，只在模型选择器内改为供应商入口。
        if isinstance(self.screen, ModelPickerScreen):
            self.screen.action_providers()
        else:
            self.action_command_palette()

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
        self._import_running = False
        self._pending_review_busy = False
        self._stream_widget: ChatReply | None = None
        self._stream_buffer = ""
        self._stream_stamp = ""
        self._stream_flush_scheduled = False
        self._usage_cache: dict[tuple, int] = {}
        self._usage_request: tuple | None = None
        self._usage_failed: tuple | None = None
        self._pending_undo_restore: dict[str, str] = {}

    # ---------- 应用层兼容访问面（仅供旧 UI 测试与只读视图） ----------

    _STATE_ALIASES = {
        "_queue": "queue", "_queue_paused_after_undo": "queue_paused_after_undo",
        "_pending_new": "pending_new", "_pending_switch": "pending_switch", "_pending_command": "pending_command",
        "_navigation_busy": "_navigation_busy", "_ask_active": "_ask_active", "_ask_in_flight": "_ask_in_flight",
        "_ask_user_message_id": "_ask_user_message_id", "_cancel_generation": "_cancel",
        "_undo_requested": "_undo_requested", "_undo_target_session_id": "_undo_target_session_id", "_undo_target_message_id": "_undo_target_message_id",
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

    def compose(self) -> ComposeResult:
        with Horizontal(id="root"):
            with Vertical(id="sidebar"):
                with Horizontal(id="sidebar-head"):
                    yield Static("Syvrio", id="brand")
                    yield SidebarToggle("«", id="hide-sidebar")
                yield NewSessionButton("+ New session", id="new-session")
                yield ListView(id="sessions")
            with Vertical(id="main"):
                yield SidebarToggle("»", id="show-sidebar")
                yield TranscriptView(id="chat", classes="thin-scroll")
                yield CommandPalette()
                yield Static("", id="send-queue", markup=False)
                yield BarInput(placeholder="Message or /command", id="input")
                with Horizontal(id="status-row"):
                    yield self._model_status
                    yield Static("", id="status")
                yield Static("Enter send   Ctrl+P commands   Ctrl+B conversations", id="composer-hint")

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
        self._run_catchup()
        self._warm_search()
        # 首次 /model、/context 会同步导入 LiteLLM；启动预热避免开屏空白期。
        self.run_worker(partial(warm_model_metadata, self.application.config.llm.litellm_model()),
                        group="warm", thread=True, exit_on_error=False)
        self._start_auto_reflection()
        self._start_git_scheduler()

    def _handle_exception(self, error: Exception) -> None:
        record_event("tui.failed", error, busy=self.busy, streaming=self._ask_active,
                     queued=self.application.conversation.queue_snapshot().count)
        super()._handle_exception(error)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            record_event("tui.failed", event.worker.error, busy=self.busy,
                         streaming=self._ask_active, queued=self.application.conversation.queue_snapshot().count)

    def _warm_search(self) -> None:
        self.run_worker(
            self._warm_search_work, group="search", thread=True, exit_on_error=False
        )

    def _warm_search_work(self) -> None:
        try:
            result = self.application.warm_search()
        except Exception as exc:
            record_event("search.failed", exc)
            self.call_from_thread(self.notify, f"Web search did not start: {exc}")
            return
        if result == "started":
            self.call_from_thread(self.notify, "Web search started")

    def _run_catchup(self) -> None:
        self.run_worker(
            self._catchup_work, group="catchup", thread=True, exit_on_error=False
        )

    def _catchup_work(self) -> None:
        try:
            result = self.application.startup_catchup()
            auto_commit = result.pop("auto_commit", None)
        except Exception as exc:
            record_event("catchup.failed", exc)
            self.call_from_thread(self._after_catchup_error, str(exc))
            return
        self.call_from_thread(self._after_catchup, result, auto_commit)

    def _after_catchup_error(self, message: str) -> None:
        self._reflection_ready = True
        # 后台补记失败只提示，不动 busy：此时用户可能正在提问或切换会话。
        self.add_card(f"Startup catch-up failed: {message}")
        self.refresh_pending()
        self.update_status()
        self._git_scheduler_tick()

    def _after_catchup(self, result: dict, auto_commit: str | None) -> None:
        self._reflection_ready = True
        if auto_commit:
            self.notify(f"Auto-committed uncommitted vault changes: {auto_commit}")
            self.refresh_sessions()
        if result["consolidated"] or result["errors"]:
            parts = []
            if result["consolidated"]:
                parts.append(
                    f"Consolidated {result['consolidated']} unfinished session(s): "
                    f"{result['admitted']} written, {result['queued']} pending"
                )
            for error in result["errors"]:
                parts.append(f"Catch-up failed: {error}")
            self.add_card("\n".join(parts), "Startup check")
            self.refresh_sessions()
            self.refresh_pending()
            self.update_status()
        self._git_scheduler_tick()

    def action_toggle_sidebar(self) -> None:
        self._set_sidebar_visible(not self.query_one("#sidebar", Vertical).display)

    def _set_sidebar_visible(self, visible: bool) -> None:
        self.query_one("#sidebar", Vertical).display = visible
        self.query_one("#show-sidebar", SidebarToggle).display = not visible

    def show_sidebar(self) -> None:
        self._set_sidebar_visible(True)
        self.query_one("#input", Input).focus()

    def _focus_active_input(self) -> None:
        if len(self.screen_stack) > 1:
            for widget in self.screen.query(Input):
                if widget.can_focus and widget.display and not widget.disabled:
                    widget.focus(); return
            return
        self.query_one("#input", Input).focus()

    def pop_screen(self):
        # 模态屏关闭后恢复输入框焦点与硬件光标；否则原生竖线会停在左上角闪烁。
        result = super().pop_screen()
        self.call_after_refresh(self._restore_input_cursor)
        return result

    def _restore_input_cursor(self) -> None:
        for widget in self.screen.query(Input):
            if widget.has_focus:
                self.cursor_position = widget.cursor_screen_offset
                return

    def action_focus_next(self) -> None:
        if len(self.screen_stack) == 1:
            widget = self.query_one("#input", Input)
            value = widget.value
            if (widget.has_focus and widget.cursor_at_end and widget.selection.is_empty
                    and value.startswith("/")):
                palette = self.query_one("#cmd-palette", CommandPalette)
                prefix = value[1:]
                if palette.prefix != prefix:
                    palette.open(prefix)
                if command := palette.current:
                    suffix = " " if command in SLASH_SUBCOMMANDS else ""
                    widget.value = f"/{command}{suffix}"
                    widget.cursor_position = len(widget.value)
                    return
        self._focus_active_input()

    def action_focus_previous(self) -> None:
        self._focus_active_input()

    def run_command_from_palette(self, command: str) -> None:
        self.query_one("#cmd-palette", CommandPalette).close()
        self.query_one("#input", Input).value = ""
        self._submit_command(f"/{command}")

    def _submit_command(self, line: str) -> None:
        name = line[1:].partition(" ")[0].lower()
        if self._ask_active and any(name == command for command, _, _ in SLASH_COMMANDS):
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
            stamp = row.get("updated_at") or row.get("started_at") or ""
            date = stamp[:10] or "Unknown date"
            if date != last_date:
                view.append(ListItem(Label(date, classes="group-label"), disabled=True))
                last_date = date
            time_text = stamp[11:16] if len(stamp) >= 16 else ""
            meta = f"{time_text} · {row['messages']} msgs" if time_text else f"{row['messages']} msgs"
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

    def on_key(self, event) -> None:
        palette = self.query_one("#cmd-palette", CommandPalette)
        if palette.display:
            if event.key == "up":
                event.prevent_default()
                palette.move(-1)
                return
            if event.key == "down":
                event.prevent_default()
                palette.move(1)
                return
            if event.key == "escape":
                event.prevent_default()
                palette.close()
                return

        if event.key == "escape" and len(self.screen_stack) == 1 and self._ask_active:
            event.prevent_default()
            event.stop()
            self._stop_generation()

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
            self.call_from_thread(self._finish_navigation_error, f"Session switch failed: {exc}", drop_pending=True)
            return
        self.call_from_thread(self._after_session_switch, stats, label)
        # 沉淀延后到后台：刚切走的会话标记 distill_deferred，这里立即补；
        # 用户已开始新对话（busy）则不打断，留给下次启动补记。
        if self.busy:
            return
        try:
            # 切走时的后台补沉淀设阈值：小尾巴攒批留给启动补记，不烧小调用。
            deferred = self.application.conversation.consolidate_deferred()
        except Exception as exc:
            record_event("catchup.failed", exc)
            return
        if deferred["consolidated"] or deferred["errors"]:
            self.call_from_thread(self._after_deferred_distill, deferred)

    def _after_deferred_distill(self, deferred: dict) -> None:
        self.refresh_pending()
        self.update_status()
        if deferred["consolidated"]:
            self.notify(
                f"Previous session distilled in background: {deferred['admitted']} written, {deferred['queued']} pending"
            )
        for error in deferred["errors"]:
            self.notify(f"Background distillation failed: {error}")

    def _after_session_switch(self, stats: dict, label: str | None) -> None:
        self._clear_chat()
        message = None
        if label is None:
            self.add_welcome()
            message = "New session · created after your first message"
        else:
            self.build_transcript()
            if self._ask_active and self._ask_in_flight is not None and self.application.conversation.current_id == self._ask_in_flight:
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
                f"Delete session \"{escape(row['title'])}\" permanently?\nThis cannot be undone.",
                [("Cancel", False), ("Delete permanently", True)],
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
        """删除守卫：当前会话、正在生成回复的会话、队列引用的会话都不可删。

        后台请求仍持有会话对象并继续写文件，物理删除后下一次追加会重建出僵尸文件。
        """
        allowed, reason = self.application.conversation.delete_guard(session_id)
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
                self.application.conversation.delete_session(row["id"])
            except OSError as exc:
                record_event("session.delete_failed", exc)
                self.notify("Session could not be deleted; the file was kept.", severity="warning")
                self._focus_session_picker_or_input()
                return
            self.refresh_sessions()
            self.notify("Session deleted.")
        else:
            self.notify("Session no longer exists.")
        # 从会话选择器里删除时，同步刷新底层选择器的列表
        for screen in self.screen_stack:
            if isinstance(screen, SessionPickerScreen):
                screen.set_rows(visible_sessions(self.application.conversation.list_sessions(100)))
        self._focus_session_picker_or_input()

    # ---------- 对话流 ----------

    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _clear_chat(self) -> None:
        self.query_one("#chat", TranscriptView).clear_transcript()
        # 视图销毁后活跃段控件失效；buffer 保留供切回时重放（落盘段由磁盘重建负责）。
        self._stream_widget = None
        self._stream_flush_scheduled = False

    def _scroll_end(self) -> None:
        # 布局刷新前后都可能收到上滑/选择事件，真正滚动时再核验跟随意图。
        def follow() -> None:
            chat = self._chat()
            if not chat.reading_paused:
                chat.scroll_end(animate=False, immediate=True)
        self.call_after_refresh(follow)

    def on_mouse_down(self, event: MouseDown) -> None:
        if event.widget is not None and self._chat() in event.widget.ancestors_with_self:
            self._chat().selecting = True
            self._chat().record_reading_state()
            self.query_one("#input", BarInput).suspend_cursor()

    def on_mouse_up(self, event: MouseUp) -> None:
        self._chat().selecting = False
        self._chat().record_reading_state()

    def add_welcome(self) -> None:
        chat = self._chat()
        chat.add_class("home")
        chat.mount(HomePanel(classes="welcome-shell"))

    def build_transcript(self) -> None:
        self._chat().remove_class("home")
        self.query_one("#chat", TranscriptView).show_session(self.application.conversation.current)
        self._scroll_end()

    @staticmethod
    def _tool_event_label(call: dict, result: dict) -> str:
        """从历史 tool 记录重建事件行；结果 JSON 被截断时退化为成功/失败，不展示原文。"""
        name = str(call.get("name") or result.get("name") or "tool")
        arguments = call.get("arguments") or {}
        ok = bool(result.get("ok"))
        content = str(result.get("content") or "")
        payload = None
        if "\n" in content:
            try:
                payload = json.loads(content.split("\n", 1)[1])
            except (TypeError, ValueError):
                payload = None
        if name == "search":
            query = str(arguments.get("query") or "")[:20]
            if ok and isinstance(payload, dict) and isinstance(payload.get("results"), list):
                provider = payload.get("provider") or "open_websearch"
                return f'Search "{query}": {len(payload["results"])} results ({provider})'
            return f'Search "{query}"' + ("" if ok else " failed")
        if name == "fetch_web":
            url = str(arguments.get("url") or "")
            if ok:
                final_url = str((payload or {}).get("final_url") or url)
                return f"Fetched {urlsplit(final_url).netloc or url}"
            detail = str((payload or {}).get("error") or "")
            return f"Fetch failed: {detail}" if detail else "Fetch failed"
        return f"{name} {'ok' if ok else 'failed'}"

    def add_user_turn(self, content: str, stamp: str | None = None) -> None:
        chat = self._chat()
        if chat.has_class("home"):
            self._clear_chat()
        chat.follow_output = True
        if stamp:
            chat.mount(Static(f"You · {stamp}", classes="who-user"))
        # 容器构造时直接带子组件，避免“先挂载容器再补挂子组件”的时序问题。
        chat.mount(Vertical(Static(content, classes="bubble"), classes="user-turn"))
        self._scroll_end()

    def add_assistant_turn(self, content: str, stamp: str, meta: str | None = None) -> None:
        chat = self._chat()
        chat.mount(Static(f"{ASSISTANT_NAME} · {stamp}", classes="who-ai"))
        chat.mount(ChatReply(content or "(empty reply)", classes="reply"))
        if meta:
            chat.mount(Static(meta, classes="meta"))
        chat.mount(Static("", classes="divider"))
        self._scroll_end()

    def add_card(self, text: str, title: str | None = None) -> None:
        body = f"{title}\n{text}" if title else text
        self._chat().mount(Static(body, classes="card", markup=False))
        self._scroll_end()

    def add_markdown_card(self, markdown: str, title: str | None = None) -> None:
        if title:
            self._chat().mount(Static(title, classes="who-ai"))
        self._chat().mount(Markdown(markdown, classes="reply"))
        self._scroll_end()

    # ---------- 状态栏与忙碌态 ----------

    def refresh_pending(self) -> None:
        self.pending_count = self.application.pending_count

    def _usage_key(self) -> tuple | None:
        session = self.application.conversation.current if self.application.conversation.has_session else None
        if session is None:
            return None
        try:
            stat = (self.application.config.vault / "SOUL.md").stat()
            persona = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            persona = None
        return (session.id, len(session.records), self.application.conversation.model_label, persona, date.today())

    def _request_usage(self, key: tuple) -> None:
        if key == self._usage_request or key == self._usage_failed:
            return
        session = self.application.conversation.current
        # 固定本次估算的事件快照；切换或继续输入后，旧结果不得覆盖新会话。
        snapshot = Session(session.id, session.started_at, session.path, list(session.records))
        self._usage_request = key
        self.run_worker(partial(self._usage_work, key, snapshot), group="usage",
                        thread=True, exclusive=True, exit_on_error=False)

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
        total = self.application.settings.context_window
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
        text = (
            f"[#8a8b90]Context[/] {bar} [#8a8b90]{used_text}/{format_tokens(total)}[/]"
        )
        if self.pending_count:
            text += f"   [#b08a6a]Pending {self.pending_count}[/]"
        if percent >= self.application.conversation.compact_threshold():
            text += " [#b08a6a]⚠ compact next turn[/]"
        self.query_one("#status", Static).update(text)
        self._render_model_status()

    @property
    def busy(self) -> bool:
        """任一操作（导航或 LLM 请求）进行中；等待请求期间输入与切换不再被禁用。"""
        return self._navigation_busy or self._ask_active

    def _begin_navigation(self) -> None:
        self._reflection_activity()
        self._navigation_busy = True
        self._navigation_started = time.monotonic()
        self.query_one("#input", Input).disabled = True
        self._render_model_status()

    def _end_navigation(self) -> None:
        self._navigation_busy = False
        self._release_input_if_idle()

    def _begin_ask(self, session_id: str | None) -> None:
        record_event("generation.started", queued=self.application.conversation.queue_snapshot().count)
        self._reflection_activity()
        self.application.conversation.begin_turn(session_id)
        self._progress_started = time.monotonic()
        self._progress_phase = "Working"
        self.query_one("#input", BarInput).set_hint("Message or /command · Esc to stop")
        self._render_model_status()

    def _end_ask(self, turn_id: int | None) -> bool:
        if turn_id is None or not self.application.conversation.end_turn(turn_id):
            return False
        record_event("generation.finished", queued=self.application.conversation.queue_snapshot().count,
                     elapsed_ms=round((time.monotonic() - self._progress_started) * 1000))
        self._stream_widget = None
        self._stream_buffer = ""
        self._stream_stamp = ""
        self._stream_flush_scheduled = False
        self.query_one("#input", BarInput).set_hint("Message or /command")
        self._release_input_if_idle()
        return True

    def _release_input_if_idle(self) -> None:
        self._reflection_activity()
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

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "input":
            return
        self._reflection_activity()
        palette = self.query_one("#cmd-palette", CommandPalette)
        value = event.value
        if value.startswith("/"):
            palette.open(value[1:])
        else:
            palette.close()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        palette = self.query_one("#cmd-palette", CommandPalette)
        if line.startswith("/"):
            if (palette.display and palette.current
                    and palette.prefix == event.value[1:] and " " not in event.value):
                self.run_command_from_palette(palette.current)
                return
            palette.close()
            self._submit_command(line)
            return
        palette.close()
        if self._ask_active:
            self._submit_during_ask(line)
            return
        # 用户主动开始下一轮消息后，撤销期间暂停的旧队列才恢复接续。
        self.application.conversation.resume_after_undo()
        self._start_ask(line)
