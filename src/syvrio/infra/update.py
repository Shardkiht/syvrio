from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .locking import VaultLock

PYPI = "https://pypi.org/pypi/syvrio/json"
# Syvrio publishes three-part versions and uses rcN for the Evo channel.
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?")


def select_release(releases: dict, current: str, channel: str) -> tuple[str, dict] | None:
    def key(version: str) -> tuple[int, ...] | None:
        match = VERSION.fullmatch(version)
        if match is None:
            return None
        major, minor, patch, rc = match.groups()
        return int(major), int(minor), int(patch), int(rc) if rc is not None else -1

    installed = key(current)
    if installed is None:
        raise ValueError(f"Unsupported installed version: {current}")
    if channel not in {"stable", "evo"}:
        raise ValueError("Update channel must be stable or evo.")
    candidates = []
    for version, files in releases.items():
        order = key(version)
        if order is None or (order[-1] >= 0) != (channel == "evo"):
            continue
        if order[:3] < installed[:3] or (
            order[:3] == installed[:3]
            and (installed[-1] == -1 or (order[-1] >= 0 and order[-1] <= installed[-1]))
        ):
            continue
        filename = f"syvrio-{version}-py3-none-any.whl"
        for file in files:
            if file.get("filename") == filename and not file.get("yanked", False):
                candidates.append((order, version, file))
    if not candidates:
        return None
    _, version, wheel = max(candidates, key=lambda item: item[0])
    return version, wheel


def _download(client: httpx.Client, wheel: dict, target: Path) -> None:
    url = urlsplit(wheel["url"])
    digest = wheel["digests"]["sha256"]
    if url.scheme != "https" or url.netloc != "files.pythonhosted.org":
        raise ValueError("Unexpected PyPI wheel host.")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("PyPI wheel has no valid SHA-256 digest.")
    checksum = hashlib.sha256()
    with client.stream("GET", url.geturl()) as response, target.open("wb") as output:
        response.raise_for_status()
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 50 * 1024 * 1024:
                raise ValueError("PyPI wheel exceeds the download limit.")
            checksum.update(chunk)
            output.write(chunk)
    if checksum.hexdigest() != digest:
        raise ValueError("PyPI wheel checksum mismatch.")


def _run(*args: str, timeout: int = 120) -> str:
    return subprocess.run(
        args, check=True, timeout=timeout, capture_output=True, text=True
    ).stdout.strip()


def _switch(link: Path, executable: Path) -> None:
    with tempfile.TemporaryDirectory(dir=link.parent) as temp:
        candidate = Path(temp) / "next"
        candidate.symlink_to(executable)
        os.replace(candidate, link)


def _healthy(client: httpx.Client, url: str, version: str) -> bool:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            response = client.get(url, timeout=2)
            data = response.json()
            if (
                response.is_success
                and data.get("status") == "ok"
                and data.get("version") == version
                and data.get("static_ready") is True
            ):
                return True
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        time.sleep(0.5)
    return False


def _install(client: httpx.Client, root: Path, version: str, wheel: dict) -> Path:
    versions = root / "versions"
    versions.mkdir(exist_ok=True)
    # A venv's entry-point shebang contains its absolute path. Keep it where it was built.
    environment = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=versions))
    try:
        with tempfile.TemporaryDirectory(dir=root) as temp:
            target = Path(temp) / wheel["filename"]
            _download(client, wheel, target)
            _run("uv", "venv", "--python", "3.12", str(environment))
            python = environment / "bin" / "python"
            _run("uv", "pip", "install", "--python", str(python), str(target), timeout=300)
        executable = environment / "bin" / "syvrio"
        if _run(str(executable), "--version") != f"Syvrio {version}":
            raise ValueError("The installed executable reports an unexpected version.")
        _run(
            str(python),
            "-c",
            "from importlib.resources import files; "
            "assert files('syvrio.app.web').joinpath('static', 'index.html').is_file()",
        )
        return executable
    except BaseException:
        shutil.rmtree(environment, ignore_errors=True)
        raise


def update(root: Path, channel: str, unit: str, port: int) -> str:
    """Update the executable behind the configured user systemd service."""
    link = root / "current"
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit) or unit.startswith("-"):
        raise ValueError("Update requires an explicit systemd user service name.")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid Web health port.")
    if not link.is_symlink():
        raise ValueError(f"Set up the managed Web service and executable symlink at {link} first.")
    with VaultLock(root / "update.lock"), httpx.Client(timeout=20, follow_redirects=False) as pypi:
        previous = link.resolve(strict=True)
        output = _run(str(previous), "--version")
        if not output.startswith("Syvrio "):
            raise ValueError("The current executable is not a Syvrio installation.")
        installed = output.removeprefix("Syvrio ")
        response = pypi.get(PYPI)
        response.raise_for_status()
        release = select_release(response.json()["releases"], installed, channel)
        if release is None:
            return f"Syvrio {installed} is up to date ({channel})."
        version, wheel = release
        new = _install(pypi, root, version, wheel)
        service = ("systemctl", "--user")
        url = f"http://127.0.0.1:{port}/api/health"
        with httpx.Client(trust_env=False) as local:
            if not _healthy(local, url, installed):
                raise ValueError("The existing Web service is not healthy for this installation.")
            _run(*service, "stop", unit)
            try:
                _switch(link, new)
                _run(*service, "start", unit)
                if not _healthy(local, url, version):
                    raise ValueError(f"Syvrio {version} failed its Web health check.")
            except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
                # Stop the failed version before repointing its executable.
                _run(*service, "stop", unit)
                _switch(link, previous)
                _run(*service, "start", unit)
                if not _healthy(local, url, installed):
                    raise ValueError(
                        f"Rollback to Syvrio {installed} failed its health check."
                    ) from None
                raise ValueError(f"Update failed; restored Syvrio {installed}.") from None
    return f"Updated Syvrio {installed} → {version} ({channel})."
