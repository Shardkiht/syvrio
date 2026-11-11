from __future__ import annotations

from ..time import format_local_timestamp
from .ui import console, styled_table


def visible_sessions(rows: list[dict]) -> list[dict]:
    return [row for row in rows if int(row.get("messages", 0) or 0) > 0]


def print_sessions(rows: list[dict]) -> None:
    rows = visible_sessions(rows)
    if not rows:
        console.print("[dim]暂无会话。[/dim]")
        return
    table = styled_table("会话")
    table.add_column("ID")
    table.add_column("最近消息")
    table.add_column("消息")
    table.add_column("tokens")
    table.add_column("摘要")
    table.add_column("标题", overflow="fold")
    for row in rows:
        table.add_row(
            row["id"],
            format_local_timestamp(
                row.get("last_message_at") or row.get("started_at"), "%Y-%m-%d %H:%M:%S"
            ),
            str(row["messages"]),
            str(row["tokens"]),
            str(row["summaries"]),
            row["title"],
        )
    console.print(table)
