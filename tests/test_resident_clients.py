"""Real HTTP, process discovery, multi-client queue/replay and terminal acceptance."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread

import httpx

from syvrio.app.resident_tui import ResidentChatApp
from syvrio.app.server import build_server
from syvrio.app.web.backend import WebBackend
from syvrio.app.web.server import create_app
from syvrio.backend.client import ResidentClient
from syvrio.backend.endpoint import client_paths, publish
from syvrio.llm import AssistantTurn
from syvrio.memory.models import MemoryDraft
from syvrio.memory.service import MemoryService


def configuration(root: Path) -> Path:
    config = root / "settings.toml"
    config.write_text(f'vault="{(root / "vault").as_posix()}"\nruntime="{(root / "runtime").as_posix()}"\n'
                      '[llm]\nprovider="custom"\nmodel="local-model"\nbase_url="http://127.0.0.1:9"\n'
                      '[retrieval]\nmethod="keyword"\n[search]\nenabled=false\n[git]\nauto_sync=false\n'
                      '[chat]\nauto_reflect=false\nextract_on_exit=false\nauto_recall=0\n', encoding="utf-8")
    return config


def test_automatic_process_discovery_cli_mcp_and_vault_ownership(tmp_path, monkeypatch):
    from fastmcp import Client

    monkeypatch.setenv("SYVRIO_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("SYVRIO_VAULT", str(tmp_path / "vault"))
    config = configuration(tmp_path)
    first = ResidentClient(config)
    try:
        second = ResidentClient(config)
        assert first.base == second.base and first.view != second.view
        value = first.memorize(MemoryDraft(text="Local test prefers tea", topic="habits"))
        assert second.read_topic("habits")["entries"][0]["id"] == value.id
        cli = subprocess.run([sys.executable, "-m", "syvrio", "topics", "--config", str(config)],
                             capture_output=True, encoding="utf-8", timeout=30, env={**os.environ, "PYTHONUTF8": "1"})
        assert cli.returncode == 0, cli.stderr
        assert "habits" in cli.stdout
        with httpx.Client(trust_env=False) as http:
            assert http.post(first.base + "/api/resident/status", json={}).status_code == 403
        from fastapi.testclient import TestClient

        from syvrio.app.web.gateway import create_gateway
        with TestClient(create_gateway(first.base, "127.0.0.1"), base_url="http://127.0.0.1") as web:
            assert web.get("/api/health").json()["single_client"] is False
            assert web.post("/api/resident/shutdown", json={}, headers={"X-Syvrio-Token": first.token}).status_code == 404
            assert web.post("/api/sessions", headers={"Origin": "http://untrusted.invalid"}).status_code == 403
        async def review():
            async with Client(build_server(service=second)) as mcp:
                result = await mcp.call_tool("read_topic", {"topic": "habits"})
                assert result.data["entries"][0]["id"] == value.id
        asyncio.run(review())

        # A different Runtime, even though it owns a distinct Runtime lock,
        # must still acquire the same OS lock for the physical Vault.
        other = tmp_path / "other-runtime"
        check = subprocess.run(
            [sys.executable, "-c",
             "from syvrio.backend.endpoint import client_paths; from syvrio.backend.ownership import BackendLease;"
             "from pathlib import Path; import sys; _,p=client_paths(Path(sys.argv[1]),None); BackendLease(p).acquire()", str(config)],
            env={**os.environ, "SYVRIO_RUNTIME": str(other)}, capture_output=True, timeout=30,
        )
        assert check.returncode != 0 and b"LockTimeout" in check.stderr
    finally:
        first.call("shutdown")


class PausingModel:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.lines = []

    def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
        latest = next(item["content"] for item in reversed(messages) if item["role"] == "user")
        self.lines.append(latest)
        if len(self.lines) == 1:
            self.entered.set()
            assert self.release.wait(45)
        if on_delta:
            on_delta("answer")
        return AssistantTurn(content="answer")


def test_shared_fifo_replay_disconnect_and_textual_pilot(tmp_path, monkeypatch):
    import socket
    import time

    import uvicorn

    monkeypatch.setenv("SYVRIO_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("SYVRIO_VAULT", str(tmp_path / "vault"))
    cfg = configuration(tmp_path)
    settings, paths = client_paths(cfg, None)
    service = MemoryService(settings, config_path=cfg)
    model = PausingModel()
    owner = WebBackend(service=service)
    owner._runtime.model = model
    app = create_app(owner, resident_token="local-test-credential")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        publish(paths, port, "local-test-credential")
        alice, bob = ResidentClient(cfg), ResidentClient(cfg)
        first = alice.submit("first", None)
        sid = first["session_id"]
        assert model.entered.wait(10)
        bob.request("POST", f"/api/sessions/{sid}/switch")
        second = bob.submit("second", sid)
        third = alice.submit("third", sid)
        reader = bob.events(first["turn_id"])
        event = next(reader)
        reader.close()
        assert event["type"] == "start"
        alice.request("POST", "/api/view/disconnect")
        assert [item["turn_id"] for item in bob.request("GET", "/api/chat/queue")["waiting"]] == [
            second["turn_id"], third["turn_id"]]
        other = ResidentClient(cfg)
        sibling = other.submit("other session", None)
        assert sibling["session_id"] != sid
        assert list(other.events(sibling["turn_id"]))[-1]["type"] == "done"

        admitted = Event()
        original_insert = owner._conversation.insert_message

        def record_insert(*args, **kwargs):
            result = original_insert(*args, **kwargs)
            if result is not None:
                admitted.set()
            return result

        monkeypatch.setattr(owner._conversation, "insert_message", record_insert)
        from textual.widgets import Input, TextArea

        async def insert_pilot():
            ui = ResidentChatApp(ResidentClient(cfg), sid)
            async with ui.run_test() as keyboard:
                for _ in range(60):
                    await keyboard.pause(.05)
                    if ui._active_id == first["turn_id"]:
                        break
                assert ui._active_id == first["turn_id"]
                field = ui.query_one("#input", Input)
                field.value = "supplement"
                await keyboard.press("ctrl+enter")
                assert await asyncio.to_thread(admitted.wait, 5)
                model.release.set()
                for _ in range(60):
                    await keyboard.pause(.05)
                    if not field.value:
                        break
                assert not field.value

        asyncio.run(insert_pilot())
        assert list(bob.events(first["turn_id"], after=event["seq"]))[-1]["type"] == "done"
        assert list(bob.events(third["turn_id"]))[-1]["type"] == "done"
        assert model.lines == ["first", "other session", "supplement", "second", "third"]
        assert list(bob.events(first["turn_id"]))[0]["seq"] == 1
        async def pilot():
            ui = ResidentChatApp(ResidentClient(cfg), sid)
            async with ui.run_test() as pilot:
                await pilot.pause()
                assert ui.sid == sid
                ui.query_one("#input", Input).value = "/undo"
                await pilot.press("enter")
                for _ in range(80):
                    await pilot.pause(.05)
                    if ui.query_one("#input", Input).value == "third":
                        break
                assert ui.query_one("#input", Input).value == "third"
                ui.query_one("#input", Input).value = "/import"
                await pilot.press("enter")
                from syvrio.app.attachment_screen import AttachmentScreen
                for _ in range(50):
                    await pilot.pause(.05)
                    if isinstance(ui.screen, AttachmentScreen):
                        break
                assert isinstance(ui.screen, AttachmentScreen)
                files = [tmp_path / "local-one.txt", tmp_path / "local-two.txt"]
                for path in files:
                    path.write_text("synthetic content", encoding="utf-8")
                ui.screen.query_one(TextArea).text = "\n".join(map(str, files))
                await pilot.click("#attach-selected")
                for _ in range(60):
                    await pilot.pause(.05)
                    if ui._uploads.get(sid):
                        break
                assert ui._uploads[sid] == tuple(files)
        asyncio.run(pilot())
        history = bob.request("GET", f"/api/sessions/{sid}")
        assert [r["content"] for r in history["items"] if r["role"] == "user"] == ["first", "supplement", "second"]
    finally:
        model.release.set()
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()
