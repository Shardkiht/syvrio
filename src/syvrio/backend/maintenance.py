"""仅供本机运维入口调用，不向网页暴露文件系统路径。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..infra.config import (
    Config,
    default_config_path,
    default_vault_path,
    load_config,
)
from ..infra.config.files import ConfigFiles
from ..infra.errors import SyvrioError
from ..infra.storage import paths as local_paths
from ..infra.storage.locking import LockTimeout, vault_write_lock
from ..infra.storage.paths import SyvrioPaths, runtime_path
from ..infra.vault.files import initialize_vault as provision_vault
from ..infra.vault.sync import GitSyncService
from .ownership import BackendLease, physical_vault_lock
from .updates.runtime_layout import prepare_runtime_upgrade


class MaintenanceError(SyvrioError, ValueError):
    pass


def prepare_vault_runtime(paths: SyvrioPaths) -> None:
    """调用方持有后端租约，在写入前核对部署目录并恢复未完成的事务。"""
    with vault_write_lock(paths):
        paths.bind()
        prepare_runtime_upgrade(paths)


def initialize_owned_vault(
    config: Config, config_path: Path | None = None, *, force: bool = False
) -> dict:
    """调用方持有该物理 Vault 的后端租约。"""
    from ..memory.service import MemoryService

    vault = config.vault
    if vault.exists() and any(vault.iterdir()) and not (vault / ".git").exists() and not force:
        raise MaintenanceError(f"{vault} 非空且不是 Syvrio 记忆库；确认无误后加 --force。")
    paths = config.paths(config_path)
    prepare_vault_runtime(paths)
    memory = MemoryService(config, config_path=config_path)
    return provision_vault(GitSyncService(config, paths), memory.initialize_files)


def initialize_vault(
    config: Config, config_path: Path | None = None, *, force: bool = False
) -> dict:
    with BackendLease(config.paths(config_path)):
        return initialize_owned_vault(config, config_path, force=force)


@contextmanager
def configured_maintenance(
    config_path: Path | None, *, initial_vault: Path | None = None
) -> Iterator[tuple[Config, Path]]:
    """首次部署可指定目录。已有部署的维护操作始终使用配置中的 Vault。"""
    path = (config_path or default_config_path()).expanduser().resolve()
    layout = ConfigFiles(path)
    requested = initial_vault.expanduser().resolve() if initial_vault is not None else None
    stored, runtime = layout.connection_locations(required=False)
    if requested is not None and stored and Path(stored).expanduser().resolve() != requested:
        raise MaintenanceError("此部署已配置 Vault，请使用当前配置重新运行初始化。")
    selected = SyvrioPaths(
        Path(stored or requested or default_vault_path()), runtime_path(runtime), path
    )
    with BackendLease(selected):
        # 获得租约后按最新配置复核目录，防止维护操作写入另一个位置。
        stored, _ = layout.connection_locations(required=False)
        if path.exists() or layout.journal.exists():
            cfg = load_config(path)
        else:
            cfg = Config(vault=default_vault_path())
        if requested is not None and not stored:
            cfg.vault = requested
        if cfg.paths(path) != selected:
            raise MaintenanceError("部署目录已变更，请重新打开配置。")
        yield cfg, path


def initialize_configured_vault(
    config_path: Path | None, vault: Path | None, *, force: bool = False
) -> dict:
    with configured_maintenance(config_path, initial_vault=vault) as (cfg, path):
        result = initialize_owned_vault(cfg, path, force=force)
        ConfigFiles(path).initialize_vault(cfg.vault)
    return {**result, "config_path": str(path)}


def verify_local_attachments(vault: Path) -> tuple[int, list[str]]:
    from ..infra.session.query import SessionQuery
    from .client import ResidentClient
    from .endpoint import client_paths, discover

    if not vault.is_dir():
        raise MaintenanceError(f"未找到待核验的 Vault 目录：{vault}")
    # 离线核验可以读取备份副本。活动目录须交给持有它的后端核验。
    lock = physical_vault_lock(vault)
    try:
        lock.acquire(0)
    except LockTimeout:
        paths = client_paths(None)
        endpoint = discover(paths) if paths.vault_dir == vault.resolve() else None
        if endpoint is None:
            raise MaintenanceError(
                "该 Vault 正被另一个后端使用，请在其配置下核验或先关闭服务。"
            ) from None
        client = ResidentClient.from_endpoint(endpoint)
        checked, errors = client.call("verify_attachments")
        return checked, errors
    try:
        return SessionQuery(vault, offline=True).verify_attachments()
    finally:
        lock.release()


def update_managed(on_progress) -> str:
    from .updates.installer import config_options, update
    from .updates.worker import prepare_restart

    root = local_paths.syvrio_home() / "update"
    try:
        options = config_options(root)
    except FileNotFoundError as exc:
        raise MaintenanceError(
            f"更新配置缺失（{root / 'config.toml'}），请先按 Web 文档配置受管理的服务。"
        ) from exc
    return update(root, *options, prepare_restart=prepare_restart, on_progress=on_progress)
