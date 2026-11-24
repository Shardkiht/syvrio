"""通过 Runtime 绑定发现本地常驻服务，并按需启动。"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from ..infra.config import default_config_path, default_vault_path
from ..infra.config.files import ConfigFiles
from ..infra.storage.locking import VaultLock
from ..infra.storage.paths import SyvrioPaths, runtime_path


def client_paths(config: Path | None) -> SyvrioPaths:
    filename = (config or default_config_path()).expanduser().resolve()
    configured_vault, runtime = ConfigFiles(filename).connection_locations(
        required=config is not None or bool(os.environ.get("SYVRIO_CONFIG"))
    )
    return SyvrioPaths(
        Path(configured_vault or default_vault_path()), runtime_path(runtime), filename
    )


def publish(paths, port: int, token: str) -> Path:
    descriptor = paths.state_dir / "backend.json"
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    payload = {"port": port, "token": token, "vault": str(paths.vault_dir.resolve())}
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=descriptor.parent, prefix=".backend-", delete=False
    ) as out:
        staged = Path(out.name)
        json.dump(payload, out)
    try:
        os.replace(staged, descriptor)
    finally:
        staged.unlink(missing_ok=True)
    return descriptor


def discover(paths) -> tuple[str, str] | None:
    try:
        data = json.loads((paths.state_dir / "backend.json").read_text(encoding="utf-8"))
        port, token = data["port"], data["token"]
        if not isinstance(port, int) or not 0 < port <= 65535 or not isinstance(token, str):
            return None
        if not secrets.compare_digest(
            str(paths.vault_dir.resolve()).encode(), data["vault"].encode()
        ):
            return None
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=1) as client:
            check = client.get(base + "/api/resident", headers={"X-Syvrio-Token": token})
            if check.status_code == 200 and check.json().get("protocol") == 1:
                return base, token
    except (OSError, KeyError, ValueError, TypeError, AttributeError, httpx.HTTPError):
        pass
    return None


def connect(paths) -> tuple[str, str]:
    existing = discover(paths)
    if existing:
        return existing
    # 临时启动锁只用于串行化服务发现和启动。常驻服务另行持有所有权锁，
    # 直到所有写入方都已停止。
    with VaultLock(paths.lock_dir / "backend-start.lock"):
        existing = discover(paths)
        if existing:
            return existing
        argv = [sys.executable, "-m", "syvrio.backend"]
        # 默认配置尚不存在时，子进程也按首次运行启动，由后端负责落盘。
        if (
            paths.config_file.exists()
            or paths.config_file != default_config_path().expanduser().resolve()
            or os.environ.get("SYVRIO_CONFIG")
            or ConfigFiles(paths.config_file).journal.exists()
        ):
            argv.extend(["--config", str(paths.config_file)])
        flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        )
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            existing = discover(paths)
            if existing:
                return existing
            if child.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError(
            "Could not connect to the resident backend. Run syvrio backend run in a terminal for diagnostics."
        )
