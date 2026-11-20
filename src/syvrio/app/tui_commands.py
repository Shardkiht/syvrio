"""Textual controls for resident command results, review and selection."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from textual.widgets import Input

from ..memory.models import Inference
from .chat_widgets import (
    ModelPickerScreen,
    PickScreen,
    ProfileScreen,
    SessionPickerScreen,
    TextInputScreen,
)
from .command_catalog import SLASH_COMMANDS
from .git_screen import GitScreen
from .inference_screen import InferenceScreen
from .pending_screen import PendingScreen


class ResidentCommands:
    def action_command_palette(self):
        self.push_screen(PickScreen("Commands", [(usage, name) for name, usage, _ in SLASH_COMMANDS],
                                    lambda name: name and self._submit_command("/" + name), searchable=True))

    def _submit_command(self, line):
        self._run_request(self._command(line))

    async def _command(self, line, *, confirmed=False, extra=None):
        name, _, argument = line.lstrip("/").partition(" ")
        if name == "rename" and not argument:
            self.push_screen(TextInputScreen("Rename current session",
                on_result=lambda value: value and self._submit_command("/rename " + value)))
            return
        data = {"line": line, "session_id": self.sid, "confirmed": confirmed, **(extra or {})}
        version = self._version
        result = await self._request("POST", "/api/commands", json=data)
        if self._version != version:
            return
        status, intent, message = result["status"], result["intent"], result["message"]
        values = result.get("data") or {}
        if status == "confirm_required":
            self._confirm(message, lambda: self._run_request(self._command(line, confirmed=True, extra=extra)))
            return
        if status == "client":
            if intent == "pick_attachments":
                self.action_attach_files()
            elif intent == "leave":
                self.exit()
            elif intent == "open_sessions":
                self.push_screen(SessionPickerScreen(self._sessions, self.sid,
                    lambda value: value and self._run_request(self._select(value))))
            elif intent == "open_settings":
                await self._providers()
            return
        if status != "ok":
            if isinstance(self.screen, (PendingScreen, InferenceScreen)):
                self.screen.show_error(message)
            else:
                self.notify(message or status, severity="warning")
            return
        if intent == "new_session":
            self._detach()
            self.sid = None
            await self._poll(force=True)
        elif intent == "undo":
            restored = values.get("restored")
            if isinstance(restored, str):
                widget = self.query_one("#input", Input)
                widget.value = restored
                widget.cursor_position = len(restored)
                widget.focus()
            self._detach()
            await self._poll(force=True)
            if not restored and message:
                self.notify(message)
        elif intent == "pending":
            await self._pending(message if argument else "")
        elif intent == "inferences":
            await self._inferences(message if argument else "")
        elif intent == "profile":
            self.push_screen(ProfileScreen(message, "Profile"))
        elif intent == "git_status":
            self._git_screen = GitScreen(values.get("status"))
            self.push_screen(self._git_screen)
        elif intent == "model" and values.get("options"):
            options = [(item["label"], (item["provider"], item["model"]), item["current"]) for item in values["options"]]
            self.push_screen(ModelPickerScreen("Model", options, self._model_choice, searchable=True))
        elif intent in {"think", "context"} and values.get("options"):
            options = [(str(value or "default"), str(value or "default"), value == values.get("current")) for value in values["options"]]
            self.push_screen(PickScreen(intent.title(), options, lambda value: value and self._submit_command(f"/{intent} {value}")))
        elif intent == "topics" and values.get("names"):
            self.push_screen(PickScreen("Topics", [(topic, topic) for topic in values["names"]],
                lambda key: key and self._submit_command(f"/topics {key}"), searchable=True))
        elif intent == "dream" and values.get("rows"):
            self.push_screen(PickScreen("Dream", [(row["text"], row["id"]) for row in values["rows"]],
                lambda key: key and self._submit_command("/dream " + key), detail=message))
        elif intent == "help":
            self.action_command_palette()
        elif message:
            self.add_card(message, name.title())
        if result.get("refresh_sessions") or result.get("refresh_history") or values.get("model_changed"):
            await self._poll(force=True)

    def start_git_sync(self, screen):
        if getattr(self, "_git_sync_running", False):
            return
        self._git_sync_running = True
        screen.set_syncing()
        self._run_request(self._git_sync(screen))

    async def _git_sync(self, screen):
        from ..infra.gitsync import SyncResult
        try:
            result = await self._request("POST", "/api/commands", json={
                "line": "/git sync", "session_id": self.sid, "confirmed": True})
            if result["status"] != "ok":
                raise ValueError(result["message"])
            status = await self._request("POST", "/api/commands", json={"line": "/git", "session_id": self.sid})
            if screen.is_mounted:
                screen.show_result(SyncResult(snapshot=status["data"]["status"],
                    **{k: v for k, v in result["data"].items() if k != "kind"}))
        except Exception as exc:
            if screen.is_mounted:
                screen.show_error(str(exc))
        finally:
            self._git_sync_running = False

    def _confirm(self, title, action):
        self.push_screen(PickScreen(title, [("Cancel", False), ("Confirm", True)], lambda yes: yes and action()))

    async def _pending(self, feedback):
        rows = await asyncio.to_thread(self.client.pending)
        if isinstance(self.screen, PendingScreen):
            if rows:
                self.screen.set_items(rows, feedback)
            else:
                self.screen.dismiss()
                self.notify(feedback or "No pending items.")
        elif rows:
            def apply(action, key, text):
                self._run_request(self._command(f"/pending {action} {key}", confirmed=True,
                    extra={"pending_text": text} if text is not None else None))
            self.push_screen(PendingScreen(rows, apply))
        else:
            self.notify("No pending items.")

    async def _inferences(self, feedback):
        data = await asyncio.to_thread(self.client.call, "inference_review")
        rows = [Inference.model_validate(row) for row in data["items"]]
        if isinstance(self.screen, InferenceScreen):
            self.screen.set_items(rows, data["sources"], feedback=feedback)
        elif rows:
            def apply(key, action, text):
                self._run_request(self._command(f"/inferences {action} {key}", confirmed=True,
                    extra={"inference_text": text} if text is not None else None))
            self.push_screen(InferenceScreen(rows, data["sources"], apply))
        else:
            self.notify("No inferences.")

    def _model_choice(self, choice):
        if not choice:
            return
        provider, model = choice
        if provider == "__providers__":
            self._run_request(self._providers())
        elif provider == "__refresh__":
            self._submit_command("/model refresh")
        elif provider == "__custom__":
            self.push_screen(TextInputScreen("Model", on_result=lambda value: value and self._submit_command("/model " + value)))
        else:
            self._submit_command(f"/model {provider}/{model}")

    async def _providers(self):
        rows = (await self._request("GET", "/api/settings/providers"))["items"]
        self.push_screen(PickScreen("Providers", [(item["label"], item, item["current"]) for item in rows],
            lambda provider: provider and self._provider_actions(provider)))

    def _provider_actions(self, provider):
        def choose(action):
            if not action:
                return
            if action == "remove":
                self._confirm("Remove the saved provider credentials?", lambda: self._run_request(self._provider_write(provider, action, "")))
                return
            self.push_screen(TextInputScreen("Endpoint" if action == "set_endpoint" else "API key",
                default=provider["endpoint"] if action == "set_endpoint" else "", password=action != "set_endpoint",
                on_result=lambda text: text and self._run_request(self._provider_write(provider, action, text))))
        self.push_screen(PickScreen(provider["label"], [("Set API key", "set_key"),
            ("Change endpoint", "set_endpoint"), ("Remove credentials", "remove")], choose))

    async def _provider_write(self, provider, action, text):
        data = {"action": action, "confirmed": action == "remove"}
        if action != "remove":
            data["endpoint" if action == "set_endpoint" else "api_key"] = text
        result = await self._request("POST", "/api/settings/providers/" + quote(provider["key"], safe=""), json=data)
        self.notify(result["message"])

    def rename_session(self, row):
        self.push_screen(TextInputScreen("Rename session", default=row["title"],
            on_result=lambda title: title and self._run_request(self._rename(row["id"], title))))

    async def _rename(self, sid, title):
        await self._request("PUT", f"/api/sessions/{sid}/title", json={"title": title})
        await self._poll(force=True)

    def confirm_delete_session(self, row):
        self._run_request(self._delete_preview(row["id"]))

    async def _delete_preview(self, sid):
        preview = await self._request("GET", f"/api/sessions/{sid}/deletion-preview")
        related = "\n".join(row["text"] for row in preview["pending"])
        message = "Permanently delete this conversation and its uploaded files?"
        if related:
            message += "\nAlso remove these pending items:\n" + related
        self._confirm(message, lambda: self._run_request(self._delete(sid, preview["revision"])))

    async def _delete(self, sid, revision):
        await self._request("DELETE", f"/api/sessions/{sid}", params={"confirmed": "true", "pending_revision": revision})
        if isinstance(self.screen, SessionPickerScreen):
            self.screen.dismiss()
        await self._poll(force=True)
