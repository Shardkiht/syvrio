"""在常驻进程的整个生命周期内持有内部监听端口和 Vault。"""

from __future__ import annotations

import json
import secrets
import socket
from pathlib import Path

from ..infra.config import ConfigError, load_config
from ..memory.service import MemoryService
from .api.channel import APIChannel
from .api.routes import create_api
from .endpoint import client_paths, publish
from .ownership import BackendLease

LOOPBACK = "127.0.0.1"


def serve_resident(config_path: Path | None) -> None:
    import uvicorn

    paths = client_paths(config_path)
    lease = BackendLease(paths)
    lease.acquire(timeout=0)
    try:
        config = load_config(config_path)
        if config.paths(paths.config_file) != paths:
            raise ConfigError("Vault 或 Runtime 在启动期间更改，请重新启动后端。")
        owner = APIChannel(service=MemoryService(config, config_path=paths.config_file))
        owner.startup(ownership=lease)
    except BaseException:
        lease.release()
        raise
    try:
        token = secrets.token_urlsafe(32)
        with socket.socket() as sock:
            sock.bind((LOOPBACK, 0))
            sock.listen()
            port = sock.getsockname()[1]
            app = create_api(owner, resident_token=token)
            server = uvicorn.Server(uvicorn.Config(app, host=LOOPBACK, port=port, access_log=False))
            app.state.stop_resident = lambda: setattr(server, "should_exit", True)
            descriptor = publish(paths, port, token)
            try:
                server.run(sockets=[sock])
            finally:
                # 在本进程仍持有 Vault 租约时移除自己的服务描述文件。
                try:
                    if json.loads(descriptor.read_bytes()).get("token") == token:
                        descriptor.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
    finally:
        owner.shutdown()
