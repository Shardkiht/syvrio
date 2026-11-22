"""Browser gateway entry and the embeddable Web application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from .assets import WebAssets
from .http import web_hosts

if TYPE_CHECKING:
    from ...backend.api.channel import APIChannel
    from ...infra.config import WebShareConfig

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def create_app(
    backend: APIChannel | None = None,
    *,
    autostart: bool = True,
    static_dir: Path | None = None,
    allowed_hosts: list[str] | None = None,
    host: str = DEFAULT_HOST,
    resident_token: str | None = None,
) -> FastAPI:
    """Build the in-process Web application with the same API and browser assets."""
    from ...backend.api.channel import APIChannel
    from ...backend.api.routes import create_api

    owner = backend or APIChannel()
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


def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    config_path: Path | None = None,
    vault_path: Path | None = None,
    backend: APIChannel | None = None,
    domains: list[str] | None = None,
    share: WebShareConfig | None = None,
) -> None:
    """Serve browser assets and requests without owning the Vault or background tasks."""
    import uvicorn

    from ...backend.endpoint import client_paths, connect, discover
    from .gateway import create_gateway

    _, paths = client_paths(config_path, vault_path)
    base, token = connect(paths)
    gateway = create_gateway(base, host, domains, locate=lambda: discover(paths), token=token)
    if share is not None:
        from .share import run_shared_gateway

        run_shared_gateway(gateway, port, share)
    else:
        uvicorn.run(gateway, host=host, port=port, workers=1, access_log=False)
