from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from syvrio.infra.storage.backup import assemble
from syvrio.infra.storage.cache_database import cache_transaction
from syvrio.infra.storage.database import DatabaseError, database_path, transaction
from syvrio.infra.storage.paths import SyvrioPaths


def test_independent_cache_first_open_while_vault_is_writing(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    gate = Barrier(4)

    def insert(index):
        gate.wait(5)
        with cache_transaction(paths, "web-search") as db:
            db.execute("INSERT INTO search_cache VALUES(?,0,'')", (str(index),))

    with transaction(paths), ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(insert, range(4)))
    assert not (paths.cache_dir / "vectors.sqlite3").exists()
    with cache_transaction(paths, "web-search", write=False) as db:
        assert db.execute("SELECT count(*) FROM search_cache").fetchone()[0] == 4
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM search_cache")
    with (
        pytest.raises(RuntimeError, match="rollback"),
        cache_transaction(paths, "web-search") as db,
    ):
        db.execute("DELETE FROM search_cache")
        raise RuntimeError("rollback")
    with cache_transaction(paths, "web-search", write=False) as db:
        assert db.execute("SELECT count(*) FROM search_cache").fetchone()[0] == 4


@pytest.mark.parametrize(
    "prepare", ["VACUUM", "CREATE TABLE unrelated(id)", "PRAGMA application_id=1234"]
)
def test_foreign_cache_files_are_preserved(tmp_path, prepare):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    paths.cache_dir.mkdir(parents=True)
    target = paths.cache_dir / "vectors.sqlite3"
    with closing(sqlite3.connect(target, isolation_level=None)) as db:
        db.execute(prepare)
    original = target.read_bytes()
    with pytest.raises(DatabaseError, match="cache database"), cache_transaction(paths, "vectors"):
        pytest.fail("accepted an unrelated SQLite file")
    assert target.read_bytes() == original
    assert sorted(paths.cache_dir.iterdir()) == [target]
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_cache_rejects_redirected_file(tmp_path, monkeypatch):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    target = paths.cache_dir / "vectors.sqlite3"
    real = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, *a, **kw: tmp_path / "outside" if path == target else real(path, *a, **kw),
    )
    with pytest.raises(DatabaseError, match="inside"), cache_transaction(paths, "vectors"):
        pytest.fail("accepted a redirected cache")


def test_backups_restore_vault_without_runtime_caches(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    with transaction(paths) as db:
        db.execute("INSERT INTO topics(name,title,updated_at) VALUES('demo','demo','2026-01-01')")
        db.execute("""INSERT INTO memories(id,topic,status,text,recorded_at,extensions)
            VALUES('one','demo','active','synthetic','2026-01-01','{}')""")
        db.execute("INSERT INTO git_sync VALUES(1,'{}')")
    with cache_transaction(paths, "vectors") as db:
        db.execute("INSERT INTO vectors VALUES('one','test','hash',1,X'01020304')")
    with cache_transaction(paths, "web-search") as db:
        db.execute("INSERT INTO search_cache VALUES('key',0,'cached')")
    backup = assemble(paths.vault_dir, tmp_path / "backup")
    assert {p.name for p in backup.iterdir()} == {"syvrio.sqlite3", "manifest.json"}
    with transaction(paths, write=False) as db:
        assert db.execute("SELECT count(*) FROM git_sync").fetchone()[0] == 1
    restored = assemble(backup, tmp_path / "restored", restore=True)
    recovered = SyvrioPaths(restored, tmp_path / "restored-runtime")
    with transaction(recovered, write=False) as db:
        assert db.execute("SELECT text FROM memories").fetchone()[0] == "synthetic"
        assert db.execute("SELECT count(*) FROM git_sync").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM usage").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT name FROM sqlite_schema WHERE name IN ('vectors','search_cache')"
            ).fetchall()
            == []
        )
    assert not recovered.cache_dir.exists()
    assert not recovered.state_dir.exists()
    for name, table in (("vectors", "vectors"), ("web-search", "search_cache")):
        with cache_transaction(recovered, name, write=False) as db:
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        with cache_transaction(paths, name, write=False) as db:
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


def test_backup_includes_committed_usage_wal_and_pause(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    with transaction(paths):
        pass
    main = database_path(paths)
    with closing(sqlite3.connect(main)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO usage VALUES('demo','daily','2026-01-01',7)")
        writer.execute("INSERT INTO quota_pauses VALUES('demo','exhausted',123)")
        writer.commit()
        assert main.with_name(main.name + "-wal").stat().st_size > 0
        backup = assemble(paths.vault_dir, tmp_path / "backup")
    restore = assemble(backup, tmp_path / "restored", restore=True)
    with transaction(SyvrioPaths(restore, tmp_path / "fresh"), write=False) as db:
        assert tuple(db.execute("SELECT * FROM usage").fetchone()) == (
            "demo",
            "daily",
            "2026-01-01",
            7,
        )
        assert tuple(db.execute("SELECT * FROM quota_pauses").fetchone()) == (
            "demo",
            "exhausted",
            123,
        )


@pytest.mark.parametrize("damage", ["missing", "changed", "unlisted"])
def test_restore_rejects_damaged_database_backup(tmp_path, damage):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    with transaction(paths):
        pass
    backup = assemble(paths.vault_dir, tmp_path / "backup")
    member = backup / database_path(paths).name
    if damage == "missing":
        member.unlink()
    elif damage == "changed":
        with closing(sqlite3.connect(member)) as db:
            db.execute("INSERT INTO quota_pauses VALUES('demo','changed',NULL)")
            db.commit()
    else:
        manifest = backup / "manifest.json"
        ledger = json.loads(manifest.read_text(encoding="utf-8"))
        del ledger["files"][member.name]
        manifest.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum|Missing"):
        assemble(backup, tmp_path / "invalid", restore=True)
    assert not (tmp_path / "invalid").exists()
