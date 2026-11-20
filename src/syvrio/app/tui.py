"""Start the Textual client against the resident selected by the CLI."""

from __future__ import annotations

from ..backend.client import ResidentClient


def run_chat(client: ResidentClient, resume: str | None = None) -> None:
    from .resident_tui import ResidentChatApp

    ResidentChatApp(client, resume).run()
