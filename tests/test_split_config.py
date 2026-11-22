from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from syvrio.infra import config_files
from syvrio.infra.config import ConfigError, load_config, write_config

pytestmark = pytest.mark.config


def test_migrate_then_remove_each_override(tmp_path):
    entry = tmp_path / "config.toml"
    vault = tmp_path / "history"
    entry.write_text(
        f'vault = "{vault.as_posix()}"\nconfidence_threshold = 0.7\n'
        '[llm]\nprovider = "spark"\nmodel = "custom-model"\n'
        "[chat]\ncontext_window = 80000\nauto_recall = 0\nauto_reflect = false\n"
        "keep_recent_tokens = 3000\n"
        "reflect_idle_seconds = 13\nreflect_interval_seconds = 77\n"
        "profile_auto_interval_hours = 123\nprofile_min_cooldown_hours = 4\n"
        'profile_min_changes = 17\n[retrieval]\nmethod = "keyword"\n'
    )
    first = load_config(entry)
    assert first == load_config(entry)
    assert tomllib.loads(entry.read_text()) == {"vault": vault.as_posix()}
    assert tomllib.loads((tmp_path / "chat.toml").read_text()) == {
        "chat": {"context_window": 80000}
    }
    assert tomllib.loads((tmp_path / "memory.toml").read_text()) == {
        "recall": {"auto_recall": 0},
        "dream": {"auto_reflect": False},
        "retrieval": {"method": "keyword"},
    }
    assert (
        first.llm.provider == "spark"
        and first.chat.auto_recall == 0
        and not first.chat.auto_reflect
    )
    (tmp_path / "memory.toml").unlink()
    restored = load_config(entry)
    assert restored.chat.auto_recall == 3 and restored.chat.auto_reflect
    assert restored.chat.context_window == 80000 and restored.llm.model == "custom-model"
    (tmp_path / "models.toml").unlink()
    assert load_config(entry).llm.provider == "deepseek"
    (tmp_path / "chat.toml").unlink()
    assert load_config(entry).chat.context_window == 50000
    assert load_config(entry).vault == vault
    entry.unlink()
    with pytest.raises(ConfigError, match="config.toml"):
        load_config(entry)
    with pytest.raises(ConfigError, match="config.toml"):
        load_config()


def test_remove_retired_chat_override_without_resplitting_existing_files(tmp_path):
    entry = tmp_path / "config.toml"
    chat = tmp_path / "chat.toml"
    model = tmp_path / "models.toml"
    entry.write_text("# Existing split configuration\n")
    model.write_text('# Keep this explicit override\n[llm]\nprovider = "deepseek"\n')
    original_model = model.read_bytes()
    chat.write_text("[chat]\nkeep_recent_tokens = 3000\ncontext_window = 87000\n")

    config = load_config(entry)
    assert config.chat.context_window == 87000
    assert tomllib.loads(chat.read_text()) == {"chat": {"context_window": 87000}}
    assert model.read_bytes() == original_model
    assert not (tmp_path / "memory.toml").exists()
    assert load_config(entry) == config


def test_saving_one_setting_preserves_independent_edits_and_follows_deleted_group(tmp_path):
    entry = tmp_path / "config.toml"
    model = tmp_path / "models.toml"
    model.write_text('# manual note\n[llm]\nmodel = "example"\n')
    cfg = load_config(entry)
    old_bytes = model.read_bytes()
    other = load_config(entry)
    other.chat.cue_limit = 9
    write_config(entry, other)
    cfg.chat.context_window = 123456
    write_config(entry, cfg)
    assert model.read_bytes() == old_bytes
    assert load_config(entry).chat.cue_limit == 9
    model.unlink()
    cfg.chat.context_window = 50000
    write_config(entry, cfg)
    assert not model.exists()
    assert "context_window" not in (tmp_path / "chat.toml").read_text()
    assert load_config(entry).llm.model == "deepseek-flash"


def test_packaged_defaults_are_required_and_used(tmp_path, monkeypatch):
    entry = tmp_path / "config.toml"
    bundle = tmp_path / "bundle"
    defaults = bundle / "defaults"
    defaults.mkdir(parents=True)
    for source in config_files.files("syvrio").joinpath("defaults").iterdir():
        if source.name.endswith(".toml"):
            (defaults / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(config_files, "files", lambda package: bundle)
    memory = defaults / "memory.toml"
    memory.write_text(memory.read_text().replace("default_limit = 10", "default_limit = 17"))
    assert load_config(entry).retrieval.default_limit == 17
    memory.unlink()
    with pytest.raises(ConfigError, match="memory.toml.*修复安装"):
        load_config(entry)


def test_migration_recovers_interrupted_publication(tmp_path, monkeypatch):
    entry = tmp_path / "config.toml"
    entry.write_text('[llm]\nmodel = "legacy"\n[chat]\ncontext_window = 90000\n')
    original = config_files.atomic_write

    def interrupt(path: Path, payload: bytes):
        if path.name == "chat.toml":
            raise OSError("interrupted")
        original(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(config_files, "atomic_write", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            load_config(entry)
    assert "legacy" in entry.read_text()
    saved = load_config(entry)
    assert saved.chat.context_window == 90000 and saved.llm.model == "legacy"
    assert not (tmp_path / ".syvrio-config-transaction.json").exists()


def test_invalid_override_reports_file_and_preserves_legacy_entry(tmp_path):
    entry = tmp_path / "config.toml"
    original = "[chat]\ncontext_window = 88000\n"
    entry.write_text(original)
    (tmp_path / "memory.toml").write_text("[dream]\ndefault_budget = 99\n")
    with pytest.raises(ConfigError, match="memory.toml.*default_budget"):
        load_config(entry)
    assert entry.read_text() == original


def test_restore_default_after_dataclass_replace(tmp_path):
    entry = tmp_path / "config.toml"
    cfg = load_config(entry)
    modified = replace(cfg, llm=replace(cfg.llm, model="example"))
    write_config(entry, modified)
    write_config(entry, replace(modified, llm=cfg.llm))
    assert load_config(entry) == cfg
