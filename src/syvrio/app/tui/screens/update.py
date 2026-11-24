"""常驻服务终端客户端的软件更新控件。"""

from __future__ import annotations

import asyncio

import httpx
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ProgressBar, Static

from ....backend.client_contracts import UpdateProgress
from ...ui import update_progress_text
from ..widgets import PickScreen


class UpdateScreen(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def action_close(self) -> None:
        self.dismiss()

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static("Software update", classes="pick-title")
            yield Static("Checking PyPI…", id="update-version", markup=False)
            yield Static("", id="update-status", markup=False)
            meter = ProgressBar(id="update-progress", show_percentage=False, show_eta=False)
            meter.display = False
            yield meter
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
            # 即使无法访问 PyPI，仍可查看本地安装结果。
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
            self._show_job(result)
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
            self._show_job({"state": "running", "message": "Requesting installation…"})
            # 提交经确认后由应用接管，关闭当前界面不影响它。
            self.app._run_request(self._install())

    async def _install(self) -> None:
        try:
            result = await self.app._request(
                "POST", "/api/update", json={"version": self._latest, "confirmed": True}
            )
        except httpx.TransportError:
            # 服务器可能已启动任务，此处只读取回执。
            if self.is_mounted:
                self.query_one("#update-status", Static).update("Checking installation status…")
                self.run_worker(self._watch())
            return
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#update-progress", ProgressBar).display = False
                self.query_one("#update-status", Static).update(
                    f"{exc}\nReopen /update to check the installation status."
                )
                return
            raise
        if self.is_mounted:
            self._show_job(result)
            self.run_worker(self._watch())

    def _show_job(self, result: dict) -> None:
        meter = self.query_one("#update-progress", ProgressBar)
        meter.display = result["state"] == "running"
        message = result["message"] or "No active update. Reopen /update to check again."
        progress = result.get("progress")
        if meter.display:
            event = UpdateProgress(**progress) if progress else UpdateProgress("waiting", message)
            message = update_progress_text(event)
            meter.update(total=event.total, progress=event.downloaded or 0)
        self.query_one("#update-status", Static).update(message)

    async def _watch(self) -> None:
        while self.is_mounted:
            try:
                result = await self.app._request("GET", "/api/update/status")
            except Exception:
                self.query_one("#update-progress", ProgressBar).display = False
                self.query_one("#update-status", Static).update(
                    "Connection interrupted. Reopen Syvrio and use /update to check the result."
                )
                return
            self._show_job(result)
            if result["state"] != "running":
                return
            await asyncio.sleep(2)
