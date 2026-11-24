"""在取得独占权后接管旧版 Runtime 的后台进度，日常任务只读写现行目录。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ...infra.storage.paths import RuntimeBindingError, SyvrioPaths
from ...infra.storage.transaction import VaultTransaction, vault_transaction

_DIGEST = re.compile(r"[0-9a-f]{64}")
_JOB = re.compile(r"(context|distillation|dream|title)/[0-9a-f]{64}\.json")
_RECEIPT = "upgrade-0.7-background.json"


def _checked(path: Path) -> Path:
    if path.is_symlink() or path.is_junction():
        raise RuntimeBindingError("Runtime upgrade found a redirected path. Check the backup.")
    return path


def _files(directory: Path) -> list[Path]:
    _checked(directory)
    return list(directory.iterdir()) if directory.exists() else []


def _read(path: Path) -> bytes | None:
    try:
        return _checked(path).read_bytes()
    except FileNotFoundError:
        return None


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _baseline(receipt: Path) -> dict[str, str]:
    saved = _read(receipt)
    if saved is None:
        return {}
    try:
        files = json.loads(saved)
        if isinstance(files, dict) and all(
            _JOB.fullmatch(name) and isinstance(value, str) and _DIGEST.fullmatch(value)
            for name, value in files.items()
        ):
            return files
    except (UnicodeError, ValueError):
        pass
    raise RuntimeBindingError("Invalid Runtime upgrade receipt. Restore the matching backup.")


def _old_jobs(background: Path, owner: str) -> dict[str, Path]:
    jobs = {}
    for group in _files(background):
        if not _DIGEST.fullmatch(group.name):
            continue
        for lane in _files(group):
            if not _checked(lane).is_dir():
                continue
            for source in _files(lane):
                if source.suffix != ".json":
                    continue
                name = f"{lane.name}/{source.name}"
                if (
                    group.name != owner
                    or not _JOB.fullmatch(name)
                    or not _checked(source).is_file()
                ):
                    raise RuntimeBindingError(
                        "Unrecognized background job or Vault owner. Check the old Runtime before upgrading."
                    )
                jobs[name] = source
    return jobs


def prepare_runtime_upgrade(paths: SyvrioPaths) -> None:
    """调用者持有 Vault 锁，后台工作线程尚未启动。"""
    vault = os.path.normcase(str(paths.vault_dir))
    journals = [p for p in _files(paths.jobs_dir / "vault-transactions") if p.suffix == ".json"]
    if any(p.suffix == ".json" for p in _files(paths.jobs_dir / "session-deletions")):
        raise RuntimeBindingError("Restart the previous version to finish its session deletion.")
    if journals:
        expected = hashlib.sha256(os.fsencode(vault)).hexdigest() + ".json"
        old = VaultTransaction(paths)
        if len(journals) != 1 or journals[0].name != expected or old.journal.exists():
            raise RuntimeBindingError(
                "Unmatched transaction journals. Recover with the previous version."
            )
        # 两版日志的格式和校验一致，就地恢复后旧版本也能从同一已提交状态启动。
        old.journal = _checked(journals[0])
        old.recover()

    with vault_transaction(paths) as tx:
        background = paths.jobs_dir / "background-memory"
        owner = hashlib.sha256(vault.encode("utf-8")).hexdigest()
        sources = _old_jobs(background, owner)
        if not (background / owner).is_dir():
            return
        receipt = paths.jobs_dir / _RECEIPT
        before = _baseline(receipt)
        if not sources and not before:
            return
        after = {}
        changes = []
        for name in sorted(sources.keys() | before.keys()):
            if name not in sources and not (background / owner / Path(name).parent).is_dir():
                # 旧作业只删除单个文件。整目录归档或移走不表示取消其中的现行任务。
                after[name] = before[name]
                continue
            source = _read(sources[name]) if name in sources else None
            digest = _digest(source)
            if digest is not None:
                after[name] = digest
            if before.get(name) == digest:
                continue
            target = background / name
            _checked(target.parent)
            current = _digest(_read(target))
            if current not in (digest, before.get(name)):
                raise RuntimeBindingError(
                    f"Conflicting background job {name}. Preserve both versions and reconcile before upgrading."
                )
            if source is not None:
                try:
                    if not isinstance(json.loads(source), dict):
                        raise ValueError
                except (ValueError, UnicodeError) as exc:
                    raise RuntimeBindingError(
                        f"Invalid background job {name}. Restore its backup."
                    ) from exc
            if current != digest:
                changes.append((target, source))
        # 旧版原件留给回退。记录已接管的摘要，重启不重放，回退后再升级只接续旧版的新改动。
        if after != before:
            for target, payload in changes:
                tx.write(target, payload)
            tx.write(receipt, json.dumps(after, sort_keys=True).encode("utf-8"))
