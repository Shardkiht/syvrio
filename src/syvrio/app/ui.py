from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic
from typing import NoReturn

import click
import typer
from rich import box
from rich.console import Console
from rich.filesize import decimal
from rich.padding import Padding
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.theme import Theme

from ..backend.client_contracts import ProgressCallback, UpdateProgress
from ..infra.observability.diagnostics import record_event

# 主题色：石板蓝（与个人主页 slate-blue 同族）；警告用暖琥珀，区别于灰色与错误红。
ACCENT = "#8FAECB"
ACCENT_DIM = "#46586B"
MUTED = "#808080"
SUCCESS = "#d0d0d0"

console = Console(
    theme=Theme(
        {
            "markdown.h1": f"bold {ACCENT}",
            "markdown.h1.border": ACCENT_DIM,
            "markdown.h2": "bold #d0d0d0",
            "markdown.h2.border": ACCENT_DIM,
            "markdown.code": "#C9D4E0",
        }
    )
)


def layout_widths(cols: int | None = None) -> tuple[int, int, int]:
    # 统一以 Rich 实际渲染宽度为准，避免终端检测不一致导致内容偏右。
    cols = cols or console.width
    width = max(40, min(cols - 4, round(cols * 0.9)))
    left = 2
    return left, width, max(0, cols - left - width)


def padded(renderable, cols: int | None = None):
    # 不向右铺满终端（expand=False 并少补 2 列），避免整行恰好占满带来的折行问题。
    left, _, right = layout_widths(cols)
    return Padding(renderable, (0, max(0, right - 2), 0, left), expand=False)


def update_progress_text(event: UpdateProgress) -> str:
    if event.downloaded is None:
        return event.message
    amount = decimal(event.downloaded)
    if event.total:
        amount += f" / {decimal(event.total)} ({event.downloaded * 100 // event.total}%)"
    return f"{event.message} · {amount}"


class _UpdateConsole(Console):
    def on_broken_pipe(self) -> None:
        # Rich 默认会退出进程，进度读取端断开时仍须让升级继续。
        self.quiet = True


@contextmanager
def show_update_progress() -> Iterator[ProgressCallback]:
    output = _UpdateConsole(stderr=True, markup=False, highlight=False)
    live = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", markup=False),
        TimeElapsedColumn(),
        console=output,
        transient=True,
        redirect_stdout=False,
        redirect_stderr=False,
        disable=not output.is_interactive,
    )
    last_stage = last_text = printed_text = ""
    printed_at = 0.0
    with live:
        task = live.add_task("", total=None, visible=False)

        def display(event: UpdateProgress) -> None:
            nonlocal last_stage, last_text, printed_at, printed_text
            text = update_progress_text(event)
            if output.is_interactive:
                if last_stage and event.stage != last_stage:
                    output.print(last_text)
                live.update(task, description=text, visible=True, refresh=True)
            else:
                if (
                    last_stage == "download"
                    and event.stage != last_stage
                    and last_text != printed_text
                ):
                    output.print(last_text)
                if (
                    event.stage != last_stage
                    or monotonic() - printed_at >= 5
                    or (event.downloaded is not None and event.downloaded == event.total)
                ):
                    output.print(text)
                    printed_at, printed_text = monotonic(), text
            last_stage, last_text = event.stage, text

        try:
            yield display
        finally:
            if output.is_interactive and last_text:
                output.print(last_text)


def styled_table(title: str) -> Table:
    return Table(
        title=title,
        box=box.ROUNDED,
        border_style=MUTED,
        title_style=f"bold {ACCENT}",
        header_style=f"bold {ACCENT}",
        expand=False,
    )


def print_hits(query: str, hits, center: bool = False) -> None:
    if not hits:
        console.print(
            padded("[dim]未找到相关记忆。[/dim]") if center else "[dim]未找到相关记忆。[/dim]"
        )
        return
    table = styled_table(f"检索：{query}")
    table.add_column("ID")
    table.add_column("主题")
    table.add_column("状态")
    table.add_column("内容", overflow="fold")
    for hit in hits:
        table.add_row(hit.id, hit.topic, hit.status, hit.text)
    console.print(padded(table) if center else table)


def fail(message: str, center: bool = False, *, exc: BaseException | None = None) -> NoReturn:
    # 本函数会把原始异常转换为 typer.Exit，只有此处能保存原始原因。
    # main() 会跳过主动退出，若未在转换前记录，日志就只剩 Exit，曾导致误判更新失败原因。
    # 调用方已记录具体事件时，无需再传 exc。
    if exc is not None:
        context = click.get_current_context(silent=True)
        command = context.command.name if context is not None and context.command else "unknown"
        record_event("cli.failed", exc, command=command)
    console.print(padded(f"[red]{message}[/red]") if center else f"[red]{message}[/red]")
    raise typer.Exit(code=1)
