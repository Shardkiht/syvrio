"""将本机历史文件 Vault 导入已初始化的 SQLite Vault。默认预演。"""

import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from vault_import.records import Records
from vault_import.snapshot import Snapshot
from vault_import.target import transfer

from syvrio.backend.ownership import physical_vault_lock
from syvrio.infra.storage.backup import assemble
from syvrio.infra.storage.database import DatabaseError, require_current_schema
from syvrio.infra.storage.paths import SyvrioPaths


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--source",
        nargs=2,
        action="append",
        required=True,
        metavar=("VAULT", "RUNTIME"),
        help="可重复，从新到旧；无对应 Runtime 时填 -",
    )
    cli.add_argument(
        "--archive-only",
        nargs=2,
        action="append",
        default=[],
        metavar=("VAULT", "RUNTIME"),
        help="只存入原始档案的历史备份",
    )
    cli.add_argument("--target", type=Path, required=True, help="已初始化的 SQLite Vault")
    cli.add_argument("--timezone", required=True, help="历史本地时间的 IANA 时区，如 Asia/Shanghai")
    cli.add_argument(
        "--backup-dir", type=Path, help="正式执行时保存导入前备份、源文件档案和报告的父目录"
    )
    cli.add_argument("--apply", action="store_true", help="写入正式目标；请先停止目标常驻后端")
    args = cli.parse_args()
    if args.apply and not args.backup_dir:
        cli.error("--apply 需要 --backup-dir")
    target = args.target.resolve(strict=True)
    zone = ZoneInfo(args.timezone)
    sources = []
    for vault, runtime in args.source + args.archive_only:
        src = Path(vault).resolve(strict=True)
        rt = Path(runtime).resolve(strict=True) if runtime != "-" else None
        if (
            not src.is_dir()
            or not any(
                (src / n).exists()
                for n in ("sessions", "memories", "records", "archived", "profile.md", "SOUL.md")
            )
            or (src / "syvrio.sqlite3").exists()
        ):
            cli.error(f"不是支持的历史 Vault: {src}")
        if rt and not rt.is_dir():
            cli.error(f"不是 Runtime 目录: {rt}")
        if any(
            a.is_relative_to(b) or b.is_relative_to(a) for a in (src, rt) if a for b in [target]
        ):
            cli.error("源与目标目录必须相互独立")
        sources.append((src, rt))
    dbfile = target / "syvrio.sqlite3"
    try:
        with closing(sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True)) as db:
            require_current_schema(db)
    except (DatabaseError, sqlite3.Error) as exc:
        cli.error(f"无法读取目标数据库，请选择已按当前结构初始化的 Syvrio Vault。{exc}")
    if args.apply:
        root = args.backup_dir.resolve()
        if any(
            root.is_relative_to(p) for p in [target, *(x for pair in sources for x in pair if x)]
        ):
            cli.error("备份目录必须在源和目标之外")
        root = root / ("vault-import-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
        root.mkdir(parents=True, mode=0o700)
        print("备份与原始档案:", root, flush=True)
    with tempfile.TemporaryDirectory(prefix="syvrio-import-") as work:
        work = Path(work)
        output = root if args.apply else work
        with closing(sqlite3.connect(output / "legacy-source.sqlite3")) as db:
            archive = Snapshot(db)
            archive.capture(sources)
            db.execute(
                "CREATE TABLE sources(id INTEGER PRIMARY KEY,vault TEXT,runtime TEXT,archive_only INTEGER)"
            )
            db.executemany(
                "INSERT INTO sources VALUES(?,?,?,?)",
                [
                    (i, str(v), str(r) if r else None, int(i >= len(args.source)))
                    for i, (v, r) in enumerate(sources)
                ],
            )
            db.commit()
            file_count = len(archive.paths)
            # 备份快照仅供追溯，其中已删除的业务对象不会重新出现在客户端。
            archive.paths = {
                key: v for key, v in archive.paths.items() if key[0] < len(args.source)
            }
            data = Records(archive, zone)
            if args.apply:
                lock = physical_vault_lock(target)
                lock.acquire(0)
            try:
                before = output / "before"
                assemble(target, before)
                # 在副本中先走完同一条写入路径，检查冲突、外键和所有附件。
                trial = work / "trial"
                assemble(before, trial, restore=False)
                expected = transfer(data, SyvrioPaths(trial, work / "trial-runtime"))
                if args.apply:
                    actual = transfer(data, SyvrioPaths(target, work / "runtime"))
                    if expected != actual:
                        raise RuntimeError("预演与实际写入计数不一致")
            finally:
                if args.apply:
                    lock.release()
            report = {
                "source_files": file_count,
                "changes": dict(expected),
                "sessions": len(data.sessions),
                "events": sum(len(e) for e, _ in data.sessions.values()),
                "memories": len(data.memories),
                "inferences": len(data.inferences),
                "source_warnings": data.deferred,
            }
            if args.apply:
                (output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
