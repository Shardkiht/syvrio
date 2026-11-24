from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from syvrio.infra.config import ConfigError, load_config, write_config
from syvrio.infra.config import files as config_files

pytestmark = pytest.mark.config


def test_removing_group_overrides_restores_only_their_defaults(tmp_path):
    entry = tmp_path / "config.toml"
    vault = tmp_path / "history"
    entry.write_text(f'vault = "{vault.as_posix()}"\n', encoding="utf-8")
    (tmp_path / "models.toml").write_text(
        '[llm]\nprovider = "spark"\nmodel = "custom-model"\n', encoding="utf-8"
    )
    (tmp_path / "chat.toml").write_text("[chat]\ncontext_window = 80000\n", encoding="utf-8")
    (tmp_path / "memory.toml").write_text(
        "[recall]\nauto_recall = 0\n[dream]\nauto_reflect = false\n", encoding="utf-8"
    )
    first = load_config(entry)
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


def test_dream_window_round_trip_and_deleted_override(tmp_path):
    entry = tmp_path / "config.toml"
    memory = tmp_path / "memory.toml"
    cfg = load_config(entry)
    cfg.dream.context_window = 128000
    cfg.chat.context_window = 64000
    write_config(entry, cfg)
    assert tomllib.loads(memory.read_text(encoding="utf-8")) == {
        "dream": {"context_window": 128000}
    }
    restored = load_config(entry)
    assert (restored.dream.context_window, restored.chat.context_window) == (128000, 64000)
    memory.unlink()
    restored = load_config(entry)
    assert (restored.dream.context_window, restored.chat.context_window) == (200000, 64000)
    memory.write_text("[dream]\ncontext_window = 0\n")
    with pytest.raises(ConfigError, match=r"dream\.context_window"):
        load_config(entry)


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
    assert "context_window" not in (tmp_path / "chat.toml").read_text(encoding="utf-8")
    assert load_config(entry).llm.model == "deepseek-flash"


def test_packaged_defaults_are_required_and_used(tmp_path, monkeypatch):
    entry = tmp_path / "config.toml"
    bundle = tmp_path / "bundle"
    defaults = bundle / "defaults"
    defaults.mkdir(parents=True)
    for source in config_files.files("syvrio.infra.config").joinpath("defaults").iterdir():
        if source.name.endswith(".toml"):
            (defaults / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(config_files, "files", lambda package: bundle)
    memory = defaults / "memory.toml"
    memory.write_text(
        memory.read_text(encoding="utf-8").replace("default_limit = 10", "default_limit = 17")
    )
    assert load_config(entry).retrieval.default_limit == 17
    memory.unlink()
    with pytest.raises(ConfigError, match="memory.toml.*修复安装"):
        load_config(entry)


def test_config_read_completes_interrupted_save(tmp_path, monkeypatch):
    entry = tmp_path / "config.toml"
    config = load_config(entry)
    config.llm.model = "new-model"
    config.chat.context_window = 90000
    original = config_files.atomic_write

    def interrupt(path: Path, payload: bytes):
        if path.name == "chat.toml":
            raise OSError("interrupted")
        original(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(config_files, "atomic_write", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            write_config(entry, config)
    saved = load_config(entry)
    assert saved.chat.context_window == 90000 and saved.llm.model == "new-model"
    assert not (tmp_path / ".syvrio-config-transaction.json").exists()


def test_restore_default_after_dataclass_replace(tmp_path):
    entry = tmp_path / "config.toml"
    cfg = load_config(entry)
    modified = replace(cfg, llm=replace(cfg.llm, model="example"))
    write_config(entry, modified)
    write_config(entry, replace(modified, llm=cfg.llm))
    assert load_config(entry) == cfg
