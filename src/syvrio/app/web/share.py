"""按需为浏览器网关启用限定单个账户的 ngrok 分享。"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import typer
import uvicorn
from fastapi import FastAPI

from ...backend.client import ResidentClient
from ...backend.client_contracts import WebShareConfig
from ...infra.errors import SyvrioError
from .server import DEFAULT_HOST, run_server


def start_share(
    port: int,
    domain: str,
    github_id: str | None,
    yes: bool,
    config: Path | None,
) -> None:
    client = ResidentClient(config)
    share = WebShareConfig(**client.call("web_share", domain=domain, github_id=github_id))
    hostname, account = share.domain, share.github_user_id
    if not shutil.which("ngrok"):
        raise SyvrioError(
            "未找到 ngrok。请安装 ngrok v3，将其加入 PATH 并按 ngrok 指引配置 authtoken。"
        )
    typer.echo(f"HTTPS: https://{hostname}/   GitHub ID: {account}   Web: {DEFAULT_HOST}:{port}")
    if not yes:
        typer.confirm("保存此设置并通过 ngrok 开放这个 Web 网关？", default=False, abort=True)
    client.call("web_share", domain=hostname, github_id=account, save=True)
    run_server(DEFAULT_HOST, port, config_path=config, domains=[hostname], share=share)


def run_shared_gateway(gateway: FastAPI, port: int, share: WebShareConfig) -> None:
    policy = {
        "on_http_request": [
            {"actions": [{"type": "oauth", "config": {"provider": "github"}}]},
            {
                "expressions": [
                    f"!(actions.ngrok.oauth.identity.provider_user_id in ['{share.github_user_id}'])"
                ],
                "actions": [{"type": "deny", "config": {"status_code": 403}}],
            },
        ]
    }
    with TemporaryDirectory(prefix="syvrio-ngrok-") as directory:
        path = Path(directory) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        command = [
            "ngrok",
            "http",
            f"http://{DEFAULT_HOST}:{port}",
            "--url",
            f"https://{share.domain}",
            "--traffic-policy-file",
            str(path),
            "--inspect=false",
        ]
        server = _SharedServer(
            uvicorn.Config(gateway, host=DEFAULT_HOST, port=port, access_log=False), command
        )
        server.run()
        if server.failure:
            raise SyvrioError(server.failure)


class _SharedServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, command: list[str]) -> None:
        super().__init__(config)
        self.command = command
        self.failure = ""
        self.agent: subprocess.Popen | None = None

    async def main_loop(self) -> None:
        # 只有网关已成功占用所选端口，Uvicorn 才会进入此循环。
        try:
            self.agent = subprocess.Popen(self.command, stdin=subprocess.DEVNULL)
        except OSError as exc:
            self.failure = f"ngrok 启动失败，请检查 PATH 和执行权限：{exc}"
            return
        try:
            await super().main_loop()
        finally:
            if self.agent.poll() is None:
                self.agent.terminate()
                try:
                    await asyncio.to_thread(self.agent.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    self.agent.kill()
                    await asyncio.to_thread(self.agent.wait)

    async def on_tick(self, counter: int) -> bool:
        if await super().on_tick(counter):
            return True
        if self.agent is not None and (code := self.agent.poll()) is not None:
            self.failure = f"ngrok 已退出（{code}）。请检查 ngrok 的错误提示、authtoken 和域名。"
            return True
        return False
