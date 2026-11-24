"""根据 CLI 选定的常驻服务启动 Textual 客户端。"""

from __future__ import annotations

from ...backend.client import ResidentClient


def run_chat(client: ResidentClient, resume: str | None = None) -> None:
    from .resident import ResidentChatApp

    ResidentChatApp(client, resume).run()
