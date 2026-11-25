from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class AttachmentScreen(ModalScreen[tuple[Path, ...] | None]):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static("Attach files", classes="pick-title")
            yield Static("Enter one local file path per line.")
            yield TextArea(id="attachment-paths", soft_wrap=False)
            yield Button("Attach selected files", id="attach-selected")

    @staticmethod
    def _select_files(paths: list[Path]) -> tuple[Path, ...]:
        if not paths or any(not item.is_file() for item in paths):
            raise ValueError("Choose one or more readable local files.")
        unique: list[Path] = []
        seen: set[bytes] = set()
        for path in dict.fromkeys(paths):
            with path.open("rb") as source:
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("Attach files of at most 20 MiB each.")
                digest = hashlib.file_digest(source, "sha256").digest()
            if digest not in seen:
                unique.append(path)
                seen.add(digest)
        return tuple(unique)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        lines = self.query_one(TextArea).text.splitlines()
        paths = [Path(line.strip().strip('"')).expanduser() for line in lines if line.strip()]
        event.button.disabled = True
        try:
            batch = await asyncio.to_thread(self._select_files, paths)
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
            event.button.disabled = False
            return
        self.dismiss(batch)
