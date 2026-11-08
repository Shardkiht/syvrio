"""推断审查界面：单窗口完成浏览、筛选、查看详情与确认/拒绝/纠正。

业务状态流转仍由 ChatApp 的 worker 调用 MemoryService.review_inference 完成；
本模块只负责展示和把用户操作回调给上层，不直接读写推断日志。
"""
from __future__ import annotations

from collections.abc import Callable

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..memory.models import Inference

STATUS_LABELS = {
    "hypothesis": "Pending",
    "confirmed": "Confirmed",
    "rejected": "Rejected",
    "superseded": "Invalidated",
}
KIND_LABELS = {"profile": "Profile", "emotion": "Emotion", "reflection": "Reflection"}
FILTERS = (
    ("all", "All"),
    ("hypothesis", "Pending"),
    ("confirmed", "Confirmed"),
    ("rejected", "Rejected"),
    ("superseded", "Invalidated"),
)
REVIEWABLE = {"hypothesis", "confirmed"}


def _excerpt(text: str, limit: int = 120) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _short_id(source_id: str) -> str:
    return source_id if len(source_id) <= 18 else source_id[:10] + "…" + source_id[-5:]


_META_COLORS = {"hypothesis": "#6cb6ff", "rejected": "#6f6f6f", "superseded": "#6f6f6f"}


def _prompt(item: Inference) -> Text:
    """单条选项内容：状态行 + 单行省略正文；OptionList 只保留文本，挂载成本低。"""
    prompt = Text(no_wrap=True, overflow="ellipsis")
    prompt.append(
        f"{STATUS_LABELS[item.status]} · {KIND_LABELS.get(item.kind, item.kind)}",
        style=_META_COLORS.get(item.status, "#8e8e8e"),
    )
    prompt.append("\n")
    prompt.append(item.text, style="#d4d4d4")
    prompt.append("\n")  # 保留旧 ListItem 的一行间距。
    return prompt


class _NoWrap:
    """包一层 Rich renderable，避免 visualize() 转成 Content 后丢失单行省略。"""

    def __init__(self, text: Text) -> None:
        self.text = text

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.text


def _option(item: Inference) -> Option:
    return Option(_NoWrap(_prompt(item)), id=item.id)


class InferenceScreen(ModalScreen):
    """推断管理：左侧列表、右侧详情；窄终端自动改为上下布局。"""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("left", "previous_filter", "Previous filter", show=False),
        Binding("right", "next_filter", "Next filter", show=False),
        Binding("c", "review('confirm')", "Confirm", show=False),
        Binding("r", "review('reject')", "Reject", show=False),
        Binding("e", "start_correct", "Edit", show=False),
        Binding("ctrl+s", "save_correction", "Save correction", show=False),
    ]

    def __init__(
        self,
        items: list[Inference],
        sources: dict[str, str],
        on_action: Callable[[str, str, str | None], None],
    ) -> None:
        super().__init__()
        self._items = list(items)
        self._sources = dict(sources)
        self._on_action = on_action
        self._filter = "hypothesis" if any(item.status == "hypothesis" for item in items) else "all"
        self._selected_id: str | None = None
        self._detail_item: Inference | None = None
        self._busy = False
        self._editing = False
        self._by_id: dict[str, Inference] = {}
        self._rendered: dict[str, Inference] = {}
        self._visible_ids: set[str] = set()
        self._list_filter: str | None = None

    # ---------- 布局 ----------
    def compose(self) -> ComposeResult:
        with Vertical(id="inference-card"):
            yield Static("Inferences", id="inference-title")
            yield Static("AI-generated hypotheses · Separate from facts", id="inference-subtitle")
            yield Static("", id="inference-filters")
            yield Static("No inferences yet. Reflection and profile review will add them here.", id="inference-empty")
            with Horizontal(id="inference-body"):
                yield OptionList(id="inference-list", classes="thin-scroll")
                with Vertical(id="inference-detail"):
                    with VerticalScroll(id="inference-detail-body", classes="thin-scroll"):
                        yield Static(id="inference-detail-status")
                        yield Static(id="inference-detail-kind", classes="inference-detail-line")
                        yield Static(id="inference-detail-text", markup=False)
                        yield Static(id="inference-detail-sources", markup=False)
                    yield TextArea(id="inference-editor", soft_wrap=True, show_line_numbers=False)
            with Horizontal(id="inference-actions"):
                yield Button("Confirm", id="inference-confirm")
                yield Button("Reject", id="inference-reject")
                yield Button("Edit", id="inference-correct")
            with Horizontal(id="inference-edit-actions"):
                yield Button("Save edit", id="inference-save")
                yield Button("Cancel", id="inference-cancel")
            yield Static("", id="inference-message", markup=False)
            yield Static("C Confirm · R Reject · E Edit · Esc Close", id="inference-hint")

    def on_mount(self) -> None:
        self.query_one("#inference-editor").display = False
        self.query_one("#inference-edit-actions").display = False
        self.set_items(self._items, self._sources)
        self._apply_layout()

    def on_resize(self) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        narrow = self.size.width < 92
        card_height = "90%" if narrow else "85%"
        self.query_one("#inference-card").styles.height = card_height if self._items else "auto"
        body = self.query_one("#inference-body")
        body.styles.layout = "vertical" if narrow else "horizontal"
        listing = self.query_one("#inference-list", OptionList)
        listing.styles.width = "1fr" if narrow else "38%"
        listing.styles.height = "50%" if narrow else "1fr"
        detail = self.query_one("#inference-detail")
        detail.styles.padding_left = 0 if narrow else 2
        detail.styles.border_top = ("solid", "#2a2a2a") if narrow else None
        detail.styles.margin_top = 1 if narrow else 0

    # ---------- 数据与刷新 ----------
    def set_items(
        self,
        items: list[Inference],
        sources: dict[str, str],
        focus_id: str | None = None,
        feedback: str | None = None,
    ) -> None:
        self._items = list(items)
        self._by_id = {item.id: item for item in items}
        self._sources = dict(sources)
        self._busy = False
        self._editing = False
        if focus_id is not None:
            self._selected_id = focus_id
        elif self._selected_id not in self._by_id:
            self._selected_id = None
        self._set_message("")
        self._refresh_filters()
        self._sync_list()
        if feedback:
            if self._filter == "hypothesis":
                feedback += f" {len(self._filtered())} pending remaining."
            self._set_message(feedback)
        self._apply_layout()

    def _refresh_filters(self) -> None:
        counts = {key: 0 for key, _ in FILTERS}
        for item in self._items:
            counts["all"] += 1
            counts[item.status] += 1
        text = Text()
        for index, (key, label) in enumerate(FILTERS):
            if index:
                text.append("  ·  ", style="#5f5f5f")
            text.append(f"{label} {counts[key]}", style="bold #6cb6ff" if key == self._filter else "#8e8e8e")
        self.query_one("#inference-filters", Static).update(text)
        self.query_one("#inference-filters").display = bool(self._items)
        self.query_one("#inference-empty").display = not self._items
        self.query_one("#inference-body").display = bool(self._items)

    def _filtered(self) -> list[Inference]:
        if self._filter == "all":
            return list(self._items)
        return [item for item in self._items if item.status == self._filter]

    def _sync_list(self) -> None:
        """按 id 增量同步选项；确认/拒绝只移除一条或改一条，不再整表重建。"""
        listing = self.query_one("#inference-list", OptionList)
        visible = self._filtered()
        self._visible_ids = {item.id for item in visible}
        new_ids = [item.id for item in visible]
        current_ids = [option.id for option in listing.options]
        rebuild = self._list_filter != self._filter
        if not rebuild:
            removed = [index for index, item_id in enumerate(current_ids) if item_id not in self._visible_ids]
            for index in reversed(removed):
                listing.remove_option_at_index(index)
            current_ids = [item_id for item_id in current_ids if item_id in self._visible_ids]
            rebuild = current_ids != new_ids[: len(current_ids)]
        if rebuild:
            listing.clear_options()
            listing.add_options(_option(item) for item in visible)
        else:
            for index, item in enumerate(visible[: len(current_ids)]):
                if self._rendered.get(item.id) != item:
                    listing.replace_option_prompt_at_index(index, _NoWrap(_prompt(item)))
            for item in visible[len(current_ids):]:
                listing.add_option(_option(item))
        self._list_filter = self._filter
        self._rendered = {item.id: item for item in visible}
        selected_index = 0
        if visible:
            selected_index = next(
                (index for index, item in enumerate(visible) if item.id == self._selected_id), 0
            )
            listing.highlighted = selected_index
            self._selected_id = visible[selected_index].id
            listing.focus()
        else:
            listing.highlighted = None
        self._show_detail(visible[selected_index] if visible else None)

    def _current(self) -> Inference | None:
        if self._detail_item is not None:
            return self._detail_item
        option = self.query_one("#inference-list", OptionList).highlighted_option
        return self._by_id.get(option.id) if option is not None and option.id else None

    def _show_detail(self, item: Inference | None) -> None:
        self._detail_item = item
        self._editing = False
        self.query_one("#inference-editor").display = False
        self.query_one("#inference-detail-body").display = True
        self._selected_id = item.id if item else None
        if item is None:
            self.query_one("#inference-detail-status").update("Nothing selected")
            self.query_one("#inference-detail-kind").update("")
            self.query_one("#inference-detail-text").update("No inference in this filter.")
            self.query_one("#inference-detail-sources").update("")
        else:
            note = "Review only · confirmation never writes facts" if item.status in REVIEWABLE else "Read-only record"
            self.query_one("#inference-detail-status").update(STATUS_LABELS[item.status])
            self.query_one("#inference-detail-kind").update(
                f"Type: {KIND_LABELS.get(item.kind, item.kind)}   Updated: {item.updated_at[:16].replace('T', ' ')}"
            )
            self.query_one("#inference-detail-text").update(item.text)
            lines = [f"Sources ({len(item.source_ids)})"]
            for source_id in item.source_ids:
                text = self._sources.get(source_id, "")
                lines.append(f"· {_excerpt(text)}" if text else f"· Source unavailable ({_short_id(source_id)})")
            lines.append(note)
            self.query_one("#inference-detail-sources").update("\n".join(lines))
        self._sync_actions()

    def _sync_actions(self) -> None:
        item = self._detail_item
        reviewable = item is not None and item.status in REVIEWABLE and not self._busy
        for action in ("confirm", "reject", "correct"):
            button = self.query_one(f"#inference-{action}", Button)
            button.disabled = not reviewable or (action == "confirm" and item.status == "confirmed")
        self.query_one("#inference-actions").display = bool(self._items) and not self._editing
        self.query_one("#inference-edit-actions").display = self._editing
        self.query_one("#inference-save", Button).disabled = self._busy
        self.query_one("#inference-cancel", Button).disabled = self._busy
        hint = "Ctrl+S Save · Esc Cancel" if self._editing else "C Confirm · R Reject · E Edit · Esc Close"
        self.query_one("#inference-hint", Static).update(hint)

    # ---------- 筛选与选择 ----------
    def _set_filter(self, key: str) -> None:
        if key == self._filter:
            return
        self._filter = key
        self._refresh_filters()
        self._sync_list()

    def _move_filter(self, step: int) -> None:
        keys = [key for key, _ in FILTERS]
        index = (keys.index(self._filter) + step) % len(keys)
        self._set_filter(keys[index])

    def action_previous_filter(self) -> None:
        if not self._editing and not self._busy:
            self._move_filter(-1)

    def action_next_filter(self) -> None:
        if not self._editing and not self._busy:
            self._move_filter(1)

    def on_click(self, event) -> None:
        if event.widget.id == "inference-filters" and not self._editing and not self._busy:
            self._move_filter(1)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        # 列表变更期间旧高亮事件可能延迟到达，不能把已移出筛选的项写回详情。
        if not self._editing and event.option.id in self._visible_ids:
            item = self._by_id.get(event.option.id)
            if item is not None:
                self._show_detail(item)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        for action in ("confirm", "reject", "correct"):
            button = self.query_one(f"#inference-{action}", Button)
            if not button.disabled:
                button.focus()
                return

    # ---------- 审查操作 ----------
    def action_review(self, action: str) -> None:
        item = self._current()
        if self._busy or self._editing or item is None or item.status not in REVIEWABLE:
            return
        if action == "confirm" and item.status == "confirmed":
            return
        self._submit(item.id, action, None)

    def action_start_correct(self) -> None:
        item = self._current()
        if self._busy or self._editing or item is None or item.status not in REVIEWABLE:
            return
        self._editing = True
        editor = self.query_one("#inference-editor", TextArea)
        editor.load_text(item.text)
        self.query_one("#inference-detail-body").display = False
        editor.display = True
        self._sync_actions()
        self._set_message("Edit the full hypothesis · Ctrl+S save · Esc cancel")
        editor.focus()

    def action_save_correction(self) -> None:
        if not self._editing or self._busy:
            return
        item = self._current()
        if item is None:
            return
        text = self.query_one("#inference-editor", TextArea).text
        self._submit(item.id, "correct", text)

    def _submit(self, item_id: str, action: str, text: str | None) -> None:
        self._busy = True
        self._set_message("Working…")
        self._sync_actions()
        self._on_action(item_id, action, text)

    def show_error(self, message: str) -> None:
        self._busy = False
        self._set_message(message, error=True)
        self.query_one("#inference-editor").display = self._editing
        self.query_one("#inference-detail-body").display = not self._editing
        self._sync_actions()
        if self._editing:
            # 保存失败只反馈错误，不重载或隐藏编辑器中的未保存草稿。
            self.query_one("#inference-editor", TextArea).focus()

    def _set_message(self, message: str, error: bool = False) -> None:
        node = self.query_one("#inference-message", Static)
        node.update(message)
        node.set_class(error, "error")
        node.display = bool(message)

    @on(Button.Pressed, "#inference-confirm")
    def _confirm_pressed(self) -> None:
        self.action_review("confirm")

    @on(Button.Pressed, "#inference-reject")
    def _reject_pressed(self) -> None:
        self.action_review("reject")

    @on(Button.Pressed, "#inference-correct")
    def _correct_pressed(self) -> None:
        self.action_start_correct()

    @on(Button.Pressed, "#inference-save")
    def _save_pressed(self) -> None:
        self.action_save_correction()

    @on(Button.Pressed, "#inference-cancel")
    def _cancel_pressed(self) -> None:
        self._show_detail(self._current())

    def action_close(self) -> None:
        if self._busy:
            return
        if self._editing:
            self._show_detail(self._current())
            return
        self.dismiss()
