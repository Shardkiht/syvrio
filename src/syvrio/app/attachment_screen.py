from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from ..infra.attachments import AttachmentBatch
from ..infra.documents import DocumentError


class AttachmentScreen(ModalScreen[AttachmentBatch | tuple[Path, ...] | None]):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, attachments_dir: Path | None = None) -> None:
        super().__init__()
        self.attachments_dir = attachments_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static("Attach files", classes="pick-title")
            yield Static("Enter one local file path per line.")
            yield TextArea(id="attachment-paths", soft_wrap=False)
            yield Button("Attach selected files", id="attach-selected")

    def _stage(self, paths: list[Path]) -> AttachmentBatch | tuple[Path, ...]:
        if self.attachments_dir is None:
            if not paths or any(not item.is_file() for item in paths):
                raise DocumentError("Choose one or more readable local files.")
            return tuple(paths)
        with ExitStack() as stack:
            streams = ((path.name, stack.enter_context(path.open("rb"))) for path in paths)
            return AttachmentBatch.stage(self.attachments_dir, streams)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        lines = self.query_one(TextArea).text.splitlines()
        paths = [Path(line.strip().strip('"')).expanduser() for line in lines if line.strip()]
        event.button.disabled = True
        try:
            batch = await asyncio.to_thread(self._stage, paths)
        except (DocumentError, OSError) as exc:
            self.notify(str(exc), severity="error")
            event.button.disabled = False
            return
        self.dismiss(batch)
