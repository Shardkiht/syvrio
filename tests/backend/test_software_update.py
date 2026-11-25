"""核验两个客户端边界和重启交接，保留现有安装。"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from syvrio.app.web.gateway import create_gateway
from syvrio.backend.updates import installer as update
from syvrio.backend.updates import service as jobs
from syvrio.backend.updates import worker as update_worker
from syvrio.infra.storage.locking import VaultLock
from syvrio.infra.storage.paths import SyvrioPaths


def link_current(root, target, monkeypatch):
    link = root / "current"
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        # 无符号链接权限的 Windows 使用真实硬链接。只模拟这个入口的解析，
        # 版本检查、依赖探测以及目标是否被替换仍由原代码和文件系统验证。
        link.hardlink_to(target)
        is_symlink, resolve = Path.is_symlink, Path.resolve

        def is_current(path):
            return path == link and path.is_file() and path.samefile(target)

        monkeypatch.setattr(Path, "is_symlink", lambda path: is_current(path) or is_symlink(path))
        monkeypatch.setattr(
            Path,
            "resolve",
            lambda path, strict=False: resolve(target if is_current(path) else path, strict),
        )
    return link


def test_install_preserves_managed_environment_extras(tmp_path, monkeypatch):
    previous = tmp_path / "versions" / "previous" / "bin"
    previous.mkdir(parents=True)
    (previous / "python").touch()
    (previous / "syvrio").touch()
    link_current(tmp_path, previous / "syvrio", monkeypatch)
    monkeypatch.setattr(update, "_uv", lambda uv: "uv")
    monkeypatch.setattr(update, "_interpreter", lambda python, spec: "/usr/bin/python3")
    monkeypatch.setattr(update, "_download", lambda *args: None)
    commands = []

    def run(*args, **kwargs):
        commands.append(args)
        if args[0] == str(previous / "python"):
            return "local-embed,documents"
        return "Syvrio 1.0.0" if args[-1] == "--version" else ""

    monkeypatch.setattr(update, "_run", run)
    with httpx.Client() as client:
        executable = update._install(
            client, tmp_path, "1.0.0", {"filename": "syvrio-1.0.0-py3-none-any.whl"}
        )
    assert executable.parent.parent.is_dir()
    command = next(args for args in commands if args[:3] == ("uv", "pip", "install"))
    assert command[-1].startswith("syvrio[local-embed,documents] @ file://")
    assert command[-1].endswith("/syvrio-1.0.0-py3-none-any.whl")


def test_changed_release_never_installs_a_different_version(tmp_path, monkeypatch):
    old = tmp_path / "bin" / "syvrio"
    old.parent.mkdir()
    old.touch()
    current = link_current(tmp_path, old, monkeypatch)
    monkeypatch.setattr(update, "_run", lambda *args: "Syvrio 0.9.0")
    monkeypatch.setattr(update, "check_release", lambda *args: ("1.1.0", {}))
    monkeypatch.setattr(update, "_install", lambda *args: pytest.fail("版本已变更，不能进入安装。"))
    with pytest.raises(update.UpdateError, match="available version changed"):
        update.update(tmp_path, "stable", "demo.service", 8765, expected_version="1.0.0")
    assert current.samefile(old)


def test_update_api_requires_authenticated_confirmation(resident_backend, monkeypatch):
    _, resident = resident_backend
    started = []

    def start(version, paths):
        started.append((version, paths))
        return {"state": "running", "message": version}

    monkeypatch.setattr(jobs, "start", start)
    payload = {"confirmed": True, "version": "1.0.0"}
    with httpx.Client(base_url=resident.base, trust_env=False) as raw:
        assert raw.post("/api/update", json=payload).status_code == 403
    for host, allowed in (("127.0.0.1", True), ("0.0.0.0", False)):
        gateway = create_gateway(resident.base, host, token=resident.token)
        with TestClient(gateway, base_url="http://127.0.0.1") as browser:
            if allowed:
                assert (
                    browser.post("/api/update", json={**payload, "confirmed": False}).status_code
                    == 422
                )
                assert (
                    browser.post(
                        "/api/update", json=payload, headers={"Origin": "https://external.invalid"}
                    ).status_code
                    == 403
                )
            reply = browser.post(
                "/api/update", json=payload, headers={"X-Syvrio-Token": resident.token}
            )
            assert reply.status_code == (202 if allowed else 403)
    assert resident.request("POST", "/api/update", json=payload)["state"] == "running"
    assert len(started) == 2


def test_gateway_exposes_progress_written_by_the_worker(resident_backend, tmp_path, monkeypatch):
    _, resident = resident_backend
    monkeypatch.setattr(jobs, "_root", lambda: tmp_path)
    monkeypatch.setattr(update, "_service_state", lambda _: "active")
    jobs._save(
        tmp_path,
        "running",
        "Downloading wheel",
        update.UpdateProgress("download", "Downloading wheel", 12, 30),
    )
    gateway = create_gateway(resident.base, "127.0.0.1", token=resident.token)
    with TestClient(gateway, base_url="http://127.0.0.1") as browser:
        payload = browser.get("/api/update/status").json()
        assert payload["progress"]["downloaded"] == 12 and payload["progress"]["total"] == 30
        jobs._save(tmp_path, "failed", "Update interrupted")
        payload = browser.get("/api/update/status").json()
        assert payload["state"] == "failed" and not payload.get("progress")


def test_worker_survives_service_handover_and_uses_confirmed_version(tmp_path, monkeypatch):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    calls = []
    monkeypatch.setattr(jobs, "_root", lambda: tmp_path)
    monkeypatch.setattr(jobs, "_managed", lambda root: True)
    monkeypatch.setattr(jobs.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(update, "_service_state", lambda name: "active")
    assert jobs.start("1.0.0", paths)["state"] == "running"
    assert jobs.status()["state"] == "running"
    with pytest.raises(update.UpdateError, match="already running"):
        jobs.start("1.0.0", paths)
    (command,) = calls
    assert command[0] == "systemd-run" and f"--unit={jobs.UNIT}" in command
    assert command[-5:] == [
        "syvrio.backend.updates.worker",
        "1.0.0",
        str(tmp_path),
        str(paths.vault_dir),
        str(paths.runtime_dir),
    ]
    monkeypatch.setattr(
        update, "config_options", lambda root: ("stable", "demo.service", 8765, None, None)
    )

    def execute(root, *options, expected_version, prepare_restart, on_progress):
        assert expected_version == "1.0.0" and root == tmp_path and callable(prepare_restart)
        on_progress(update.UpdateProgress("download", "Downloading wheel", 12, 30))
        assert jobs.status()["progress"] == {
            "stage": "download",
            "message": "Downloading wheel",
            "downloaded": 12,
            "total": 30,
        }
        on_progress(update.UpdateProgress("install", "Installing dependencies"))
        assert jobs.status()["message"] == "Installing dependencies"
        assert jobs.status()["progress"]["total"] is None
        return "Verified version"

    monkeypatch.setattr(update, "update", execute)
    update_worker.run_worker("1.0.0", tmp_path, paths)
    assert jobs.status() == {"state": "completed", "message": "Verified version"}


def test_submit_failure_and_interrupted_worker_can_be_retried(tmp_path, monkeypatch):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    monkeypatch.setattr(jobs, "_root", lambda: tmp_path)
    monkeypatch.setattr(jobs, "_managed", lambda root: True)
    monkeypatch.setattr(update, "_service_state", lambda name: "inactive")

    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, "systemd-run")

    monkeypatch.setattr(jobs.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        jobs.start("1.0.0", paths)
    assert jobs.status()["state"] == "failed"
    monkeypatch.setattr(jobs.subprocess, "run", lambda *args, **kw: None)
    assert jobs.start("1.0.0", paths)["state"] == "running"
    assert "interrupted" in jobs.status()["message"]


@pytest.mark.parametrize(
    "busy,same,expected", [(True, True, "busy"), (False, False, "resident"), (False, True, None)]
)
def test_handover_only_shuts_down_verified_idle_resident(
    tmp_path, monkeypatch, busy, same, expected
):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    monkeypatch.setattr(
        update_worker, "discover", lambda paths: ("http://127.0.0.1:8760", "credential")
    )
    stopped = []

    def respond(request):
        if request.url.path == "/api/update/instance":
            return httpx.Response(200, json={"instance": "new", "busy": busy})
        assert request.headers["X-Syvrio-Token"] == "credential"
        if request.method == "POST":
            stopped.append(str(request.url))
        return httpx.Response(200, json={"instance": "new" if same else "other"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client, ExitStack() as cleanup:
        if expected:
            with pytest.raises(update.UpdateError) as error:
                update_worker.prepare_restart(client, 8765, cleanup, paths=paths)
            assert error.value.stage == expected and not stopped
        else:
            finish = update_worker.prepare_restart(client, 8765, cleanup, paths=paths)
            assert stopped == ["http://127.0.0.1:8760/api/resident/update_drain"]
            finish()
            assert stopped[-1] == "http://127.0.0.1:8760/api/resident/shutdown"


def test_drain_serializes_writes_and_recovers_after_abort_or_worker_death(
    tmp_path, monkeypatch, resident_backend
):
    _, resident = resident_backend
    monkeypatch.setattr("syvrio.backend.api.update_admission.syvrio_home", lambda: tmp_path)
    entered, finish = Event(), Event()

    def slow_start(*args):
        entered.set()
        assert finish.wait(5)
        return {"state": "running", "message": ""}

    monkeypatch.setattr(jobs, "start", slow_start)
    ticket = {"ticket": "af" * 16}
    headers = {"X-Syvrio-Token": resident.token}
    with httpx.Client(base_url=resident.base, headers=headers, trust_env=False) as client:
        with ThreadPoolExecutor() as pool:
            work = pool.submit(
                client.post, "/api/update", json={"confirmed": True, "version": "2.0.0"}
            )
            try:
                assert entered.wait(5)
                assert client.post("/api/resident/update_drain", json=ticket).status_code == 409
            finally:
                finish.set()
            assert work.result().status_code == 202
        with VaultLock(tmp_path / "update" / "update.lock"):
            assert client.post("/api/resident/update_drain", json=ticket).status_code == 200
            assert client.post("/api/sessions", json={}).status_code == 409
            assert client.get("/api/update/instance").status_code == 200
            assert client.post("/api/resident/update_resume", json=ticket).status_code == 200
            assert client.post("/api/sessions", json={}).status_code == 201
            assert client.post("/api/resident/update_drain", json=ticket).status_code == 200
        assert client.post("/api/sessions", json={}).status_code == 201


def test_gateway_without_instance_cannot_authorize_restart():
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with httpx.Client(transport=transport) as client, ExitStack() as cleanup:
        with pytest.raises(update.UpdateError, match="resident instance"):
            update_worker.prepare_restart(client, 8765, cleanup)


def test_restored_update_explains_the_failed_health_check(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "config_options", lambda root: ())

    def failed(*args, **kwargs):
        try:
            raise update.UpdateError("health", "synthetic details")
        except update.UpdateError as error:
            raise update.UpdateError("restored", "restored") from error

    monkeypatch.setattr(update, "update", failed)
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    with pytest.raises(update.UpdateError, match="restored"):
        update_worker.run_worker("0.7.1", tmp_path, paths)
    status = jobs._status(tmp_path)
    assert status["state"] == "failed"
    assert "did not become healthy" in status["message"]
    assert "previous version is running again" in status["message"]
