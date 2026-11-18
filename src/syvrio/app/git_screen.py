"""Git 管理界面：状态展示、二次确认、同步阶段与结果。

Ctrl+S 只注册在 GitScreen 内；同步由 ChatApp 的后台 Worker 执行，
关闭界面不会取消正在进行的 Git 操作。
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..infra.gitsync import SyncResult, SyncStage
from ..time import format_local_timestamp


def _stamp(value: str | None) -> str:
    return format_local_timestamp(value) or "Unknown"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _row(label: str, value: str, color: str | None = None) -> str:
    value = escape(value)
    rendered = f"[{color}]{value}[/]" if color else value
    return f"{label:<16}{rendered}"


def status_text(status: dict | None) -> str:
    if status is None:
        return "Loading Git status…"
    repository = "Initialized" if status.get("initialized") else "Not initialized"
    remote = status.get("remote") or "Not configured"
    upstream = status.get("upstream") or "Not configured"
    branch = status.get("branch") or "Unknown"
    modified = _plural(int(status.get("modified") or 0), "modified file")
    unpushed = status.get("unpushed")
    unpushed_text = (
        "Unknown (no upstream)" if unpushed is None else _plural(int(unpushed), "commit")
    )
    last_commit = _stamp(status.get("last_commit_at"))
    last_push = _stamp(status.get("last_push_at"))
    auto_sync = "Enabled" if status.get("auto_sync") else "Disabled"
    schedule = f"Daily {status.get('schedule')}"
    next_sync = _stamp(status.get("next_sync")) if status.get("auto_sync") else "Not scheduled"
    last_result = (status.get("last_result") or "Never").capitalize()
    remote_label = f"{remote} (configured)" if status.get("remote") else remote

    lines = [
        _row("Repository", repository),
        _row("Remote", remote_label),
        _row("Upstream", upstream),
        _row("Branch", branch),
        "",
        _row("Working Tree", modified),
        _row("Unpushed", unpushed_text),
        "",
        _row("Last Commit", last_commit),
        _row("Last Push", last_push),
        "",
        _row("Auto Sync", auto_sync),
        _row("Schedule", schedule),
        _row("Next Sync", next_sync),
        "",
        _row("Last Sync", _stamp(status.get("last_sync_at"))),
        _row("Last Result", last_result),
    ]
    if status.get("last_error"):
        lines.append(_row("Last Error", str(status["last_error"]), "#c98b8b"))
    if status.get("running"):
        lines.append("")
        lines.append("[#6cb6ff]Synchronization is running…[/]")
    lines.append("")
    lines.append(
        "[#818181]Status is read from the local repository; external Git operations may require a refresh.[/]"
    )
    return "\n".join(lines)


def _stage_line(stage: SyncStage) -> str:
    labels = {
        "waiting": "Waiting",
        "running": "Running",
        "done": "Done",
        "skipped": "Skipped",
        "failed": "Failed",
    }
    colors = {
        "waiting": "#818181",
        "running": "#6cb6ff",
        "done": "#8fb98f",
        "skipped": "#818181",
        "failed": "#c98b8b",
    }
    label = labels.get(stage.state, stage.state.title())
    return f"[{colors.get(stage.state, '#818181')}][{label}][/]    {escape(stage.name)}"


def progress_text(stages: list[SyncStage]) -> str:
    lines = ["Git Synchronization", ""]
    lines.extend(_stage_line(stage) for stage in stages)
    lines.append("")
    lines.append(
        "[#818181]Git operations run in the background; closing this screen does not cancel them.[/]"
    )
    return "\n".join(lines)


def result_text(result: SyncResult, status: dict | None) -> str:
    heading = {
        "success": "Synchronization Completed",
        "partial": "Synchronization Partially Completed",
        "failed": "Synchronization Failed",
    }.get(result.status, "Synchronization Finished")
    commit = result.commit_hash or "Not created"
    push = "Success" if result.pushed else ("Skipped" if result.push_skipped else "Failed")
    upstream = (status or {}).get("upstream") or "Not configured"
    lines = [
        heading,
        "",
        _row("Commit", commit),
        _row("Push", push),
        _row("Remote", upstream),
    ]
    if result.error:
        lines.extend(["", f"[#c98b8b]Reason: {escape(result.error)}[/]"])
        if result.committed:
            lines.append("[#818181]Local commit has been preserved.[/]")
    if result.committed and not result.pushed:
        lines.append(
            "[#818181]The next synchronize attempt will retry the push without creating another commit.[/]"
        )
    return "\n".join(lines)


class GitScreen(ModalScreen):
    BINDINGS = [
        ("escape", "close_screen", "Close"),
        Binding("ctrl+s", "synchronize", "Synchronize", priority=True),
    ]

    def __init__(self, status: dict | None = None) -> None:
        super().__init__()
        self._status = status
        self._stages: list[SyncStage] = []
        self._result: SyncResult | None = None
        self._syncing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="git-card"):
            yield Static("Git Management", classes="pick-title")
            with VerticalScroll(id="git-body", classes="thin-scroll"):
                yield Static(status_text(self._status), id="git-content")
            yield Static("Ctrl+S  Synchronize", id="git-action")
            yield Static("Esc     Close", classes="pick-hint")

    def apply_status(self, status: dict) -> None:
        self._status = status
        self._refresh_display()

    def set_syncing(self) -> None:
        self._syncing = True
        self._stages = []
        self._result = None
        self._refresh_display()

    def update_progress(self, stages: list[SyncStage]) -> None:
        self._stages = stages
        self._syncing = True
        self._result = None
        self._refresh_display()

    def show_result(self, result: SyncResult) -> None:
        self._syncing = False
        self._result = result
        if result.snapshot is not None:
            self._status = result.snapshot
        self._refresh_display()

    def show_error(self, message: str) -> None:
        self._syncing = False
        self._result = None
        try:
            content = self.query_one("#git-content", Static)
        except Exception:
            return
        content.update(f"[#c98b8b]Unable to read Git status: {escape(message)}[/]")

    def sync_unavailable(self, message: str) -> None:
        self.app.notify(message)

    def _refresh_display(self) -> None:
        if not self.is_mounted:
            return
        try:
            content_widget = self.query_one("#git-content", Static)
            action = self.query_one("#git-action", Static)
        except Exception:
            return
        if self._syncing:
            content = progress_text(self._stages)
        elif self._result is not None:
            content = result_text(self._result, self._status) + "\n\n" + status_text(self._status)
        else:
            content = status_text(self._status)
        content_widget.update(content)
        action.update("Synchronizing…" if self._syncing else "Ctrl+S  Synchronize")

    def action_synchronize(self) -> None:
        if self._syncing or getattr(self.app, "_git_sync_running", False):
            self.sync_unavailable("A Git synchronization is already running.")
            return
        self.app.push_screen(GitConfirmScreen(self._status, self._confirmed))

    def _confirmed(self, confirmed) -> None:
        if confirmed is True and self.is_mounted:
            self.app.start_git_sync(self)

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "git-action":
            self.action_synchronize()

    def action_close_screen(self) -> None:
        # 同步 Worker 与锁的生命周期属于 ChatApp，关闭界面不取消任务。
        if self.app._git_screen is self:
            self.app._git_screen = None
        self.dismiss()
        self.app.call_later(self.app._focus_active_input)


class GitConfirmScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, status: dict | None, on_result) -> None:
        super().__init__()
        self._status = status or {}
        self._on_result = on_result

    def compose(self) -> ComposeResult:
        modified = int(self._status.get("modified") or 0)
        unpushed = self._status.get("unpushed")
        unpushed_text = (
            "Unknown (no upstream)" if unpushed is None else _plural(int(unpushed), "commit")
        )
        upstream = self._status.get("upstream")
        push_note = "" if upstream else "\n- Push will be skipped until an upstream is configured"
        body = (
            f"Branch: {self._status.get('branch') or 'Unknown'}\n"
            f"Upstream: {upstream or 'Not configured'}\n"
            f"Uncommitted files: {modified}\n"
            f"Unpushed commits: {unpushed_text}\n\n"
            "This operation will:\n"
            "- Create a local commit from eligible Vault changes if needed\n"
            f"- Push local commits to the configured upstream{push_note}\n"
            "- Keep the local commit if pushing fails"
        )
        with Vertical(id="git-card"):
            yield Static("Confirm Git Synchronization", classes="pick-title")
            with VerticalScroll(id="git-confirm-body", classes="thin-scroll"):
                yield Static(body, id="git-confirm", markup=False)
            with Horizontal(id="git-confirm-actions"):
                yield Button("Cancel", id="git-cancel")
                yield Button("Synchronize", id="git-synchronize")
            yield Static("Enter Select · Esc Cancel", classes="pick-hint")

    def on_mount(self) -> None:
        self.query_one("#git-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "git-synchronize":
            self._finish(True)
        elif event.button.id == "git-cancel":
            self._finish(False)

    def action_cancel(self) -> None:
        self._finish(False)

    def _finish(self, value: bool) -> None:
        on_result = self._on_result
        self.dismiss()
        self.app.call_later(on_result, value)
