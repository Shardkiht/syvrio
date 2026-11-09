from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widgets import Button, TextArea


class ThinkingText(TextArea):
    """Keep wheel events inside the reasoning pane, including at its edges."""

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        event.stop()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        event.stop()


class ThinkingPanel(Vertical):
    def __init__(
        self, text: str, *, active: bool = False, record_id: str | None = None,
        seconds: float | None = None,
    ) -> None:
        super().__init__(classes="thinking-panel")
        self.record_id = record_id
        self._active = active
        self._mode = "preview" if active else "collapsed"
        self._seconds = seconds
        self._initial = text
        self._pending: list[str] = []
        self._scheduled = False
        self._button = Button(self._label(), classes="thinking-toggle", compact=True)
        self._area = ThinkingText(text if active else "", read_only=True, soft_wrap=True,
                                  show_line_numbers=False, show_cursor=False, classes="thinking-text")
        self._area.display = active
        self._area.styles.height = 3

    def compose(self) -> ComposeResult:
        yield self._button
        yield self._area

    def on_mount(self) -> None:
        if self._active:
            self._initial = ""
        self._apply_mode()
        if self._active:
            self.call_after_refresh(self._flush)
            self.call_after_refresh(self._follow)

    def on_resize(self) -> None:
        self._apply_mode()

    def append(self, text: str) -> None:
        self._pending.append(text)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self.is_mounted and self._pending and not self._scheduled:
            self._scheduled = True
            self.call_after_refresh(self._flush)

    def _flush(self) -> None:
        self._scheduled = False
        if not self.is_mounted or self._mode == "collapsed" or not self._pending:
            return
        area = self._area
        following = self._mode == "preview" or area.is_vertical_scroll_end
        area.insert("".join(self._pending), area.document.end)
        area.history.clear()
        self._pending.clear()
        if following:
            area.scroll_end(animate=False, immediate=True)

    def _follow(self) -> None:
        if self.is_mounted and self._mode == "preview":
            self._area.scroll_end(animate=False, immediate=True)

    def fold_preview(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._mode == "preview":
            self._mode = "collapsed"
        self._apply_mode()

    def finish(self, text: str, record_id: str, seconds: float | None) -> None:
        self.record_id = record_id
        self._seconds = seconds
        self.fold_preview()
        self._button.label = self._label()
        if not self.is_mounted:
            self._initial = text
            self._pending.clear()
            return
        area = self._area
        if self._mode == "collapsed":
            # Load the complete, redacted text on demand.
            self._initial = text
            self._pending.clear()
        else:
            self._flush()
            if area.text != text:
                scroll_y = area.scroll_y
                area.load_text(text)
                area.scroll_to(y=scroll_y, animate=False, immediate=True)

    def _label(self) -> str:
        return "Thinking…" if self._active else (
            f"Thought for {self._seconds:.1f}s" if self._seconds is not None else "Thinking"
        )

    def _apply_mode(self) -> None:
        area = self._area
        area.display = self._mode != "collapsed"
        area.styles.height = min(10, max(3, self.screen.size.height // 3)) if self._mode == "expanded" else 3
        self._button.label = self._label()

    @on(Button.Pressed, ".thinking-toggle")
    def _toggle(self, event: Button.Pressed) -> None:
        event.stop()
        if self._mode == "expanded":
            self._mode = "collapsed"
        else:
            previous = self._mode
            self._mode = "expanded"
            if previous == "collapsed" and self._initial:
                area = self._area
                if area.text != self._initial:
                    area.load_text(self._initial)
                self._initial = ""
        self._apply_mode()
        self._schedule_flush()
