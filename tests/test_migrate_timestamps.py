import json
import subprocess
import sys
from pathlib import Path

import pytest

from syvrio.memory.inference import InferenceStore, source_fingerprint
from syvrio.memory.schema import Core, parse_document, render_record
from syvrio.memory.store import MemoryStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_timestamps_to_utc.py"


def migrate(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode())
    return path


def test_migration_preview_apply_backup_and_idempotence(tmp_path):
    vault, runtime, backup = (tmp_path / name for name in ("vault", "runtime", "backup"))
    session = write(
        vault / "sessions/legacy/undated/one.jsonl",
        '{"type":"meta","started_at":"2026-11-11T09:00:00"}\r\n'
        '{"type":"message","at":"2026-11-11T02:00:00Z","content":"2026-11-11T09:00:00"}\r\n'
        '{"type":"undo"}\r\n',
    )
    memory = write(
        vault / "memories/one.md",
        "# Synthetic\r\n\r\n"
        + render_record(
            "Synthetic text with 2026-11-11T09:00:00",
            Core(id="mem-1", source="test", status="active", confidence=1),
            [("event_time", "2026-11-11")],
            [("syvrio.recorded_at", "2026-11-11T09:00:00"), ("unknown.at", "2026-11-11T09:00:00")],
            "\r\n",
        )
        + "\r\n"
        + render_record(
            "Day precision",
            Core(id="mem-2", source="test", status="active", confidence=1),
            extensions=[("syvrio.recorded_at", "2026-11-11")],
            newline="\r\n",
        ),
    )
    state = write(
        runtime / "state/reflection.json",
        '{"profile":{"last_attempt_at":"2026-11-11T09:00:00-05:00",'
        '"next_retry_at":null},"dream":{"last_success_at":"2026-11-11T09:00:00"}}\n',
    )
    audit = write(vault / "records/memory/audit.jsonl", '{"at":"2026-11-11T10:00:00+08:00"}\n')
    originals = {path: path.read_bytes() for path in (session, memory, state, audit)}
    args = [
        "--vault",
        vault,
        "--runtime",
        runtime,
        "--source-timezone",
        "Asia/Shanghai",
        "--sessions-source-timezone",
        "UTC",
        "--runtime-source-timezone",
        "America/New_York",
    ]
    preview = migrate(*args)
    assert preview.returncode == 0, preview.stderr
    assert "4 files to change, 0 errors" in preview.stdout
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert not (runtime / "locks").exists()

    result = migrate(*args, "--apply", "--backup-dir", backup)
    assert result.returncode == 0, result.stderr
    assert (backup / "sessions/legacy/undated/one.jsonl").read_bytes() == originals[session]
    assert (backup / "vault/memories/one.md").read_bytes() == originals[memory]
    assert (backup / "runtime/state/reflection.json").read_bytes() == originals[state]
    assert (backup / "vault/records/memory/audit.jsonl").read_bytes() == originals[audit]

    rows = [json.loads(line) for line in session.read_bytes().splitlines()]
    assert rows[0]["started_at"] == "2026-11-11T09:00:00+00:00"
    assert rows[1]["at"] == "2026-11-11T02:00:00+00:00"
    assert rows[1]["content"] == "2026-11-11T09:00:00"
    assert "at" not in rows[2]
    assert memory.read_bytes() == originals[memory].replace(
        b"`syvrio.recorded_at`: `2026-11-11T09:00:00`",
        b"`syvrio.recorded_at`: `2026-11-11T01:00:00+00:00`",
    )
    entries = MemoryStore(vault).entries()
    assert [entry.timestamp for entry in entries] == ["2026-11-11T01:00:00+00:00", "2026-11-11"]
    assert json.loads(audit.read_bytes())["at"] == "2026-11-11T02:00:00+00:00"
    info = json.loads(state.read_bytes())
    assert info["profile"]["last_attempt_at"] == "2026-11-11T14:00:00+00:00"
    assert info["profile"]["next_retry_at"] is None
    assert info["dream"]["last_success_at"] == "2026-11-11T14:00:00+00:00"
    migrated = {path: path.read_bytes() for path in originals}
    rerun = migrate(*args, "--apply", "--backup-dir", tmp_path / "backup-2")
    assert rerun.returncode == 0, rerun.stderr
    assert "0 files to change, 0 errors" in rerun.stdout
    assert all(path.read_bytes() == data for path, data in migrated.items())
    assert not (tmp_path / "backup-2").exists()


@pytest.mark.parametrize(
    ("value", "zone", "error"),
    [
        ("2026-11-01T01:30:00", "America/New_York", "ambiguous local time"),
        ("2026-03-08T02:30:00", "America/New_York", "nonexistent local time"),
        ("not a time", "Asia/Shanghai", "invalid ISO 8601"),
        ("2026-11-11T09:00:00", None, "missing source timezone"),
    ],
)
def test_apply_refuses_entire_batch_with_unconvertible_timestamp(tmp_path, value, zone, error):
    good = write(tmp_path / "sessions/a.jsonl", '{"at":"2026-11-11T09:00:00+08:00"}\n')
    bad = write(tmp_path / "sessions/z.jsonl", json.dumps({"at": value}) + "\n")
    originals = (good.read_bytes(), bad.read_bytes())
    backup = tmp_path / "backup"
    args = ["--sessions", good.parent, "--apply", "--backup-dir", backup]
    if zone:
        args.extend(["--source-timezone", zone])
    result = migrate(*args)
    assert result.returncode == 1
    assert "z.jsonl:1 at" in result.stderr and error in result.stderr
    assert (good.read_bytes(), bad.read_bytes()) == originals
    assert not backup.exists()


def test_inference_revisions_keep_only_matching_evidence_current(tmp_path):
    vault = tmp_path / "vault"
    memory = write(
        vault / "memories/synthetic.md",
        render_record(
            "Synthetic evidence",
            Core(id="mem-1", source="test", status="active", confidence=1),
            extensions=[("syvrio.recorded_at", "2026-11-11T09:00:00")],
        ),
    )
    old_entry = parse_document(memory.read_text(), "synthetic.md", "synthetic").records[0].entry
    old_digest = source_fingerprint(old_entry)
    record = {
        "id": "i-1",
        "text": "Synthetic hypothesis",
        "kind": "profile",
        "source_ids": ["mem-1"],
        "created_at": "2026-11-11T09:00:00",
        "updated_at": "2026-11-11T09:00:00",
        "source_fingerprints": {"mem-1": old_digest},
    }
    stale = {**record, "id": "i-2", "source_fingerprints": {"mem-1": "0" * 64}}
    write(
        vault / "records/memory/inferences.jsonl",
        json.dumps({"type": "revision", "records": [record, stale]}) + "\n",
    )
    run = migrate(
        "--vault",
        vault,
        "--source-timezone",
        "UTC",
        "--timezone-for",
        "vault/memories=Asia/Shanghai",
        "--apply",
        "--backup-dir",
        tmp_path / "backup",
    )
    assert run.returncode == 0, run.stderr
    entry = MemoryStore(vault).find("mem-1")
    assert entry.timestamp == "2026-11-11T01:00:00+00:00"
    updated, outdated = InferenceStore(vault).all()
    assert updated.created_at == updated.updated_at == "2026-11-11T09:00:00+00:00"
    assert updated.source_fingerprints == {"mem-1": source_fingerprint(entry)}
    assert outdated.source_fingerprints == {"mem-1": "0" * 64}
