from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.memory

from syvrio.agent.session import SessionStore
from syvrio.infra.config import (
    Config,
    ConfigError,
    GitConfig,
    LLMConfig,
    RetrievalConfig,
    load_config,
    write_example_config,
)
from syvrio.infra.gitops import GitError, classify_git_error
from syvrio.infra.gitsync import STAGE_CHECK, STAGE_COMMIT, STAGE_PUSH
from syvrio.infra.locking import LockTimeout, git_operation_lock, vault_write_lock
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import ExtractionResult
from syvrio.memory.service import MemoryService

TZ = timezone(timedelta(hours=8))


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=check
    )


def make_uninitialized_service(vault: Path, *, auto_sync: bool = True) -> MemoryService:
    config = Config(
        vault=vault,
        llm=LLMConfig(),
        retrieval=RetrievalConfig(method="keyword"),
        git=GitConfig(auto_sync=auto_sync),
        auto_push=False,
    )
    return MemoryService(
        config, extractor=StaticExtractor(ExtractionResult(memories=[]))
    )


def make_service(tmp_path: Path, *, auto_sync: bool = True) -> MemoryService:
    service = make_uninitialized_service(tmp_path / "vault", auto_sync=auto_sync)
    service.init_vault()
    return service


def add_remote(service: MemoryService, tmp_path: Path, *, upstream: bool = True) -> Path:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    _git("remote", "add", "origin", str(remote), cwd=service.config.vault)
    if upstream:
        _git("push", "-u", "origin", "HEAD", cwd=service.config.vault)
    return remote


def remote_log(remote: Path) -> list[str]:
    return [line for line in _git("log", "--oneline", cwd=remote, check=False).stdout.splitlines() if line]


def read_state(service: MemoryService) -> dict:
    return json.loads(service.git_sync.state_path.read_text(encoding="utf-8"))


def test_sync_commits_and_pushes(tmp_path):
    service = make_service(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("合成偏好：喜欢安静环境", topic="preferences")

    result = service.git_sync.sync("manual")

    assert result.status == "success"
    assert result.committed and result.commit_hash
    assert result.pushed
    assert service.repo.modified_count() == 0
    assert result.snapshot is not None and result.snapshot["running"] is False
    assert remote_log(remote)[0].startswith(result.commit_hash)


def test_commit_command_pushes_existing_unpushed_without_new_commit(tmp_path):
    service = make_service(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("先本地提交", topic="misc")
    service.commit(push=False)

    result = service.commit(push=True)

    assert not result["committed"]
    assert result["pushed"]
    assert service.repo.unpushed_count() == 0
    head = _git("rev-parse", "--short", "HEAD", cwd=service.config.vault).stdout.strip()
    assert any(head in line for line in remote_log(remote))
    assert read_state(service)["last_push_at"]


def test_sync_pushes_existing_unpushed_without_new_commit(tmp_path):
    service = make_service(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("待补推的记录", topic="misc")
    service.commit(push=False)
    assert service.repo.unpushed_count() == 1

    result = service.git_sync.sync("manual")

    assert result.status == "success"
    assert not result.committed
    assert result.pushed
    assert service.repo.unpushed_count() == 0
    assert any("memory:" in line for line in remote_log(remote))


def test_sync_without_changes_is_noop(tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)

    result = service.git_sync.sync("manual")

    assert result.status == "success"
    assert not result.committed
    assert not result.pushed
    assert result.push_skipped


def test_repeated_sync_never_creates_empty_commit(tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)
    service.note("一次性记录", topic="misc")

    assert service.git_sync.sync("manual").committed
    second = service.git_sync.sync("manual")

    assert not second.committed
    assert service.repo.modified_count() == 0


def test_missing_remote_keeps_local_commit_and_reports_partial(tmp_path):
    service = make_service(tmp_path)
    service.note("仅本地记录", topic="misc")

    result = service.git_sync.sync("manual")

    assert result.status == "partial"
    assert result.committed and result.commit_hash
    assert not result.pushed
    assert result.error_kind == "no_remote"
    assert "remote" in result.error
    state = read_state(service)
    assert state["last_commit_hash"] == result.commit_hash
    assert "last_push_at" not in state


def test_missing_upstream_reports_partial(tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path, upstream=False)
    service.note("无 upstream", topic="misc")

    result = service.git_sync.sync("manual")

    assert result.status == "partial"
    assert result.committed
    assert result.error_kind == "no_upstream"
    assert "push -u" in result.error


def test_push_failure_preserves_commit_and_next_sync_pushes_it(tmp_path):
    service = make_service(tmp_path)
    remote = add_remote(service, tmp_path)
    _git("remote", "set-url", "origin", str(tmp_path / "missing.git"), cwd=service.config.vault)
    service.note("推送失败的记录", topic="misc")

    failed = service.git_sync.sync("manual")
    assert failed.status == "partial"
    assert failed.committed and failed.commit_hash
    assert not failed.pushed
    assert failed.error

    _git("remote", "set-url", "origin", str(remote), cwd=service.config.vault)
    recovered = service.git_sync.sync("manual")

    assert recovered.status == "success"
    assert not recovered.committed  # 只补推已有提交
    assert recovered.pushed
    assert any(failed.commit_hash in line for line in remote_log(remote))


def test_diverged_remote_stops_push_without_force(tmp_path):
    service = make_service(tmp_path)
    remote = add_remote(service, tmp_path)
    other = tmp_path / "other"
    _git("clone", str(remote), str(other))
    _git(
        "-c", "user.name=synthetic", "-c", "user.email=synthetic@example.invalid",
        "commit", "--allow-empty", "-m", "remote-only", cwd=other,
    )
    _git("push", cwd=other)
    service.note("本地新记录", topic="misc")

    result = service.git_sync.sync("manual")

    assert result.status == "partial"
    assert result.committed
    assert not result.pushed
    assert result.error_kind == "diverged"
    assert remote_log(remote)[0].endswith("remote-only")


def test_sensitive_file_blocks_commit(tmp_path):
    service = make_service(tmp_path)
    (service.config.vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    service.note("正常记录", topic="misc")
    before = _git("rev-list", "--count", "HEAD", cwd=service.config.vault).stdout.strip()

    result = service.git_sync.sync("manual")

    assert result.status == "failed"
    assert not result.committed
    assert result.error_kind == "sensitive"
    assert ".env" in result.error
    assert service.repo.has_changes()
    after = _git("rev-list", "--count", "HEAD", cwd=service.config.vault).stdout.strip()
    assert before == after


@pytest.mark.parametrize("entrypoint", ["sync", "startup"])
def test_sensitive_recheck_restores_partial_staging(tmp_path, monkeypatch, entrypoint):
    service = make_service(tmp_path)
    vault = service.config.vault
    path = vault / "sample.txt"
    path.write_text("staged version", encoding="utf-8")
    _git("add", "--", path.name, cwd=vault)
    path.write_text("working tree version", encoding="utf-8")
    before = _git("diff", "--cached", "--binary", cwd=vault).stdout
    stage = service.repo.stage_all

    def add_sensitive_file_then_stage():
        (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
        stage()

    monkeypatch.setattr(service.repo, "stage_all", add_sensitive_file_then_stage)
    if entrypoint == "sync":
        result = service.git_sync.sync("manual")
        assert result.error_kind == "sensitive"
        assert not result.committed and not result.pushed
    else:
        with pytest.raises(GitError, match=r"\.env") as raised:
            service.commit_pending_changes()
        assert raised.value.kind == "sensitive"
    assert service.repo.staged_paths() == [path.name]
    assert _git("diff", "--cached", "--binary", cwd=vault).stdout == before
    assert path.read_text(encoding="utf-8") == "working tree version"
    assert (vault / ".env").exists()


# ---------- Git 工作树根目录约束 ----------


def test_standalone_vault_initializes_its_own_repository(tmp_path):
    service = make_service(tmp_path)

    assert service.repo.is_repo
    assert service.repo.foreign_repo_root() is None
    assert service.repo.repo_root().resolve() == service.config.vault.resolve()


def test_relative_vault_path_is_recognized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = make_uninitialized_service(Path("vault"))

    service.init_vault()

    assert service.repo.is_repo
    assert service.repo.repo_root().resolve() == (tmp_path / "vault").resolve()


def test_symlinked_vault_path_is_recognized(tmp_path):
    real = tmp_path / "real-vault"
    real.mkdir()
    link = tmp_path / "linked-vault"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    service = make_uninitialized_service(link)

    service.init_vault()

    assert service.repo.is_repo
    assert (real / ".git").exists()


def test_vault_inside_parent_repository_is_not_reused(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git("init", cwd=parent)
    vault = parent / "vault"
    vault.mkdir()
    service = make_uninitialized_service(vault)

    assert not service.repo.is_repo
    assert service.repo.foreign_repo_root().resolve() == parent.resolve()
    with pytest.raises(GitError) as caught:
        service.init_vault()
    assert caught.value.kind == "foreign_repo"
    assert not (vault / ".git").exists()
    assert not (vault / "README.md").exists()
    assert not (vault / "SOUL.md").exists()
    assert not (vault / ".gitignore").exists()
    assert not (vault / "memories").exists()


def test_pre_staged_parent_files_are_never_committed(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git("init", cwd=parent)
    external = parent / "external.txt"
    external.write_text("synthetic\n", encoding="utf-8")
    _git("add", "external.txt", cwd=parent)
    vault = parent / "vault"
    vault.mkdir()
    service = make_uninitialized_service(vault)

    with pytest.raises(GitError, match="另一个 Git 仓库"):
        service.init_vault()

    assert _git("rev-list", "--count", "HEAD", cwd=parent, check=False).returncode != 0
    staged = [item for item in _git("diff", "--cached", "--name-only", "-z", cwd=parent).stdout.split("\x00") if item]
    assert "external.txt" in staged
    assert not any(item.startswith("vault/") for item in staged)


def test_sync_refuses_parent_repository_and_never_pushes(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git("init", cwd=parent)
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    _git("remote", "add", "origin", str(remote), cwd=parent)
    vault = parent / "vault"
    vault.mkdir()
    service = make_uninitialized_service(vault)

    result = service.git_sync.sync("manual")

    assert result.status == "failed"
    assert result.error_kind == "foreign_repo"
    assert "another Git repository" in result.error
    assert remote_log(remote) == []
    with pytest.raises(GitError) as caught:
        service.commit(push=True)
    assert caught.value.kind == "foreign_repo"


def test_own_vault_commit_and_push_still_work(tmp_path):
    service = make_service(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("合法仓库记录", topic="misc")

    result = service.commit(push=True)

    assert result["committed"] and result["pushed"]
    assert any(result["hash"] in line for line in remote_log(remote))
    assert service.repo.foreign_repo_root() is None


# ---------- GitRepo.commit 的索引级最终保护 ----------


@pytest.mark.parametrize("name", [".env", "keys.env", "my secrets.env", "密钥 文件.env"])
def test_commit_rejects_staged_sensitive_file(tmp_path, name):
    service = make_service(tmp_path)
    vault = service.config.vault
    target = vault / name
    target.write_text("synthetic\n", encoding="utf-8")
    _git("add", name, cwd=vault)
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    staged_before = service.repo.staged_paths()

    with pytest.raises(GitError) as caught:
        service.repo.commit("synthetic commit")

    assert caught.value.kind == "sensitive"
    assert name in str(caught.value)
    assert _git("rev-parse", "HEAD", cwd=vault).stdout.strip() == head_before
    assert service.repo.staged_paths() == staged_before
    assert target.exists()


def test_commit_allows_staged_clean_file_with_unstaged_sensitive_file(tmp_path):
    service = make_service(tmp_path)
    vault = service.config.vault
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    (vault / "notes.md").write_text("synthetic\n", encoding="utf-8")
    _git("add", "notes.md", cwd=vault)

    commit_hash = service.repo.commit("synthetic commit")

    assert commit_hash
    tracked = _git("ls-files", cwd=vault).stdout.splitlines()
    assert "notes.md" in tracked
    assert ".env" not in tracked
    assert (vault / ".env").exists()


def test_commit_normal_file_succeeds(tmp_path):
    service = make_service(tmp_path)
    vault = service.config.vault
    (vault / "notes.md").write_text("synthetic\n", encoding="utf-8")
    _git("add", "notes.md", cwd=vault)

    commit_hash = service.repo.commit("synthetic commit")

    assert commit_hash
    assert _git("log", "-1", "--pretty=%s", cwd=vault).stdout.strip() == "synthetic commit"


def test_direct_commit_still_rejects_parent_repository(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git("init", cwd=parent)
    vault = parent / "vault"
    vault.mkdir()
    service = make_uninitialized_service(vault)
    (vault / "notes.md").write_text("synthetic\n", encoding="utf-8")

    with pytest.raises(GitError) as caught:
        service.repo.commit("synthetic commit")

    assert caught.value.kind == "foreign_repo"
    assert _git("rev-list", "--count", "HEAD", cwd=parent, check=False).returncode != 0


# ---------- 敏感路径 ----------


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.local",
        "keys.env",
        "secrets.env",
        "credentials.env",
        "nested/Keys.ENV",
        "my keys.env",
        "敏感.env",
        "config.toml",
        "syvrio.toml",
    ],
)
def test_sensitive_name_variants_are_detected(tmp_path, relative):
    service = make_service(tmp_path)
    path = service.config.vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic\n", encoding="utf-8")

    service.repo.stage_all()

    assert relative in service.repo.staged_paths()
    assert relative in service.repo.sensitive_paths()


@pytest.mark.parametrize(
    "relative",
    ["notes.md", "notes with space.md", "weird[1].md", "memories/2026-01.md"],
)
def test_normal_files_are_not_flagged_as_sensitive(tmp_path, relative):
    service = make_service(tmp_path)
    path = service.config.vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic\n", encoding="utf-8")

    service.repo.stage_all()

    assert relative in service.repo.staged_paths()
    assert service.repo.sensitive_paths() == []


def test_init_vault_refuses_untracked_sensitive_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    service = make_uninitialized_service(vault)

    with pytest.raises(GitError) as caught:
        service.init_vault()

    assert caught.value.kind == "sensitive"
    assert (vault / ".env").exists()
    assert _git("rev-list", "--count", "HEAD", cwd=vault, check=False).returncode != 0


def test_init_vault_refuses_pre_staged_sensitive_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _git("init", cwd=vault)
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    _git("add", ".env", cwd=vault)
    service = make_uninitialized_service(vault)

    with pytest.raises(GitError) as caught:
        service.init_vault()

    assert caught.value.kind == "sensitive"
    assert (vault / ".env").exists()
    staged = [item for item in _git("diff", "--cached", "--name-only", "-z", cwd=vault).stdout.split("\x00") if item]
    assert ".env" in staged
    assert _git("rev-list", "--count", "HEAD", cwd=vault, check=False).returncode != 0


def test_init_vault_refuses_staged_sensitive_file_even_if_gitignored(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _git("init", cwd=vault)
    (vault / ".gitignore").write_text("*.env\n", encoding="utf-8")
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=vault)
    _git("add", "-f", ".env", cwd=vault)
    service = make_uninitialized_service(vault)

    with pytest.raises(GitError) as caught:
        service.init_vault()

    assert caught.value.kind == "sensitive"
    assert ".env" in caught.value.args[0]
    assert (vault / ".env").exists()
    assert _git("rev-list", "--count", "HEAD", cwd=vault, check=False).returncode != 0


def test_init_vault_commits_normal_files(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "notes.md").write_text("# synthetic\n", encoding="utf-8")
    service = make_uninitialized_service(vault)

    result = service.init_vault()

    assert result["commit"]
    tracked = _git("ls-files", cwd=vault).stdout.splitlines()
    assert "notes.md" in tracked
    assert not any(name.endswith(".env") for name in tracked)


def test_sync_never_calls_llm(monkeypatch, tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        "syvrio.memory.service.summarize_diff",
        lambda *args, **kwargs: calls.append(args) or "should not be used",
    )
    service.note("自动同步不调用模型", topic="misc")

    result = service.git_sync.sync(
        "scheduled", now=datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    )

    assert result.status == "success"
    assert result.committed
    assert calls == []


def test_sync_reports_real_stage_progress(tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)
    service.note("阶段测试", topic="misc")
    snapshots: list[dict[str, str]] = []

    def progress(stages):
        snapshots.append({stage.name: stage.state for stage in stages})

    result = service.git_sync.sync("manual", progress)

    assert result.status == "success"
    assert snapshots
    assert any(states[STAGE_CHECK] == "running" for states in snapshots)
    assert snapshots[-1][STAGE_CHECK] == "done"
    assert snapshots[-1][STAGE_COMMIT] == "done"
    assert snapshots[-1][STAGE_PUSH] == "done"


def test_state_file_and_locks_are_gitignored(tmp_path):
    service = make_service(tmp_path)
    service.note("状态文件不应入库", topic="misc")

    service.git_sync.sync("manual")

    status = _git("status", "--porcelain", cwd=service.config.vault).stdout
    assert ".syvrio/git-sync-state.json" not in status
    assert ".syvrio/vault.lock" not in status
    assert ".syvrio/git.lock" not in status


def test_git_error_redacts_url_credentials():
    error = GitError(
        "fatal: unable to access 'https://synthetic-user:synthetic-token@example.invalid/repo.git'"
    )
    assert "synthetic-token" not in str(error)
    assert "***@" in str(error)


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("fatal: Authentication failed for 'https://example.invalid/repo.git'", "auth"),
        ("ssh: Could not resolve hostname example.invalid", "network"),
        ("! [rejected] main -> main (non-fast-forward)", "diverged"),
        ("fatal: The current branch main has no upstream branch", "no_upstream"),
        ("something unexpected", "other"),
    ],
)
def test_git_error_classification(message, kind):
    assert classify_git_error(message) == kind


# ---------- 调度判断 ----------


def test_due_respects_daily_schedule_and_success_state(tmp_path):
    service = make_service(tmp_path)
    sync = service.git_sync
    before = datetime(2026, 11, 2, 2, 59, tzinfo=TZ)
    at_time = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)

    assert not sync.due(before)
    assert sync.due(at_time)

    result = sync.sync("scheduled", now=at_time)
    assert result.status == "partial"  # 未配置 remote，但本地提交成功
    state = read_state(service)
    assert state.get("last_daily_success_date") != "2026-11-02"
    assert state["last_daily_attempt_date"] == "2026-11-02"
    assert state["last_daily_blocked_date"] == "2026-11-02"
    assert not sync.due(datetime(2026, 11, 2, 23, 0, tzinfo=TZ))
    assert sync.due(datetime(2026, 11, 3, 3, 0, tzinfo=TZ))


def test_due_is_disabled_and_retry_after_is_respected(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)
    sync = service.git_sync
    at_time = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    attempts = {"fail": True}

    def push():
        if attempts["fail"]:
            raise GitError("Could not resolve host: example.invalid", kind="network")
        return ""

    monkeypatch.setattr(service.repo, "push", push)
    service.note("网络失败", topic="misc")
    result = sync.sync("scheduled", now=at_time)

    assert result.error_kind == "network"
    state = read_state(service)
    assert "last_daily_success_date" not in state
    assert sync.next_sync_at(at_time) == at_time  # 已到期但等待重试
    assert not sync.due(at_time + timedelta(minutes=29))
    assert sync.due(at_time + timedelta(minutes=31))

    attempts["fail"] = False
    recovered = sync.sync("scheduled", now=at_time + timedelta(minutes=31))
    assert recovered.status == "success"
    assert recovered.pushed
    assert read_state(service)["last_daily_success_date"] == "2026-11-02"

    service.config.git.auto_sync = False
    assert not sync.due(datetime(2026, 11, 4, 3, 0, tzinfo=TZ))


def test_next_sync_at_rolls_to_next_day_after_success(tmp_path):
    service = make_service(tmp_path)
    sync = service.git_sync
    at_time = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    sync.sync("scheduled", now=at_time)

    assert sync.next_sync_at(at_time) == at_time + timedelta(days=1)


# ---------- 并发与跨进程 ----------


def test_session_append_waits_for_vault_lock(tmp_path):
    service = make_service(tmp_path)
    store = SessionStore(service.config.vault)
    session = store.create()
    finished = threading.Event()

    def writer():
        store.append(
            session,
            {"type": "message", "id": "m-synthetic", "role": "user", "content": "并发写入"},
        )
        finished.set()

    with service.write_lock:
        thread = threading.Thread(target=writer)
        thread.start()
        assert not finished.wait(0.2)
    assert finished.wait(2)
    thread.join()


def _lock_child(vault: Path, code: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.Popen(
        [sys.executable, "-c", code.format(vault=str(vault))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_vault_lock_same_process_contention_times_out_without_releasing_owner(tmp_path):
    vault = tmp_path / "vault"
    lock = vault_write_lock(vault)
    entered, release = threading.Event(), threading.Event()
    result: dict[str, object] = {}

    def owner():
        with lock:
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(LockTimeout):
            lock.acquire(timeout=0.1)
        assert lock.locked  # 另一线程仍拥有锁，不允许错误释放。
        result["owner_active"] = thread.is_alive()
    finally:
        release.set()
        thread.join(timeout=5)
    assert result["owner_active"] is True
    assert not lock.locked
    with lock:
        assert lock.locked


def test_git_lock_same_process_contention_causes_busy_result(tmp_path):
    service = make_service(tmp_path)
    lock = git_operation_lock(service.config.vault)
    entered, release = threading.Event(), threading.Event()

    def owner():
        with lock:
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(5)
        result = service.git_sync.sync("manual", wait_for_git=False)
        assert result.status == "failed"
        assert result.error_kind == "busy"
    finally:
        release.set()
        thread.join(timeout=5)


def test_vault_lock_contention_does_not_commit_unprotected(tmp_path):
    service = make_service(tmp_path)
    service.note("待提交数据", topic="misc")
    lock = vault_write_lock(service.config.vault)
    entered, release = threading.Event(), threading.Event()

    def owner():
        with lock:
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(5)
        # 使用同进程另一个线程执行同步，确保测试真实模拟锁竞争。
        # vault 锁等待默认为 30s；此处将其 acquire 包装成较短超时。
        original_acquire = lock.acquire
        def acquire_short(timeout=None):
            return original_acquire(timeout=0.1)
        lock.acquire = acquire_short
        try:
            result = service.git_sync.sync("manual")
            assert result.status == "failed"
            assert result.error_kind == "vault_busy"
            assert service.repo.has_changes()
        finally:
            lock.acquire = original_acquire
    finally:
        release.set()
        thread.join(timeout=5)


def test_vault_lock_is_cross_process(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    child = _lock_child(
        vault,
        "import time\nfrom syvrio.infra.locking import vault_write_lock\n"
        "lock = vault_write_lock({vault!r})\nlock.acquire()\n"
        "print('locked', flush=True)\ntime.sleep(30)\n",
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(LockTimeout):
            vault_write_lock(vault).acquire(timeout=0.2)
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert vault_write_lock(vault).acquire(timeout=2)
    vault_write_lock(vault).release()


def test_lock_released_after_process_crash(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    child = _lock_child(
        vault,
        "import os\nfrom syvrio.infra.locking import vault_write_lock\n"
        "lock = vault_write_lock({vault!r})\nlock.acquire()\n"
        "print('locked', flush=True)\nos._exit(0)\n",
    )
    assert child.stdout.readline().strip() == "locked"
    assert child.wait(timeout=5) == 0
    assert vault_write_lock(vault).acquire(timeout=2)
    vault_write_lock(vault).release()


def test_git_operation_lock_is_cross_process(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    child = _lock_child(
        vault,
        "import time\nfrom syvrio.infra.locking import git_operation_lock\n"
        "lock = git_operation_lock({vault!r})\nlock.acquire()\n"
        "print('locked', flush=True)\ntime.sleep(30)\n",
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(LockTimeout):
            git_operation_lock(vault).acquire(timeout=0.2)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_sync_reports_busy_when_another_process_holds_git_lock(tmp_path):
    service = make_service(tmp_path)
    child = _lock_child(
        service.config.vault,
        "import time\nfrom syvrio.infra.locking import git_operation_lock\n"
        "lock = git_operation_lock({vault!r})\nlock.acquire()\n"
        "print('locked', flush=True)\ntime.sleep(30)\n",
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        result = service.git_sync.sync("manual", wait_for_git=False)
        assert result.status == "failed"
        assert result.error_kind == "busy"
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_second_sync_is_rejected_while_first_runs(tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)
    service.note("互斥测试", topic="misc")
    started, release = threading.Event(), threading.Event()

    def slow_push():
        started.set()
        assert release.wait(5)
        return ""

    service.repo.push = slow_push
    result_box: dict[str, object] = {}

    def first_sync():
        result_box["first"] = service.git_sync.sync("manual")

    thread = threading.Thread(target=first_sync)
    thread.start()
    try:
        assert started.wait(5)
        second = service.git_sync.sync("manual")
        assert second.status == "failed"
        assert second.error_kind == "busy"
    finally:
        release.set()
        thread.join(timeout=10)
    assert result_box["first"].status == "success"


def test_push_network_wait_does_not_block_memory_writes(tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)
    service.note("推送前记录", topic="misc")
    started, release = threading.Event(), threading.Event()

    def slow_push():
        started.set()
        assert release.wait(5)
        return ""

    service.repo.push = slow_push
    result_box: dict[str, object] = {}

    def sync():
        result_box["result"] = service.git_sync.sync("manual")

    thread = threading.Thread(target=sync)
    thread.start()
    try:
        assert started.wait(5)
        started_at = time.monotonic()
        service.note("推送期间的新记录", topic="misc")
        assert time.monotonic() - started_at < 2
    finally:
        release.set()
        thread.join(timeout=10)
    assert result_box["result"].status == "success"
    assert service.repo.has_changes()  # Push 期间的新写入留给下次同步


def test_session_jsonl_integrity_during_concurrent_sync(tmp_path):
    service = make_service(tmp_path)
    add_remote(service, tmp_path)
    store = SessionStore(service.config.vault)
    session = store.create()
    store.append(
        session,
        {"type": "message", "id": "m-before", "role": "user", "content": "同步前"},
    )
    started, release = threading.Event(), threading.Event()

    def slow_push():
        started.set()
        assert release.wait(5)
        return ""

    service.repo.push = slow_push
    result_box: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_box.setdefault("result", service.git_sync.sync("manual"))
    )
    thread.start()
    try:
        assert started.wait(5)
        for index in range(20):
            store.append(
                session,
                {
                    "type": "message",
                    "id": f"m-during-{index}",
                    "role": "user",
                    "content": f"推送期间写入 {index}",
                },
            )
    finally:
        release.set()
        thread.join(timeout=10)

    result = result_box["result"]
    assert result.status == "success"
    worktree = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    committed_raw = _git(
        "show", f"HEAD:{session.path.relative_to(service.config.vault).as_posix()}", cwd=service.config.vault
    ).stdout
    committed = [json.loads(line) for line in committed_raw.splitlines()]
    assert all(isinstance(record, dict) for record in worktree)
    assert all(isinstance(record, dict) for record in committed)
    assert len(committed) < len(worktree)


# ---------- 配置 ----------


def test_git_config_load_validate_and_example(tmp_path):
    path = tmp_path / "syvrio.toml"
    path.write_text(
        "[git]\nauto_sync = false\nsync_time = \"04:30\"\nretry_interval_minutes = 45\n",
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded.git.auto_sync is False
    assert loaded.git.sync_time == "04:30"
    assert loaded.git.retry_interval_minutes == 45

    bad = tmp_path / "bad.toml"
    bad.write_text("[git]\nsync_time = \"25:00\"\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="HH:MM"):
        load_config(bad)

    negative = tmp_path / "negative.toml"
    negative.write_text("[git]\nretry_interval_minutes = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="必须大于 0"):
        load_config(negative)

    example = tmp_path / "example.toml"
    write_example_config(example, tmp_path / "vault")
    text = example.read_text(encoding="utf-8")
    assert "[git]" in text and "sync_time" in text
    assert load_config(example).git.auto_sync is True

    from syvrio.infra.providers import write_config

    config = Config(
        vault=tmp_path / "vault",
        git=GitConfig(auto_sync=False, sync_time="05:15", retry_interval_minutes=10),
    )
    written = tmp_path / "written.toml"
    write_config(written, config)
    roundtripped = load_config(written)
    assert roundtripped.git.auto_sync is False
    assert roundtripped.git.sync_time == "05:15"
    assert roundtripped.git.retry_interval_minutes == 10


def test_nonretryable_daily_failure_is_blocked_not_success(tmp_path):
    service = make_service(tmp_path)
    sync = service.git_sync
    stamp = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    result = sync.sync("scheduled", now=stamp)
    assert result.status == "partial"
    assert result.error_kind == "no_remote"
    state = read_state(service)
    assert "last_daily_success_date" not in state
    assert state["last_daily_blocked_date"] == "2026-11-02"
    assert state["last_daily_attempt_date"] == "2026-11-02"
    assert not sync.due(stamp + timedelta(hours=1))
    assert sync.next_sync_at(stamp) == stamp + timedelta(days=1)
    assert sync.due(stamp + timedelta(days=1))
