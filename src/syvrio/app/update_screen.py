"""Software update controls for the resident terminal client."""

from __future__ import annotations

import asyncio

import httpx
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .chat_widgets import PickScreen


class UpdateScreen(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def action_close(self) -> None:
        self.dismiss()

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static("Software update", classes="pick-title")
            yield Static("Checking PyPI…", id="update-version", markup=False)
            yield Static("", id="update-status", markup=False)
            with Horizontal(id="update-actions"):
                yield Button("Close", id="update-close")
                yield Button("Install", id="update-install", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#update-close", Button).focus()
        self.run_worker(self._check())

    async def _check(self) -> None:
        try:
            result = await self.app._request("GET", "/api/update")
        except Exception as exc:
            self.query_one("#update-version", Static).update(f"Release check failed: {exc}")
            # Local install results remain available even if PyPI is unreachable.
            await self._watch()
            return
        self._latest = result["latest"]
        self.query_one("#update-version", Static).update(
            f"Installed {result['current']} · Latest stable {self._latest}"
        )
        details = [result["message"]] if result["message"] else []
        if not result["available"]:
            details.append("No newer stable release is available.")
        elif not result["managed"]:
            details.append("Update this installation with its package manager.")
        self.query_one("#update-status", Static).update("\n".join(details))
        if result["state"] == "running":
            await self._watch()
        else:
            self.query_one("#update-install", Button).disabled = not (
                result["available"] and result["managed"]
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "update-close":
            self.dismiss()
        elif event.button.id == "update-install":
            self.app.push_screen(
                PickScreen(
                    f"Install Syvrio {self._latest}?",
                    [("Cancel", False), ("Install", True)],
                    self._confirmed,
                    detail="The managed service will restart. Reopen Syvrio to use the new terminal version.",
                )
            )

    def _confirmed(self, install: bool | None) -> None:
        if install and self.is_mounted:
            self.query_one("#update-install", Button).disabled = True
            self.query_one("#update-status", Static).update("Requesting installation…")
            # The confirmed submission belongs to the app even if this screen is dismissed.
            self.app._run_request(self._install())

    async def _install(self) -> None:
        try:
            result = await self.app._request(
                "POST", "/api/update", json={"version": self._latest, "confirmed": True}
            )
        except httpx.TransportError:
            # The server may already have started the job. Only read its receipt.
            if self.is_mounted:
                self.query_one("#update-status", Static).update("Checking installation status…")
                self.run_worker(self._watch())
            return
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#update-status", Static).update(
                    f"{exc}\nReopen /update to check the installation status."
                )
                return
            raise
        if self.is_mounted:
            self.query_one("#update-status", Static).update(result["message"])
            self.run_worker(self._watch())

    async def _watch(self) -> None:
        while self.is_mounted:
            try:
                result = await self.app._request("GET", "/api/update/status")
            except Exception:
                self.query_one("#update-status", Static).update(
                    "Connection interrupted. Reopen Syvrio and use /update to check the result."
                )
                return
            self.query_one("#update-status", Static).update(
                result["message"] or "No active update. Reopen /update to check again."
            )
            if result["state"] != "running":
                return
            await asyncio.sleep(2)
