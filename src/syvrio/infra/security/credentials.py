from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from platformdirs import user_data_path

from ..errors import SyvrioError
from ..storage.files import atomic_write

KEYRING_SERVICE = "syvrio"


class CredentialStoreError(SyvrioError):
    """当前选定的凭据存储无法安全使用。"""


def _credential_path(env_name: str) -> Path | None:
    if sys.platform != "linux":
        return None
    override = os.environ.get("SYVRIO_CREDENTIALS_DIR")
    directory = Path(override) if override else user_data_path(KEYRING_SERVICE) / "credentials"
    if not directory.is_absolute():
        raise CredentialStoreError("SYVRIO_CREDENTIALS_DIR must be an absolute path.")
    # 常规条目名原样可读；自定义名称里的路径分隔符和百分号按字节转义。
    name = quote(env_name, safe="")
    if not name or name in {".", ".."} or len(name) > 235:
        raise CredentialStoreError("Choose a nonempty, shorter credential entry name.")
    if re.fullmatch(r"syvrio-[0-9a-f]{64}", name):
        name = "%73" + name[1:]  # 与待迁移的旧文件名空间分开。
    return directory / f"{name}.cred"


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


def _read_encrypted(path: Path) -> bytes:
    ciphertext = path.read_bytes()
    return _systemd_creds("decrypt", path, ciphertext) if ciphertext else b""


def _write_encrypted(path: Path, value: bytes) -> None:
    atomic_write(path, _systemd_creds("encrypt", path, value) if value else b"")


def get_password(env_name: str) -> str | None:
    path = _credential_path(env_name)
    if path is not None and _has_file(path):
        return _read_encrypted(path).decode("utf-8") or None

    import keyring

    return keyring.get_password(KEYRING_SERVICE, env_name) or None


def stored_key(env_name: str) -> str | None:
    """凭据存储暂时不可用时记录一次诊断，让调用方按缺少凭据处理。"""
    try:
        return get_password(env_name)
    except Exception as exc:
        from ..observability.diagnostics import record_once

        record_once("degraded.keyring", exc)
        return None


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
    _write_encrypted(path, password.encode("utf-8"))
    if not existing:
        from ..observability.diagnostics import record_once

        record_once("degraded.keyring", "Using user-scoped systemd credentials without a keyring.")


def delete_password(env_name: str) -> None:
    path = _credential_path(env_name)
    if path is not None and _has_file(path):
        # 即使旧版本地密钥环重新可用，这个名称仍沿用已选的存储后端。
        atomic_write(path, b"")
        return

    import keyring

    keyring.delete_password(KEYRING_SERVICE, env_name)


def save_api_key(env_name: str, api_key: str) -> bool:
    try:
        set_password(env_name, api_key)
        return True
    except Exception as exc:
        from ..observability.diagnostics import record_once

        # 第三方后端的异常可能包含刚提交的密码。
        record_once("degraded.keyring", f"Credential save failed ({type(exc).__name__}).")
        return False


def delete_api_key(env_name: str) -> bool:
    try:
        delete_password(env_name)
        return True
    except Exception as exc:
        from ..observability.diagnostics import record_once

        record_once("degraded.keyring", exc)
        return False
