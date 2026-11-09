"""TUI 小组件：命令面板、选择/输入模态与侧栏条目和按钮。

组件一律通过 self.app 鸭子类型回调 ChatApp，不反向 import，避免界面层内部循环依赖。
"""
from __future__ import annotations

import re
import unicodedata

from markdown_it import MarkdownIt
from markdown_it.rules_inline import emphasis
from markdown_it.rules_inline.state_inline import StateInline
from rich.markup import escape
from rich.markdown import Markdown as RichMarkdown, Heading, CodeBlock
from rich.syntax import Syntax
from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import MouseScrollUp, MouseScrollDown
from textual.content import Content
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.style import Style
from textual.strip import Strip
from textual.visual import RenderOptions, RichVisual
from textual.widgets import Button, Input, Label, ListView, ListItem, Static

from ..agent.session import Session
from .thinking_panel import ThinkingPanel
from ..infra.diagnostics import record_event
from .command_catalog import SLASH_COMMANDS, slash_matches
from .chat_theme import MARKDOWN_THEME


_CJK_EMPHASIS = re.compile(r"[\u3400-\u9fff]\*\*[^\w\s*]|[^\w\s*]\*\*[\u3400-\u9fff]")


def _cjk_strong(state: StateInline, silent: bool) -> bool:
    start = state.pos
    if silent or state.src[start:start + 2] != "**":
        return False
    if state.src[start:start + 3] == "***":
        return False
    before = state.src[start - 1] if start else " "
    after = state.src[start + 2] if start + 2 < state.posMax else " "
    can_open = 0x3400 <= ord(before) <= 0x9FFF and unicodedata.category(after).startswith("P")
    can_close = unicodedata.category(before).startswith("P") and 0x3400 <= ord(after) <= 0x9FFF
    if not (can_open or can_close):
        return False
    # Keep markdown-it's delimiter pairing, escapes and code rules intact.
    emphasis.tokenize(state, silent)
    for delimiter in state.delimiters[-2:]:
        delimiter.open = delimiter.open or can_open
        delimiter.close = delimiter.close or can_close
    return True


class _ReplyHeading(Heading):
    LEVEL_ALIGN = dict.fromkeys(("h1", "h2", "h3", "h4", "h5", "h6"), "left")
    new_line = False


class _ReplyCodeBlock(CodeBlock):
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Remove only the fence's terminal newline, never code indentation/spaces.
        yield Syntax(str(self.text).removesuffix("\n"), self.lexer_name,
                     theme=self.theme, background_color="#202020",
                     word_wrap=True, padding=(0, 1))


class _ReplyMarkdown(RichMarkdown):
    elements = {**RichMarkdown.elements, "heading_open": _ReplyHeading,
                "fence": _ReplyCodeBlock, "code_block": _ReplyCodeBlock}

    def __init__(self, markdown: str) -> None:
        compatible = bool(_CJK_EMPHASIS.search(markdown))
        # Initialize Rich's rendering state without parsing an affected reply twice.
        super().__init__("" if compatible else markdown, code_theme="github-dark", style="markdown.text")
        if compatible:
            self.markup = markdown
            parser = MarkdownIt().enable("strikethrough").enable("table")
            parser.inline.ruler.before("emphasis", "cjk_strong", _cjk_strong)
            self.parsed = parser.parse(markdown)


class _SelectableMarkdown(RichVisual):
    """Rich Markdown 本身不提供文本坐标，转成 Content 后才能选择和高亮。"""

    def __init__(self, widget: Static, markdown: str) -> None:
        super().__init__(widget, _ReplyMarkdown(markdown))
        self._content_cache: tuple[int, Content] | None = None

    def selectable_content(self, width: int) -> Content:
        width = max(1, width)
        if self._content_cache is None or self._content_cache[0] != width:
            console = self._widget.app.console
            options = self._widget.app.console_options.update(width=width, highlight=False)
            with console.use_theme(MARKDOWN_THEME):
                text = Text.assemble(*(
                    (segment.text, segment.style)
                    for segment in console.render(self._renderable, options)
                    if not segment.control
                ))
            text = text[:len(text.plain.rstrip("\n"))]
            self._content_cache = (width, Content.from_rich_text(text))
        return self._content_cache[1]

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        return self.selectable_content(width).render_strips(width, height, style, options)


class ChatReply(Static):
    """整条回复用一个可缓存控件绘制，避免每个 Markdown 块再注册一组子控件。"""

    def __init__(self, content: str, **kwargs) -> None:
        self._markdown = content
        self._pending_markdown: str | None = None
        super().__init__(**kwargs)
        super().update(_SelectableMarkdown(self, content))

    def update(self, content: str) -> None:
        chat = next((node for node in self.ancestors if isinstance(node, TranscriptView)), None)
        if chat is not None and chat.reading_paused:
            # 只缓存最新全文；用户阅读的 Markdown 与选区坐标保持不变。
            self._pending_markdown = content
            return
        self._pending_markdown = None
        if content == self._markdown:
            return
        self._markdown = content
        super().update(_SelectableMarkdown(self, content))

    def flush_pending(self) -> bool:
        if self._pending_markdown is None:
            return False
        self.update(self._pending_markdown)
        return True

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        content = self.visual.selectable_content(self.content_size.width)
        return selection.extract(str(content)), "\n"


ASSISTANT_NAME = "Syvrio"


class HistoryMore(Static):
    can_focus = True
    BINDINGS = [("enter", "load", "Load history"), ("space", "load", "Load history")]

    def on_click(self) -> None:
        self.action_load()

    def action_load(self) -> None:
        self.parent.load_earlier()


class TranscriptView(VerticalScroll):
    PAGE_SIZE = 10

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._turns: list[dict] = []
        self._tools: dict[str, dict] = {}
        self._start = 0
        self._more: HistoryMore | None = None
        self.follow_output = True
        self.selecting = False
        self._reading_state: tuple | None = None

    def record_reading_state(self) -> None:
        state = (self.follow_output, self.selecting, bool(self.screen.selections),
                 self.is_vertical_scrollbar_grabbed)
        if state == self._reading_state:
            return
        self._reading_state = state
        record_event("reading.changed", follow=state[0], selecting=state[1],
                     selected=state[2], grabbed=state[3], scroll_y=self.scroll_y,
                     scroll_max=self.max_scroll_y,
                     streaming=getattr(self.app, "_ask_active", False))

    def on_resize(self) -> None:
        # Buffered Markdown can change virtual height one layout after flush.
        # Follow the new bottom only while the user's follow intent is active.
        if not self.reading_paused:
            self.scroll_end(animate=False, immediate=True)

    def watch_virtual_size(self) -> None:
        # A reply may grow without changing the viewport or emitting Resize.
        if self.is_mounted and not self.reading_paused:
            self.app._scroll_end()

    def clear_transcript(self) -> None:
        self.follow_output = True
        self.selecting = False
        self.remove_class("home")
        self._turns = []
        self._tools = {}
        self._start = 0
        self._more = None
        self.remove_children()

    @property
    def reading_paused(self) -> bool:
        return (not self.follow_output or self.selecting or self.is_vertical_scrollbar_grabbed
                or any(self in widget.ancestors_with_self for widget in self.screen.selections))

    def resume_output(self) -> None:
        selected = any(self in widget.ancestors_with_self for widget in self.screen.selections)
        if not self.is_vertical_scroll_end or self.selecting or selected or self.is_vertical_scrollbar_grabbed:
            return
        self.follow_output = True
        changed = [reply.flush_pending() for reply in self.query(ChatReply)]
        self.record_reading_state()
        if any(changed):
            self.app._scroll_end()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value:
            self.follow_output = False
            self.record_reading_state()
        elif self.is_vertical_scroll_end:
            self.call_after_refresh(self.resume_output)

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self.follow_output = False
        self.record_reading_state()
        self.app.query_one("#input").suspend_cursor()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self.app.query_one("#input").suspend_cursor()
        self.call_after_refresh(self.resume_output)

    def show_session(self, session: Session) -> None:
        self._turns = []
        self._tools = {}
        for record in session.records_after_summary():
            kind = record.get("type")
            if kind == "tool":
                self._tools[str(record.get("tool_call_id"))] = record
            elif kind == "message" and record.get("role") == "user" and str(record.get("content") or "").strip():
                self._turns.append(record)
            elif kind == "assistant" and (record.get("content") or record.get("tool_calls") or record.get("reasoning")):
                self._turns.append(record)
        widgets = []
        if session.damaged_tail_line is not None:
            widgets.append(Static("Incomplete session tail: recovered complete records; original file preserved.", classes="card"))
        summary = session.summary_text()
        if summary:
            widgets.extend([Static("Earlier summary", classes="who-ai"), ChatReply(summary, classes="reply")])
        self._start = max(0, len(self._turns) - self.PAGE_SIZE)
        if self._start:
            self._more = HistoryMore(f"Load earlier records ({self._start} remaining)", classes="card")
            widgets.append(self._more)
        widgets.extend(self._widgets(self._turns[self._start:]))
        if widgets:
            self.mount(*widgets)

    def _widgets(self, turns: list[dict]) -> list:
        widgets = []
        for record in turns:
            content = str(record.get("content") or "")
            stamp = str(record.get("at") or "")[11:16]
            if record.get("type") == "message":
                if stamp:
                    widgets.append(Static(f"You · {stamp}", classes="who-user"))
                widgets.append(Vertical(Static(content, classes="bubble"), classes="user-turn"))
                continue
            reasoning = record.get("reasoning") or {}
            if (reasoning.get("text") or "").strip():
                widgets.append(ThinkingPanel(reasoning["text"], record_id=record["id"],
                                             seconds=reasoning.get("seconds")))
            if content.strip():
                widgets.append(Static(f"Syvrio · {stamp}" if stamp else "Syvrio", classes="who-ai"))
                widgets.append(ChatReply(content, classes="reply"))
            for call in record.get("tool_calls") or []:
                result = self._tools.get(str(call.get("id")))
                if result is not None:
                    event = self.app._tool_event_label(call, result)
                    memo = event.startswith(("Saved", "Corrected", "Deleted"))
                    widgets.append(Static(f"✓ {event}" if memo else event,
                                          classes="event-card memo" if memo else "event-card"))
        return widgets

    def load_earlier(self) -> None:
        if not self._start or self._more is None:
            return
        more = self._more
        start = max(0, self._start - self.PAGE_SIZE)
        self.mount(*self._widgets(self._turns[start:self._start]), after=more)
        self._start = start
        if start:
            more.update(f"Load earlier records ({start} remaining)")
        else:
            more.remove()
            self._more = None
        self.call_after_refresh(self.scroll_home, animate=False)


class HomePanel(Vertical):
    """Seven-row terminal wordmark with rounded strokes and dot transitions."""

    WORDMARK = "Syvrio"
    GLYPHS = {
        "S": ("╭─────╮", "│      ", "│      ", "╰─────╮", "      │", "      │", "·─────╯"),
        "y": ("       ", "       ", "       ", "·     ·", "│     │", "╰─────┤", "      ·"),
        "v": ("        ", "        ", "        ", "╲      ╱", " ╲    ╱ ", "  ╲  ╱  ", "   ╲╱   "),
        "r": ("       ", "       ", "       ", "╭─────╮", "│      ", "│      ", "·      "),
        "i": ("   ", "   ", " · ", " │ ", " │ ", " │ ", " · "),
        "o": ("       ", "       ", "       ", "╭─────╮", "│     │", "│     │", "╰─────╯"),
    }

    @classmethod
    def wordmark_width(cls) -> int:
        return sum(len(cls.GLYPHS[letter][0]) for letter in cls.WORDMARK) + len(cls.WORDMARK) - 1

    @classmethod
    def wordmark(cls) -> Text:
        result = Text(no_wrap=True)
        for row in range(7):
            if row:
                result.append("\n")
            for index, letter in enumerate(cls.WORDMARK):
                if index:
                    result.append(" ")
                style = "#6CB6FF" if letter == "i" and row == 2 else "#b0b0b0"
                result.append(cls.GLYPHS[letter][row], style=style)
        return result

    def compose(self) -> ComposeResult:
        with Center():
            yield Static(self.wordmark(), classes="welcome-wordmark")
        yield Static("Talk naturally. I'll remember the details.", classes="welcome-line")

    def on_mount(self) -> None:
        self.call_after_refresh(self._fit_wordmark)

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit_wordmark)

    def _fit_wordmark(self) -> None:
        if not self.is_mounted:
            return
        logo = next(iter(self.query(".welcome-wordmark")), None)
        if logo is None:
            return
        width = self.wordmark_width()
        compact = self.size.width < width
        logo.update(Text(self.WORDMARK, style="#b0b0b0") if compact else self.wordmark())
        logo.styles.width = len(self.WORDMARK) if compact else width
        logo.styles.height = 1 if compact else 7


class OptionItem(ListItem):
    def __init__(self, label: str, value, marker: bool = False) -> None:
        self.label = label
        self.value = value
        super().__init__(Label(f"{label}  ← current" if marker else label, markup=False))


class SessionItem(ListItem):
    def __init__(self, row: dict) -> None:
        self.row = row
        super().__init__(
            Vertical(
                Label(row["title"], classes="session-title"),
                Label(row["meta"], classes="session-meta"),
            )
        )


class PickScreen(ModalScreen):
    """单选模态：ESC 取消（回调收到 None），Enter/点击确认；可选用搜索框过滤。"""
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("up", "move_selection(-1)", "Previous"),
        ("down", "move_selection(1)", "Next"),
        Binding("pageup", "read_detail(-1)", show=False, priority=True),
        Binding("pagedown", "read_detail(1)", show=False, priority=True),
    ]

    def __init__(self, title: str, options: list[tuple], on_result, searchable: bool = False,
                 *, detail: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._on_result = on_result
        self._searchable = searchable
        self._detail = detail

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return self._detail is not None if action == "read_detail" else super().check_action(action, parameters)

    def action_read_detail(self, direction: int) -> None:
        panel = self.query_one("#pick-detail", VerticalScroll)
        (panel.scroll_page_up if direction < 0 else panel.scroll_page_down)(animate=False)

    @staticmethod
    def _items(options: list[tuple]) -> list[OptionItem]:
        return [OptionItem(*option) for option in options]

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card", classes="has-detail" if self._detail is not None else ""):
            yield Static(self._title, classes="pick-title")
            if self._detail is not None:
                with VerticalScroll(id="pick-detail", classes="thin-scroll"):
                    yield Static(self._detail, markup=False, id="pick-detail-text")
            if self._searchable:
                yield Input(placeholder="Search models…", id="pick-search")
            yield ListView(*self._items(self._options), id="pick-list", classes="thin-scroll")
            hint = (
                "Type to filter · ↑↓ select · Enter confirm · ESC cancel"
                if self._searchable
                else "↑↓ select · Enter confirm · ESC cancel"
            )
            if self._detail is not None:
                hint = "PgUp/PgDn read · " + hint
            yield Static(hint, classes="pick-hint")

    def on_mount(self) -> None:
        self._focus_current()
        self.call_after_refresh(self._focus_current)
        self.on_resize()

    def _focus_current(self) -> None:
        widget = self.query_one("#pick-search" if self._searchable else "#pick-list")
        if widget.can_focus:
            widget.focus()
            if isinstance(widget, Input): self.app.cursor_position = widget.cursor_screen_offset

    def on_resize(self) -> None:
        if self._detail is not None:
            hint = "PgUp/PgDn Read · Enter Confirm · Esc Cancel" if self.size.width < 72 else "PgUp/PgDn read · ↑↓ select · Enter confirm · ESC cancel"
            self.query_one(".pick-hint", Static).update(hint)

    def update_options(self, options: list[tuple]) -> None:
        self._options = list(options)
        self._apply_filter(self.query_one("#pick-search", Input).value if self._searchable else "", preserve=True)

    def _filtered_options(self, needle: str) -> list[tuple]:
        return [option for option in self._options if needle in option[0].lower()]

    def _apply_filter(self, value: str, preserve: bool = False) -> None:
        needle = value.strip().lower()
        options = self._filtered_options(needle)
        listing = self.query_one("#pick-list", ListView)
        previous, previous_index = listing.highlighted_child, listing.index
        listing.clear()
        listing.extend(self._items(options))
        if options:
            index = 0
            if preserve and previous is not None:
                # 后台刷新不打断键盘导航：按值找回高亮项，找不到再按原位置收敛。
                index = next((i for i, option in enumerate(options) if option[1] == previous.value), min(previous_index or 0, len(options) - 1))
            listing.index = index

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._searchable: self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._searchable:
            return
        event.stop()
        item = self.query_one("#pick-list", ListView).highlighted_child
        if item is not None: self._finish(item.value)

    def action_move_selection(self, step: int) -> None:
        # 搜索框保持焦点，方向键只移动高亮，用户可以继续输入过滤。
        listing = self.query_one("#pick-list", ListView)
        if listing.children:
            listing.index = max(0, min(len(listing.children) - 1, (listing.index or 0) + step))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        value = getattr(event.item, "value", None) if event.item is not None else None
        self._finish(value)

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value) -> None:
        on_result = self._on_result
        self.dismiss()
        # dismiss 尚未完成时直接 push 下一层会撞屏，延迟到下一帧再回调。
        self.app.call_later(on_result, value)


class ModelPickerScreen(PickScreen):
    """模型列表只显示模型；管理入口使用独立快捷键，搜索框聚焦时也可用。"""

    BINDINGS = [
        Binding("ctrl+p", "providers", "Manage providers", priority=True),
        Binding("ctrl+r", "refresh_models", "Refresh models", priority=True),
        Binding("ctrl+n", "custom_model", "Custom model", priority=True),
    ]

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one(".pick-hint", Static).update(
            "Type to search · ↑↓ select · Enter to confirm · Esc to cancel\n"
            "Ctrl+P providers · Ctrl+R refresh · Ctrl+N custom model"
        )

    def _filtered_options(self, needle: str) -> list[tuple]:
        options = super()._filtered_options(needle)
        return sorted(options, key=lambda option: option[1][1].lower() != needle) if needle else options

    def action_providers(self) -> None:
        self._finish(("__providers__", ""))

    def action_refresh_models(self) -> None:
        self._finish(("__refresh__", ""))

    def action_custom_model(self) -> None:
        self._finish(("__custom__", ""))


class NewSessionButton(Static):
    """＋ 新会话：Button 默认 hover 样式（tall 边框 + 整块背景）会盖过虚线框，改 Static 自绘。"""

    def on_click(self) -> None:
        self.app._new_session()


class SidebarToggle(Static):
    """«/» 折叠切换按钮：Button 的标签渲染会溢出窄按钮，Static 按内容框精确渲染。"""

    def on_click(self) -> None:
        self.app.action_toggle_sidebar()
        self.app.query_one("#input", Input).focus()


class CommandRow(Static):
    """命令面板的一行：命令用法 + 说明；点击即执行。"""

    def __init__(self, command: str, usage: str, description: str) -> None:
        self.command = command
        usage_width = max(len(item[1]) for item in SLASH_COMMANDS)
        # 直接组合文本样式，嵌套的可选参数方括号也不经过 markup 解析。
        super().__init__(Content.assemble(
            (f"{usage:<{usage_width}}", "cmd-usage"), " ", (description, "cmd-desc"),
        ))
        self.add_class("cmd-row")

    def on_click(self) -> None:
        self.app.run_command_from_palette(self.command)


class CommandPalette(Vertical):
    """浮层命令列表：外层限高，内层滚动；高亮项始终滚入可视区域。"""

    def __init__(self) -> None:
        super().__init__(id="cmd-palette")
        self.matches: list[tuple[str, str, str]] = []
        self.index = 0
        self.prefix: str | None = None

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="cmd-list", classes="thin-scroll")

    @property
    def listing(self) -> VerticalScroll:
        return self.query_one("#cmd-list", VerticalScroll)

    def open(self, prefix: str) -> None:
        self.prefix = prefix
        self.matches = slash_matches(prefix)
        self.index = 0
        self.rebuild()
        self.display = bool(self.matches)

    def close(self) -> None:
        self.prefix = None
        self.matches = []
        self.display = False

    def move(self, delta: int) -> None:
        if not self.matches:
            return
        self.index = (self.index + delta) % len(self.matches)
        self.refresh_rows()
        self.scroll_current()

    @property
    def current(self) -> str | None:
        if 0 <= self.index < len(self.matches):
            return self.matches[self.index][0]
        return None

    def rebuild(self) -> None:
        self.listing.remove_children()
        rows = [CommandRow(*match) for match in self.matches]
        for position, row in enumerate(rows):
            row.set_class(position == self.index, "cmd-active")
        if rows:
            self.listing.mount(*rows)
        self.call_after_refresh(self._after_rebuild)

    def _after_rebuild(self) -> None:
        self.refresh_rows()
        self.scroll_current()

    def rows(self) -> list[CommandRow]:
        return list(self.listing.query(CommandRow))

    def refresh_rows(self) -> None:
        for position, row in enumerate(self.rows()):
            row.set_class(position == self.index, "cmd-active")

    def scroll_current(self) -> None:
        rows = self.rows()
        if 0 <= self.index < len(rows):
            self.listing.scroll_to_widget(rows[self.index], animate=False, immediate=True)


class SessionPickerScreen(ModalScreen):
    """居中会话选择器：搜索过滤、Enter 恢复、Ctrl+R 重命名、Ctrl+D 删除。"""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("down", "focus_list", "Select"),
        ("ctrl+r", "rename_session", "Rename"),
        Binding("ctrl+d", "delete_session", "Delete", priority=True),
    ]

    def __init__(self, rows: list[dict], current_id: str | None, on_result) -> None:
        super().__init__()
        self._rows = rows
        self._current_id = current_id
        self._on_result = on_result
        self._filtered = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static("Sessions", classes="pick-title")
            yield Input(placeholder="Search session title or ID…", id="picker-input")
            yield ListView(id="picker-list", classes="thin-scroll")
            with Horizontal(id="session-actions"):
                yield Button("Delete", id="session-delete")
            yield Static("Ctrl+R Rename · Ctrl+D Delete · Esc Close", classes="pick-hint")

    def on_mount(self) -> None:
        self._rebuild()
        self._apply_layout()
        self.query_one("#picker-input", Input).focus()

    def on_resize(self) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        # 标题、搜索、操作和提示固定，列表只分配剩余高度，窄窗口也不挤压底部操作。
        max_rows = max(1, min(16, int(self.size.height * 0.85) - 14))
        self.query_one("#picker-list").styles.max_height = max_rows

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = rows
        # 外部刷新（如删除会话）也要重算过滤结果，否则已删条目仍显示并可被选中。
        needle = self.query_one("#picker-input", Input).value.strip().lower()
        self._filtered = [
            row
            for row in rows
            if needle in row["title"].lower() or needle in row["id"].lower()
        ] if needle else rows
        self._rebuild()

    def on_input_changed(self, event: Input.Changed) -> None:
        needle = event.value.strip().lower()
        if needle:
            self._filtered = [
                row
                for row in self._rows
                if needle in row["title"].lower() or needle in row["id"].lower()
            ]
        else:
            self._filtered = self._rows
        self._rebuild()

    def _rebuild(self) -> None:
        listing = self.query_one("#picker-list", ListView)
        listing.clear()
        for row in self._filtered:
            time_text = (row.get("updated_at") or "")[11:16]
            meta = f"{time_text} · {row['messages']} msgs".lstrip("· ")
            if row["id"] == self._current_id:
                meta += " · ← current"
            item = SessionItem({**row, "meta": meta})
            if row["id"] == self._current_id:
                item.add_class("current")
            listing.append(item)
        # 清空重建后 index 为 None（无高亮），Enter 会落空——显式高亮第一项。
        if self._filtered:
            listing.index = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # 焦点在搜索框时 Enter：确认当前高亮的会话。
        event.stop()
        item = self.query_one("#picker-list", ListView).highlighted_child
        if isinstance(item, SessionItem):
            self._finish(item.row["id"])

    def action_focus_list(self) -> None:
        self.query_one("#picker-list", ListView).focus()

    def action_rename_session(self) -> None:
        item = self.query_one("#picker-list", ListView).highlighted_child
        if isinstance(item, SessionItem):
            self.app.rename_session(item.row)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        event.prevent_default()
        row = getattr(event.item, "row", None)
        if row is not None:
            self._finish(row["id"])

    def action_delete_session(self) -> None:
        item = self.query_one("#picker-list", ListView).highlighted_child
        if isinstance(item, SessionItem):
            self.app.confirm_delete_session(item.row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "session-delete":
            event.stop()
            self.action_delete_session()

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value) -> None:
        on_result = self._on_result
        self.dismiss()
        self.app.call_later(on_result, value)


class ProfileScreen(ModalScreen):
    """画像摘要独立弹窗：固定元信息与操作提示，正文单控件滚动。"""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, body: str, status: str, stale: bool = False,
                 generated_at: str | None = None, fact_count: int | None = None, unique_count: int | None = None) -> None:
        super().__init__()
        self._body = body
        self._status = status
        self._stale = stale
        self._metadata = ""
        if generated_at is not None:
            generated = generated_at[:16].replace("T", " ")
            self._metadata = f"Generated {generated} · Facts {fact_count} · Unique {unique_count}"

    def compose(self) -> ComposeResult:
        with Vertical(id="profile-card"):
            yield Static("User Profile", id="profile-title")
            yield Static("Personal memory overview", id="profile-subtitle")
            yield Static(self._status, id="profile-status", classes="profile-status stale" if self._stale else "profile-status")
            yield Static(self._metadata, id="profile-meta")
            with VerticalScroll(id="profile-body", classes="thin-scroll"):
                yield Static(self._body, id="profile-text", markup=False)
            yield Static("↑↓ Scroll · PgUp/PgDn Page · Esc Close", id="profile-hint")

    def on_mount(self) -> None:
        self.query_one("#profile-meta").display = bool(self._metadata)
        # 短摘要保持紧凑；长正文改用固定弹窗高度，让滚动区分配剩余空间。
        long_body = len(self._body) > 240 or self._body.count("\n") > 8
        self.query_one("#profile-card").set_class(long_body, "long")
        body = self.query_one("#profile-body", VerticalScroll)
        body.can_focus = True
        body.focus()

    def action_close(self) -> None:
        self.dismiss()


class TextInputScreen(ModalScreen):
    """单行输入模态：ESC 取消（None），Enter 提交。"""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, default: str = "", on_result=None, password: bool = False) -> None:
        super().__init__()
        self._title = title
        self._default = default
        self._on_result = on_result
        self._password = password

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static(self._title, classes="pick-title")
            yield Input(value=self._default, id="pick-input", password=self._password)
            yield Static("Enter confirm · ESC cancel", classes="pick-hint")

    def on_mount(self) -> None:
        self.query_one("#pick-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # 模态输入必须在此终止；否则同一个 Enter 还会冒泡到主输入框被当成聊天消息。
        event.stop()
        self._finish(event.value.strip())

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value) -> None:
        on_result = self._on_result
        self.dismiss()
        self.app.call_later(on_result, value or None)
