from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from syvrio.infra.gitops import GitError, GitRepo, classify_git_error, redact_git_message
from syvrio.infra.locking import LockTimeout, vault_write_lock
from syvrio.infra.paths import SyvrioPaths, runtime_path, vault_paths


def test_runtime_path_prefers_env_over_config_and_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SYVRIO_RUNTIME", str(tmp_path / "env-runtime"))
    assert runtime_path(str(tmp_path / "config-runtime")) == (tmp_path / "env-runtime").resolve()
    monkeypatch.delenv("SYVRIO_RUNTIME")
    assert runtime_path(str(tmp_path / "config-runtime")) == (
        tmp_path / "config-runtime"
    ).resolve()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert runtime_path() == (tmp_path / ".syvrio" / "runtime").resolve()


def test_validate_rejects_runtime_inside_vault(tmp_path):
    paths = SyvrioPaths(tmp_path, tmp_path / "runtime")
    with pytest.raises(ValueError, match="separate"):
        paths.validate()


def test_validate_rejects_runtime_inside_git_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    paths = SyvrioPaths(tmp_path / "vault", repo / "runtime")
    with pytest.raises(ValueError, match="Git working tree"):
        paths.validate()


def test_bind_writes_marker_and_invalidates_only_on_rebind(tmp_path):
    vault_a, vault_b = tmp_path / "a", tmp_path / "b"
    paths = SyvrioPaths(vault_a, tmp_path / "runtime")
    paths.bind()
    marker = paths.state_dir / "vault-binding.json"
    assert marker.is_file()
    # 派生缓存在同一 Vault 绑定下保留。
    paths.index_dir.mkdir(parents=True, exist_ok=True)
    stale = paths.index_dir / "vectors.npz"
    stale.write_bytes(b"stale")
    paths.bind()
    assert stale.exists()
    # 换绑 Vault 后失效。
    rebound = SyvrioPaths(vault_b, tmp_path / "runtime")
    rebound.bind()
    assert not stale.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["vault"] == str(vault_b.resolve())


def test_vault_lock_is_reentrant_and_shared_by_path(tmp_path):
    lock = vault_write_lock(tmp_path)
    same = vault_write_lock(vault_paths(tmp_path))
    assert same is lock
    with lock:
        with same:
            assert lock.locked
    assert not lock.locked


def test_lock_timeout_when_held_by_other_thread(tmp_path):
    lock = vault_write_lock(tmp_path)
    lock.acquire()
    outcome: dict[str, bool] = {}

    def contender():
        try:
            lock.acquire(timeout=0.2)
        except LockTimeout:
            outcome["timeout"] = True

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join(5)
    lock.release()
    assert outcome.get("timeout")


def _fcntl_probe(lock_path: Path) -> subprocess.CompletedProcess:
    code = (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "deadline = time.monotonic() + 1.0\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        print('acquired'); sys.exit(0)\n"
        "    except BlockingIOError:\n"
        "        time.sleep(0.05)\n"
        "print('busy'); sys.exit(3)\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)


def test_vault_lock_mutex_across_processes(tmp_path):
    paths = vault_paths(tmp_path)
    lock = vault_write_lock(paths)
    lock.acquire()
    try:
        assert _fcntl_probe(paths.lock_dir / "vault.lock").stdout.strip() == "busy"
    finally:
        lock.release()
    time.sleep(0.05)
    assert _fcntl_probe(paths.lock_dir / "vault.lock").stdout.strip() == "acquired"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_gitops_refuses_vault_inside_foreign_repo(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init", "-q")
    vault = parent / "vault"
    vault.mkdir()
    repo = GitRepo(vault)
    assert repo.is_repo is False
    with pytest.raises(GitError, match="另一个 Git 仓库"):
        repo.ensure_init()


def test_gitops_refuses_committing_sensitive_files(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    repo = GitRepo(vault)
    repo.ensure_init()
    (vault / ".env").write_text("SECRET=synthetic", encoding="utf-8")
    (vault / "topic.md").write_text("普通记忆", encoding="utf-8")
    repo.stage_all()
    with pytest.raises(GitError, match="敏感文件"):
        repo.commit("memory: test")


def test_gitops_classifies_and_redacts_errors():
    assert classify_git_error("HTTP 403 Forbidden") == "auth"
    assert classify_git_error("fatal: could not resolve host") == "network"
    assert classify_git_error("hint: non-fast-forward rejected") == "diverged"
    assert classify_git_error("has no upstream branch") == "no_upstream"
    assert classify_git_error("其他错误") == "other"
    assert redact_git_message("https://user:token@example.com/repo.git") == (
        "https://***@example.com/repo.git"
    )


def test_session_ids_keep_path_traversal_blocked(tmp_path):
    from syvrio.agent.session import SessionStore

    store = SessionStore(tmp_path)
    with pytest.raises(KeyError, match="非法会话"):
        store.load("../../etc/passwd")
    with pytest.raises(KeyError, match="非法会话"):
        store.load("2026/11/escape")
    # 合法 ID 允许字母数字与连字符下划线（宽松正则供迁移工具使用）。
    assert store._validate_session_id("s-20261114-120000-abcd") is None
    assert os.environ.get("SYVRIO_RUNTIME")  # 隔离夹具在场
