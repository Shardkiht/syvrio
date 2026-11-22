"""Textual client for the shared resident. Its timers only refresh the display."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
from textual.app import App
from textual.binding import Binding
from textual.events import Mount, Unmount
from textual.widgets import Input, ListView, Static

from ..backend.client import ResidentClient
from ..time import format_local_timestamp
from .attachment_screen import AttachmentScreen
from .chat_input import BarInput
from .chat_theme import APP_CSS
from .chat_view import ChatViewMixin
from .chat_widgets import ChatReply, CommandPalette, SessionItem, TranscriptView
from .thinking_panel import ThinkingPanel
from .tui_commands import ResidentCommands


@dataclass
class UnsentMessage:
    id: str
    session_id: str | None
    text: str
    files: tuple[Path, ...]
    reconnect: bool = False


class ResidentChatApp(ResidentCommands, ChatViewMixin, App):
    TITLE = "Syvrio — Personal Memory"
    CSS = APP_CSS
    BINDINGS = [
        Binding("ctrl+p", "model_providers", "Manage providers", priority=True, show=False),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+b", "toggle_sidebar", "Toggle sidebar"),
        ("ctrl+o", "attach_files", "Attach files"),
        Binding("ctrl+enter", "insert_message", "Insert message", priority=True, show=False),
        Binding("ctrl+r", "retry_delivery", "Retry delivery", priority=True, show=False),
    ]

    def __init__(self, client: ResidentClient, resume: str | None = None):
        super().__init__()
        self.client = client
        self.sid = resume
        self._model_status = Static("", id="model-status", markup=False)
        self._sessions: list[dict] = []
        self._version = 0
        self._ask_active = False
        self._active_id: int | None = None
        self._stream = None
        self._stream_widget = None
        self._thinking_widget = None
        self._stream_buffer = ""
        self._stream_flush_scheduled = False
        self._revision: int | None = None
        self._uploads: dict[str | None, tuple[Path, ...]] = {}
        self._drafts: dict[str | None, str] = {}
        self._refresh = asyncio.Lock()
        self._submitting = asyncio.Lock()
        self._unsent: list[UnsentMessage] = []
        self._offline = False
        self._review_locks = {name: asyncio.Lock() for name in ("pending", "inferences")}

    async def on_mount(self, event: Mount):
        self._set_sidebar_visible(False)
        self.query_one("#sessions", ListView).can_focus = False
        self.query_one("#chat", TranscriptView).can_focus = False
        self.query_one("#input", Input).focus()
        if self.sid:
            await self._request("POST", f"/api/sessions/{self.sid}/switch")
        await self._poll(force=True)
        self.set_interval(1, self._poll)

    async def on_unmount(self, event: Unmount):
        self._version += 1
        if self._stream:
            self._stream.cancel()
        try:
            await self._request("POST", "/api/view/disconnect")
        except (ValueError, OSError, httpx.TransportError):
            # During a software update the old resident may already have stopped.
            pass

    async def _request(self, method, path, **values):
        return await asyncio.to_thread(self.client.request, method, path, **values)

    def _run_request(self, coroutine):
        async def run():
            try:
                await coroutine
            except Exception as exc:
                self.notify(str(exc), severity="error")

        self.run_worker(run(), exit_on_error=False)

    async def _poll(self, force=False):
        if self._refresh.locked() and not force:
            return
        async with self._refresh:
            version = self._version
            try:
                state, health, listing = await asyncio.gather(
                    self._request("GET", "/api/chat/queue"),
                    self._request("GET", "/api/health"),
                    self._request("GET", "/api/sessions"),
                )
                if version != self._version or (not force and self._submitting.locked()):
                    return
                archived = self.sid is not None and state.get("archived_session_id") == self.sid
                if self.sid != state["session_id"]:
                    self._bind(state["session_id"], keep_input=archived)
                    version = self._version
                self._model_status.update(f"{health['provider']}/{health['model']}")
                if listing["items"] != self._sessions:
                    self._sessions = listing["items"]
                    view = self.query_one("#sessions", ListView)
                    await view.clear()
                    for row in self._sessions:
                        item = SessionItem({**row, "meta": f"{row['messages']} msgs"})
                        item.set_class(row["id"] == self.sid, "current")
                        await view.append(item)
                waiting = state["waiting"]
                queue = self.query_one("#send-queue", Static)
                failed = [item for item in self._unsent if item.session_id == self.sid]
                lines = [item["text"] for item in waiting]
                lines.extend(
                    f"Delivery unconfirmed (Ctrl+R to retry): {item.text}" for item in failed
                )
                queue.update("\n".join(lines))
                queue.display = bool(lines)
                active = state["active"]
                changed = self._revision != state["revision"]
                self._revision = state["revision"]
                if active and (self._active_id != active["turn_id"] or self._stream is None):
                    await self._watch(active)
                elif not active and self._active_id is None and (changed or force or archived):
                    await self._history()
                    if archived:
                        self.add_card("This conversation was moved to Archive.", "Archive")
                self._ask_active = bool(active) or self._active_id is not None
                self.query_one("#status", Static).update(
                    "Queued" if waiting else "Generating" if active else ""
                )
                if self._offline:
                    self._offline = False
                    for item in failed:
                        if item.reconnect:
                            self._run_request(
                                self._send(item.text, item.files, version, retry=item)
                            )
            except Exception as exc:
                if isinstance(exc, httpx.TransportError):
                    self._offline = True
                self.query_one("#status", Static).update(str(exc))

    async def _history(self, before: str | None = None):
        sid, version = self.sid, self._version
        history = await self._request("GET", f"/api/sessions/{sid}") if sid else None
        if version != self._version or sid != self.sid:
            return
        self._clear_chat()
        if not history or not history["items"]:
            self.add_welcome()
            return
        for item in history["items"]:
            if before and item["id"] == before:
                break
            stamp = format_local_timestamp(item["at"]) if item["at"] else ""
            text = item["content"]
            if item["role"] == "user":
                names = [a["name"] for a in item["attachments"]]
                self.add_user_turn("\n".join([text, *names]), stamp)
            elif item["role"] == "assistant":
                if block := item.get("reasoning"):
                    self._chat().mount(
                        ThinkingPanel(
                            block["text"], record_id=item["id"], seconds=block.get("seconds")
                        )
                    )
                if text:
                    self.add_assistant_turn(text, stamp)
            elif item["role"] == "tool":
                self._chat().mount(
                    Static(f"{item['tool_name']} · {text}", classes="event-card", markup=False)
                )
            elif item["role"] == "error":
                self.add_card(text, "Reply failed")
            elif text:
                self.add_card(text, "Summary")

    async def _watch(self, turn):
        if self._stream:
            self._stream.cancel()
        self._active_id = turn["turn_id"]
        self._ask_active = True
        version = self._version
        await self._history(before=turn.get("message_id"))
        if version != self._version:
            return
        self.add_user_turn(turn["text"])
        self._stream_buffer = ""
        self._stream_widget = None
        self._thinking_widget = None
        self._stream = self.run_worker(
            self._receive(turn, version), group="turn-stream", exclusive=True, exit_on_error=False
        )

    async def _receive(self, turn, version):
        latest = 0
        while version == self._version:
            try:
                async for event in self.client.aevents(turn["turn_id"], latest):
                    if version != self._version:
                        return
                    if event["seq"] <= latest:
                        continue
                    latest = event["seq"]
                    self._show_event(event)
                break
            except KeyError:
                # A restarted resident can serve the saved history after its event stream expires.
                break
            except ValueError as exc:
                self.notify(str(exc), severity="error")
                break
            except OSError:
                await asyncio.sleep(1)
        if version == self._version:
            self._active_id = None
            self._stream = None
            self._ask_active = False
            self._revision = None
            await self._poll(force=True)

    def _show_event(self, event):
        kind, text = event["type"], event.get("text") or ""
        if kind == "content_delta":
            if self._stream_widget is None:
                self._stream_widget = ChatReply("", classes="reply")
                self._chat().mount(self._stream_widget)
            self._stream_buffer += text
            self._stream_widget.update(self._stream_buffer)
        elif kind.startswith("reasoning_"):
            if self._thinking_widget is None:
                self._thinking_widget = ThinkingPanel("", active=True)
                self._chat().mount(self._thinking_widget)
            if kind == "reasoning_delta":
                self._thinking_widget.append(text)
            else:
                self._thinking_widget.finish(
                    text, f"{event['turn_id']}-{event['phase']}", event.get("seconds")
                )
                self._thinking_widget = None
        elif kind == "message_inserted":
            self.add_user_turn(text)
            self._stream_widget = None
            self._stream_buffer = ""
        elif kind in ("event", "tool_start", "tool_end"):
            self._chat().mount(
                Static(text or event.get("name", ""), classes="event-card", markup=False)
            )
            self._stream_widget = None
            self._stream_buffer = ""
        elif kind == "failed":
            self.notify(event.get("message", "Request failed."), severity="error")
        elif kind in ("progress", "compaction"):
            self.query_one("#status", Static).update(text)
        self._scroll_end()

    def action_attach_files(self):
        def selected(paths):
            if paths:
                self._uploads[self.sid] = paths
                self.notify(" · ".join(path.name for path in paths))

        self.push_screen(AttachmentScreen(), selected)

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id != "input":
            return
        event.stop()
        text = event.value.strip()
        files = self._uploads.get(self.sid, ())
        if not text and not files:
            return
        palette = self.query_one(CommandPalette)
        if text.startswith("/") and palette.display and palette.current and " " not in text:
            self.run_command_from_palette(palette.current)
            return
        palette.close()
        if text.startswith("/"):
            event.input.value = ""
            self._submit_command(text)
            return
        event.input.value = ""
        self._uploads.pop(self.sid, None)
        self._run_request(self._send(text or "Analyze the attached files.", files, self._version))

    def action_retry_delivery(self) -> None:
        item = next((row for row in self._unsent if row.session_id == self.sid), None)
        if item:
            self._run_request(self._send(item.text, item.files, self._version, retry=item))

    async def _send(self, text, files, version, *, retry: UnsentMessage | None = None):
        async with self._submitting:
            if version != self._version:
                return
            if retry is not None and retry not in self._unsent:
                return
            item = retry or UnsentMessage(uuid4().hex, self.sid, text, files)
            try:
                receipt = await asyncio.to_thread(
                    self.client.submit, text, item.session_id, files, submission_id=item.id
                )
            except Exception as exc:
                item.reconnect = isinstance(exc, (httpx.TransportError, json.JSONDecodeError))
                if item not in self._unsent:
                    self._unsent.append(item)
                self.notify("Message retained. Press Ctrl+R to retry delivery.", severity="warning")
                raise
            if item in self._unsent:
                self._unsent.remove(item)
            if version == self._version:
                self.sid = receipt["session_id"]
                await self._poll(force=True)

    async def action_insert_message(self):
        widget = self.query_one("#input", BarInput)
        if not self._active_id:
            await widget.action_submit()
            return
        if self._uploads.get(self.sid):
            self.notify("Press Enter to queue attachments.")
            return
        text = widget.value.strip()
        if not text:
            return
        sid, turn_id = self.sid, self._active_id

        async def insert():
            await self._request(
                "POST",
                "/api/chat/insert",
                json={"turn_id": turn_id, "session_id": sid, "message": text},
            )
            if self.sid == sid and widget.value.strip() == text:
                widget.value = ""
            elif self.sid != sid and self._drafts.get(sid, "").strip() == text:
                self._drafts.pop(sid)

        self._run_request(insert())

    def _stop_generation(self):
        if self._active_id:
            self._run_request(
                self._request("POST", "/api/chat/cancel", json={"turn_id": self._active_id})
            )

    def _new_session(self):
        self._submit_command("/new")

    def on_list_view_selected(self, event: ListView.Selected):
        if isinstance(event.item, SessionItem) and event.item.row["id"] != self.sid:
            self._run_request(self._select(event.item.row["id"]))

    async def _select(self, sid):
        if not sid:
            return
        async with self._submitting:
            await self._request("POST", f"/api/sessions/{sid}/switch")
            self._bind(sid)
            await self._poll(force=True)

    def _bind(self, sid: str | None, *, keep_input: bool = False) -> None:
        if sid != self.sid:
            field = self.query_one("#input", BarInput)
            if keep_input:
                if files := self._uploads.pop(self.sid, ()):
                    self._uploads[sid] = files
            else:
                if field.value:
                    self._drafts[self.sid] = field.value
                field.value = self._drafts.pop(sid, "")
                field.cursor_position = len(field.value)
        self._detach()
        self.sid = sid

    def _detach(self):
        self._version += 1
        if self._stream:
            self._stream.cancel()
            self._stream = None
        self._active_id = None
        self._revision = None
        self._ask_active = False
