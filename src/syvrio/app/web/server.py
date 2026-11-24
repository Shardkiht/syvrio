"""浏览器网关入口和可嵌入网页应用。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from ...backend.client_contracts import WebShareConfig

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def create_app(
    *,
    config_path: Path | None = None,
    host: str = DEFAULT_HOST,
    allowed_hosts: list[str] | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """通过常驻后端提供 API，并由网关提供静态页面。"""
    from ...backend.endpoint import client_paths, connect, discover
    from .gateway import create_gateway

    paths = client_paths(config_path)
    base, token = connect(paths)
    return create_gateway(
        base,
        host,
        allowed_hosts,
        static_dir=static_dir,
        locate=lambda: discover(paths),
        token=token,
    )


def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    config_path: Path | None = None,
    domains: list[str] | None = None,
    share: WebShareConfig | None = None,
) -> None:
    """提供浏览器资源并处理请求，Vault 和后台任务由常驻服务管理。"""
    import uvicorn

    gateway = create_app(config_path=config_path, host=host, allowed_hosts=domains)
    if share is not None:
        from .share import run_shared_gateway

        run_shared_gateway(gateway, port, share)
    else:
        uvicorn.run(gateway, host=host, port=port, workers=1, access_log=False)
