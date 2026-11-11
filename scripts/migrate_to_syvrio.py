"""One-shot QuietMemo -> Syvrio data migration.

Default is a dry-run. ``--apply`` backs up the source data, migrates the
Vault metadata directory and config file, rewrites escaped memory tags, and
optionally migrates Keyring entries. The old metadata directory is removed
only after every validation passes; the backup is never removed.

Usage:
    uv run python scripts/migrate_to_syvrio.py --dry-run
    uv run python scripts/migrate_to_syvrio.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

OLD_META = ".quiet-memo"
NEW_META = ".syvrio"
OLD_CONFIG_NAME = "quiet-memo.toml"
NEW_CONFIG_NAME = "syvrio.toml"
OLD_KEYRING_SERVICE = "quiet-memo"
NEW_KEYRING_SERVICE = "syvrio"
OLD_TAG_PREFIX = "~qm-escaped~"
NEW_TAG_PREFIX = "~syvrio-escaped~"
TAG_KEY = r"(?:id|status|supersedes|source|confidence|note|session|message|context|change_kind)"
TAG_RE = re.compile(rf"(`{TAG_KEY}=)([^`]*)`")
CACHE_FILES = frozenset({"vectors.npz", "search-cache.json", "profile-summary.json"})
LOCK_SUFFIX = ".lock"
KNOWN_KEY_NAMES = (
    "DEEPSEEK_API_KEY",
    "SILICONFLOW_API_KEY",
    "DASHSCOPE_API_KEY",
    "SPARK_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "EXA_API_KEY",
    "BAIDU_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "YDC_API_KEY",
    "VOLCENGINE_SEARCH_API_KEY",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TagPlan:
    path: Path
    old_text: str
    new_text: str
    changes: int


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "syvrio"


def default_old_config_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "quiet-memo" / OLD_CONFIG_NAME


def _memory_files(vault: Path) -> list[Path]:
    root = vault / "memories"
    return sorted(root.rglob("*.md")) if root.exists() else []


def _decoded_tag_value(value: str) -> str:
    if value.startswith(OLD_TAG_PREFIX):
        return unquote(value[len(OLD_TAG_PREFIX) :])
    if value.startswith(NEW_TAG_PREFIX):
        return unquote(value[len(NEW_TAG_PREFIX) :])
    return value


def _semantic_line(line: str) -> str:
    return TAG_RE.sub(
        lambda match: match.group(1) + _decoded_tag_value(match.group(2)) + "`",
        line,
    )


def _memory_fingerprint(vault: Path) -> dict[str, object]:
    files = _memory_files(vault)
    lines = 0
    entries = 0
    ids: set[str] = set()
    digest = hashlib.sha256()
    for path in files:
        for raw in path.read_bytes().decode("utf-8").splitlines(keepends=True):
            lines += 1
            digest.update(_semantic_line(raw).encode("utf-8"))
            if raw.startswith("- "):
                entries += 1
            match = re.search(r"`id=([^`]+)`", raw)
            if match:
                ids.add(match.group(1))
    return {
        "files": len(files),
        "lines": lines,
        "entries": entries,
        "ids": ids,
        "digest": digest.hexdigest(),
    }


def _session_fingerprint(vault: Path) -> dict[str, int]:
    root = vault / "sessions"
    files = sorted(root.glob("*.jsonl")) if root.exists() else []
    return {"files": len(files), "lines": sum(len(p.read_bytes().splitlines()) for p in files)}


def _jsonl_stats(path: Path) -> tuple[int, int, int]:
    """Return (lines, valid_json_lines, damaged_tail_lines)."""
    if not path.exists():
        return 0, 0, 0
    raw_lines = path.read_bytes().splitlines()
    valid = 0
    for index, line in enumerate(raw_lines):
        try:
            json.loads(line)
            valid += 1
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index != len(raw_lines) - 1:
                raise MigrationError(f"中间行损坏，拒绝迁移：{path} 第 {index + 1} 行") from None
    return len(raw_lines), valid, len(raw_lines) - valid


def _metadata_fingerprint(directory: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    if not directory.exists():
        return result
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.endswith(LOCK_SUFFIX):
            continue
        if path.suffix == ".jsonl":
            result[path.name] = _jsonl_stats(path)
        else:
            result[path.name] = _sha(path.read_bytes())
    return result


def _plan_tags(vault: Path) -> list[TagPlan]:
    plans: list[TagPlan] = []
    for path in _memory_files(vault):
        old_text = path.read_bytes().decode("utf-8")
        output: list[str] = []
        changes = 0

        def rewrite(line: str) -> str:
            nonlocal changes

            def replace(match: re.Match[str]) -> str:
                nonlocal changes
                value = match.group(2)
                if not value.startswith(OLD_TAG_PREFIX):
                    return match.group(0)
                changes += 1
                return match.group(1) + NEW_TAG_PREFIX + value[len(OLD_TAG_PREFIX) :] + "`"

            return TAG_RE.sub(replace, line)

        for line in old_text.splitlines(keepends=True):
            output.append(rewrite(line))
        new_text = "".join(output)
        if changes:
            if _semantic_digest(old_text) != _semantic_digest(new_text):
                raise MigrationError(f"标签迁移会改变语义，已停止：{path}")
            plans.append(TagPlan(path, old_text, new_text, changes))
    return plans


def _semantic_digest(text: str) -> str:
    digest = hashlib.sha256()
    for line in text.splitlines(keepends=True):
        digest.update(_semantic_line(line).encode("utf-8"))
    return digest.hexdigest()


def _copy_metadata(source: Path, target: Path) -> list[str]:
    skipped: list[str] = []
    target.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.iterdir()):
        if not path.is_file():
            raise MigrationError(f"元数据目录包含未预期的子目录：{path}")
        if path.name.endswith(LOCK_SUFFIX):
            skipped.append(path.name)
            continue
        if path.name in CACHE_FILES:
            skipped.append(path.name)
            continue
        shutil.copy2(path, target / path.name)
    return skipped


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.migrating")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _restore_file(path: Path, original: bytes | None) -> None:
    """恢复到迁移前的状态；原文件不存在时删除本次新建的文件。"""
    if original is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(path, original)


def resolve_vault(cli_path: Path | None, environ: Mapping[str, str] | None = None) -> Path:
    """--vault > 非空 SYVRIO_VAULT > ~/.syvrio/vault；空字符串视为未设置。"""
    if cli_path is not None:
        return Path(cli_path).expanduser()
    values = os.environ if environ is None else environ
    configured = values.get("SYVRIO_VAULT", "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".syvrio" / "vault"


def _backup(vault: Path, source: Path, config: Path | None, backup_root: Path) -> Path:
    backup = backup_root / f"syvrio-migration-backup-{_stamp()}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source, backup / OLD_META)
    for name in ("memories", "sessions"):
        candidate = vault / name
        if candidate.exists():
            shutil.copytree(candidate, backup / name)
    if config is not None and config.exists():
        shutil.copy2(config, backup / config.name)
    manifest = {
        "vault": str(vault),
        "source_meta": str(source),
        "config": str(config) if config else None,
        "memory": {
            k: (sorted(v) if isinstance(v, set) else v)
            for k, v in _memory_fingerprint(vault).items()
        },
        "sessions": _session_fingerprint(vault),
        "metadata": _metadata_fingerprint(source),
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup


def _migrate_config(old: Path, new: Path, apply: bool) -> str:
    if not old.exists():
        return "源配置不存在，跳过"
    if new.exists():
        if old.read_bytes() != new.read_bytes():
            raise MigrationError(f"新旧配置同时存在且内容不同，已停止：{old} / {new}")
        return "新配置已存在且内容一致，跳过"
    try:
        tomllib.loads(old.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise MigrationError(f"源配置无法解析：{exc}") from exc
    if not apply:
        return f"将复制配置：{old} -> {new}"
    new.parent.mkdir(parents=True, exist_ok=True)
    tmp = new.with_name(f".{new.name}.migrating")
    try:
        shutil.copy2(old, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, new)
        if tomllib.loads(new.read_text(encoding="utf-8")) != tomllib.loads(
            old.read_text(encoding="utf-8")
        ):
            raise MigrationError("配置迁移后内容不一致")
    finally:
        tmp.unlink(missing_ok=True)
    return f"已迁移配置：{old} -> {new}"


def _keyring_failure(exc: Exception, migrated: list[str], deleted: list[str]) -> str:
    detail = type(exc).__name__
    if deleted:
        detail += f"；已删除旧条目 {len(deleted)} 个，其余保留"
    elif migrated:
        detail += f"；新条目已写入 {len(migrated)} 个，旧条目未删除"
    if isinstance(exc, MigrationError) and str(exc):
        detail += f"：{exc}"
    return f"Keyring 迁移失败，{detail}"


def _migrate_keyring(apply: bool) -> str:
    try:
        import keyring
    except Exception as exc:
        return f"Keyring 不可用，跳过：{type(exc).__name__}"
    migrated: list[str] = []
    deleted: list[str] = []
    try:
        backend = keyring.get_keyring()
        if type(backend).__module__.startswith("keyring.backends.fail"):
            return "Keyring 后端不可用，跳过（未读取或写入任何凭据）"
        planned: list[str] = []
        conflicts: list[str] = []
        for name in KNOWN_KEY_NAMES:
            try:
                old_value = keyring.get_password(OLD_KEYRING_SERVICE, name)
                new_value = keyring.get_password(NEW_KEYRING_SERVICE, name)
            except Exception as exc:
                return f"Keyring 读取失败，跳过：{type(exc).__name__}"
            if not old_value:
                continue
            if new_value and new_value != old_value:
                conflicts.append(name)
                continue
            planned.append(name)
        if conflicts:
            raise MigrationError("Keyring 新服务名已有不同值，已停止：" + ", ".join(conflicts))
        if not planned:
            return "没有需要迁移的 Keyring 条目"
        if not apply:
            return f"将迁移 {len(planned)} 个 Keyring 条目：{', '.join(planned)}"
        for name in planned:
            keyring.set_password(
                NEW_KEYRING_SERVICE, name, keyring.get_password(OLD_KEYRING_SERVICE, name)
            )
            if keyring.get_password(NEW_KEYRING_SERVICE, name) != keyring.get_password(
                OLD_KEYRING_SERVICE, name
            ):
                raise MigrationError(f"Keyring 校验失败：{name}")
            migrated.append(name)
        for name in planned:
            keyring.delete_password(OLD_KEYRING_SERVICE, name)
            deleted.append(name)
        return f"已迁移 {len(planned)} 个 Keyring 条目：{', '.join(planned)}"
    except MigrationError as exc:
        if not apply:
            raise
        return _keyring_failure(exc, migrated, deleted)
    except Exception as exc:
        return _keyring_failure(exc, migrated, deleted)


def _run_keyring(apply: bool, enabled: bool) -> tuple[str, bool]:
    """Keyring 无法像文件一样回滚，故在文件迁移完成后执行并只报告结果。"""
    if not enabled:
        return "已跳过 Keyring", True
    try:
        message = _migrate_keyring(apply)
    except Exception as exc:
        if not apply:
            # dry-run 阶段的冲突/读取失败必须在写入前暴露。
            raise
        return (
            f"Keyring 迁移未完成（{type(exc).__name__}）；文件迁移已保留，请人工核对。",
            False,
        )
    complete = not message.startswith(("Keyring 迁移失败", "Keyring 读取失败"))
    return message, complete


def migrate(
    vault: Path,
    old_config: Path,
    new_config: Path,
    *,
    apply: bool,
    backup_root: Path | None,
    keyring: bool,
) -> dict[str, object]:
    source = vault / OLD_META
    target = vault / NEW_META
    if source.exists() and target.exists():
        raise MigrationError(
            f"{OLD_META} 与 {NEW_META} 同时存在，已停止；请人工确认后保留一个：{vault}"
        )
    already = target.exists() and not source.exists()
    if not already and not source.exists():
        raise MigrationError(f"未找到 {OLD_META}：{vault}")

    report: dict[str, object] = {
        "vault": str(vault),
        "old_meta": str(source),
        "new_meta": str(target),
        "already_migrated": already,
        "config": None,
        "backup": None,
        "tag_files": 0,
        "tag_values": 0,
        "metadata_files": 0,
        "skipped_files": [],
        "keyring": None,
        "keyring_complete": True,
        "validation": {},
    }
    new_config_before = new_config.read_bytes() if new_config.exists() else None

    if already:
        try:
            report["config"] = _migrate_config(old_config, new_config, apply)
        except Exception:
            _restore_file(new_config, new_config_before)
            raise
        report["keyring"], report["keyring_complete"] = _run_keyring(apply, keyring)
        report["validation"] = {
            "memory": {
                k: (sorted(v) if isinstance(v, set) else v)
                for k, v in _memory_fingerprint(vault).items()
            },
            "sessions": _session_fingerprint(vault),
            "metadata": _metadata_fingerprint(target),
        }
        return report

    before_memory = _memory_fingerprint(vault)
    before_sessions = _session_fingerprint(vault)
    before_meta = _metadata_fingerprint(source)
    tags = _plan_tags(vault)
    report["tag_files"] = len(tags)
    report["tag_values"] = sum(item.changes for item in tags)
    report["metadata_files"] = len(
        [p for p in source.iterdir() if p.is_file() and not p.name.endswith(LOCK_SUFFIX)]
    )
    report["config"] = _migrate_config(old_config, new_config, False)
    report["keyring"] = _migrate_keyring(False) if keyring else "已跳过 Keyring"

    if not apply:
        return report

    backup = _backup(
        vault, source, old_config if old_config.exists() else None, backup_root or vault.parent
    )
    report["backup"] = str(backup)
    staging = target.with_name(f"{NEW_META}.migrating")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        report["skipped_files"] = _copy_metadata(source, staging)
        os.replace(staging, target)
        for plan in tags:
            _atomic_write(plan.path, plan.new_text)
        after_memory = _memory_fingerprint(vault)
        after_sessions = _session_fingerprint(vault)
        after_meta = _metadata_fingerprint(target)
        if {k: (sorted(v) if isinstance(v, set) else v) for k, v in before_memory.items()} != {
            k: (sorted(v) if isinstance(v, set) else v) for k, v in after_memory.items()
        }:
            raise MigrationError("记忆条目/ID 校验失败，旧元数据已保留")
        if before_sessions != after_sessions:
            raise MigrationError("会话数量校验失败，旧元数据已保留")
        expected = {name: value for name, value in before_meta.items() if name not in CACHE_FILES}
        if expected != after_meta:
            raise MigrationError("元数据文件校验失败，旧元数据已保留")
        shutil.rmtree(source)
        report["config"] = _migrate_config(old_config, new_config, True)
    except Exception as exc:
        restore_errors: list[Exception] = []
        # 记忆文件按迁移前字节恢复；不使用备份目录内容做移动，保证备份可独立回滚。
        for plan in tags:
            try:
                _atomic_write(plan.path, plan.old_text)
            except Exception as error:
                restore_errors.append(error)
        try:
            _restore_file(new_config, new_config_before)
        except Exception as error:
            restore_errors.append(error)
        try:
            if target.exists():
                shutil.rmtree(target)
            if (backup / OLD_META).exists():
                shutil.copytree(backup / OLD_META, source, dirs_exist_ok=True)
        except Exception as error:
            restore_errors.append(error)
        try:
            if staging.exists():
                shutil.rmtree(staging)
        except Exception as error:
            restore_errors.append(error)
        if restore_errors:
            detail = ", ".join(type(error).__name__ for error in restore_errors)
            raise MigrationError(
                f"迁移失败且恢复不完整（{type(exc).__name__}）；需人工检查：{detail}"
            ) from exc
        raise
    report["keyring"], report["keyring_complete"] = _run_keyring(True, keyring)
    report["validation"] = {
        "memory": {k: (sorted(v) if isinstance(v, set) else v) for k, v in after_memory.items()},
        "sessions": after_sessions,
        "metadata": after_meta,
    }
    return report


def _print_report(report: dict[str, object], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] vault: {report['vault']}")
    print(f"  old metadata: {report['old_meta']}")
    print(f"  new metadata: {report['new_meta']}")
    if report["already_migrated"]:
        print("  already migrated; no file changes")
    else:
        print(f"  metadata files: {report['metadata_files']}")
        print(f"  tag files: {report['tag_files']}, tag values: {report['tag_values']}")
        if report["skipped_files"]:
            print(f"  skipped (locks/caches): {', '.join(report['skipped_files'])}")
    print(f"  config: {report['config']}")
    print(f"  keyring: {report['keyring']}")
    if report["backup"]:
        print(f"  backup: {report['backup']}")
    validation = report.get("validation") or {}
    if validation:
        memory = validation.get("memory", {})
        print(
            f"  validated: files={memory.get('files')} entries={memory.get('entries')} "
            f"sessions={validation.get('sessions')}"
        )
    print("  no secret values are printed; backup is retained for rollback")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate QuietMemo data to Syvrio")
    parser.add_argument("--vault", type=Path, help="Vault path (default: SYVRIO_VAULT)")
    parser.add_argument("--config", type=Path, help="source config path")
    parser.add_argument("--new-config", type=Path, help="target config path")
    parser.add_argument("--backup-dir", type=Path, help="backup parent directory")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan only")
    parser.add_argument("--skip-keyring", action="store_true", help="do not inspect Keyring")
    args = parser.parse_args(argv)
    apply = args.apply and not args.dry_run
    vault = resolve_vault(args.vault)
    old_config = (args.config or default_old_config_path()).expanduser()
    new_config = (args.new_config or _config_dir() / NEW_CONFIG_NAME).expanduser()
    if args.backup_dir:
        backup_root = args.backup_dir.expanduser()
    else:
        backup_root = vault.parent
    try:
        report = migrate(
            vault,
            old_config,
            new_config,
            apply=apply,
            backup_root=backup_root,
            keyring=not args.skip_keyring,
        )
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_report(report, apply)
    if apply and report.get("keyring_complete") is False:
        print(
            "WARNING: file migration is complete and was kept, but Keyring migration "
            "is incomplete; review the reported entries manually.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
