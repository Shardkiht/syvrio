"""共用的 Textual 布局、对话记录渲染和输入交互。"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import MouseDown, MouseUp
from textual.widgets import Button, Input, ListView, Markdown, Static

from ..application.commands import parse_command
from .chat_input import BarInput
from .chat_widgets import (
    ASSISTANT_NAME,
    ChatReply,
    CommandPalette,
    HomePanel,
    ModelPickerScreen,
    NewSessionButton,
    SidebarToggle,
    TranscriptView,
)
from .command_catalog import SLASH_SUBCOMMANDS, slash_matches


class ChatViewMixin:
    def action_model_providers(self) -> None:
        # Textual 的全局命令面板也使用 Ctrl+P，只在模型选择器内改为供应商入口。
        if isinstance(self.screen, ModelPickerScreen):
            self.screen.action_providers()
        else:
            self.action_command_palette()

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
                    yield Button("Attach", id="attach-files")
                    yield Button("Undo", id="undo-turn")
                    yield self._model_status
                    yield Static("", id="status")
                yield Static(
                    "Enter send   Ctrl+P commands   Ctrl+B conversations", id="composer-hint"
                )

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
                    widget.focus()
                    return
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
            if (
                widget.has_focus
                and widget.cursor_at_end
                and widget.selection.is_empty
                and value.startswith("/")
            ):
                palette = self.query_one("#cmd-palette", CommandPalette)
                prefix = value[1:]
                if palette.prefix != prefix:
                    palette.open(prefix)
                if palette.matches:
                    command = palette.current or palette.matches[0][0]
                    suffix = " " if command in SLASH_SUBCOMMANDS else ""
                    self.fill_command(command + suffix)
                    return
        self._focus_active_input()

    def action_focus_previous(self) -> None:
        self._focus_active_input()

    def fill_command(self, command: str) -> None:
        widget = self.query_one("#input", Input)
        widget.value = "/" + command
        widget.cursor_position = len(widget.value)
        widget.focus()

    def _submit_command_input(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        name, argument = parse_command(line)
        typed = f"{name} {argument.lower()}" if argument else name
        palette = self.query_one("#cmd-palette", CommandPalette)
        choice = palette.current if palette.display and palette.prefix == event.value[1:] else None
        if choice and choice != typed:
            self.fill_command(choice)
            return
        # 即使已关闭提示框，仍可继续编辑有补全候选的前缀。
        matches = slash_matches(line[1:])
        if matches and not any(key == typed for key, _, _ in matches):
            self.notify("Complete the command before sending.")
            return
        palette.close()
        event.input.value = ""
        self._submit_command(line)

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

    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _clear_chat(self) -> None:
        self.query_one("#chat", TranscriptView).clear_transcript()
        # 视图销毁后活跃段控件失效；buffer 保留供切回时重放（落盘段由磁盘重建负责）。
        self._stream_widget = None
        self._thinking_widget = None
        self._stream_flush_scheduled = False

    def _scroll_end(self) -> None:
        # 布局刷新前后都可能收到上滑/选择事件，真正滚动时再核验跟随意图。
        self.call_after_refresh(self._chat().scroll_to_follow)

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
        chat.mount(Vertical(Static(content, classes="bubble", markup=False), classes="user-turn"))
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

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "input":
            return
        palette = self.query_one("#cmd-palette", CommandPalette)
        value = event.value
        if value.startswith("/"):
            palette.open(value[1:])
        else:
            palette.close()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "attach-files":
            event.stop()
            self.action_attach_files()
            return
        if event.button.id == "undo-turn":
            event.stop()
            self.query_one("#input", Input).focus()
            self._submit_command("/undo")
