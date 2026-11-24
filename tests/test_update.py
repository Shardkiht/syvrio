from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest

from syvrio.infra import update

pytestmark = pytest.mark.cli


@pytest.fixture(autouse=True)
def stub_uv_lookup(monkeypatch):
    """这些用例不依赖宿主装没装 uv；要测「找不到 uv」的用例在用例内自行覆盖。"""
    monkeypatch.setattr(update.shutil, "which", lambda name: f"/usr/bin/{name}")


def wheel(version: str, requires_python: str | None = None) -> dict:
    return {"filename": f"syvrio-{version}-py3-none-any.whl", "requires_python": requires_python}


def fake_interpreter(directory: Path, version: str = "3.13.5", name: str = "python3.13") -> Path:
    """用于测试的可执行文件只对版本查询产生可观察的行为。"""
    script = directory / name
    script.write_text(f"#!/bin/sh\nprintf '{version}'\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def patch_release_transport(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"releases": {"0.6.0": [wheel("0.6.0")]}})
    )
    factory = httpx.Client
    monkeypatch.setattr(update.httpx, "Client", lambda **kwargs: factory(transport=transport))


class FakeClock:
    """用于等待循环的确定性时钟，休眠只推进模拟时间。"""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def use_fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(update.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(update.time, "sleep", clock.sleep)
    return clock


def patch_run_with_failing_stop(monkeypatch, old, events) -> None:
    """模拟 _run，版本查询成功，所有停止命令均报错。"""

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        if args[2:] == ("stop", "syvrio-web.service"):
            events.append("stop")
            raise subprocess.CalledProcessError(1, args)
        events.append(args[2])
        return ""

    monkeypatch.setattr(update, "_run", run)


def make_versions(tmp_path):
    """按更新器实际构建的布局创建新旧环境目录。"""
    old_env = tmp_path / "old-env"
    (old_env / "bin").mkdir(parents=True)
    old = old_env / "bin" / "syvrio"
    old.touch()
    new_env = tmp_path / "versions" / "0.6.0-abc"
    (new_env / "bin").mkdir(parents=True)
    new = new_env / "bin" / "syvrio"
    new.touch()
    return old_env, old, new_env, new


def test_update_channels_ignore_yanked_and_source_releases():
    releases = {v: [wheel(v)] for v in ("0.5.2rc1", "0.5.2rc10", "0.5.2", "0.5.3")}
    releases["0.5.3"][0]["yanked"] = True
    releases["0.6.0"] = [{"filename": "syvrio-0.6.0.tar.gz"}]
    assert update.select_release(releases, "0.5.2rc1", "stable")[0] == "0.5.2"
    assert update.select_release(releases, "0.5.2rc1", "evo")[0] == "0.5.2"
    assert update.select_release(releases, "0.5.2", "evo") is None


def test_download_rejects_corrupt_wheel(tmp_path):
    asset = {
        "url": "https://files.pythonhosted.org/packages/test.whl",
        "digests": {"sha256": hashlib.sha256(b"expected").hexdigest()},
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"corrupt"))
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError, match="checksum"):
        update._download(client, asset, tmp_path / "test.whl")


@pytest.mark.parametrize("size_source", ["metadata", "header", "unknown", "mismatch"])
def test_download_reports_actual_bytes_before_verification(tmp_path, size_source):
    data = b"synthetic wheel"
    asset = {
        "url": "https://files.pythonhosted.org/packages/test.whl",
        "digests": {"sha256": hashlib.sha256(data).hexdigest()},
    }
    if size_source == "metadata":
        asset["size"] = len(data)
    elif size_source == "mismatch":
        asset["size"] = len(data) + 3
    headers = {"Content-Length": str(len(data))} if size_source == "header" else {}
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers=headers, stream=httpx.ByteStream(data))
    )
    events = []
    with httpx.Client(transport=transport) as client:
        update._download(client, asset, tmp_path / "test.whl", events.append)
    assert events[0].downloaded == 0
    assert events[-2].downloaded == len(data)
    assert events[-2].total == (len(data) if size_source in {"metadata", "header"} else None)
    assert events[-1].stage == "checksum"


# --- 解释器选择（任务一）：只用本地解释器，绝不触发 uv 联网下载。 ---


def test_interpreter_prefers_base_of_running_venv(tmp_path, monkeypatch):
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    real = fake_interpreter(base / "bin")
    monkeypatch.setattr(update.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(update.sys, "base_prefix", str(base))
    monkeypatch.setattr(update.sys, "version_info", (3, 13, 5, "final", 0))
    assert update._interpreter(None, ">=3.12") == str(real)


def test_interpreter_uses_explicitly_configured_python(tmp_path):
    configured = fake_interpreter(tmp_path, name="python3.12")
    assert update._interpreter(str(configured), ">=3.12") == str(configured)


def test_interpreter_config_never_reuses_the_running_venv(tmp_path, monkeypatch):
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    real = fake_interpreter(base / "bin")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(update.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(update.sys, "base_prefix", str(tmp_path / "base"))
    # --copies 的 venv 内解释器：解析后仍在旧 venv 内，不得作为新基础环境。
    copied = fake_interpreter(venv_bin)
    with pytest.raises(update.UpdateError) as raised:
        update._interpreter(str(copied), ">=3.12")
    assert "inside the running venv" in str(raised.value)
    # 符号链接的 venv 内解释器：解析到真实基础解释器，可用。
    link = venv_bin / "python"
    link.symlink_to(real)
    assert update._interpreter(str(link), ">=3.12") == str(real)


def test_interpreter_missing_fails_fast_without_subprocess_work(tmp_path, monkeypatch):
    monkeypatch.setattr(update.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(update.sys, "base_prefix", str(tmp_path / "base"))
    # venv 内解释器指向 venv 自身（parents 含 prefix），不得作为新基础环境。
    monkeypatch.setattr(update.sys, "executable", str(tmp_path / "venv" / "bin" / "python"))
    calls = []
    monkeypatch.setattr(update, "_run", lambda *args, **kwargs: calls.append(args) or "")
    with pytest.raises(update.UpdateError) as raised:
        update._interpreter(None, ">=3.12")
    assert raised.value.stage == "interpreter"
    assert "python = " in str(raised.value)
    assert calls == []


def test_interpreter_rejects_incompatible_wheel(tmp_path):
    old = fake_interpreter(tmp_path, version="3.13.5", name="python3.13")
    with pytest.raises(update.UpdateError) as raised:
        update._interpreter(str(old), ">=3.14")
    assert raised.value.stage == "interpreter"
    assert "3.13.5" in str(raised.value) and "3.14" in str(raised.value)


def test_satisfies_handles_common_requires_python_ranges():
    assert update._satisfies((3, 13, 5), ">=3.12")
    assert not update._satisfies((3, 13, 5), ">=3.14")
    assert update._satisfies((3, 12, 0), "==3.12.*")
    assert not update._satisfies((3, 13, 0), "==3.12.*")
    assert not update._satisfies((3, 13, 0), "!=3.13")
    assert update._satisfies((3, 12, 1), ">=3.12,<3.13")
    assert not update._satisfies((3, 13, 0), ">=3.12,<3.13")
    assert update._satisfies((3, 13, 5), "~=3.12")
    assert not update._satisfies((3, 13, 5), "~=3.12.2")
    with pytest.raises(update.UpdateError, match="Invalid Requires-Python"):
        update._satisfies((3, 13, 5), ">=wrong")
    # 边界与空格：不得错误拒绝合法版本。
    assert update._satisfies((3, 12, 1), ">3.12")
    assert not update._satisfies((3, 12, 0), ">3.12")
    assert not update._satisfies((3, 12, 5), "!=3.12.*")
    assert update._satisfies((3, 99, 0), ">=3.12")
    assert update._satisfies((3, 13, 5), ">= 3.12")


def test_install_builds_venv_from_explicit_local_interpreter(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    interpreter = fake_interpreter(local)  # 机器上只有这一个 3.13，没有 3.12
    monkeypatch.setattr(update, "_interpreter", lambda python, spec: str(interpreter))
    monkeypatch.setattr(update, "_download", lambda client, wheel, target, on_progress: None)
    calls: list[tuple[tuple, dict]] = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return "Syvrio 0.5.4" if args[-1] == "--version" else ""

    monkeypatch.setattr(update, "_run", run)
    executable = update._install(httpx.Client(), tmp_path, "0.5.4", wheel("0.5.4", ">=3.12"), None)
    venv_args, venv_kwargs = next(item for item in calls if item[0][:2] == ("uv", "venv"))
    # 实际传给 uv 的参数：显式本地解释器路径 + 禁止自动下载（flag 与环境变量双保险）。
    assert venv_args[:4] == ("uv", "venv", "--no-python-downloads", "--python")
    assert venv_args[4] == str(interpreter)
    assert Path(venv_args[5]).is_absolute()
    assert venv_kwargs["env"] == {"UV_PYTHON_DOWNLOADS": "never"}
    pip_args, pip_kwargs = next(item for item in calls if item[0][:3] == ("uv", "pip", "install"))
    assert pip_args[3:5] == ("--python", str(Path(venv_args[5]) / "bin" / "python"))
    assert pip_kwargs["env"] == {"UV_PYTHON_DOWNLOADS": "never"}
    assert executable == Path(venv_args[5]) / "bin" / "syvrio"


def test_install_venv_failure_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "_interpreter", lambda python, spec: "/usr/bin/python3.13")
    monkeypatch.setattr(update, "_download", lambda client, wheel, target, on_progress: None)

    def run(*args, **kwargs):
        if args[:2] == ("uv", "venv"):
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 120))
        return ""

    monkeypatch.setattr(update, "_run", run)
    with pytest.raises(update.UpdateError) as raised:
        update._install(httpx.Client(), tmp_path, "0.5.4", wheel("0.5.4"))
    assert raised.value.stage == "venv"
    assert "timed out" in str(raised.value)
    assert list((tmp_path / "versions").iterdir()) == []


def test_install_dependency_failure_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "_interpreter", lambda python, spec: "/usr/bin/python3.13")
    monkeypatch.setattr(update, "_download", lambda client, wheel, target, on_progress: None)

    def run(*args, **kwargs):
        if args[:2] == ("uv", "venv"):
            return ""
        if args[:3] == ("uv", "pip", "install"):
            raise subprocess.CalledProcessError(1, args, stderr="No solution found")
        return ""

    monkeypatch.setattr(update, "_run", run)
    with pytest.raises(update.UpdateError) as raised:
        update._install(httpx.Client(), tmp_path, "0.5.4", wheel("0.5.4"))
    assert raised.value.stage == "install"
    assert "No solution found" in str(raised.value)
    assert list((tmp_path / "versions").iterdir()) == []


def test_install_verify_timeout_cleans_up(tmp_path, monkeypatch):
    # verify 阶段的 TimeoutExpired 不得落进 CalledProcessError 分支（否则丢阶段与超时信息）。
    monkeypatch.setattr(update, "_interpreter", lambda python, spec: "/usr/bin/python3.13")
    monkeypatch.setattr(update, "_download", lambda client, wheel, target, on_progress: None)

    def run(*args, **kwargs):
        if args[:2] == ("uv", "venv") or args[:3] == ("uv", "pip", "install"):
            return ""
        if args[-1] == "--version":
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 120))
        return ""

    monkeypatch.setattr(update, "_run", run)
    with pytest.raises(update.UpdateError) as raised:
        update._install(httpx.Client(), tmp_path, "0.5.4", wheel("0.5.4"))
    assert raised.value.stage == "verify"
    assert "timed out after 120 s" in str(raised.value)
    assert list((tmp_path / "versions").iterdir()) == []


# --- update() 编排（任务二）：stop/start/回滚全程不得破坏运行中的旧服务。 ---


@pytest.mark.parametrize("healthy", [True, False])
def test_update_switches_or_restores_service(tmp_path, monkeypatch, healthy):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.touch()
    new.touch()
    link = tmp_path / "current"
    link.symlink_to(old)
    events = []
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    monkeypatch.setattr(
        update, "_healthy", lambda c, u, version, seconds=20: version == "0.3.4" or healthy
    )

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        events.append((args[2], link.resolve()))
        return ""

    monkeypatch.setattr(update, "_run", run)
    stages = []

    def observe(event):
        stages.append(event.stage)
        if event.stage == "switch":
            raise BrokenPipeError("Display disconnected")

    if healthy:
        assert "0.6.0" in update.update(
            tmp_path, "evo", "syvrio-web.service", 8765, on_progress=observe
        )
        assert link.resolve() == new
        assert events == [("stop", old), ("start", new)]
    else:
        with pytest.raises(ValueError, match="restored"):
            update.update(tmp_path, "evo", "syvrio-web.service", 8765, on_progress=observe)
        assert link.resolve() == old
        assert events == [("stop", old), ("start", new), ("stop", new), ("start", old)]
    expected = ["preflight", "check", "pre_restart", "stop", "switch", "start", "health"]
    assert stages == (expected if healthy else [*expected, "rollback"])


def test_update_forwards_python_and_uv_to_install(tmp_path, monkeypatch):
    """CLI 与 _install 各自正确还不够：编排层必须原样传递两个显式路径。"""
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.touch()
    new.touch()
    (tmp_path / "current").symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_healthy", lambda *args, **kwargs: True)
    seen: dict[str, str | None] = {}

    def install(client, root, version, wheel, python=None, uv=None, on_progress=None):
        seen.update(python=python, uv=uv)
        return new

    monkeypatch.setattr(update, "_install", install)
    monkeypatch.setattr(
        update,
        "_run",
        lambda *args, **kwargs: "Syvrio 0.3.4" if args == (str(old), "--version") else "",
    )

    result = update.update(
        tmp_path,
        "evo",
        "syvrio-web.service",
        8765,
        "/usr/bin/python3.13",
        "/opt/uv/bin/uv",
    )

    assert "Updated Syvrio 0.3.4" in result
    assert seen == {"python": "/usr/bin/python3.13", "uv": "/opt/uv/bin/uv"}


def test_update_stop_failure_leaves_old_service_running(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.touch()
    new.touch()
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: True)
    monkeypatch.setattr(update, "_service_state", lambda unit: "active")
    use_fake_clock(monkeypatch)
    events = []
    patch_run_with_failing_stop(monkeypatch, old, events)
    with pytest.raises(update.UpdateError) as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    assert raised.value.stage == "stop"
    assert "unconfirmed" in str(raised.value)
    assert link.resolve() == old
    assert events == ["stop"]


def test_update_waits_for_settling_stop(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.touch()
    new.touch()
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: True)
    states = iter(["deactivating", "inactive"])  # 多一次调用会让测试失败
    monkeypatch.setattr(update, "_service_state", lambda unit: next(states))
    monkeypatch.setattr(update.time, "sleep", lambda seconds: None)
    events = []

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        if args[2:] == ("stop", "syvrio-web.service"):
            events.append("stop")
            raise subprocess.CalledProcessError(1, args)
        events.append((args[2], link.resolve()))
        return ""

    monkeypatch.setattr(update, "_run", run)
    assert "0.6.0" in update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    assert link.resolve() == new
    assert events == ["stop", ("start", new)]


def test_update_stop_deactivating_timeout_aborts(tmp_path, monkeypatch):
    # 停止命令失败后单元一直停在 deactivating：不得当作停止成功，链接不得切换。
    old_env, old, new_env, new = make_versions(tmp_path)
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: True)
    monkeypatch.setattr(update, "_service_state", lambda unit: "deactivating")
    use_fake_clock(monkeypatch)
    events = []
    patch_run_with_failing_stop(monkeypatch, old, events)
    with pytest.raises(update.UpdateError) as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    assert raised.value.stage == "stop"
    assert "deactivating" in str(raised.value) and "unconfirmed" in str(raised.value)
    assert "is-active" in str(raised.value)  # 人工处理指引
    assert link.resolve() == old
    assert events == ["stop"]
    assert not new_env.exists() and old_env.exists()  # 失败安装已清理，旧版本保留


def test_update_stop_unknown_state_aborts(tmp_path, monkeypatch):
    old_env, old, new_env, new = make_versions(tmp_path)
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: True)
    monkeypatch.setattr(update, "_service_state", lambda unit: "unknown")
    use_fake_clock(monkeypatch)
    events = []
    patch_run_with_failing_stop(monkeypatch, old, events)
    with pytest.raises(update.UpdateError) as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    assert raised.value.stage == "stop"
    assert "'unknown'" in str(raised.value)
    assert link.resolve() == old
    assert events == ["stop"]
    assert not new_env.exists() and old_env.exists()


def test_update_stop_state_query_failure_aborts(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.touch()
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: tmp_path / "new")
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: True)

    def broken_state(unit):
        raise OSError("systemctl unavailable")

    monkeypatch.setattr(update, "_service_state", broken_state)
    events = []
    patch_run_with_failing_stop(monkeypatch, old, events)
    with pytest.raises(update.UpdateError) as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    assert raised.value.stage == "stop"
    assert "could not confirm" in str(raised.value)
    assert link.resolve() == old
    assert events == ["stop"]


def test_update_rollback_failure_reports_both_errors(tmp_path, monkeypatch):
    old_env, old, new_env, new = make_versions(tmp_path)
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    # 旧版（0.3.4）预检通过；新版（0.6.0）健康检查失败，触发回滚。
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: version == "0.3.4")

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        if args[2] == "start" and link.resolve() == old:
            raise subprocess.CalledProcessError(1, args, stderr="unit is masked")
        return ""

    monkeypatch.setattr(update, "_run", run)
    with pytest.raises(update.UpdateError) as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    assert raised.value.stage == "rollback"
    assert "manually" in str(raised.value)
    assert link.resolve() == old
    # 回滚失败：新环境保留，供人工判断后处理；旧版本目录不动。
    assert new_env.exists() and old_env.exists()


def test_update_precheck_failure_discards_failed_install(tmp_path, monkeypatch):
    old_env, old, new_env, new = make_versions(tmp_path)
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: False)

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        raise AssertionError(f"unexpected subprocess call during pre-check failure: {args}")

    monkeypatch.setattr(update, "_run", run)
    with pytest.raises(ValueError, match="not healthy"):
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    # 预检失败：current 未动过，新环境安全清理；旧版本目录与链接保留。
    assert not new_env.exists() and old_env.exists()
    assert link.resolve() == old


def test_update_rollback_success_discards_failed_install(tmp_path, monkeypatch):
    old_env, old, new_env, new = make_versions(tmp_path)
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    health_checks = []

    def healthy(client, url, version, seconds=20):
        health_checks.append((version, seconds))
        return version == "0.3.4"

    monkeypatch.setattr(update, "_healthy", healthy)

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        return ""

    monkeypatch.setattr(update, "_run", run)
    with pytest.raises(ValueError, match="restored"):
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    # 回滚成功：current 回到旧版，新环境安全清理；旧版本目录保留。
    assert not new_env.exists() and old_env.exists()
    assert link.resolve() == old
    assert health_checks == [
        ("0.3.4", 20),
        ("0.6.0", update.STARTUP_SECONDS),
        ("0.3.4", update.STARTUP_SECONDS),
    ]


def test_update_install_failure_keeps_running_service(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.touch()
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_healthy", lambda c, u, version, seconds=20: True)

    def failing_install(*args):
        raise update.UpdateError("venv", "uv venv failed: boom")

    monkeypatch.setattr(update, "_install", failing_install)
    events = []

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        events.append(args[2])
        return ""

    monkeypatch.setattr(update, "_run", run)
    with pytest.raises(update.UpdateError) as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)
    assert raised.value.stage == "venv"
    assert events == []
    assert link.resolve() == old


# --- 健康检查（任务二）：慢启动不得误判，超时必须返回失败。 ---


def test_healthy_tolerates_slow_startup():
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        if len(attempts) < 3:
            raise httpx.ConnectError("not listening yet", request=request)
        return httpx.Response(200, json={"status": "ok", "version": "0.5.4", "static_ready": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert update._healthy(client, "http://127.0.0.1:8765/api/health", "0.5.4", seconds=10)
    assert len(attempts) == 3


def test_healthy_fails_after_deadline(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        return httpx.Response(503, json={"status": "starting"})

    monkeypatch.setattr(update.time, "sleep", lambda seconds: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert not update._healthy(client, "http://127.0.0.1:8765/api/health", "0.5.4", seconds=1)
    assert len(attempts) > 1


# 检验首次切换可执行文件失败时的回滚。


def _switch_failure_setup(tmp_path, monkeypatch):
    old_env, old, new_env, new = make_versions(tmp_path)
    link = tmp_path / "current"
    link.symlink_to(old)
    patch_release_transport(monkeypatch)
    monkeypatch.setattr(update, "_install", lambda *args: new)
    health_checks = []

    def healthy(client, url, version, seconds=20):
        health_checks.append((version, seconds))
        return version == "0.3.4"

    monkeypatch.setattr(update, "_healthy", healthy)
    events = []

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        events.append((args[2], link.resolve(strict=False)))
        return ""

    monkeypatch.setattr(update, "_run", run)
    return old_env, old, new_env, new, link, events, health_checks


def test_update_switch_error_restores_old_without_second_switch(tmp_path, monkeypatch):
    old_env, old, new_env, new, link, events, health = _switch_failure_setup(tmp_path, monkeypatch)
    switches = []

    def switch(path, target):
        switches.append(target)
        raise OSError("read-only filesystem")

    monkeypatch.setattr(update, "_switch", switch)
    with pytest.raises(ValueError, match="restored"):
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)

    assert switches == [new]
    assert link.resolve() == old
    assert events == [("stop", old), ("start", old)]
    assert health == [("0.3.4", 20), ("0.3.4", update.STARTUP_SECONDS)]
    assert not new_env.exists() and old_env.exists()


def test_update_switch_partial_change_and_failed_rollback(tmp_path, monkeypatch):
    old_env, old, new_env, new, link, events, health = _switch_failure_setup(tmp_path, monkeypatch)
    switches = []

    def switch(path, target):
        switches.append(target)
        if len(switches) == 1:
            path.unlink()
            path.symlink_to(new)
        raise OSError("filesystem error")

    monkeypatch.setattr(update, "_switch", switch)
    with pytest.raises(update.UpdateError, match="restoring") as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)

    assert raised.value.stage == "rollback"
    assert switches == [new, old]
    assert link.resolve() == new
    assert events == [("stop", old), ("stop", new)]
    assert new_env.exists() and old_env.exists()


@pytest.mark.parametrize("link_kind", ["unknown", "dangling"])
def test_update_switch_failure_with_unexpected_link_aborts(tmp_path, monkeypatch, link_kind):
    old_env, old, new_env, new, link, events, health = _switch_failure_setup(tmp_path, monkeypatch)
    other = tmp_path / "other"
    if link_kind == "unknown":
        other.touch()
    switches = []

    def switch(path, target):
        switches.append(target)
        path.unlink()
        path.symlink_to(other)
        raise OSError("switch interrupted")

    monkeypatch.setattr(update, "_switch", switch)
    with pytest.raises(update.UpdateError) as raised:
        update.update(tmp_path, "evo", "syvrio-web.service", 8765)

    assert raised.value.stage == "rollback"
    expected = "unexpected executable" if link_kind == "unknown" else "cannot resolve"
    assert expected in str(raised.value)
    assert switches == [new]
    assert not any(event[0] == "start" for event in events)
    assert link.resolve(strict=False) == other
    assert new_env.exists() and old_env.exists()


def test_install_requires_uv_before_downloading(tmp_path, monkeypatch):
    """uv 不在 PATH 上时必须立刻失败，且不能开始建目录或下载。"""
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    calls: list[tuple] = []

    def run(*args, **kwargs):
        calls.append(args)
        return ""

    monkeypatch.setattr(update, "_run", run)
    monkeypatch.setattr(update, "_download", lambda *args, **kwargs: None)
    with pytest.raises(update.UpdateError) as raised:
        update._install(httpx.Client(), tmp_path, "0.5.4", wheel("0.5.4"))

    assert raised.value.stage == "uv"
    # 报错要能直接指导操作：说明 PATH 与配置键两种解法。
    assert "PATH" in str(raised.value) and "uv = " in str(raised.value)
    assert calls == []
    assert not (tmp_path / "versions").exists()


def test_install_uses_configured_uv_path(tmp_path, monkeypatch):
    """配置里给出绝对路径时，两个 uv 步骤都用它，不依赖调用者 PATH。"""
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    local = tmp_path / "local"
    local.mkdir()
    uv = local / "uv"
    uv.write_text("", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setattr(update, "_interpreter", lambda python, spec: "/usr/bin/python3.13")
    monkeypatch.setattr(update, "_download", lambda client, wheel, target, on_progress: None)
    calls: list[tuple] = []

    def run(*args, **kwargs):
        calls.append(args)
        return "Syvrio 0.5.4" if args[-1] == "--version" else ""

    monkeypatch.setattr(update, "_run", run)
    update._install(httpx.Client(), tmp_path, "0.5.4", wheel("0.5.4"), None, str(uv))

    assert calls[0][:2] == (str(uv), "venv")
    assert calls[1][:3] == (str(uv), "pip", "install")


def test_install_reports_unusable_configured_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    with pytest.raises(update.UpdateError) as raised:
        update._install(
            httpx.Client(), tmp_path, "0.5.4", wheel("0.5.4"), None, str(tmp_path / "absent")
        )

    assert raised.value.stage == "uv"
    assert "missing or not executable" in str(raised.value)
    assert not (tmp_path / "versions").exists()
