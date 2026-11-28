"""离线迁移覆盖来源、附件、撤销、重跑和事务回滚。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from syvrio.infra.storage.database import transaction
from syvrio.infra.storage.paths import SyvrioPaths

pytestmark = pytest.mark.memory


def test_import_legacy_inference_without_evidence_version(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from vault_import.records import Records
    from vault_import.snapshot import Snapshot
    from vault_import.target import transfer

    old = tmp_path / "old"
    facts = old / "memories" / "notes"
    facts.mkdir(parents=True)
    stamp = "2024-02-03T01:02:03+00:00"
    (facts / "test.md").write_text(
        "# Notes\n\n* **Core**\n  * `id`: `mem-one`\n  * `status`: `active`\n"
        "  * `text`: `Example fact`\n\n  **Extensions**\n"
        f"  * `recorded_at`: `{stamp}`\n"
    )
    hypotheses = old / "inferences"
    hypotheses.mkdir()
    (hypotheses / "example.md").write_text(
        "# Example\n\n* **Core**\n  * `id`: `inf-one`\n  * `kind`: `habit`\n"
        "  * `text`: `Example inference`\n  * `status`: `confirmed`\n"
        "  * `source_ids`:\n    * `id`: `mem-one`\n\n  **Extensions**\n"
        f"  * `created_at`: `{stamp}`\n  * `updated_at`: `{stamp}`\n"
    )
    paths = SyvrioPaths(tmp_path / "target", tmp_path / "runtime")
    with closing(sqlite3.connect(":memory:")) as db:
        archive = Snapshot(db)
        archive.capture([(old, None)])
        records = Records(archive, ZoneInfo("UTC"))
        assert transfer(records, paths)["inferences"] == 1
        assert not transfer(records, paths)

    with transaction(paths, write=False) as target:
        assert tuple(target.execute("SELECT id,status FROM inferences").fetchone()) == (
            "inf-one",
            "confirmed",
        )
        assert tuple(
            target.execute("SELECT memory_id,memory_version FROM inference_sources").fetchone()
        ) == (
            "mem-one",
            None,
        )


def test_import_topic_dates_scan_memories_once(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from vault_import.target import transfer

    class Counted(dict):
        visits = 0

        def values(self):
            for row in super().values():
                self.visits += 1
                yield row

    memories = Counted()
    for i in reversed(range(100)):
        memories[f"mem-{i}"] = (
            f"topic-{i % 10}",
            "active",
            f"sample {i}",
            f"2026-01-{i // 10 + 1:02}T00:00:00+00:00",
            None,
            None,
            None,
            None,
            {},
        )
    records = SimpleNamespace(
        sessions={},
        candidates={},
        inferences={},
        logs={},
        profile=None,
        soul=None,
        topics={f"topic-{i}": (f"Topic {i}", False) for i in range(11)},
        memories=memories,
    )
    paths = SyvrioPaths(tmp_path / "target", tmp_path / "runtime")
    assert transfer(records, paths)["topics"] == 11 and memories.visits == 100
    assert not transfer(records, paths) and memories.visits == 100
    with transaction(paths, write=False) as db:
        dates = dict(db.execute("SELECT name,updated_at FROM topics"))
    assert dates.pop("topic-10") == ""
    assert set(dates.values()) == {"2026-01-10T00:00:00+00:00"}


def test_import_session_and_rollback(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from vault_import.records import Records
    from vault_import.snapshot import Snapshot
    from vault_import.target import transfer

    old = tmp_path / "old"
    sessions = old / "archived" / "sessions"
    sessions.mkdir(parents=True)
    content = b"attachment sample"
    sha = hashlib.sha256(content).hexdigest()
    artifact = old / "attachments" / ".objects" / sha[:2] / sha / "original"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    stamp = "2024-02-03T01:02:03"
    rows = [
        {"type": "meta", "id": "session", "started_at": stamp},
        {
            "type": "message",
            "id": "m1",
            "role": "user",
            "content": "example",
            "at": stamp,
            "attachments": [
                {
                    "id": "a" * 32,
                    "sha256": sha,
                    "name": "example.txt",
                    "size": len(content),
                    "media_type": "text/plain",
                }
            ],
        },
        {"type": "assistant", "id": "m2", "content": "ok", "at": stamp, "reasoning_local": True},
        {"type": "undo", "id": "u1", "message_id": "m1", "at": stamp},
    ]
    (sessions / "session.jsonl").write_text(
        "\n".join(json.dumps(e) for e in rows), encoding="utf-8"
    )
    rt = tmp_path / "rt"
    side = rt / "session-reasoning" / "session" / "m2.json"
    side.parent.mkdir(parents=True)
    side.write_text('{"text":"sample reasoning"}', encoding="utf-8")
    paths = SyvrioPaths(tmp_path / "target", tmp_path / "target-runtime")
    with closing(sqlite3.connect(":memory:")) as db:
        archive = Snapshot(db)
        archive.capture([(old, rt)])
        data = Records(archive, ZoneInfo("Asia/Shanghai"))
        data.inferences["broken"] = (
            "reflection",
            "test",
            "hypothesis",
            "2024-01-01",
            "2024-01-01",
            ["absent"],
        )
        with pytest.raises(ValueError, match="外键"):
            transfer(data, paths)
        assert not (paths.attachments_dir / "objects" / sha[:2] / sha).exists()
        with transaction(paths, write=False) as target:
            assert not target.execute("SELECT id FROM sessions").fetchall()
        data.inferences.clear()
        data.logs[("memory-changes.jsonl", "example-digest", 1)] = {
            "id": "example-memory",
            "at": "2024-02-03T00:00:00+00:00",
            "change": {"status": "deprecated"},
        }
        assert transfer(data, paths)["sessions"] == 1
        assert not transfer(data, paths)
        with transaction(paths, write=False) as target:
            imported = target.execute(
                "SELECT at,details,memory_change FROM audit WHERE action='legacy_memory_change'"
            ).fetchall()
            assert len(imported) == 1 and imported[0]["at"] == "2024-02-03T00:00:00+00:00"
            assert json.loads(imported[0]["details"]) == {"memory_id": "example-memory"}
            assert json.loads(imported[0]["memory_change"]) == {"status": "deprecated"}
            assert tuple(target.execute("SELECT archived,started_at FROM sessions").fetchone()) == (
                1,
                "2024-02-02T17:02:03+00:00",
            )
            assert (
                json.loads(
                    target.execute(
                        "SELECT payload FROM session_events WHERE message_id='m1'"
                    ).fetchone()[0]
                )["original_local_date"]
                == "2024-02-03"
            )
            assert (
                target.execute(
                    "SELECT active FROM session_events WHERE message_id='m1'"
                ).fetchone()[0]
                == 0
            )
            assert target.execute("SELECT count(*) FROM attachments").fetchone()[0] == 1
            assert (
                json.loads(
                    target.execute(
                        "SELECT reasoning FROM session_events WHERE reasoning IS NOT NULL"
                    ).fetchone()[0]
                )["text"]
                == "sample reasoning"
            )
