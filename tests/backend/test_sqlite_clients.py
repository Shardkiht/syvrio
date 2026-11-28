"""Protocol-level smoke with an isolated deployment, local model and four clients."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from keyring.backends.null import Keyring as NullKeyring
from textual.widgets import Input

from syvrio.app.tui.resident import ResidentChatApp
from syvrio.backend.client import ResidentClient
from syvrio.backend.endpoint import client_paths, discover
from syvrio.infra.storage.database import database_path


class LocalModelKeyring(NullKeyring):
    def get_password(self, service, username):
        return "local-test-only" if (service, username) == ("syvrio", "LOCAL_TEST_TOKEN") else None


class LocalModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        stream = request.get("stream", False)
        text = "synthetic sqlite response"
        if "response_format" in request:
            prompt = request["messages"][0]["content"]
            if '"summary"' in prompt:
                result = {"summary": "Synthetic persona"}
            elif '"proposals"' in prompt:
                result = {"proposals": []}
            elif '"title"' in prompt:
                result = {"title": "Synthetic session"}
            else:
                result = {"memories": []}
            text = json.dumps(result)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        self.end_headers()
        common = {"id": "synthetic", "created": 1, "model": "local-demo"}
        if stream:
            for delta, finish in [({"role": "assistant", "content": text}, None), ({}, "stop")]:
                chunk = {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(
                json.dumps(
                    {
                        **common,
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ).encode()
            )


@contextmanager
def child(args, log):
    with log.open("wb") as output:
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def wait_for(probe):
    for _ in range(200):
        try:
            value = await asyncio.to_thread(probe)
            if value:
                return value
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("Timed out waiting for isolated service.")


async def test_four_clients_share_one_database(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    model = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    thread = Thread(target=model.serve_forever, daemon=True)
    thread.start()
    for name, value in {
        "SYVRIO_CONFIG": config,
        "SYVRIO_VAULT": vault,
        "SYVRIO_RUNTIME": runtime,
        "SYVRIO_CREDENTIALS_DIR": tmp_path / "creds",
        "PYTHON_KEYRING_BACKEND": "tests.backend.test_sqlite_clients.LocalModelKeyring",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }.items():
        monkeypatch.setenv(name, str(value))
    config.write_text(
        f'vault = "{vault.as_posix()}"\nruntime = "{runtime.as_posix()}"\n[git]\nauto_sync=false\n[search]\nenabled=false\nauto_start=false\n'
    )
    (tmp_path / "models.toml").write_text(
        f'[llm]\nprovider="custom"\nbase_url="http://127.0.0.1:{model.server_port}/v1"\nmodel="local-demo"\napi_key_env="LOCAL_TEST_TOKEN"\nnum_retries=0\ntimeout=4\n'
    )
    (tmp_path / "memory.toml").write_text(
        '[retrieval]\nmethod="keyword"\n[recall]\nauto_recall=0\n[dream]\nauto_reflect=false\n'
    )
    paths = client_paths(config)
    assert (paths.vault_dir, paths.runtime_dir, paths.config_file) == (vault, runtime, config)
    assert discover(paths) is None
    cli = shutil.which("syvrio", path=os.path.dirname(sys.executable))
    assert cli
    try:
        with child(
            [sys.executable, "-m", "syvrio.backend", "--config", str(config)],
            tmp_path / "resident.log",
        ) as backend:

            def endpoint():
                assert backend.poll() is None, (tmp_path / "resident.log").read_text()
                return discover(paths)

            base, _ = await wait_for(endpoint)
            assert database_path(paths).is_file()
            result = subprocess.run(
                [cli, "note", "synthetic cobalt prism", "--topic", "demo", "--config", str(config)],
                capture_output=True,
                encoding="utf-8",
                timeout=20,
            )
            assert result.returncode == 0, result.stderr
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with child(
                [cli, "web", "--no-hosts", "--port", str(port), "--config", str(config)],
                tmp_path / "gateway.log",
            ) as gateway:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
                ) as browser:

                    def health():
                        assert gateway.poll() is None, (tmp_path / "gateway.log").read_text()
                        return browser.get("/api/health").is_success

                    await wait_for(health)
                    response = browser.post("/api/chat", json={"message": "Hello local model"})
                    assert response.is_success and "synthetic sqlite response" in response.text, (
                        response.text
                    )
                    listing = browser.get("/api/sessions").json()["items"]
                    sid = listing[0]["id"]
                    assert "Hello local model" in browser.get(f"/api/sessions/{sid}").text
                    client = ResidentClient(config)
                    assert client.base == base
                    assert sid in {row["id"] for row in client.call("sessions")}
                    entry = client.read_topic("demo")["entries"][0]
                    versions = client.call("memory_history", entry_id=entry["id"])
                    assert [row["memory_id"] for row in versions] == [entry["id"]]
                    tui = ResidentChatApp(client, sid)
                    async with tui.run_test() as pilot:
                        tui.query_one("#input", Input).value = "TUI synthetic message"
                        await pilot.press("enter")
                        await wait_for(
                            lambda: (
                                "TUI synthetic message" in browser.get(f"/api/sessions/{sid}").text
                            )
                        )
                    transport = StdioTransport(
                        command=cli, args=["serve", "--config", str(config)], env=dict(os.environ)
                    )
                    async with Client(transport) as mcp:
                        read = await mcp.call_tool("read_topic", {"topic": "demo"})
                        assert "synthetic cobalt prism" in str(read.data)
                        args = {"topic": "demo", "text": "mcp synthetic quartz"}
                        first = await mcp.call_tool("note", args)
                        second = await mcp.call_tool(
                            "note", {**args, "review_token": first.data["review_token"]}
                        )
                        assert "review_required" not in str(second.data)
                        assert "mcp synthetic quartz" in str(
                            (await mcp.call_tool("recall", {"query": "quartz"})).data
                        )
                    started = client.call("dream", action="run")
                    assert started["status"] == "ok", started
                    await wait_for(
                        lambda: (
                            client.call("dream", action="status")["data"]["state"] == "completed"
                        )
                    )
                    assert "Synthetic persona" in client.call("profile")
        assert discover(paths) is None
        with child(
            [sys.executable, "-m", "syvrio.backend", "--config", str(config)],
            tmp_path / "restart.log",
        ):
            await wait_for(lambda: discover(paths))
            assert "mcp synthetic quartz" in str(ResidentClient(config).read_topic("demo"))
    finally:
        model.shutdown()
        model.server_close()
        thread.join(timeout=3)
