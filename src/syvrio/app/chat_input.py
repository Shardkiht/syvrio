from __future__ import annotations

import os
import platform
import sys

from rich.control import Control
from rich.segment import Segment
from textual.geometry import Offset
from textual.strip import Strip
from textual.widgets import Input
from textual.widgets.input import Selection

from ..infra.diagnostics import record_event


class BarInput(Input):
    """Input with a thin vertical-bar cursor instead of a full-cell block."""

    _hint = ""

    def __init__(self, *args, **kwargs) -> None:
        # 提示词放边框：中文输入法组字期间 Textual 仍认为值为空，留在文字区会和组字重叠。
        hint = kwargs.pop("placeholder", "")
        kwargs["placeholder"] = ""
        super().__init__(*args, **kwargs)
        self._native_cursor_active = False
        self._cursor_suspended = False
        self._hint = hint
        self.border_title = hint or None

    @staticmethod
    def _terminal_supports_native_cursor() -> bool:
        # conhost 不识别 DECSCUSR：转义序列会被画出来或光标错置。
        # Windows Terminal（WT_*）与 VS Code 等（TERM_PROGRAM）支持，其余退回自绘光标。
        return bool(
            os.environ.get("WT_SESSION")
            or os.environ.get("WT_PROFILE_ID")
            or os.environ.get("TERM_PROGRAM")
        )

    @property
    def _uses_native_cursor(self) -> bool:
        driver = self.app._driver
        if driver is None or driver.is_headless:
            return False
        if sys.platform == "win32":
            return self._terminal_supports_native_cursor()
        # Windows OpenSSH starts the remote TUI inside WSL without WT_SESSION.
        # Its xterm-compatible frontend still anchors IME to the terminal caret.
        return (
            sys.platform == "linux"
            and "microsoft" in platform.release().lower()
            and os.environ.get("TERM", "").startswith("xterm")
        )

    @property
    def cursor_screen_offset(self) -> Offset:
        viewport = self.scrollable_content_region
        return Offset(viewport.x + self._position_to_cell(self.cursor_position) - self.scroll_offset.x, viewport.y)

    def _sync_caret(self) -> None:
        viewport = self.scrollable_content_region
        if not viewport.width:
            return
        # Commit the scroll before publishing the terminal coordinate. A deferred
        # scroll can otherwise leave the caret using the previous viewport.
        cell = self._position_to_cell(self.cursor_position)
        self.scroll_x = min(cell, max(self.scroll_offset.x, cell - viewport.width + 1))
        if self.has_focus:
            self.app.cursor_position = self.cursor_screen_offset

    def _watch_selection(self, selection: Selection) -> None:
        self.app.clear_selection()
        self._sync_caret()

    def on_resize(self) -> None:
        self._sync_caret()

    def _restart_blink(self) -> None:
        if self._uses_native_cursor:
            self._pause_blink()
            self._cursor_visible = False
        else:
            super()._restart_blink()

    def _toggle_cursor(self) -> None:
        if not self._uses_native_cursor:
            super()._toggle_cursor()

    def on_focus(self) -> None:
        self.call_after_refresh(self._show_native_cursor)

    def _show_native_cursor(self) -> None:
        if self._uses_native_cursor and self.has_focus and not self.disabled and not self._cursor_suspended:
            # IME preedit belongs to the terminal; a painted glyph cannot follow it.
            app = self.app
            app.cursor_position = self.cursor_screen_offset
            position = self.screen.outer_size.clamp_offset(app.cursor_position)
            app._driver.write(Control.move_to(*position).segment.text)
            app._driver.write("\x1b[5 q\x1b[?25h")
            self._native_cursor_active = True

    def _hide_native_cursor(self) -> None:
        if self._native_cursor_active:
            if self.app._driver is not None:
                self.app._driver.write("\x1b[?25l\x1b[0 q")
            self._native_cursor_active = False

    def on_blur(self) -> None:
        self._hide_native_cursor()

    def on_unmount(self) -> None:
        self._hide_native_cursor()

    def suspend_cursor(self) -> None:
        # 拖选正文时暂停输入区光标，避免终端每帧重定位硬件光标干扰选择。
        changed = not self._cursor_suspended
        self._cursor_suspended = True
        self._hide_native_cursor()
        if changed:
            self._record_cursor_state()
        self.refresh()

    def _resume_cursor(self) -> None:
        if self._cursor_suspended:
            self._cursor_suspended = False
            self._show_native_cursor()
            self._record_cursor_state()
            self.refresh()

    def _record_cursor_state(self) -> None:
        position = self.cursor_screen_offset
        record_event("cursor.changed", native=self._native_cursor_active,
                     suspended=self._cursor_suspended, cursor_x=position.x, cursor_y=position.y)

    def on_mouse_down(self) -> None:
        self._resume_cursor()
        self.app.clear_selection()
        self.app._chat().resume_output()

    def on_key(self) -> None:
        self._resume_cursor()

    def set_hint(self, hint: str) -> None:
        self._hint = hint
        self.border_title = None if self.value else (hint or None)

    def _watch_value(self, value: str) -> None:
        super()._watch_value(value)
        self.border_title = None if value else (self._hint or None)
        self._sync_caret()

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if self._cursor_suspended or self._uses_native_cursor or y != 0 or not self.has_focus or not self._cursor_visible:
            return strip

        # 在渲染结果里插竖线，不能改 _value：额外字符会让 Textual 的输入框重绘
        # 把背景刷到左侧，形成横贯侧栏的黑条。
        cursor_cell = self._position_to_cell(self.cursor_position) - self.scroll_offset.x
        if not 0 <= cursor_cell < strip.cell_length:
            return strip
        prefix = strip.crop(0, cursor_cell)
        rest = strip.crop(cursor_cell, strip.cell_length)
        # 插一格就要从行尾裁掉一格，保持整行宽度不变。
        tail = rest.crop(0, max(0, rest.cell_length - 1))
        bar = Strip([Segment("│", self.get_component_rich_style("input--cursor"))])
        return prefix + bar + tail
