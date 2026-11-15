from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .locking import VaultLock

PYPI = "https://pypi.org/pypi/syvrio/json"
# Syvrio publishes three-part versions and uses rcN for the Evo channel.
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?")
# The managed unit stops itself with TimeoutStopSec=5min; out-wait it before judging a stop failed.
STOP_TIMEOUT = 360
# Warm starts may run slow initialization or memory catch-up before /api/health responds.
STARTUP_SECONDS = 120


class UpdateError(ValueError):
    """An update failure tagged with the stage that produced it."""

    def __init__(self, stage: str, detail: str):
        super().__init__(f"{stage} failed: {detail}")
        self.stage = stage


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


def _satisfies(version: tuple[int, int, int], requires_python: str | None) -> bool:
    """Evaluate the plain comparison clauses PyPI ships; exotic ones stay with uv."""
    if not requires_python:
        return True
    for clause in requires_python.split(","):
        match = re.fullmatch(
            r"(>=|<=|==|!=|>|<)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(\.\*)?", clause.strip()
        )
        if match is None:
            continue
        operator, major, minor, patch, wildcard = match.groups()
        bound = (int(major), int(minor or 0), int(patch or 0))
        if wildcard:
            specified = 1 + (minor is not None) + (patch is not None)
            equal = version[:specified] == bound[:specified]
            if (operator == "==" and not equal) or (operator == "!=" and equal):
                return False
        elif not {
            ">=": version >= bound,
            ">": version > bound,
            "<=": version <= bound,
            "<": version < bound,
            "==": version == bound,
            "!=": version != bound,
        }[operator]:
            return False
    return True


def _probe(candidate: Path) -> tuple[int, int, int] | None:
    """Run the interpreter in isolation and report its (major, minor, micro) version."""
    try:
        output = subprocess.run(
            (
                str(candidate),
                "-I",
                "-c",
                "import sys; print('{}.{}.{}'.format(*sys.version_info[:3]))",
            ),
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _interpreter(python: str | None, requires_python: str | None) -> str:
    """Pick a local interpreter for the new venv; uv must never download one."""
    if python:
        candidates = [Path(python).expanduser()]
    elif sys.prefix == sys.base_prefix:
        candidates = [Path(sys.executable)]
    else:
        # The updater runs inside the previous version's venv. Base the new environment on
        # that venv's underlying interpreter instead of reusing the old venv itself.
        version = f"{sys.version_info[0]}.{sys.version_info[1]}"
        candidates = [Path(sys.base_prefix) / "bin" / f"python{version}", Path(sys.executable)]
    attempts = []
    for candidate in candidates:
        # A symlinked venv python resolves to its base interpreter; a copied (--copies) one
        # stays inside the old venv and must never become the base of the new environment.
        candidate = candidate.resolve()
        if sys.prefix != sys.base_prefix and candidate.is_relative_to(sys.prefix):
            attempts.append(f"{candidate} is inside the running venv")
            continue
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            attempts.append(f"{candidate} is missing or not executable")
            continue
        found = _probe(candidate)
        if found is None:
            attempts.append(f"{candidate} failed to run")
        elif not _satisfies(found, requires_python):
            attempts.append(
                f"{candidate} is {'.'.join(map(str, found))}, requires {requires_python}"
            )
        else:
            return str(candidate)
    raise UpdateError(
        "interpreter",
        "no usable local Python interpreter for the new environment ("
        + "; ".join(attempts)
        + '); install Python >= 3.12 locally or set python = "..." in the update config; '
        "interpreter downloads are disabled",
    )


def _detail(error: subprocess.CalledProcessError) -> str:
    output = " ".join(((error.stderr or "") + " " + (error.stdout or "")).split())
    return output[-200:] or f"exit code {error.returncode}"


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


def _run(*args: str, timeout: int = 120, env: dict[str, str] | None = None) -> str:
    environment = {**os.environ, **env} if env else None
    return subprocess.run(
        args, check=True, timeout=timeout, capture_output=True, text=True, env=environment
    ).stdout.strip()


def _switch(link: Path, executable: Path) -> None:
    with tempfile.TemporaryDirectory(dir=link.parent) as temp:
        candidate = Path(temp) / "next"
        candidate.symlink_to(executable)
        os.replace(candidate, link)


def _healthy(client: httpx.Client, url: str, version: str, seconds: int = 20) -> bool:
    deadline = time.monotonic() + seconds
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


def _install(
    client: httpx.Client, root: Path, version: str, wheel: dict, python: str | None = None
) -> Path:
    # Interpreter selection comes before the download: a missing interpreter must fail fast.
    interpreter = _interpreter(python, wheel.get("requires_python"))
    versions = root / "versions"
    versions.mkdir(exist_ok=True)
    # A venv's entry-point shebang contains its absolute path. Keep it where it was built.
    environment = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=versions))
    try:
        with tempfile.TemporaryDirectory(dir=root) as temp:
            target = Path(temp) / wheel["filename"]
            try:
                _download(client, wheel, target)
            except (httpx.HTTPError, OSError, ValueError) as error:
                raise UpdateError("download", f"{wheel['filename']}: {error}") from error
            # An explicit local interpreter plus --no-python-downloads (and the matching env
            # var, so no uv step can ever fetch a managed CPython) keeps the install strictly
            # local: the old hard-coded 3.12 hung 120 s on 3.13-only hosts.
            no_downloads = {"UV_PYTHON_DOWNLOADS": "never"}
            try:
                _run(
                    "uv",
                    "venv",
                    "--no-python-downloads",
                    "--python",
                    interpreter,
                    str(environment),
                    env=no_downloads,
                )
            except subprocess.TimeoutExpired as error:
                raise UpdateError("venv", f"uv venv timed out after {error.timeout} s") from error
            except subprocess.CalledProcessError as error:
                raise UpdateError("venv", f"uv venv failed: {_detail(error)}") from error
            venv_python = environment / "bin" / "python"
            try:
                _run(
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(venv_python),
                    str(target),
                    timeout=300,
                    env=no_downloads,
                )
            except subprocess.TimeoutExpired as error:
                raise UpdateError(
                    "install", f"uv pip install timed out after {error.timeout} s"
                ) from error
            except subprocess.CalledProcessError as error:
                raise UpdateError("install", f"uv pip install failed: {_detail(error)}") from error
        executable = environment / "bin" / "syvrio"
        try:
            output = _run(str(executable), "--version")
            if output != f"Syvrio {version}":
                raise UpdateError(
                    "verify", f"the executable reports {output!r}, expected Syvrio {version}"
                )
            _run(
                str(venv_python),
                "-c",
                "from importlib.resources import files; "
                "assert files('syvrio.app.web').joinpath('static', 'index.html').is_file()",
            )
        except subprocess.TimeoutExpired as error:
            raise UpdateError(
                "verify",
                f"the installed environment is broken: verification timed out after {error.timeout} s",
            ) from error
        except subprocess.CalledProcessError as error:
            raise UpdateError(
                "verify", f"the installed environment is broken: {_detail(error)}"
            ) from error
        return executable
    except BaseException:
        shutil.rmtree(environment, ignore_errors=True)
        raise


def _service_state(unit: str) -> str:
    result = subprocess.run(
        ("systemctl", "--user", "is-active", unit),
        check=False,
        timeout=30,
        capture_output=True,
        text=True,
    )
    return (result.stdout or result.stderr or "unknown").strip() or "unknown"


def _stop_service(unit: str) -> None:
    """Stop the unit and return only with a confirmed stop; otherwise abort the update."""
    try:
        _run("systemctl", "--user", "stop", unit, timeout=STOP_TIMEOUT)
        return
    except subprocess.SubprocessError:
        pass
    # The stop job may still be concluding (the unit stops itself with a 5 min timeout);
    # wait for it to settle instead of switching under a unit that is going away anyway.
    deadline = time.monotonic() + 60
    while True:
        try:
            state = _service_state(unit)
        except (OSError, subprocess.SubprocessError) as error:
            raise UpdateError(
                "stop", f"could not confirm the state of {unit}: {error}; nothing was changed"
            ) from error
        if state in {"inactive", "failed"}:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    # Still not confirmed stopped: the unit may go down on its own later, e.g. while
    # deactivating. Keep the old link untouched and leave the decision to the operator.
    raise UpdateError(
        "stop",
        f"{unit} still reports {state!r} after a failed stop command, so the stop is "
        f"unconfirmed and the update was aborted; the unit may stop on its own later. "
        f"Check systemctl --user is-active {unit} and start it again if needed.",
    )


def update(root: Path, channel: str, unit: str, port: int, python: str | None = None) -> str:
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
        # Built and verified while the old service keeps serving.
        new = _install(pypi, root, version, wheel, python)
        new_environment = new.parent.parent

        def discard_install() -> None:
            # The fresh environment is ours alone. Drop it only once current cannot point
            # into it, so the previous version, current and every other environment stay.
            try:
                if not link.resolve(strict=True).is_relative_to(new_environment):
                    shutil.rmtree(new_environment, ignore_errors=True)
            except OSError:
                pass

        service = ("systemctl", "--user")
        url = f"http://127.0.0.1:{port}/api/health"
        with httpx.Client(trust_env=False) as local:
            if not _healthy(local, url, installed):
                discard_install()
                raise ValueError("The existing Web service is not healthy for this installation.")
            try:
                _stop_service(unit)
            except BaseException:
                discard_install()
                raise
            try:
                try:
                    _switch(link, new)
                except OSError as error:
                    raise UpdateError("switch", f"could not repoint {link}: {error}") from error
                try:
                    _run(*service, "start", unit)
                except subprocess.TimeoutExpired as error:
                    raise UpdateError(
                        "start", f"systemctl start timed out after {error.timeout} s"
                    ) from error
                except subprocess.CalledProcessError as error:
                    raise UpdateError(
                        "start", f"systemctl start failed: {_detail(error)}"
                    ) from error
                if not _healthy(local, url, version, seconds=STARTUP_SECONDS):
                    raise UpdateError(
                        "health",
                        f"Syvrio {version} did not pass the Web health check within {STARTUP_SECONDS} s",
                    )
            except (UpdateError, OSError, KeyboardInterrupt) as error:
                try:
                    # Stop the failed version before repointing its executable.
                    _stop_service(unit)
                    _switch(link, previous)
                    _run(*service, "start", unit)
                    if not _healthy(local, url, installed):
                        raise UpdateError(
                            "health", f"the restored Syvrio {installed} failed its Web health check"
                        ) from None
                except (
                    UpdateError,
                    OSError,
                    subprocess.SubprocessError,
                    KeyboardInterrupt,
                ) as rollback:
                    raise UpdateError(
                        "rollback",
                        f"the update failed ({error}) and restoring Syvrio {installed} also failed "
                        f"({rollback}); repoint {link} at {previous} and start {unit} manually",
                    ) from error
                # Rollback succeeded: current is back on the old version, so the fresh
                # environment is unreachable and safe to remove.
                discard_install()
                raise ValueError(f"Update failed; restored Syvrio {installed}.") from error
    return f"Updated Syvrio {installed} → {version} ({channel})."
