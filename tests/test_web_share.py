from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

from syvrio.app.cli import app
from syvrio.app.web import share
from syvrio.infra.config import WebShareConfig, load_config

pytestmark = pytest.mark.web


def test_share_confirmation_settings_and_private_default(monkeypatch, tmp_path):
    entry = tmp_path / "config.toml"
    entry.touch()
    calls = []
    monkeypatch.setattr(share.shutil, "which", lambda name: "/bin/ngrok")
    monkeypatch.setattr(share, "run_server", lambda *a, **kw: calls.append((a, kw)))
    runner = CliRunner()
    args = ["web", "share", "--config", str(entry), "--port", "9432"]
    flags = ["--domain", "demo.ngrok-free.dev", "--github-id", "12345"]
    assert runner.invoke(app, args + flags, input="n\n").exit_code != 0
    assert entry.read_text() == "" and not calls
    assert runner.invoke(app, args + flags + ["--yes"]).exit_code == 0
    assert load_config(entry).web_share == WebShareConfig("demo.ngrok-free.dev", "12345")
    assert runner.invoke(app, args + ["--yes"]).exit_code == 0
    assert calls[0] == calls[1]
    assert calls[0][0] == ("127.0.0.1", 9432)
    assert calls[0][1]["domains"] == ["demo.ngrok-free.dev"]
    monkeypatch.setattr("syvrio.app.web.server.run_server", lambda *a, **kw: calls.append((a, kw)))
    assert runner.invoke(app, ["web", "--config", str(entry)]).exit_code == 0
    assert calls[-1][1]["domains"] == [] and "share" not in calls[-1][1]


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--github-id", "0"],
        ["--github-id", "123' || true"],
        ["--github-id", "123", "--host", "0.0.0.0"],
        ["--github-id", "123", "--map-only"],
    ],
)
def test_invalid_share_never_starts(monkeypatch, extra):
    monkeypatch.setattr(share, "run_server", lambda *a, **kw: pytest.fail("gateway launched"))
    assert (
        CliRunner()
        .invoke(app, ["web", "share", "--domain", "demo.example", "--yes", *extra])
        .exit_code
        != 0
    )


def test_ngrok_argv_and_restricted_policy(monkeypatch):
    def inspect(server):
        args = server.command
        assert args[:5] == [
            "ngrok",
            "http",
            "http://127.0.0.1:9876",
            "--url",
            "https://demo.example",
        ]
        assert "--inspect=false" in args
        rules = json.loads(Path(args[args.index("--traffic-policy-file") + 1]).read_text())[
            "on_http_request"
        ]
        assert rules == [
            {"actions": [{"type": "oauth", "config": {"provider": "github"}}]},
            {
                "expressions": ["!(actions.ngrok.oauth.identity.provider_user_id in ['123'])"],
                "actions": [{"type": "deny", "config": {"status_code": 403}}],
            },
        ]

    monkeypatch.setattr(share._SharedServer, "run", inspect)
    share.run_shared_gateway(FastAPI(), 9876, WebShareConfig("demo.example", "123"))


async def test_agent_exit_stops_gateway_and_gateway_exit_stops_agent():
    # 使用真实的 Uvicorn 与子进程，监听范围仅限本机。
    for program in ["pass", "import time; time.sleep(60)"]:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            server = share._SharedServer(
                uvicorn.Config(FastAPI(), lifespan="off"), [sys.executable, "-c", program]
            )
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                if program != "pass":
                    async with asyncio.timeout(5):
                        while server.agent is None:
                            await asyncio.sleep(0.01)
                    server.should_exit = True
                await asyncio.wait_for(task, timeout=6)
            finally:
                server.should_exit = True
                await task
            assert server.agent is not None and server.agent.poll() is not None
            assert bool(server.failure) == (program == "pass")
