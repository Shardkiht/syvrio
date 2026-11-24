from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

TURN_UNDO_DIR = "turn-undo"


def syvrio_home() -> Path:
    return Path.home() / ".syvrio"


def runtime_path(value: str = "") -> Path:
    return (
        Path(os.environ.get("SYVRIO_RUNTIME") or value or syvrio_home() / "runtime")
        .expanduser()
        .resolve()
    )


def _comparison_path(path: PurePath) -> PurePath:
    # 同一 Windows 物理路径可能同时以普通路径和扩展路径返回，尤其是末端目录尚不存在时。
    # 仅统一盘符和 UNC 两种别名参与范围比较，实际文件操作仍使用原路径。
    if isinstance(path, PureWindowsPath):
        prefix = "\\\\?\\"
        value = str(path)
        if value.startswith(prefix):
            tail = value[len(prefix) :]
            if tail.upper().startswith("UNC\\"):
                return PureWindowsPath("\\\\" + tail[4:])
            if re.match(r"[a-zA-Z]:\\", tail):
                return PureWindowsPath(tail)
    return path


@dataclass(frozen=True, slots=True)
class SyvrioPaths:
    vault_dir: Path
    runtime_dir: Path
    config_file: Path | None = None

    def __post_init__(self) -> None:
        # 配置中的 Vault 根目录可能是别名，检查其子路径时使用物理根目录。
        object.__setattr__(self, "vault_dir", self.vault_dir.expanduser().resolve())

    @property
    def config_dir(self) -> Path:
        return self.config_file.parent if self.config_file else syvrio_home()

    @property
    def memories_dir(self) -> Path:
        return self.vault_dir / "memories"

    @property
    def sessions_dir(self) -> Path:
        return self.vault_dir / "sessions"

    @property
    def archived_dir(self) -> Path:
        return self.vault_dir / "archived"

    @property
    def archived_sessions_dir(self) -> Path:
        return self.archived_dir / "sessions"

    @property
    def attachments_dir(self) -> Path:
        return self.vault_dir.resolve() / "attachments"

    @property
    def records_dir(self) -> Path:
        return self.vault_dir / "records" / "memory"

    @property
    def index_dir(self) -> Path:
        return self.runtime_dir / "indexes"

    @property
    def cache_dir(self) -> Path:
        return self.runtime_dir / "cache"

    @property
    def state_dir(self) -> Path:
        return self.runtime_dir / "state"

    @property
    def jobs_dir(self) -> Path:
        return self.runtime_dir / "jobs"

    @property
    def lock_dir(self) -> Path:
        return self.runtime_dir / "locks"

    @property
    def log_dir(self) -> Path:
        return self.runtime_dir / "logs"

    def validate(self) -> None:
        if _comparison_path(self.archived_dir.resolve()) != _comparison_path(self.archived_dir):
            raise ValueError("Vault archive directory cannot be redirected")
        runtime = self.runtime_dir.resolve()
        runtime_key = _comparison_path(runtime)
        vault_key = _comparison_path(self.vault_dir.resolve())
        if vault_key.is_relative_to(runtime_key) or runtime_key.is_relative_to(vault_key):
            raise ValueError("Vault and Runtime must be separate directories")
        if any(
            ((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file())
            for parent in (runtime, *runtime.parents)
        ):
            raise ValueError("Runtime cannot be placed inside a Git working tree")
        for directory in (
            self.index_dir,
            self.cache_dir,
            self.state_dir,
            self.jobs_dir,
            self.lock_dir,
            self.log_dir,
            self.cache_dir / "profile",
            self.cache_dir / "search",
            self.jobs_dir / "dreaming",
        ):
            if directory.is_symlink() or not _comparison_path(directory.resolve()).is_relative_to(
                runtime_key
            ):
                raise ValueError("Runtime subdirectories cannot redirect outside Runtime")

    def bind(self) -> None:
        """在 Git 和 Vault 锁内调用，只使已知的 Vault 派生文件失效。"""
        self.validate()
        marker = self.state_dir / "vault-binding.json"
        vault = str(self.vault_dir.resolve())
        if marker.is_symlink():
            raise ValueError("Runtime binding marker cannot be a symlink")
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = None
        except ValueError:
            previous = None
        if previous == {"version": 2, "vault": vault}:
            return
        for path in (
            self.index_dir / "vectors.npz",
            self.cache_dir / "profile" / "profile-summary.json",
            self.state_dir / "fingerprints.json",
            self.state_dir / "reflection.json",
            self.state_dir / "git-sync.json",
        ):
            path.unlink(missing_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.state_dir, delete=False
        ) as handle:
            tmp = Path(handle.name)
            json.dump({"version": 2, "vault": vault}, handle)
        try:
            os.replace(tmp, marker)
        finally:
            tmp.unlink(missing_ok=True)


def vault_paths(vault: Path) -> SyvrioPaths:
    return SyvrioPaths(Path(vault), runtime_path())
