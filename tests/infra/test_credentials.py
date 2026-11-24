from __future__ import annotations

import hashlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from keyring.errors import KeyringLocked, NoKeyringError

from syvrio.infra.config import LLMConfig, read_api_key
from syvrio.infra.security import credentials
from syvrio.infra.security.credentials import delete_api_key, save_api_key

pytestmark = pytest.mark.config


def test_native_credentials_need_no_files_or_system_commands(monkeypatch):
    import keyring

    stored = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, n, v: stored.__setitem__((s, n), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, n: stored.get((s, n)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, n: stored.pop((s, n)), raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unexpected command"))

    name = "SYNTHETIC_NATIVE_KEY"
    assert save_api_key(name, "sample-value")
    assert read_api_key(name) == "sample-value"
    assert delete_api_key(name)
    assert not read_api_key(name)
    if sys.platform == "linux":
        assert not credentials._credential_path(name).parent.exists()


@pytest.fixture
def encrypted_store(monkeypatch):
    import keyring

    def no_keyring(*args):
        raise NoKeyringError("no installed backend")

    monkeypatch.setattr(keyring, "set_password", no_keyring)
    blobs = {}

    def crypt(args, *, input, stdout, stderr, start_new_session, timeout, check):
        assert args[0] == "systemd-creds" and "--user" in args and args[-2:] == ["-", "-"]
        assert stdout == subprocess.PIPE and stderr == subprocess.DEVNULL and start_new_session
        assert timeout and check
        binding = next(arg for arg in args if arg.startswith("--name="))
        if args[1] == "encrypt":
            assert "--with-key=host" in args and input.decode() not in " ".join(args)
            ciphertext = hashlib.sha256(binding.encode() + input).digest()
            blobs[binding, ciphertext] = input
            return subprocess.CompletedProcess(args, 0, stdout=ciphertext)
        assert args[1] == "decrypt"
        return subprocess.CompletedProcess(args, 0, stdout=blobs[binding, input])

    monkeypatch.setattr(subprocess, "run", crypt)


@pytest.mark.skipif(sys.platform != "linux", reason="user-scoped systemd credentials")
def test_systemd_key_survives_native_backend_changes_and_stays_deleted(
    encrypted_store, monkeypatch
):
    import keyring

    # 自定义密钥名可能包含不适合文件名或凭据标识符的字符。
    name = "../../synthetic/provider"
    llm = LLMConfig(api_key_env=name)
    assert save_api_key(name, "fake-first-key")
    path = credentials._credential_path(name)
    assert path.parent.name == "credentials"
    assert path.parent.parent.name == "syvrio"
    assert path.stem == "syvrio-" + hashlib.sha256(name.encode()).hexdigest()
    assert b"fake-first-key" not in path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert llm.api_key() == "fake-first-key"

    other = "SYNTHETIC_OTHER_PROVIDER"
    assert save_api_key(other, "fake-other-key")
    monkeypatch.setattr(keyring, "get_password", lambda *a: "obsolete-native-value")
    monkeypatch.setattr(keyring, "set_password", lambda *a: pytest.fail("changed backend"))
    assert save_api_key(name, "fake-second-key")
    assert read_api_key(name) == "fake-second-key"
    monkeypatch.setenv(name, "fake-environment-value")
    assert llm.api_key() == "fake-environment-value"
    monkeypatch.delenv(name)
    assert delete_api_key(name)
    assert path.read_bytes() == b""
    assert LLMConfig(api_key_env=name).api_key() is None
    assert read_api_key(other) == "fake-other-key"
    assert save_api_key(name, "fake-readded-key")
    assert read_api_key(name) == "fake-readded-key"


@pytest.mark.skipif(sys.platform != "linux", reason="user-scoped systemd credentials")
def test_locked_keyring_does_not_create_fallback(monkeypatch):
    import keyring

    def locked(*args):
        raise KeyringLocked("test locked")

    monkeypatch.setattr(keyring, "set_password", locked)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unexpected command"))
    name = "SYNTHETIC_LOCKED_KEY"
    assert not save_api_key(name, "fake-example")
    assert not credentials._credential_path(name).parent.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="user-scoped systemd credentials")
def test_credential_directory_override_requires_absolute_path(
    encrypted_store, monkeypatch, tmp_path
):
    name = "SYNTHETIC_CUSTOM_DIR"
    monkeypatch.setenv("SYVRIO_CREDENTIALS_DIR", "relative")
    assert not save_api_key(name, "fake-custom-value")
    private_dir = tmp_path / "private-credentials"
    monkeypatch.setenv("SYVRIO_CREDENTIALS_DIR", str(private_dir))
    assert save_api_key(name, "fake-custom-value")
    assert credentials._credential_path(name).parent == private_dir
    assert read_api_key(name) == "fake-custom-value"


@pytest.mark.skipif(sys.platform != "linux", reason="user-scoped systemd credentials")
def test_insecure_credential_directory_is_rejected(encrypted_store, monkeypatch, tmp_path):
    name = "SYNTHETIC_PERMISSIONS"
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    monkeypatch.setenv("SYVRIO_CREDENTIALS_DIR", str(shared))
    assert not save_api_key(name, "fake-value")
    assert not list(shared.iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="user-scoped systemd credentials")
def test_failed_encryption_preserves_last_saved_value(encrypted_store, monkeypatch):
    name = "SYNTHETIC_FAILED_KEY"
    assert save_api_key(name, "fake-working-key")
    path: Path = credentials._credential_path(name)
    saved_bytes = path.read_bytes()
    with monkeypatch.context() as failed:

        def fail(*args, **kwargs):
            raise FileNotFoundError("systemd-creds unavailable")

        failed.setattr(subprocess, "run", fail)
        assert not save_api_key(name, "fake-replacement")
    assert path.read_bytes() == saved_bytes
    assert read_api_key(name) == "fake-working-key"
