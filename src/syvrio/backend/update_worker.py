"""Handover of a managed gateway and its resident, including separately started residents."""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx

from ..application.software_update import _save
from ..infra import update
from ..infra.config import load_config
from ..infra.diagnostics import record_event
from ..infra.paths import SyvrioPaths
from .endpoint import discover
from .ownership import BackendLease


def prepare_restart(
    client: httpx.Client, port: int, cleanup: ExitStack, *, paths: SyvrioPaths | None = None
) -> Callable[[], None] | None:
    response = client.get(f"http://127.0.0.1:{port}/api/update/instance")
    if response.status_code == 404 and paths is None:
        # Legacy CLI upgrades predate resident identity. Keep their original service lifecycle.
        return None
    response.raise_for_status()
    gateway = response.json()
    if gateway["busy"]:
        raise update.UpdateError("busy", "Wait for all replies to finish and try again.")
    paths = paths or load_config().paths()
    resident = discover(paths)
    if resident is None:
        raise update.UpdateError("resident", "Cannot identify the configured Web resident.")
    base, token = resident
    headers = {"X-Syvrio-Token": token}
    direct = client.get(f"{base}/api/resident", headers=headers)
    direct.raise_for_status()
    if not gateway.get("instance") or direct.json().get("instance") != gateway["instance"]:
        raise update.UpdateError("resident", "This Vault belongs to a different Web service.")

    ticket = {"ticket": uuid4().hex}
    stopped = False

    def resume() -> None:
        if not stopped:
            try:
                client.post(f"{base}/api/resident/update_resume", headers=headers, json=ticket)
            except httpx.TransportError:
                pass  # A lost worker/connection also releases admission via update.lock.

    cleanup.callback(resume)
    drain = client.post(f"{base}/api/resident/update_drain", headers=headers, json=ticket)
    if drain.status_code == 409:
        raise update.UpdateError("busy", "Wait for active work to finish and try again.")
    drain.raise_for_status()

    def finish_stop() -> None:
        nonlocal stopped
        try:
            try:
                # A systemd child already stopped with the gateway. A resident started
                # from another client still holds the lease and needs a graceful shutdown.
                result = client.post(f"{base}/api/resident/shutdown", headers=headers, json={})
                result.raise_for_status()
            except httpx.TransportError:
                pass  # Confirm actual release of ownership below.
            lease = BackendLease(paths)
            lease.acquire(timeout=update.STOP_TIMEOUT)
            lease.release()
            stopped = True
        except (OSError, httpx.HTTPError) as exc:
            raise update.UpdateError(
                "resident", "The resident did not finish shutting down."
            ) from exc

    return finish_stop


def run_worker(version: str, root: Path, paths: SyvrioPaths) -> None:
    def report(progress: update.UpdateProgress) -> None:
        _save(root, "running", progress.message, progress)

    try:
        result = update.update(
            root,
            *update.config_options(root),
            expected_version=version,
            on_progress=report,
            prepare_restart=partial(prepare_restart, paths=paths),
        )
    except Exception as exc:
        record_event("update.failed", exc)
        stage = exc.stage if isinstance(exc, update.UpdateError) else "installation"
        message = {
            "restored": "Update failed. The previous version is running again.",
            "rollback": "Update and rollback failed. Check the managed service on the host.",
            "busy": "Active work prevented the update. Wait for it to finish and try again.",
            "version": "The available release changed. Check for updates again.",
        }.get(stage, f"Update failed during {stage}. See the update service journal.")
        _save(root, "failed", message)
        raise
    _save(root, "completed", result)


if __name__ == "__main__":
    version, root, vault, runtime = sys.argv[1:]
    run_worker(version, Path(root), SyvrioPaths(Path(vault), Path(runtime)))
