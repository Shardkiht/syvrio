"""Opt-in, single-account ngrok sharing for the browser gateway."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import typer
import uvicorn
from fastapi import FastAPI

from ...backend.endpoint import client_paths
from ...infra.config import ConfigError, WebShareConfig, write_config
from .hosts import normalize_domain
from .server import DEFAULT_HOST, run_server


def start_share(
    port: int,
    domain: str,
    github_id: str | None,
    yes: bool,
    config: Path | None,
    vault: Path | None,
) -> None:
    settings, paths = client_paths(config, None)
    try:
        hostname = normalize_domain(domain or settings.web_share.domain)
    except ValueError as exc:
        raise ConfigError(
            "请用 --domain 指定 ngrok 分配的完整主机名，例如 demo.ngrok-free.dev。"
        ) from exc
    account = settings.web_share.github_user_id if github_id is None else github_id
    if not re.fullmatch(r"[1-9][0-9]{0,19}", account):
        raise ConfigError("请用 --github-id 指定唯一允许登录的 GitHub 数字账号 ID。")
    if not shutil.which("ngrok"):
        raise ConfigError(
            "未找到 ngrok。请安装 ngrok v3，将其加入 PATH 并按 ngrok 指引配置 authtoken。"
        )
    share = WebShareConfig(domain=hostname, github_user_id=account)
    typer.echo(f"HTTPS: https://{hostname}/   GitHub ID: {account}   Web: {DEFAULT_HOST}:{port}")
    if not yes:
        typer.confirm("保存此设置并通过 ngrok 开放这个 Web 网关？", default=False, abort=True)
    if settings.web_share != share:
        settings.web_share = share
        write_config(paths.config_file, settings)
    run_server(
        DEFAULT_HOST, port, config_path=config, vault_path=vault, domains=[hostname], share=share
    )


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
            raise ConfigError(server.failure)


class _SharedServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, command: list[str]) -> None:
        super().__init__(config)
        self.command = command
        self.failure = ""
        self.agent: subprocess.Popen | None = None

    async def main_loop(self) -> None:
        # Uvicorn enters this loop only after the gateway owns the selected port.
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
