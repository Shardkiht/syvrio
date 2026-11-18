from __future__ import annotations

import sys
import tomllib
from importlib import metadata
from pathlib import Path

import syvrio
from syvrio.app.chat_widgets import ASSISTANT_NAME
from syvrio.infra.config import (
    CONFIG_FILENAME,
    KEYRING_SERVICE,
    Config,
    LLMConfig,
    RetrievalConfig,
    default_config_path,
    default_vault_path,
)
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import ExtractionResult
from syvrio.memory.service import MemoryService


def test_only_syvrio_console_script():
    scripts = {entry.name: entry.value for entry in metadata.entry_points(group="console_scripts")}
    assert scripts["syvrio"] == "syvrio.app.cli:main"


def test_version_and_assistant_branding():
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert syvrio.__version__ == project["project"]["version"]
    assert ASSISTANT_NAME == "Syvrio"


def _isolate_config_dir(monkeypatch, tmp_path: Path) -> None:
    if sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _expected_config_path(tmp_path: Path) -> Path:
    base = tmp_path / "appdata" if sys.platform == "win32" else tmp_path / "xdg"
    return base / "syvrio" / "syvrio.toml"


def test_default_paths_are_syvrio(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SYVRIO_CONFIG", raising=False)
    monkeypatch.delenv("SYVRIO_VAULT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _isolate_config_dir(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert default_config_path() == tmp_path / ".syvrio" / "config.toml"
    assert default_vault_path() == tmp_path / ".syvrio" / "vault"
    assert CONFIG_FILENAME == "syvrio.toml"


def test_setup_defaults_to_config_directory(monkeypatch, tmp_path: Path):
    from syvrio.infra.providers import resolve_config_path

    monkeypatch.delenv("SYVRIO_CONFIG", raising=False)
    _isolate_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    assert resolve_config_path(None) == tmp_path / ".syvrio" / "config.toml"


def test_keyring_service_name():
    assert KEYRING_SERVICE == "syvrio"


def test_runtime_metadata_uses_syvrio_directory(tmp_path: Path):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        retrieval=RetrievalConfig(method="keyword"),
    )
    service = MemoryService(config, extractor=StaticExtractor(ExtractionResult(memories=[])))
    service.init_vault()
    assert service.paths.state_dir.is_dir()
    assert not (config.vault / ".syvrio").exists()
    service.note("品牌测试条目", topic="misc")
    assert (config.vault / "memories" / "general" / "misc.md").exists()
