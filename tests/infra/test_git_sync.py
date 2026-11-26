from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from syvrio.infra.config import (
    Config,
    ConfigError,
    GitConfig,
    LLMConfig,
    RetrievalConfig,
    load_config,
    write_example_config,
)
from syvrio.infra.session import SessionStore
from syvrio.infra.storage.locking import LockTimeout, git_operation_lock
from syvrio.infra.system.gitops import GitError, classify_git_error
from syvrio.infra.vault.files import initialize_vault
from syvrio.infra.vault.sync import STAGE_CHECK, STAGE_COMMIT, STAGE_PUSH, GitSyncService
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.memory

TZ = timezone(timedelta(hours=8))


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=check
    )


def make_uninitialized_services(
    vault: Path, *, auto_sync: bool = True
) -> tuple[MemoryService, GitSyncService]:
    config = Config(
        vault=vault,
        llm=LLMConfig(),
        retrieval=RetrievalConfig(method="keyword"),
        git=GitConfig(auto_sync=auto_sync),
        auto_push=False,
    )
    memory = MemoryService(config)
    return memory, GitSyncService(config, memory.paths)


def make_services(
    tmp_path: Path, *, auto_sync: bool = True
) -> tuple[MemoryService, GitSyncService]:
    service, git = make_uninitialized_services(tmp_path / "vault", auto_sync=auto_sync)
    initialize_vault(git, service.initialize_files)
    return service, git


def test_git_state_follows_the_vault_transaction(tmp_path):
    from syvrio.infra.storage.database import transaction

    service, git = make_uninitialized_services(tmp_path / "vault")
    with pytest.raises(RuntimeError, match="abort"), transaction(service.paths) as db:
        git.record_push_success()
        assert json.loads(db.execute("SELECT state FROM git_sync").fetchone()[0])["last_push_at"]
        raise RuntimeError("abort")

    assert git._read_state() == {}
    git.record_push_success()
    assert git._read_state()["last_push_at"]


def test_vault_git_never_stages_or_summarizes_archived_data(tmp_path):
    service, git = make_services(tmp_path)
    repo, vault = git.repo, git.repo.path
    store = SessionStore(service.paths)
    session = store.create()
    marker = "quartz archive only text"
    store.append(session, {"type": "message", "role": "user", "content": marker})
    git.is_dirty()
    repo.stage_all()
    assert repo.commit("Save active session")
    store.set_archived(session.id, True)
    extra = vault / "archived" / "misc" / "credentials.txt"
    extra.parent.mkdir(parents=True)
    extra.write_text(marker)
    assert not repo.sensitive_paths()
    git.is_dirty()
    repo.stage_all()
    assert all(not path.startswith("archived/") for path in repo.staged_paths())
    assert marker not in repo.staged_diff()
    assert repo.commit("Archive conversation")
    assert not repo.has_changes()

    _git("add", "-f", "archived/misc/credentials.txt", cwd=vault)
    (vault / "active.txt").write_text("active")
    with pytest.raises(GitError, match="Archive changes"):
        with repo.stage_checked():
            pytest.fail("A managed commit cannot consume a manually staged archive")
    assert _git("diff", "--cached", "--name-only", cwd=vault).stdout.splitlines() == [
        "archived/misc/credentials.txt"
    ]
    assert marker not in repo.staged_diff()


def test_commit_summary_keeps_later_files_and_fits_the_model_window(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from syvrio.infra.llm.tokens import estimate_messages_tokens
    from syvrio.infra.vault import commit_summary

    service, git = make_services(tmp_path)
    vault = git.repo.path / "content"
    (vault / "a-long.md").write_text(
        "".join(f"line {index} synthetic change\n" for index in range(2000)), encoding="utf-8"
    )
    (vault / "z-small.md").write_text("synthetic final change", encoding="utf-8")
    git.repo.stage_all()
    diff = git.repo.staged_diff()
    assert len(diff) > 24000 and "synthetic final change" in diff
    stat = git.repo.staged_stat()
    monkeypatch.setattr(LLMConfig, "api_key", lambda _: "fixture")
    monkeypatch.setattr(
        commit_summary, "effective_context_window", lambda _, configured: min(4096, configured)
    )
    received = []

    def complete(config, messages, *, temperature, max_tokens):
        assert estimate_messages_tokens(messages) + max_tokens <= 4096
        received.append(messages[-1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="摘要"))])

    monkeypatch.setattr(commit_summary, "complete", complete)
    assert commit_summary.summarize_diff(diff, service.config.llm, stat, 50000) == "摘要"
    assert stat in received[0] and "+synthetic final change" in received[0]
    assert "以下为各文件的差异节选" in received[0]
    assert "+line 1999 synthetic change" not in received[0]
    assert commit_summary._summary_messages(diff, stat, 1) == []


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"max_input_tokens": 4096, "max_output_tokens": 256}, 256),
        ({"max_input_tokens": 4096, "max_tokens": 512}, 512),
        ({"max_input_tokens": 512}, 128),
    ],
)
def test_git_summary_respects_model_output_limit(monkeypatch, metadata, expected):
    from types import SimpleNamespace

    from syvrio.infra.llm import client
    from syvrio.infra.llm.tokens import estimate_messages_tokens
    from syvrio.infra.vault import commit_summary

    config = LLMConfig(provider="local")
    monkeypatch.setattr(client, "model_metadata", lambda _: metadata)
    monkeypatch.setattr(commit_summary, "model_metadata", lambda _: metadata)

    def complete(config, messages, *, max_tokens, temperature):
        assert max_tokens == expected
        assert estimate_messages_tokens(messages) + max_tokens <= metadata["max_input_tokens"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="摘要"))])

    monkeypatch.setattr(commit_summary, "complete", complete)
    assert commit_summary.summarize_diff("+synthetic", config, "x | 1 +", 50000) == "摘要"


def add_remote(service: MemoryService, tmp_path: Path, *, upstream: bool = True) -> Path:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    _git("remote", "add", "origin", str(remote), cwd=service.config.vault / "export")
    if upstream:
        _git("push", "-u", "origin", "HEAD", cwd=service.config.vault / "export")
    return remote


def remote_log(remote: Path) -> list[str]:
    return [
        line
        for line in _git("log", "--oneline", cwd=remote, check=False).stdout.splitlines()
        if line
    ]


def read_state(git: GitSyncService) -> dict:
    return git._read_state()


def test_sync_commits_and_pushes(tmp_path):
    service, git = make_services(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("合成偏好：喜欢安静环境", topic="preferences")

    result = git.sync("manual")

    assert result.status == "success"
    assert result.committed and result.commit_hash
    assert result.pushed
    assert git.repo.modified_count() == 0
    assert result.snapshot is not None and result.snapshot["running"] is False
    assert remote_log(remote)[0].startswith(result.commit_hash)


def test_commit_command_pushes_existing_unpushed_without_new_commit(tmp_path):
    service, git = make_services(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("先本地提交", topic="misc")
    git.commit(push=False)

    result = git.commit(push=True)

    assert not result["committed"]
    assert result["pushed"]
    assert git.repo.unpushed_count() == 0
    head = _git("rev-parse", "--short", "HEAD", cwd=service.config.vault / "export").stdout.strip()
    assert any(head in line for line in remote_log(remote))
    assert read_state(git)["last_push_at"]


def test_sync_pushes_existing_unpushed_without_new_commit(tmp_path):
    service, git = make_services(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("待补推的记录", topic="misc")
    git.commit(push=False)
    assert git.repo.unpushed_count() == 1

    result = git.sync("manual")

    assert result.status == "success"
    assert not result.committed
    assert result.pushed
    assert git.repo.unpushed_count() == 0
    assert any("memory:" in line for line in remote_log(remote))


def test_sync_without_changes_is_noop(tmp_path):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path)

    result = git.sync("manual")

    assert result.status == "success"
    assert not result.committed
    assert not result.pushed
    assert result.push_skipped


def test_repeated_sync_never_creates_empty_commit(tmp_path):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path)
    service.note("一次性记录", topic="misc")

    assert git.sync("manual").committed
    second = git.sync("manual")

    assert not second.committed
    assert git.repo.modified_count() == 0


def test_missing_remote_keeps_local_commit_and_reports_partial(tmp_path):
    service, git = make_services(tmp_path)
    service.note("仅本地记录", topic="misc")

    result = git.sync("manual")

    assert result.status == "partial"
    assert result.committed and result.commit_hash
    assert not result.pushed
    assert result.error_kind == "no_remote"
    assert "remote" in result.error
    state = read_state(git)
    assert state["last_commit_hash"] == result.commit_hash
    assert "last_push_at" not in state


def test_missing_upstream_reports_partial(tmp_path):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path, upstream=False)
    service.note("无 upstream", topic="misc")

    result = git.sync("manual")

    assert result.status == "partial"
    assert result.committed
    assert result.error_kind == "no_upstream"
    assert "push -u" in result.error


def test_push_failure_preserves_commit_and_next_sync_pushes_it(tmp_path):
    service, git = make_services(tmp_path)
    remote = add_remote(service, tmp_path)
    _git(
        "remote",
        "set-url",
        "origin",
        str(tmp_path / "missing.git"),
        cwd=service.config.vault / "export",
    )
    service.note("推送失败的记录", topic="misc")

    failed = git.sync("manual")
    assert failed.status == "partial"
    assert failed.committed and failed.commit_hash
    assert not failed.pushed
    assert failed.error

    _git("remote", "set-url", "origin", str(remote), cwd=service.config.vault / "export")
    recovered = git.sync("manual")

    assert recovered.status == "success"
    assert not recovered.committed  # 只补推已有提交
    assert recovered.pushed
    assert any(failed.commit_hash in line for line in remote_log(remote))


def test_diverged_remote_stops_push_without_force(tmp_path):
    service, git = make_services(tmp_path)
    remote = add_remote(service, tmp_path)
    other = tmp_path / "other"
    _git("clone", str(remote), str(other))
    _git(
        "-c",
        "user.name=synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "remote-only",
        cwd=other,
    )
    _git("push", cwd=other)
    service.note("本地新记录", topic="misc")

    result = git.sync("manual")

    assert result.status == "partial"
    assert result.committed
    assert not result.pushed
    assert result.error_kind == "diverged"
    assert remote_log(remote)[0].endswith("remote-only")


def test_sensitive_file_blocks_commit(tmp_path):
    service, git = make_services(tmp_path)
    (git.repo.path / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    service.note("正常记录", topic="misc")
    before = _git("rev-list", "--count", "HEAD", cwd=service.config.vault / "export").stdout.strip()

    result = git.sync("manual")

    assert result.status == "failed"
    assert not result.committed
    assert result.error_kind == "sensitive"
    assert ".env" in result.error
    assert git.repo.has_changes()
    after = _git("rev-list", "--count", "HEAD", cwd=service.config.vault / "export").stdout.strip()
    assert before == after


@pytest.mark.parametrize("entrypoint", ["sync", "startup"])
def test_sensitive_recheck_restores_partial_staging(tmp_path, monkeypatch, entrypoint):
    service, git = make_services(tmp_path)
    vault = git.repo.path
    service.note("synthetic change")
    path = vault / "sample.txt"
    path.write_text("staged version", encoding="utf-8")
    _git("add", "--", path.name, cwd=vault)
    path.write_text("working tree version", encoding="utf-8")
    before = _git("diff", "--cached", "--binary", cwd=vault).stdout
    stage = git.repo.stage_all

    def add_sensitive_file_then_stage():
        (vault / "content" / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
        stage()

    monkeypatch.setattr(git.repo, "stage_all", add_sensitive_file_then_stage)
    if entrypoint == "sync":
        result = git.sync("manual")
        assert result.error_kind == "sensitive"
        assert not result.committed and not result.pushed
    else:
        with pytest.raises(GitError, match=r"\.env") as raised:
            git.commit_pending_changes()
        assert raised.value.kind == "sensitive"
    assert _git("diff", "--cached", "--name-only", cwd=vault).stdout.splitlines() == [path.name]
    assert _git("diff", "--cached", "--binary", cwd=vault).stdout == before
    assert path.read_text(encoding="utf-8") == "working tree version"
    assert (vault / "content" / ".env").exists()


# ---------- Git 工作树根目录约束 ----------


def test_standalone_vault_initializes_its_own_repository(tmp_path):
    service, git = make_services(tmp_path)

    assert git.repo.is_repo
    assert git.repo.foreign_repo_root() is None
    assert git.repo.repo_root().resolve() == (service.config.vault / "export").resolve()


def test_relative_vault_path_is_recognized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service, git = make_uninitialized_services(Path("vault"))

    initialize_vault(git, service.initialize_files)

    assert git.repo.is_repo
    assert git.repo.repo_root().resolve() == (tmp_path / "vault" / "export").resolve()


def test_symlinked_vault_path_is_recognized(tmp_path):
    real = tmp_path / "real-vault"
    real.mkdir()
    link = tmp_path / "linked-vault"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    service, git = make_uninitialized_services(link)

    initialize_vault(git, service.initialize_files)

    assert git.repo.is_repo
    assert (real / "export" / ".git").exists()
    fact = service.note("Synthetic reading preference", topic="general/preferences")
    assert (real / "syvrio.sqlite3").is_file()
    assert service.read_topic("preferences")["entries"][0]["id"] == fact.id
    reopened, _ = make_uninitialized_services(real)
    assert reopened.store.find(fact.id).text == fact.text


def test_vault_inside_parent_repository_is_not_reused(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git("init", cwd=parent)
    vault = parent / "vault"
    vault.mkdir()
    service, git = make_uninitialized_services(vault)

    assert not git.repo.is_repo
    assert git.repo.foreign_repo_root().resolve() == parent.resolve()
    with pytest.raises(GitError) as caught:
        initialize_vault(git, service.initialize_files)
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
    service, git = make_uninitialized_services(vault)

    with pytest.raises(GitError, match="另一个 Git 仓库"):
        initialize_vault(git, service.initialize_files)

    assert _git("rev-list", "--count", "HEAD", cwd=parent, check=False).returncode != 0
    staged = [
        item
        for item in _git("diff", "--cached", "--name-only", "-z", cwd=parent).stdout.split("\x00")
        if item
    ]
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
    _, git = make_uninitialized_services(vault)

    result = git.sync("manual")

    assert result.status == "failed"
    assert result.error_kind == "foreign_repo"
    assert "another Git repository" in result.error
    assert remote_log(remote) == []
    with pytest.raises(GitError) as caught:
        git.commit(push=True)
    assert caught.value.kind == "foreign_repo"


def test_own_vault_commit_and_push_still_work(tmp_path):
    service, git = make_services(tmp_path)
    remote = add_remote(service, tmp_path)
    service.note("合法仓库记录", topic="misc")

    result = git.commit(push=True)

    assert result["committed"] and result["pushed"]
    assert any(result["hash"] in line for line in remote_log(remote))
    assert git.repo.foreign_repo_root() is None


# ---------- VaultGitRepo.commit 的索引级最终保护 ----------


@pytest.mark.parametrize("name", [".env", "keys.env", "my secrets.env", "密钥 文件.env"])
def test_commit_rejects_staged_sensitive_file(tmp_path, name):
    service, git = make_services(tmp_path)
    vault = git.repo.path
    target = vault / name
    target.write_text("synthetic\n", encoding="utf-8")
    _git("add", name, cwd=vault)
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    staged_before = git.repo.staged_paths()

    with pytest.raises(GitError) as caught:
        git.repo.commit("synthetic commit")

    assert caught.value.kind == "sensitive"
    assert name in str(caught.value)
    assert _git("rev-parse", "HEAD", cwd=vault).stdout.strip() == head_before
    assert git.repo.staged_paths() == staged_before
    assert target.exists()


def test_commit_allows_staged_clean_file_with_unstaged_sensitive_file(tmp_path):
    service, git = make_services(tmp_path)
    vault = git.repo.path
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    (vault / "content/notes.md").write_text("synthetic\n", encoding="utf-8")
    _git("add", "content/notes.md", cwd=vault)

    commit_hash = git.repo.commit("synthetic commit")

    assert commit_hash
    tracked = _git("ls-files", cwd=vault).stdout.splitlines()
    assert "content/notes.md" in tracked
    assert ".env" not in tracked
    assert (vault / ".env").exists()


def test_commit_normal_file_succeeds(tmp_path):
    service, git = make_services(tmp_path)
    vault = git.repo.path
    (vault / "content/notes.md").write_text("synthetic\n", encoding="utf-8")
    _git("add", "content/notes.md", cwd=vault)

    commit_hash = git.repo.commit("synthetic commit")

    assert commit_hash
    assert _git("log", "-1", "--pretty=%s", cwd=vault).stdout.strip() == "synthetic commit"


def test_direct_commit_still_rejects_parent_repository(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git("init", cwd=parent)
    vault = parent / "vault"
    vault.mkdir()
    _, git = make_uninitialized_services(vault)
    (vault / "notes.md").write_text("synthetic\n", encoding="utf-8")

    with pytest.raises(GitError) as caught:
        git.repo.commit("synthetic commit")

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
    service, git = make_services(tmp_path)
    relative = "content/" + relative
    path = git.repo.path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic\n", encoding="utf-8")

    git.repo.stage_all()

    assert relative in git.repo.staged_paths()
    assert relative in git.repo.sensitive_paths()


@pytest.mark.parametrize(
    "relative",
    ["notes.md", "notes with space.md", "weird[1].md", "memories/2026-01.md"],
)
def test_normal_files_are_not_flagged_as_sensitive(tmp_path, relative):
    service, git = make_services(tmp_path)
    relative = "content/" + relative
    path = git.repo.path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic\n", encoding="utf-8")

    git.repo.stage_all()

    assert relative in git.repo.staged_paths()
    assert git.repo.sensitive_paths() == []


def test_init_vault_refuses_untracked_sensitive_file(tmp_path):
    vault = tmp_path / "vault" / "export"
    vault.mkdir(parents=True)
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    service, git = make_uninitialized_services(vault.parent)

    with pytest.raises(GitError) as caught:
        initialize_vault(git, service.initialize_files)

    assert caught.value.kind == "sensitive"
    assert (vault / ".env").exists()
    assert _git("rev-list", "--count", "HEAD", cwd=vault, check=False).returncode != 0


def test_init_vault_refuses_pre_staged_sensitive_file(tmp_path):
    vault = tmp_path / "vault" / "export"
    vault.mkdir(parents=True)
    _git("init", cwd=vault)
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    _git("add", ".env", cwd=vault)
    service, git = make_uninitialized_services(vault.parent)

    with pytest.raises(GitError) as caught:
        initialize_vault(git, service.initialize_files)

    assert caught.value.kind == "sensitive"
    assert (vault / ".env").exists()
    staged = [
        item
        for item in _git("diff", "--cached", "--name-only", "-z", cwd=vault).stdout.split("\x00")
        if item
    ]
    assert ".env" in staged
    assert _git("rev-list", "--count", "HEAD", cwd=vault, check=False).returncode != 0


def test_init_vault_refuses_staged_sensitive_file_even_if_gitignored(tmp_path):
    vault = tmp_path / "vault" / "export"
    vault.mkdir(parents=True)
    _git("init", cwd=vault)
    (vault / ".gitignore").write_text("*.env\n", encoding="utf-8")
    (vault / ".env").write_text("API_KEY=synthetic\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=vault)
    _git("add", "-f", ".env", cwd=vault)
    service, git = make_uninitialized_services(vault.parent)

    with pytest.raises(GitError) as caught:
        initialize_vault(git, service.initialize_files)

    assert caught.value.kind == "sensitive"
    assert ".env" in caught.value.args[0]
    assert (vault / ".env").exists()
    assert _git("rev-list", "--count", "HEAD", cwd=vault, check=False).returncode != 0


def test_init_vault_commits_normal_files(tmp_path):
    vault = tmp_path / "vault" / "export"
    vault.mkdir(parents=True)
    (vault / "notes.md").write_text("# synthetic\n", encoding="utf-8")
    service, git = make_uninitialized_services(vault.parent)

    result = initialize_vault(git, service.initialize_files)

    assert result["commit"]
    tracked = _git("ls-files", cwd=vault).stdout.splitlines()
    assert "notes.md" not in tracked
    assert "content/inferences.jsonl" in tracked
    assert not any(name.endswith(".env") for name in tracked)


def test_sync_never_calls_llm(monkeypatch, tmp_path):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        "syvrio.infra.vault.sync.summarize_diff",
        lambda *args, **kwargs: calls.append(args) or "should not be used",
    )
    service.note("自动同步不调用模型", topic="misc")

    result = git.sync("scheduled", now=datetime(2026, 11, 2, 3, 0, tzinfo=TZ))

    assert result.status == "success"
    assert result.committed
    assert calls == []


def test_sync_reports_real_stage_progress(tmp_path):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path)
    service.note("阶段测试", topic="misc")
    snapshots: list[dict[str, str]] = []

    def progress(stages):
        snapshots.append({stage.name: stage.state for stage in stages})

    result = git.sync("manual", progress)

    assert result.status == "success"
    assert snapshots
    assert any(states[STAGE_CHECK] == "running" for states in snapshots)
    assert snapshots[-1][STAGE_CHECK] == "done"
    assert snapshots[-1][STAGE_COMMIT] == "done"
    assert snapshots[-1][STAGE_PUSH] == "done"


def test_state_file_and_locks_are_gitignored(tmp_path):
    service, git = make_services(tmp_path)
    service.note("状态文件不应入库", topic="misc")

    git.sync("manual")

    status = _git("status", "--porcelain", cwd=service.config.vault / "export").stdout
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
    _, git = make_services(tmp_path)
    before = datetime(2026, 11, 2, 2, 59, tzinfo=TZ)
    at_time = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)

    assert not git.due(before)
    assert git.due(at_time)

    result = git.sync("scheduled", now=at_time)
    assert result.status == "partial"  # 未配置 remote，但本地提交成功
    state = read_state(git)
    assert state.get("last_daily_success_date") != "2026-11-02"
    assert state["last_daily_attempt_date"] == "2026-11-02"
    assert state["last_daily_blocked_date"] == "2026-11-02"
    assert not git.due(datetime(2026, 11, 2, 23, 0, tzinfo=TZ))
    assert git.due(datetime(2026, 11, 3, 3, 0, tzinfo=TZ))


def test_due_is_disabled_and_retry_after_is_respected(tmp_path, monkeypatch):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path)
    at_time = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    attempts = {"fail": True}

    def push():
        if attempts["fail"]:
            raise GitError("Could not resolve host: example.invalid", kind="network")
        return ""

    monkeypatch.setattr(git.repo, "push", push)
    service.note("网络失败", topic="misc")
    result = git.sync("scheduled", now=at_time)

    assert result.error_kind == "network"
    state = read_state(git)
    assert "last_daily_success_date" not in state
    assert git.next_sync_at(at_time) == at_time  # 已到期但等待重试
    assert not git.due(at_time + timedelta(minutes=29))
    assert git.due(at_time + timedelta(minutes=31))

    attempts["fail"] = False
    recovered = git.sync("scheduled", now=at_time + timedelta(minutes=31))
    assert recovered.status == "success"
    assert recovered.pushed
    assert read_state(git)["last_daily_success_date"] == "2026-11-02"

    service.config.git.auto_sync = False
    assert not git.due(datetime(2026, 11, 4, 3, 0, tzinfo=TZ))


def test_next_sync_at_rolls_to_next_day_after_success(tmp_path):
    _, git = make_services(tmp_path)
    at_time = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    git.sync("scheduled", now=at_time)

    assert git.next_sync_at(at_time) == at_time + timedelta(days=1)


# ---------- 并发与跨进程 ----------


def _lock_child(vault: Path, code: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    return subprocess.Popen(
        [sys.executable, "-c", code.format(vault=str(vault))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_git_lock_same_process_contention_causes_busy_result(tmp_path):
    service, git = make_services(tmp_path)
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
        result = git.sync("manual", wait_for_git=False)
        assert result.status == "failed"
        assert result.error_kind == "busy"
    finally:
        release.set()
        thread.join(timeout=5)


def test_git_operation_lock_is_cross_process(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    child = _lock_child(
        vault,
        "import time\nfrom syvrio.infra.storage.locking import git_operation_lock\n"
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
    service, git = make_services(tmp_path)
    child = _lock_child(
        service.config.vault,
        "import time\nfrom syvrio.infra.storage.locking import git_operation_lock\n"
        "lock = git_operation_lock({vault!r})\nlock.acquire()\n"
        "print('locked', flush=True)\ntime.sleep(30)\n",
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        result = git.sync("manual", wait_for_git=False)
        assert result.status == "failed"
        assert result.error_kind == "busy"
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_second_sync_is_rejected_while_first_runs(tmp_path):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path)
    service.note("互斥测试", topic="misc")
    started, release = threading.Event(), threading.Event()

    def slow_push():
        started.set()
        assert release.wait(30)
        return ""

    git.repo.push = slow_push
    result_box: dict[str, object] = {}

    def first_sync():
        result_box["first"] = git.sync("manual")

    thread = threading.Thread(target=first_sync)
    thread.start()
    try:
        assert started.wait(30)
        second = git.sync("manual")
        assert second.status == "failed"
        assert second.error_kind == "busy"
    finally:
        release.set()
        thread.join(timeout=10)
    assert result_box["first"].status == "success"


def test_push_network_wait_does_not_block_memory_writes(tmp_path):
    service, git = make_services(tmp_path)
    add_remote(service, tmp_path)
    service.note("推送前记录", topic="misc")
    started, release = threading.Event(), threading.Event()

    def slow_push():
        started.set()
        assert release.wait(30)
        return ""

    git.repo.push = slow_push
    result_box: dict[str, object] = {}

    def sync():
        result_box["result"] = git.sync("manual")

    thread = threading.Thread(target=sync)
    thread.start()
    try:
        assert started.wait(30)
        # 写入须在模拟网络仍受阻时完成；实际磁盘写入速度因环境而异。
        service.note("推送期间的新记录", topic="misc")
        assert thread.is_alive() and not release.is_set()
    finally:
        release.set()
        thread.join(timeout=10)
    assert result_box["result"].status == "success"
    assert git.is_dirty()  # Push 期间的新写入留给下次同步


def test_session_jsonl_integrity_during_concurrent_sync(tmp_path):
    service, git = make_services(tmp_path)
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
        assert release.wait(30)
        return ""

    git.repo.push = slow_push
    result_box: dict[str, object] = {}
    thread = threading.Thread(target=lambda: result_box.setdefault("result", git.sync("manual")))
    thread.start()
    try:
        assert started.wait(30)
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
        assert thread.is_alive() and not release.is_set()
    finally:
        release.set()
        thread.join(timeout=10)

    result = result_box["result"]
    assert result.status == "success"
    worktree = store.load(session.id).records
    committed_raw = _git(
        "show",
        f"HEAD:content/sessions/{session.id}.jsonl",
        cwd=service.config.vault / "export",
    ).stdout
    committed = [json.loads(line) for line in committed_raw.splitlines()]
    assert all(isinstance(record, dict) for record in worktree)
    assert all(isinstance(record, dict) for record in committed)
    assert len(committed) < len(worktree)


# ---------- 配置 ----------


def test_git_config_load_validate_and_example(tmp_path):
    path = tmp_path / "syvrio.toml"
    path.write_text(
        '[git]\nauto_sync = false\nsync_time = "04:30"\nretry_interval_minutes = 45\n',
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded.git.auto_sync is False
    assert loaded.git.sync_time == "04:30"
    assert loaded.git.retry_interval_minutes == 45

    bad = tmp_path / "bad.toml"
    bad.write_text('[git]\nsync_time = "25:00"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="HH:MM"):
        load_config(bad)

    negative = tmp_path / "negative.toml"
    negative.write_text("[git]\nretry_interval_minutes = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="必须大于 0"):
        load_config(negative)

    example = tmp_path / "example.toml"
    write_example_config(example, tmp_path / "vault")
    text = example.read_text(encoding="utf-8")
    assert "[git]" not in text
    assert load_config(example).git.sync_time == "03:00"
    assert load_config(example).git.auto_sync is True

    from syvrio.infra.config import write_config

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
    _, git = make_services(tmp_path)
    stamp = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    result = git.sync("scheduled", now=stamp)
    assert result.status == "partial"
    assert result.error_kind == "no_remote"
    state = read_state(git)
    assert "last_daily_success_date" not in state
    assert state["last_daily_blocked_date"] == "2026-11-02"
    assert state["last_daily_attempt_date"] == "2026-11-02"
    assert not git.due(stamp + timedelta(hours=1))
    assert git.next_sync_at(stamp) == stamp + timedelta(days=1)
    assert git.due(stamp + timedelta(days=1))
