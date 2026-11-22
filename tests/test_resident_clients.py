"""Real HTTP, process discovery, multi-client queue/replay and terminal acceptance."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest

from syvrio.app.resident_tui import ResidentChatApp
from syvrio.app.server import build_server
from syvrio.app.web.backend import WebBackend
from syvrio.app.web.server import create_app
from syvrio.backend.client import ResidentClient
from syvrio.backend.endpoint import client_paths, discover, publish
from syvrio.llm import AssistantTurn
from syvrio.memory.models import MemoryDraft
from syvrio.memory.service import MemoryService


def configuration(root: Path) -> Path:
    config = root / "settings.toml"
    config.write_text(
        f'vault="{(root / "vault").as_posix()}"\nruntime="{(root / "runtime").as_posix()}"\n'
        '[llm]\nprovider="custom"\nmodel="local-model"\nbase_url="http://127.0.0.1:9"\n'
        '[retrieval]\nmethod="keyword"\n[search]\nenabled=false\n[git]\nauto_sync=false\n'
        "[chat]\nauto_reflect=false\nextract_on_exit=false\nauto_recall=0\n",
        encoding="utf-8",
    )
    return config


@pytest.fixture
def headless_config(tmp_path, monkeypatch):
    # Both startup entry points must work when even importing the Web package fails.
    guard = tmp_path / "headless"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        'import sys\nsys.modules["syvrio.app.web"] = None\n', encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(guard), prepend=os.pathsep)
    monkeypatch.setenv("SYVRIO_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("SYVRIO_VAULT", str(tmp_path / "vault"))
    return configuration(tmp_path)


def test_foreground_backend_starts_without_the_web_package(headless_config):
    _, paths = client_paths(headless_config, None)
    process = subprocess.Popen(
        [sys.executable, "-m", "syvrio", "backend", "run", "--config", str(headless_config)]
    )
    try:
        deadline = time.monotonic() + 30
        while not (endpoint := discover(paths)):
            assert process.poll() is None and time.monotonic() < deadline, (
                "Foreground backend failed to start"
            )
            time.sleep(0.05)
        client = ResidentClient(headless_config)
        assert client.base == endpoint[0]
        client.call("shutdown")
        assert process.wait(timeout=15) == 0
        assert discover(paths) is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_automatic_process_discovery_cli_mcp_and_vault_ownership(tmp_path, headless_config):
    from fastmcp import Client

    config = headless_config
    first = ResidentClient(config)
    try:
        second = ResidentClient(config)
        assert first.base == second.base and first.view != second.view
        value = first.memorize(MemoryDraft(text="Local test prefers tea", topic="habits"))
        assert second.read_topic("habits")["entries"][0]["id"] == value.id
        cli = subprocess.run(
            [sys.executable, "-m", "syvrio", "topics", "--config", str(config)],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        assert cli.returncode == 0, cli.stderr
        assert "habits" in cli.stdout
        with httpx.Client(trust_env=False) as http:
            assert http.post(first.base + "/api/resident/status", json={}).status_code == 403
            assert http.get(first.base + "/").status_code == 404
            assert http.get(first.base + "/api/health").json()["static_ready"] is False
        from fastapi.testclient import TestClient

        from syvrio.app.web.gateway import create_gateway

        assets = tmp_path / "static"
        assets.mkdir()
        with TestClient(
            create_gateway(first.base, "127.0.0.1", static_dir=assets), base_url="http://127.0.0.1"
        ) as web:
            assert web.get("/").json()["error"]["code"] == "assets_missing"
            assert web.get("/api/health").json()["static_ready"] is False
            (assets / "index.html").write_text(
                "<html>Local browser bundle</html>", encoding="utf-8"
            )
            (assets / "app.js").write_text("export {}", encoding="utf-8")
            assert (
                web.get("/").text
                == web.get("/sessions/example").text
                == "<html>Local browser bundle</html>"
            )
            assert web.get("/app.js").text == "export {}"
            assert web.head("/app.js").status_code == 200
            assert web.get("/app.js", headers={"Host": "untrusted.invalid"}).status_code == 403
            assert web.get("/api/missing").status_code == 404
            assert "/api/health" in web.get("/openapi.json").json()["paths"]
            assert web.get("/api/health").json()["static_ready"] is True
            assert web.get("/api/health").json()["single_client"] is False
            assert (
                web.post(
                    "/api/resident/shutdown", json={}, headers={"X-Syvrio-Token": first.token}
                ).status_code
                == 404
            )
            assert (
                web.post(
                    "/api/sessions", headers={"Origin": "http://untrusted.invalid"}
                ).status_code
                == 403
            )

        async def review():
            async with Client(build_server(service=second)) as mcp:
                result = await mcp.call_tool("read_topic", {"topic": "habits"})
                assert result.data["entries"][0]["id"] == value.id

        asyncio.run(review())

        # A different Runtime, even though it owns a distinct Runtime lock,
        # must still acquire the same OS lock for the physical Vault.
        other = tmp_path / "other-runtime"
        check = subprocess.run(
            [
                sys.executable,
                "-c",
                "from syvrio.backend.endpoint import client_paths; from syvrio.backend.ownership import BackendLease;"
                "from pathlib import Path; import sys; _,p=client_paths(Path(sys.argv[1]),None); BackendLease(p).acquire()",
                str(config),
            ],
            env={**os.environ, "SYVRIO_RUNTIME": str(other)},
            capture_output=True,
            timeout=30,
        )
        assert check.returncode != 0 and b"LockTimeout" in check.stderr
    finally:
        first.call("shutdown")


def test_terminal_reloads_saved_state_when_a_stream_is_no_longer_available(resident_backend):
    owner, client = resident_backend
    sid = owner._sessions.create().id

    async def revisit():
        ui = ResidentChatApp(client, sid)
        async with ui.run_test():
            ui._ask_active = True
            ui._active_id = 987654
            await ui._receive({"turn_id": ui._active_id}, ui._version)
            assert ui.sid == sid and not ui._ask_active and ui._active_id is None

    asyncio.run(revisit())


@pytest.mark.parametrize("lose_receipt", [False, True])
def test_terminal_update_requires_confirmation_for_the_checked_version(
    resident_backend, monkeypatch, lose_receipt
):
    from textual.widgets import Button, ListView, Static

    from syvrio.app.chat_widgets import PickScreen
    from syvrio.app.update_screen import UpdateScreen

    _, client = resident_backend
    original = client.request
    requests = []

    def request(method, path, **values):
        if path == "/api/update":
            requests.append((method, values.get("json")))
            if method == "GET":
                return {
                    "current": "1.0.0",
                    "latest": "1.1.0",
                    "managed": True,
                    "available": True,
                    "state": "idle",
                    "message": "",
                }
            if lose_receipt:
                raise httpx.ReadError("Disconnected after the update was accepted")
            return {"state": "running", "message": "Installation started"}
        if path == "/api/update/status":
            assert method == "GET"
            return {"state": "completed", "message": "Installation completed"}
        return original(method, path, **values)

    monkeypatch.setattr(client, "request", request)

    async def install():
        ui = ResidentChatApp(client)
        async with ui.run_test() as pilot:
            await ui._command("/update")
            await pilot.pause()
            screen = ui.screen
            assert isinstance(screen, UpdateScreen)
            assert not screen.query_one("#update-install", Button).disabled
            await pilot.click("#update-install")
            assert isinstance(ui.screen, PickScreen)
            assert ui.screen.query_one("#pick-list", ListView).index == 0
            await pilot.press("enter")
            await pilot.pause()
            assert ui.screen is screen and requests == [("GET", None)]
            await pilot.click("#update-install")
            await pilot.press("down", "enter")
            await pilot.pause()
            assert screen.query_one("#update-install", Button).disabled
            async with asyncio.timeout(6):
                status = screen.query_one("#update-status", Static)
                while "Installation completed" not in str(status.render()):
                    await asyncio.sleep(0.05)
            assert requests == [("GET", None), ("POST", {"version": "1.1.0", "confirmed": True})]

    asyncio.run(install())


def test_terminal_update_shows_saved_result_when_pypi_is_unavailable(resident_backend, monkeypatch):
    from textual.widgets import Button, Static

    _, client = resident_backend
    original = client.request

    def request(method, path, **values):
        if path == "/api/update":
            assert method == "GET"
            raise ValueError("PyPI unavailable")
        if path == "/api/update/status":
            assert method == "GET"
            return {"state": "completed", "message": "Updated Syvrio 1.0.0 → 1.1.0."}
        return original(method, path, **values)

    monkeypatch.setattr(client, "request", request)

    async def reopen():
        ui = ResidentChatApp(client)
        async with ui.run_test() as pilot:
            await ui._command("/update")
            await pilot.pause()
            assert "PyPI unavailable" in str(
                ui.screen.query_one("#update-version", Static).render()
            )
            assert "Updated Syvrio 1.0.0 → 1.1.0." in str(
                ui.screen.query_one("#update-status", Static).render()
            )
            assert ui.screen.query_one("#update-install", Button).disabled

    asyncio.run(reopen())


@pytest.mark.parametrize("available,managed", [(True, False), (False, True)])
def test_terminal_update_shows_read_only_status(resident_backend, monkeypatch, available, managed):
    from textual.widgets import Button, Static

    _, client = resident_backend
    original = client.request

    def request(method, path, **values):
        if path == "/api/update":
            assert method == "GET"
            return {
                "current": "1.0.0",
                "latest": "1.1.0" if available else "1.0.0",
                "managed": managed,
                "available": available,
                "state": "idle",
                "message": "",
            }
        return original(method, path, **values)

    monkeypatch.setattr(client, "request", request)

    async def check():
        ui = ResidentChatApp(client)
        async with ui.run_test() as pilot:
            await ui._command("/update")
            await pilot.pause()
            assert ui.screen.query_one("#update-install", Button).disabled
            status = str(ui.screen.query_one("#update-status", Static).render())
            assert ("package manager" if available else "No newer stable release") in status
            await pilot.press("escape")
            assert len(ui.screen_stack) == 1

    asyncio.run(check())


def test_terminal_reconciles_a_lost_switch_reply_without_touching_the_new_draft(
    resident_backend, monkeypatch
):
    from textual.widgets import Input

    owner, client = resident_backend
    first, other = owner._sessions.create().id, owner._sessions.create().id

    async def revisit():
        ui = ResidentChatApp(client, first)
        async with ui.run_test():
            field = ui.query_one("#input", Input)
            field.value = "unsent in first"
            ui._active_id = 91
            started, release = asyncio.Event(), asyncio.Event()
            original = ui._request

            async def hold_insert(method, path, **values):
                if path != "/api/chat/insert":
                    return await original(method, path, **values)
                assert values["json"] == {
                    "turn_id": 91,
                    "session_id": first,
                    "message": "unsent in first",
                }
                started.set()
                await release.wait()

            monkeypatch.setattr(ui, "_request", hold_insert)
            pending = []
            monkeypatch.setattr(
                ui, "_run_request", lambda coro: pending.append(asyncio.create_task(coro))
            )
            await ui.action_insert_message()
            await asyncio.wait_for(started.wait(), 2)
            try:
                await original("POST", f"/api/sessions/{other}/switch")
                await ui._poll(force=True)
                assert ui.sid == other and ui._active_id is None
                assert field.value == ""
                field.value = "unsent in first"
            finally:
                release.set()
                await pending[0]
            assert field.value == "unsent in first"
            await ui._select(first)
            assert field.value == ""
            await ui._select(other)
            assert field.value == "unsent in first"

    asyncio.run(revisit())


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
            time.sleep(0.05)
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
            second["turn_id"],
            third["turn_id"],
        ]
        assert owner.conversation.active_turn_id == first["turn_id"]
        assert [item.line for item in owner.conversation.queue_snapshot().items] == [
            "second",
            "third",
        ]
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
                    await keyboard.pause(0.05)
                    if ui._active_id == first["turn_id"]:
                        break
                assert ui._active_id == first["turn_id"]
                field = ui.query_one("#input", Input)
                await ui._select(sibling["session_id"])
                assert ui.sid == sibling["session_id"] and ui._active_id is None
                await ui._command("/new")
                assert ui.sid is None
                await ui._select(sid)
                assert ui._active_id == first["turn_id"]
                field.value = "/context 62000"
                await keyboard.press("enter")
                for _ in range(60):
                    await keyboard.pause(0.05)
                    if service.config.chat.context_window == 62000:
                        break
                assert service.config.chat.context_window == 62000
                assert ui._active_id == first["turn_id"]
                field.value = "supplement"
                await keyboard.press("ctrl+enter")
                assert await asyncio.to_thread(admitted.wait, 5)
                model.release.set()
                for _ in range(60):
                    await keyboard.pause(0.05)
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
                    await pilot.pause(0.05)
                    if ui.query_one("#input", Input).value == "third":
                        break
                assert ui.query_one("#input", Input).value == "third"
                ui.query_one("#input", Input).value = "/import"
                await pilot.press("enter")
                from syvrio.app.attachment_screen import AttachmentScreen

                for _ in range(50):
                    await pilot.pause(0.05)
                    if isinstance(ui.screen, AttachmentScreen):
                        break
                assert isinstance(ui.screen, AttachmentScreen)
                files = [tmp_path / "local-one.txt", tmp_path / "local-two.txt"]
                for path in files:
                    path.write_text("synthetic content", encoding="utf-8")
                ui.screen.query_one(TextArea).text = "\n".join(map(str, files))
                await pilot.click("#attach-selected")
                for _ in range(60):
                    await pilot.pause(0.05)
                    if ui._uploads.get(sid):
                        break
                assert ui._uploads[sid] == tuple(files)

        asyncio.run(pilot())
        history = bob.request("GET", f"/api/sessions/{sid}")
        assert [r["content"] for r in history["items"] if r["role"] == "user"] == [
            "first",
            "supplement",
            "second",
        ]
    finally:
        model.release.set()
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()
