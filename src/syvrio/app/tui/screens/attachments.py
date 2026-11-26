from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from ....backend.client_contracts import AttachmentPolicy


class AttachmentScreen(ModalScreen[tuple[Path, ...] | None]):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, policy: AttachmentPolicy, existing: tuple[Path, ...]) -> None:
        super().__init__()
        self.policy = policy
        self.existing = existing

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static("Attach files", classes="pick-title")
            yield Static("Enter one local file path per line.")
            yield TextArea(id="attachment-paths", soft_wrap=False)
            yield Button("Attach selected files", id="attach-selected")

    def _select_files(self, paths: list[Path]) -> tuple[Path, ...]:
        if not paths or any(not item.is_file() for item in paths):
            raise ValueError("Choose one or more readable local files.")
        unique: list[Path] = []
        seen: set[bytes] = set()
        total = 0
        for path in dict.fromkeys((*self.existing, *paths)):
            self.policy.validate_name(path.name)
            with path.open("rb") as source:
                size = os.fstat(source.fileno()).st_size
                self.policy.validate_size(size)
                digest = hashlib.file_digest(source, "sha256").digest()
            if digest not in seen:
                total += size
                self.policy.validate_batch(len(unique) + 1, total)
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
