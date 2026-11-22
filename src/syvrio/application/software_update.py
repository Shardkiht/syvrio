"""Shared update operations for clients of a managed Syvrio installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import httpx

from .. import __version__
from ..infra import update
from ..infra.files import atomic_write
from ..infra.locking import VaultLock
from ..infra.paths import SyvrioPaths, syvrio_home

UNIT = "syvrio-software-update.service"


def _root() -> Path:
    return syvrio_home() / "update"


def _managed(root: Path) -> bool:
    link = root / "current"
    return (
        sys.platform.startswith("linux")
        and link.is_symlink()
        and link.is_file()
        and link.resolve() == (Path(sys.prefix) / "bin" / "syvrio").resolve()
        and (root / "config.toml").is_file()
        and shutil.which("systemd-run") is not None
    )


def _save(
    root: Path, state: str, message: str, progress: update.UpdateProgress | None = None
) -> dict:
    data: dict = {"state": state, "message": message}
    if progress is not None:
        data["progress"] = asdict(progress)
    atomic_write(root / "job.json", json.dumps(data).encode())
    return data


def _status(root: Path) -> dict:
    try:
        result = json.loads((root / "job.json").read_bytes())
    except FileNotFoundError:
        return {"state": "idle", "message": ""}
    if result["state"] == "running" and update._service_state(UNIT) in {"inactive", "failed"}:
        # The worker can finish just before systemd reports its unit as inactive.
        result = json.loads((root / "job.json").read_bytes())
        if result["state"] == "running":
            return _save(root, "failed", "The previous update was interrupted.")
    return result


def status() -> dict:
    root = _root()
    if not (root / "job.json").is_file():
        return {"state": "idle", "message": ""}
    with VaultLock(root / "submit.lock"):
        return _status(root)


def check() -> dict:
    root = _root()
    managed = _managed(root)
    channel = update.config_options(root)[0] if managed else "stable"
    with httpx.Client(timeout=12, follow_redirects=False) as client:
        release = update.check_release(client, __version__, channel)
    return {
        "current": __version__,
        "latest": release[0] if release else __version__,
        "available": bool(release),
        "managed": managed,
        **status(),
    }


def start(version: str, paths: SyvrioPaths) -> dict[str, str]:
    root = _root()
    if not _managed(root):
        raise update.UpdateError("unmanaged", "This installation is not managed by the updater.")
    with VaultLock(root / "submit.lock"):
        if _status(root)["state"] == "running":
            raise update.UpdateError("busy", "An update is already running.")
        # Only a locally configured installation may launch a job. The worker checks
        # the exact confirmed version against official PyPI under the shared CLI lock.
        # A separate user unit survives when the updater stops the managed Web unit.
        command = [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            "--service-type=exec",
            f"--unit={UNIT}",
        ]
        for key in (
            "PATH",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "https_proxy",
            "http_proxy",
            "no_proxy",
            "UV_DEFAULT_INDEX",
            "UV_INDEX_URL",
            "UV_CACHE_DIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        ):
            if key in os.environ:
                command.append(f"--setenv={key}")
        message = f"Updating Syvrio to {version}."
        _save(root, "running", message)
        try:
            subprocess.run(
                [
                    *command,
                    sys.executable,
                    "-I",
                    "-m",
                    "syvrio.backend.update_worker",
                    version,
                    str(root),
                    str(paths.vault_dir),
                    str(paths.runtime_dir),
                ],
                check=True,
                timeout=30,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            # The systemd manager may already own the job. Keep its state for polling.
            raise
        except (OSError, subprocess.CalledProcessError):
            _save(root, "failed", "Failed to start the update service.")
            raise
        return {"state": "running", "message": message}
