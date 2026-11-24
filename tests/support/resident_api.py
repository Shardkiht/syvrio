"""使用临时 Vault 在同一进程内验证后端 API 与网页资源。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from syvrio.app.web.assets import WebAssets
from syvrio.app.web.http import web_hosts
from syvrio.app.web.server import DEFAULT_HOST
from syvrio.backend.api.channel import APIChannel


def create_app(
    backend: APIChannel,
    *,
    autostart: bool = True,
    static_dir: Path | None = None,
    allowed_hosts: list[str] | None = None,
    host: str = DEFAULT_HOST,
    resident_token: str | None = None,
) -> FastAPI:
    """组合临时后端和静态资源以测试路由。"""
    from syvrio.backend.api.routes import create_api

    owner = backend
    assets = WebAssets(static_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autostart:
            owner.startup()
        try:
            yield
        finally:
            owner.shutdown()

    app = create_api(
        owner,
        host=host,
        allowed_hosts=web_hosts(allowed_hosts),
        lifespan=lifespan,
        static_ready=assets.ready,
        resident_token=resident_token,
    )
    app.add_api_route(
        "/{full_path:path}", assets.serve, methods=["GET", "HEAD"], include_in_schema=False
    )
    return app
