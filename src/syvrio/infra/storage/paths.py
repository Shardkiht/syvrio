from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from ..errors import SyvrioError
from .files import atomic_write

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class RuntimeBindingError(SyvrioError, ValueError):
    """无法确定 Runtime 属于当前 Vault，或其中仍有不同布局的待处理记录。"""


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
    def attachments_dir(self) -> Path:
        return self.vault_dir.resolve() / "attachments"

    @property
    def state_dir(self) -> Path:
        return self.runtime_dir / "state"

    @property
    def lock_dir(self) -> Path:
        return self.runtime_dir / "locks"

    @property
    def log_dir(self) -> Path:
        return self.runtime_dir / "logs"

    def validate(self) -> None:
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
        for directory in (self.attachments_dir, self.vault_dir / "export"):
            if directory.resolve() != directory:
                raise ValueError("Vault storage directories cannot be redirected.")
        for directory in (
            self.state_dir,
            self.lock_dir,
            self.log_dir,
        ):
            if directory.is_symlink() or not _comparison_path(directory.resolve()).is_relative_to(
                runtime_key
            ):
                raise ValueError("Runtime subdirectories cannot redirect outside Runtime")

    def bind(self) -> None:
        """持有 Runtime 写锁时检查归属，首次启动时记录唯一的 Vault。"""
        self.validate()
        marker = self.state_dir / "vault-binding.json"
        if marker.is_symlink():
            raise RuntimeBindingError("Runtime binding marker cannot be a symlink.")
        try:
            saved = json.loads(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            saved = None
        except ValueError as exc:
            raise RuntimeBindingError(
                "Invalid Runtime binding. Restore the matching marker."
            ) from exc
        else:
            if not (
                isinstance(saved, dict)
                and saved.keys() == {"version", "vault"}
                and type(saved["version"]) is int
                and saved["version"] == 2
                and isinstance(saved["vault"], str)
                and Path(saved["vault"]).is_absolute()
            ):
                raise RuntimeBindingError("Invalid Runtime binding. Restore the matching marker.")
            if _comparison_path(Path(saved["vault"])) != _comparison_path(self.vault_dir):
                raise RuntimeBindingError(
                    "Runtime belongs to a different Vault. Use its matching Runtime."
                )

        if saved is not None:
            return
        atomic_write(
            marker, json.dumps({"version": 2, "vault": str(self.vault_dir)}).encode("utf-8")
        )


def vault_paths(vault: Path) -> SyvrioPaths:
    return SyvrioPaths(Path(vault), runtime_path())
