from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def syvrio_home() -> Path:
    return Path.home() / ".syvrio"


def runtime_path(value: str = "") -> Path:
    return (
        Path(os.environ.get("SYVRIO_RUNTIME") or value or syvrio_home() / "runtime")
        .expanduser()
        .resolve()
    )


@dataclass(frozen=True, slots=True)
class SyvrioPaths:
    vault_dir: Path
    runtime_dir: Path
    config_file: Path | None = None

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
        vault, runtime = self.vault_dir.resolve(), self.runtime_dir.resolve()
        if vault.is_relative_to(runtime) or runtime.is_relative_to(vault):
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
            if directory.is_symlink() or not directory.resolve().is_relative_to(runtime):
                raise ValueError("Runtime subdirectories cannot redirect outside Runtime")

    def bind(self) -> None:
        """Called under Git and Vault locks. Only known, vault-derived files are invalidated."""
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
