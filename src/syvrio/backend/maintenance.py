"""仅供本机运维入口调用，不向网页暴露文件系统路径。"""

from __future__ import annotations

from pathlib import Path

from ..infra.config import (
    Config,
    default_config_path,
    default_vault_path,
    load_config,
    write_config,
    write_example_config,
)
from ..infra.errors import SyvrioError
from ..infra.storage import paths as local_paths
from ..infra.storage.locking import LockTimeout, git_operation_lock, vault_write_lock
from ..infra.storage.paths import SyvrioPaths
from ..infra.storage.transaction import recover_vault_transaction
from ..infra.vault.files import initialize_vault as provision_vault
from ..infra.vault.sync import GitSyncService
from .ownership import BackendLease, physical_vault_lock


class MaintenanceError(SyvrioError, ValueError):
    pass


def prepare_vault_runtime(paths: SyvrioPaths) -> None:
    """调用方持有后端租约；切换绑定时按 Git、Vault 顺序保护派生状态。"""
    with git_operation_lock(paths), vault_write_lock(paths):
        paths.bind()
        recover_vault_transaction(paths)


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


def initialize_configured_vault(
    config_path: Path | None, vault: Path | None, *, force: bool = False
) -> dict:
    vault = (vault or default_vault_path()).expanduser().resolve()
    path = (config_path or default_config_path()).expanduser().resolve()
    from ..infra.config.files import ConfigFiles
    from .endpoint import client_paths

    layout = ConfigFiles(path)
    saved = path.exists() or layout.journal.exists()
    selected = client_paths(path, vault) if saved else Config(vault=vault).paths(path)
    with BackendLease(selected):
        cfg = load_config(path) if path.exists() or layout.journal.exists() else Config(vault=vault)
        changed = cfg.vault.expanduser().resolve() != vault
        cfg.vault = vault
        if cfg.paths(path) != selected:
            raise MaintenanceError("Vault 或 Runtime 已变更，请重新运行初始化。")
        result = initialize_owned_vault(cfg, path, force=force)
        if not path.exists():
            write_example_config(path, vault)
        elif changed:
            write_config(path, cfg)
    return {**result, "config_path": str(path)}


def verify_local_attachments(vault: Path) -> tuple[int, list[str]]:
    from ..infra.session.query import SessionQuery
    from .client import ResidentClient
    from .endpoint import client_paths, discover

    if not vault.is_dir():
        raise MaintenanceError(f"未找到待核验的 Vault 目录：{vault}")
    # 核验只读取 Vault。其他 Vault 正在使用同一个 Runtime 时，仍可核验迁移副本。
    lock = physical_vault_lock(vault)
    try:
        lock.acquire(0)
    except LockTimeout:
        endpoint = discover(client_paths(None, vault))
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
