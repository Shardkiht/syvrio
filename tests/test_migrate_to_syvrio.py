from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate_to_syvrio.py"
_spec = importlib.util.spec_from_file_location("migrate_to_syvrio", SCRIPT)
migrate_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["migrate_to_syvrio"] = migrate_mod
_spec.loader.exec_module(migrate_mod)


def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "memories").mkdir(parents=True)
    (vault / "sessions").mkdir()
    meta = vault / ".quiet-memo"
    meta.mkdir()
    (meta / "pending.jsonl").write_text('{"id": "p1", "text": "合成候选"}\n', encoding="utf-8")
    (meta / "audit.jsonl").write_text('{"event": "a1"}\n', encoding="utf-8")
    (meta / "vectors.npz").write_bytes(b"cache")
    (meta / "vault.lock").write_bytes(b"")
    (vault / "sessions" / "s-1.jsonl").write_text('{"type": "meta"}\n', encoding="utf-8")
    (vault / "memories" / "01-test.md").write_text(
        "---\ntopic: test\ntitle: Test\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n"
        "- 2026-01-01T00:00:00 — 普通条目 `id=e1` `status=active`\n"
        "- 2026-01-01T00:00:01 — 特殊条目 `id=e2` "
        "`source=~qm-escaped~a%60b%0A` `note=~qm-escaped~x%7Ey`\n",
        encoding="utf-8",
    )
    return vault


def make_config(tmp_path: Path) -> Path:
    config = tmp_path / "quiet-memo.toml"
    config.write_text('vault = "~/vault"\n', encoding="utf-8")
    return config


def run(vault: Path, config: Path, new_config: Path, *, apply: bool, keyring: bool = False):
    return migrate_mod.migrate(
        vault, config, new_config, apply=apply,
        backup_root=vault.parent, keyring=keyring,
    )


def test_dry_run_does_not_touch_data(tmp_path):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"
    report = run(vault, config, new_config, apply=False)
    assert report["tag_values"] == 2
    assert (vault / ".quiet-memo").exists()
    assert not (vault / ".syvrio").exists()
    assert not new_config.exists()
    assert "~qm-escaped~" in (vault / "memories" / "01-test.md").read_text(encoding="utf-8")


def test_apply_migrates_metadata_tags_and_config(tmp_path):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"
    report = run(vault, config, new_config, apply=True)
    assert not (vault / ".quiet-memo").exists()
    assert (vault / ".syvrio" / "pending.jsonl").exists()
    assert (vault / ".syvrio" / "audit.jsonl").exists()
    assert not (vault / ".syvrio" / "vectors.npz").exists()
    assert not (vault / ".syvrio" / "vault.lock").exists()
    text = (vault / "memories" / "01-test.md").read_text(encoding="utf-8")
    assert "~qm-escaped~" not in text
    assert text.count("~syvrio-escaped~") == 2
    assert "`id=e1`" in text and "`id=e2`" in text
    pending = json.loads((vault / ".syvrio" / "pending.jsonl").read_text(encoding="utf-8"))
    assert pending == {"id": "p1", "text": "合成候选"}
    assert new_config.exists()
    if sys.platform != "win32":
        assert (new_config.stat().st_mode & 0o777) == 0o600
    assert Path(report["backup"]).exists()
    assert (Path(report["backup"]) / ".quiet-memo" / "pending.jsonl").exists()


def test_repeat_apply_is_idempotent(tmp_path):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"
    run(vault, config, new_config, apply=True)
    before = (vault / ".syvrio" / "pending.jsonl").read_bytes()
    report = run(vault, config, new_config, apply=True)
    assert report["already_migrated"] is True
    assert (vault / ".syvrio" / "pending.jsonl").read_bytes() == before
    assert not (vault / ".quiet-memo").exists()


def test_conflicting_metadata_directories_stop(tmp_path):
    vault = make_vault(tmp_path)
    (vault / ".syvrio").mkdir()
    with pytest.raises(migrate_mod.MigrationError, match="同时存在"):
        run(vault, make_config(tmp_path), tmp_path / "new.toml", apply=False)


def test_failure_keeps_old_metadata(tmp_path):
    vault = make_vault(tmp_path)
    (vault / ".quiet-memo" / "unexpected-dir").mkdir()
    with pytest.raises(migrate_mod.MigrationError, match="子目录"):
        run(vault, make_config(tmp_path), tmp_path / "new.toml", apply=True)
    assert (vault / ".quiet-memo" / "pending.jsonl").exists()
    assert not (vault / ".syvrio").exists()


def test_middle_jsonl_corruption_stops(tmp_path):
    vault = make_vault(tmp_path)
    (vault / ".quiet-memo" / "pending.jsonl").write_text(
        '{"id": "p1"}\nnot-json\n{"id": "p2"}\n', encoding="utf-8"
    )
    with pytest.raises(migrate_mod.MigrationError, match="中间行损坏"):
        run(vault, make_config(tmp_path), tmp_path / "new.toml", apply=False)


def test_config_conflict_stops(tmp_path):
    vault = make_vault(tmp_path)
    old = make_config(tmp_path)
    new = tmp_path / "syvrio.toml"
    new.write_text('vault = "other"\n', encoding="utf-8")
    with pytest.raises(migrate_mod.MigrationError, match="内容不同"):
        run(vault, old, new, apply=False)


class _Backend:
    def __init__(self, store):
        self.store = store


def fake_keyring(monkeypatch):
    store = {("quiet-memo", "DEEPSEEK_API_KEY"): "synthetic-secret"}
    module = SimpleNamespace(
        store=store,
        get_keyring=lambda: _Backend(store),
        get_password=lambda service, name: store.get((service, name)),
        set_password=lambda service, name, value: store.__setitem__((service, name), value),
        delete_password=lambda service, name: store.pop((service, name), None),
    )
    monkeypatch.setitem(__import__("sys").modules, "keyring", module)
    return module


def test_keyring_dry_run_and_apply(tmp_path, monkeypatch):
    fake = fake_keyring(monkeypatch)
    assert "1" in migrate_mod._migrate_keyring(False)
    assert ("syvrio", "DEEPSEEK_API_KEY") not in fake.store
    text = migrate_mod._migrate_keyring(True)
    assert "1" in text
    assert fake.store[("syvrio", "DEEPSEEK_API_KEY")] == "synthetic-secret"
    assert ("quiet-memo", "DEEPSEEK_API_KEY") not in fake.store
    assert "synthetic-secret" not in text


def test_keyring_partial_failure_reports_actual_progress(tmp_path, monkeypatch):
    fake = fake_keyring(monkeypatch)
    fake.store[("quiet-memo", "SILICONFLOW_API_KEY")] = "second-synthetic"
    calls = {"deletes": 0}
    real_delete = fake.delete_password

    def flaky_delete(service, name):
        calls["deletes"] += 1
        if calls["deletes"] == 2:
            raise RuntimeError("synthetic")
        return real_delete(service, name)

    fake.delete_password = flaky_delete
    text = migrate_mod._migrate_keyring(True)

    assert text.startswith("Keyring 迁移失败")
    assert "已删除旧条目 1 个" in text
    assert "synthetic" not in text
    assert fake.store[("syvrio", "DEEPSEEK_API_KEY")] == "synthetic-secret"
    assert fake.store[("syvrio", "SILICONFLOW_API_KEY")] == "second-synthetic"
    assert ("quiet-memo", "SILICONFLOW_API_KEY") in fake.store


def test_keyring_conflict_stops(tmp_path, monkeypatch):
    fake = fake_keyring(monkeypatch)
    fake.store[("syvrio", "DEEPSEEK_API_KEY")] = "different"
    with pytest.raises(migrate_mod.MigrationError, match="不同值"):
        migrate_mod._migrate_keyring(False)


# ---------- 迁移失败恢复 ----------


def _backups(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("syvrio-migration-backup-*"))


def test_validation_failure_restores_tags_metadata_and_config(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"
    memory_path = vault / "memories" / "01-test.md"
    memory_before = memory_path.read_bytes()
    meta_before = {
        path.name: path.read_bytes()
        for path in (vault / ".quiet-memo").iterdir()
        if path.is_file()
    }
    real = migrate_mod._memory_fingerprint
    calls = {"count": 0}

    def fingerprint(path):
        calls["count"] += 1
        current = real(path)
        if calls["count"] >= 3:  # before / backup manifest / after tags
            return {**current, "digest": current["digest"] + "changed"}
        return current

    monkeypatch.setattr(migrate_mod, "_memory_fingerprint", fingerprint)
    with pytest.raises(migrate_mod.MigrationError, match="记忆条目/ID"):
        run(vault, config, new_config, apply=True)

    assert memory_path.read_bytes() == memory_before
    assert (vault / ".quiet-memo").exists()
    assert {
        path.name: path.read_bytes()
        for path in (vault / ".quiet-memo").iterdir()
        if path.is_file()
    } == meta_before
    assert not (vault / ".syvrio").exists()
    assert not new_config.exists()
    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert (backups[0] / ".quiet-memo" / "pending.jsonl").read_bytes() == meta_before["pending.jsonl"]


def test_config_failure_after_metadata_removal_restores_source(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"
    memory_path = vault / "memories" / "01-test.md"
    memory_before = memory_path.read_bytes()
    pending_before = (vault / ".quiet-memo" / "pending.jsonl").read_bytes()
    real = migrate_mod._migrate_config

    def failing(old, new, apply):
        if apply:
            raise migrate_mod.MigrationError("合成配置迁移失败")
        return real(old, new, apply)

    monkeypatch.setattr(migrate_mod, "_migrate_config", failing)
    with pytest.raises(migrate_mod.MigrationError, match="合成配置迁移失败"):
        run(vault, config, new_config, apply=True)

    assert memory_path.read_bytes() == memory_before
    assert (vault / ".quiet-memo" / "pending.jsonl").read_bytes() == pending_before
    assert not (vault / ".syvrio").exists()
    assert not new_config.exists()
    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert (backups[0] / ".quiet-memo" / "pending.jsonl").read_bytes() == pending_before


def test_keyring_partial_failure_keeps_file_migration_and_reports(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"

    def failing_keyring(apply):
        if not apply:
            return "dry"
        raise RuntimeError("synthetic")

    monkeypatch.setattr(migrate_mod, "_migrate_keyring", failing_keyring)
    report = run(vault, config, new_config, apply=True, keyring=True)

    assert report["keyring_complete"] is False
    assert "未完成" in report["keyring"]
    assert (vault / ".syvrio").exists()
    assert not (vault / ".quiet-memo").exists()
    text = (vault / "memories" / "01-test.md").read_text(encoding="utf-8")
    assert "~qm-escaped~" not in text and "~syvrio-escaped~" in text
    assert report["validation"]


# ---------- 默认 Vault 路径 ----------


def _cli(vault: Path, config: Path, new_config: Path, backup_dir: Path, *options: str) -> int:
    return migrate_mod.main([
        "--vault", str(vault),
        "--config", str(config),
        "--new-config", str(new_config),
        "--backup-dir", str(backup_dir),
        *options,
    ])


def test_cli_partial_keyring_failure_returns_nonzero_and_keeps_files(
    tmp_path, monkeypatch, capsys
):
    fake = fake_keyring(monkeypatch)
    fake.store[("quiet-memo", "SILICONFLOW_API_KEY")] = "second-synthetic"
    real_delete = fake.delete_password
    calls = {"deletes": 0}

    def flaky_delete(service, name):
        calls["deletes"] += 1
        if calls["deletes"] == 2:
            raise RuntimeError("synthetic")
        return real_delete(service, name)

    fake.delete_password = flaky_delete
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"

    code = _cli(vault, config, new_config, tmp_path, "--apply")
    output = capsys.readouterr()

    assert code == 2
    assert "Keyring 迁移失败" in output.out + output.err
    assert "file migration is complete" in output.err
    assert "synthetic-secret" not in output.out + output.err
    assert "second-synthetic" not in output.out + output.err
    assert (vault / ".syvrio").exists()
    assert not (vault / ".quiet-memo").exists()
    assert new_config.exists()


def test_cli_already_migrated_reports_keyring_failure(tmp_path, monkeypatch, capsys):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"
    assert _cli(vault, config, new_config, tmp_path, "--apply", "--skip-keyring") == 0
    capsys.readouterr()

    fake = fake_keyring(monkeypatch)

    def fail_set(service, name, value):
        raise RuntimeError("synthetic")

    fake.set_password = fail_set
    code = _cli(vault, config, new_config, tmp_path, "--apply")
    output = capsys.readouterr()

    assert code == 2
    assert "already migrated" in output.out
    assert "Keyring 迁移失败" in output.out + output.err
    assert "synthetic-secret" not in output.out + output.err


def test_cli_keyring_success_returns_zero(tmp_path, monkeypatch, capsys):
    fake = fake_keyring(monkeypatch)
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"

    code = _cli(vault, config, new_config, tmp_path, "--apply")
    output = capsys.readouterr()

    assert code == 0
    assert "synthetic-secret" not in output.out + output.err
    assert fake.store[("syvrio", "DEEPSEEK_API_KEY")] == "synthetic-secret"
    assert ("quiet-memo", "DEEPSEEK_API_KEY") not in fake.store
    assert (vault / ".syvrio").exists()
    assert new_config.exists()


def test_cli_skip_keyring_returns_zero(tmp_path, monkeypatch, capsys):
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"

    code = _cli(vault, config, new_config, tmp_path, "--apply", "--skip-keyring")
    output = capsys.readouterr()

    assert code == 0
    assert "已跳过 Keyring" in output.out
    assert (vault / ".syvrio").exists()


def test_cli_dry_run_writes_nothing_and_returns_zero(tmp_path, monkeypatch, capsys):
    fake = fake_keyring(monkeypatch)
    before = dict(fake.store)
    vault = make_vault(tmp_path)
    config = make_config(tmp_path)
    new_config = tmp_path / "config" / "syvrio.toml"

    code = _cli(vault, config, new_config, tmp_path)
    output = capsys.readouterr()

    assert code == 0
    assert "DRY-RUN" in output.out
    assert "~qm-escaped~" in (vault / "memories" / "01-test.md").read_text(encoding="utf-8")
    assert not (vault / ".syvrio").exists()
    assert not new_config.exists()
    assert fake.store == before


def test_resolve_vault_priority(tmp_path):
    explicit = Path("synthetic-vault")
    assert migrate_mod.resolve_vault(explicit, {}) == explicit
    assert migrate_mod.resolve_vault(None, {"SYVRIO_VAULT": str(tmp_path / "env-vault")}) == (
        tmp_path / "env-vault"
    )
    default = Path.home() / ".syvrio" / "vault"
    assert migrate_mod.resolve_vault(None, {}) == default
    assert migrate_mod.resolve_vault(None, {"SYVRIO_VAULT": ""}) == default
