from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

from platformdirs import user_data_path

from ..errors import SyvrioError
from .files import atomic_write

KEYRING_SERVICE = "syvrio"


class CredentialStoreError(SyvrioError):
    """The selected credential storage cannot be used safely."""


def _credential_path(env_name: str) -> Path | None:
    if sys.platform != "linux":
        return None
    override = os.environ.get("SYVRIO_CREDENTIALS_DIR")
    directory = Path(override) if override else user_data_path(KEYRING_SERVICE) / "credentials"
    if not directory.is_absolute():
        raise CredentialStoreError("SYVRIO_CREDENTIALS_DIR must be an absolute path.")
    digest = hashlib.sha256(env_name.encode("utf-8")).hexdigest()
    return directory / f"syvrio-{digest}.cred"


def _check_directory(directory: Path) -> None:
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CredentialStoreError("The Syvrio credential directory must be private to its owner.")


def _has_file(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    _check_directory(path.parent)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CredentialStoreError("The Syvrio credential file must be private to its owner.")
    return True


def _systemd_creds(action: str, path: Path, payload: bytes) -> bytes:
    args = ["systemd-creds", action, "--user", f"--name={path.stem}"]
    if action == "encrypt":
        args.append("--with-key=host")
    result = subprocess.run(
        [*args, "-", "-"],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        timeout=10,
        check=True,
    )
    if not result.stdout:
        raise CredentialStoreError("The system credential service returned an empty result.")
    return result.stdout


def get_password(env_name: str) -> str | None:
    path = _credential_path(env_name)
    if path is not None and _has_file(path):
        ciphertext = path.read_bytes()
        return _systemd_creds("decrypt", path, ciphertext).decode("utf-8") if ciphertext else None

    import keyring

    return keyring.get_password(KEYRING_SERVICE, env_name) or None


def set_password(env_name: str, password: str) -> None:
    path = _credential_path(env_name)
    existing = _has_file(path)
    if not existing:
        import keyring

        try:
            keyring.set_password(KEYRING_SERVICE, env_name, password)
            return
        except Exception as error:
            from keyring.errors import NoKeyringError

            if path is None or not isinstance(error, NoKeyringError):
                raise
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_directory(path.parent)

    assert path is not None
    ciphertext = _systemd_creds("encrypt", path, password.encode("utf-8"))
    atomic_write(path, ciphertext)
    if not existing:
        from .diagnostics import record_once

        record_once("degraded.keyring", "Using user-scoped systemd credentials without a keyring.")


def delete_password(env_name: str) -> None:
    path = _credential_path(env_name)
    if path is not None and _has_file(path):
        # Keep this name on the same backend even if an older native keyring returns.
        atomic_write(path, b"")
        return

    import keyring

    keyring.delete_password(KEYRING_SERVICE, env_name)
