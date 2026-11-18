"""Review pending facts in one modal while the app handles queue writes."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..memory.models import PendingItem


def _option(item: PendingItem) -> Option:
    preview = " ".join(item.text.split())
    if len(preview) > 36:
        preview = preview[:35] + "…"
    return Option(Text.assemble((item.topic, "#6cb6ff"), "\n", preview), id=item.id)


class PendingScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("c", "decide('approve')", "Approve", show=False),
        Binding("r", "decide('reject')", "Reject", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("ctrl+s", "save", "Save and approve", show=False, priority=True),
        Binding("pageup", "read(-1)", "Read up", show=False, priority=True),
        Binding("pagedown", "read(1)", "Read down", show=False, priority=True),
    ]

    def __init__(
        self, items: list[PendingItem], on_action: Callable[[str, str, str | None], None]
    ) -> None:
        super().__init__()
        self._items = items
        self._on_action = on_action
        self._editing = False
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="pending-card"):
            yield Static("Pending Memories", id="pending-title")
            yield Static("Review candidates before writing to memory", id="pending-subtitle")
            yield Static(id="pending-count")
            with Horizontal(id="pending-body"):
                yield OptionList(
                    id="pending-list", classes="thin-scroll", markup=False, compact=True
                )
                with Vertical(id="pending-detail"):
                    with VerticalScroll(id="pending-detail-body", classes="thin-scroll"):
                        yield Static(id="pending-topic", markup=False)
                        yield Static(
                            id="pending-reason", classes="inference-detail-line", markup=False
                        )
                        yield Static(id="pending-text", markup=False)
                    yield TextArea(id="pending-editor", soft_wrap=True, show_line_numbers=False)
            with Horizontal(id="pending-actions"):
                yield Button("Approve", id="pending-approve")
                yield Button("Reject", id="pending-reject")
                yield Button("Edit", id="pending-edit")
            with Horizontal(id="pending-edit-actions"):
                yield Button("Save and approve", id="pending-save")
                yield Button("Cancel", id="pending-cancel")
            yield Static(id="pending-message", markup=False)
            yield Static(id="pending-hint")

    def on_mount(self) -> None:
        self.set_items(self._items)

    def on_resize(self) -> None:
        narrow = self.size.width < 92
        self.query_one("#pending-body").styles.layout = "vertical" if narrow else "horizontal"
        listing = self.query_one("#pending-list")
        listing.styles.width = "1fr" if narrow else "38%"
        listing.styles.height = "35%" if narrow else "1fr"
        detail = self.query_one("#pending-detail")
        detail.styles.padding_left = 0 if narrow else 2
        detail.styles.border_top = ("solid", "#2a2a2a") if narrow else None

    def set_items(self, items: list[PendingItem], feedback: str = "") -> None:
        listing = self.query_one(OptionList)
        index = listing.highlighted or 0
        self._items = items
        listing.set_options(_option(item) for item in items)
        listing.highlighted = min(index, len(items) - 1)
        self.query_one("#pending-count", Static).update(f"Pending {len(items)}")
        self._busy = False
        self._end_edit()
        self._message(feedback)

    def _current(self) -> PendingItem:
        return self._items[self.query_one(OptionList).highlighted or 0]

    def _show_detail(self) -> None:
        item = self._current()
        self.query_one("#pending-topic", Static).update(item.topic)
        self.query_one("#pending-reason", Static).update(item.reason)
        self.query_one("#pending-text", Static).update(item.text)
        self.query_one("#pending-detail-body", VerticalScroll).scroll_home(
            animate=False, immediate=True
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if not self._editing and event.option.id == self._current().id:
            self._show_detail()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action in {"decide", "edit", "read"}:
            return not self._editing and not self._busy
        return True

    def action_read(self, step: int) -> None:
        detail = self.query_one("#pending-detail-body", VerticalScroll)
        detail.scroll_relative(y=step * detail.scrollable_content_region.height, animate=False)

    def action_decide(self, action: str) -> None:
        if not self._editing and not self._busy:
            self._submit(action)

    def action_edit(self) -> None:
        if self._busy or self._editing:
            return
        self._editing = True
        editor = self.query_one(TextArea)
        editor.load_text(self._current().text)
        self._message("")
        self._sync_actions()
        editor.focus()

    def action_save(self) -> None:
        if self._editing and not self._busy:
            draft = self.query_one(TextArea).text.strip()
            if draft:
                self._submit("approve", draft)
            else:
                self.show_error("Enter text before saving.")

    def _submit(self, action: str, text: str | None = None) -> None:
        item = self._current()
        self._busy = True
        self._message("Working…")
        self._sync_actions()
        self._on_action(action, item.id, text)

    def show_error(self, message: str) -> None:
        self._busy = False
        self._message(message, error=True)
        self._sync_actions()
        self.query_one(TextArea if self._editing else OptionList).focus()

    def _message(self, message: str, *, error: bool = False) -> None:
        node = self.query_one("#pending-message", Static)
        node.update(message)
        node.set_class(error, "error")
        node.display = bool(message)

    def _sync_actions(self) -> None:
        self.query_one(OptionList).disabled = self._editing or self._busy
        editor = self.query_one(TextArea)
        editor.display = self._editing
        editor.read_only = self._busy
        self.query_one("#pending-detail-body").display = not self._editing
        self.query_one("#pending-actions").display = not self._editing
        self.query_one("#pending-edit-actions").display = self._editing
        for button in self.query(Button):
            button.disabled = self._busy
        hint = (
            "Ctrl+S Save and approve · Esc Cancel"
            if self._editing
            else ("C Approve · R Reject · E Edit · PgUp/PgDn Read · Esc Close")
        )
        self.query_one("#pending-hint", Static).update(hint)

    def _end_edit(self) -> None:
        self._editing = False
        self._sync_actions()
        self._show_detail()
        self.query_one(OptionList).focus()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "pending-approve":
                self.action_decide("approve")
            case "pending-reject":
                self.action_decide("reject")
            case "pending-edit":
                self.action_edit()
            case "pending-save":
                self.action_save()
            case "pending-cancel":
                self.action_close()

    def action_close(self) -> None:
        if self._editing and not self._busy:
            self._message("")
            self._end_edit()
        else:
            self.dismiss()
