import json
import subprocess
import sys
from pathlib import Path

import pytest

from syvrio.agent.session import SessionStore
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
    # foundation 的 Inference 模型（extra=forbid）没有 source_fingerprints 字段，
    # 因此这里在 JSON 层断言脚本行为；该脚本分支对 foundation 写出的行是空操作。
    rows = [
        json.loads(line)
        for line in (vault / "records/memory/inferences.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    updated, outdated = rows[0]["records"]
    assert updated["created_at"] == updated["updated_at"] == "2026-11-11T09:00:00+00:00"
    assert updated["source_fingerprints"] == {"mem-1": source_fingerprint(entry)}
    assert outdated["source_fingerprints"] == {"mem-1": "0" * 64}


def test_foundation_inference_rows_survive_timestamp_migration(tmp_path):
    vault = tmp_path / "vault"
    write(
        vault / "records/memory/inferences.jsonl",
        '{"id":"i-1","text":"Synthetic hypothesis","kind":"profile","source_ids":["mem-1"],'
        '"created_at":"2026-11-11T09:00:00+08:00","updated_at":"2026-11-11T09:00:00+08:00",'
        '"status":"hypothesis"}\n',
    )
    run = migrate(
        "--vault",
        vault,
        "--source-timezone",
        "UTC",
        "--apply",
        "--backup-dir",
        tmp_path / "backup",
    )
    assert run.returncode == 0, run.stderr
    loaded = InferenceStore(vault).all()
    assert len(loaded) == 1
    assert loaded[0].created_at == loaded[0].updated_at == "2026-11-11T01:00:00+00:00"


SESSION_ID = "s-20260101-120000-abcd"


def write_recovery_session(path, ignored_line, *, with_broken_line=True):
    """构造与 SessionStore 恢复规则同形的 4 行会话（坏行在 2，marker 在 3）。"""
    lines = [
        json.dumps({"type": "meta", "id": SESSION_ID, "started_at": "2026-01-01T12:00:00"}),
    ]
    if with_broken_line:
        lines.append("{broken")
    lines.append(json.dumps({"type": "recovery", "ignored_line": ignored_line,
                             "at": "2026-01-01T12:00:01"}))
    lines.append(json.dumps({"type": "message", "role": "user", "id": "m-1",
                             "content": "恢复后的消息", "at": "2026-01-01T12:00:02"}))
    return write(path, "\n".join(lines) + "\n")


def test_session_recovery_marker_allows_migration_and_runtime_reload(tmp_path):
    # SessionStore 的恢复规则：坏行 2 被第 3 行的有效 marker 授权保留。
    vault = tmp_path / "vault"
    backup = tmp_path / "utc-backup"
    session = write_recovery_session(
        vault / f"sessions/2026/01/{SESSION_ID}.jsonl", 2, with_broken_line=True
    )
    before = session.read_bytes()

    preview = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai", "--dry-run")
    assert preview.returncode == 0, preview.stderr
    assert SESSION_ID in preview.stdout and "0 errors" in preview.stdout
    assert session.read_bytes() == before

    result = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai",
                     "--apply", "--backup-dir", backup)
    assert result.returncode == 0, result.stderr

    after_lines = session.read_bytes().split(b"\n")
    # 坏行原字节、原 EOL、原位置保持不动。
    assert after_lines[1] == b"{broken"
    assert before.split(b"\n")[1] == after_lines[1]
    rows = [json.loads(line) for line in after_lines if line.strip() and line != b"{broken"]
    # 有效记录（含 marker 的 at）按上海时区转为 UTC；没有生成新的恢复标记。
    assert rows[0]["started_at"] == "2026-01-01T04:00:00+00:00"
    assert rows[1]["ignored_line"] == 2 and rows[1]["at"] == "2026-01-01T04:00:01+00:00"
    assert rows[2]["at"] == "2026-01-01T04:00:02+00:00"
    assert sum(1 for row in rows if row.get("type") == "recovery") == 1
    # 备份保存改写前的原字节。
    assert (backup / "sessions/2026/01" / f"{SESSION_ID}.jsonl").read_bytes() == before
    # 迁移后新程序必须能加载该会话，坏行被跳过，正文完好。
    loaded = SessionStore(vault).load(SESSION_ID)
    assert [r.get("content") for r in loaded.records if r.get("type") == "message"] == ["恢复后的消息"]
    assert loaded.damaged_tail_line is None


@pytest.mark.parametrize(
    "ignored_line",
    [pytest.param(None, id="missing-marker"),
     pytest.param(True, id="bool-not-int"),
     pytest.param("2", id="string-not-int"),
     pytest.param(1, id="points-at-valid-line"),
     pytest.param(99, id="points-beyond-file")],
)
def test_session_invalid_or_missing_marker_refuses_and_keeps_file(tmp_path, ignored_line):
    # 无有效 marker 授权的坏行必须中止迁移，且文件字节保持不变。
    vault = tmp_path / "vault"
    memory = write(
        vault / "memories/one.md",
        "# One\n\n"
        + render_record(
            "Synthetic text", Core(id="mem-1", source="test", status="active", confidence=1),
            extensions=[("syvrio.recorded_at", "2026-11-11T09:00:00")],
        )
        + "\n",
    )
    session = write_recovery_session(
        vault / f"sessions/2026/01/{SESSION_ID}.jsonl", ignored_line, with_broken_line=True
    )
    before = (session.read_bytes(), memory.read_bytes())

    run = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai", "--dry-run")
    assert run.returncode != 0
    assert "unmarked corrupt line" in run.stderr
    # dry-run 与 apply 都拒绝；库内所有文件保持原字节（整批判定，不部分改写）。
    assert (session.read_bytes(), memory.read_bytes()) == before
    apply_run = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai",
                        "--apply", "--backup-dir", tmp_path / "utc-backup")
    assert apply_run.returncode != 0
    assert (session.read_bytes(), memory.read_bytes()) == before
    assert not (tmp_path / "utc-backup").exists()


def test_plain_jsonl_recovery_marker_does_not_authorize_bad_lines(tmp_path):
    # 恢复分支只适用于会话 JSONL：records 与 runtime 文件保持严格，同类 marker 不授权坏行。
    for relative in ("records/memory/audit.jsonl", "jobs/dreaming/jobs.jsonl"):
        vault = tmp_path / f"vault-{relative.split('/')[0]}"
        runtime = tmp_path / "runtime"
        vault.mkdir()
        runtime.mkdir(exist_ok=True)
        scope_root = vault if relative.startswith("records") else runtime
        target = write(scope_root / relative, "\n".join([
            json.dumps({"at": "2026-01-01T12:00:00", "action": "note"}),
            "{broken",
            json.dumps({"type": "recovery", "ignored_line": 2, "at": "2026-01-01T12:00:01"}),
        ]) + "\n")
        before = target.read_bytes()
        run = migrate("--vault", vault, "--runtime", runtime,
                      "--source-timezone", "Asia/Shanghai", "--dry-run")
        assert run.returncode != 0, relative
        assert "invalid JSON object" in run.stderr
        assert target.read_bytes() == before


def test_session_marked_invalid_utf8_line_migrates_and_stays_in_place(tmp_path):
    # 已被恢复标记覆盖的非法 UTF-8 坏行：按 bytes 保留原字节与物理位置，其余时间转 UTC。
    vault = tmp_path / "vault"
    backup = tmp_path / "utc-backup"
    session = vault / f"sessions/2026/01/{SESSION_ID}.jsonl"
    session.parent.mkdir(parents=True)
    lines = [
        json.dumps({"type": "meta", "id": SESSION_ID, "started_at": "2026-01-01T12:00:00"}).encode() + b"\n",
        b"{\xff\r\n",
        json.dumps({"type": "recovery", "ignored_line": 2, "at": "2026-01-01T12:00:01"}).encode() + b"\n",
        json.dumps({"type": "message", "role": "user", "id": "m-1",
                    "content": "after recovery", "at": "2026-01-01T12:00:02"}).encode() + b"\n",
    ]
    session.write_bytes(b"".join(lines))
    before = session.read_bytes()

    preview = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai", "--dry-run")
    assert preview.returncode == 0, preview.stderr
    assert session.read_bytes() == before

    result = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai",
                     "--apply", "--backup-dir", backup)
    assert result.returncode == 0, result.stderr

    after = session.read_bytes()
    after_lines = after.split(b"\n")
    # 坏行原字节、原 EOL（CRLF）、原物理位置未变；行数不变；末行换行保留。
    assert after_lines[1] == b"{\xff\r" and after.endswith(b"\n") and len(after_lines) == len(lines) + 1
    assert json.loads(after.splitlines()[2])["type"] == "recovery"
    rows = [json.loads(line) for line in after.splitlines() if line != b"{\xff"]
    assert rows[0]["started_at"] == "2026-01-01T04:00:00+00:00"
    assert rows[1]["ignored_line"] == 2 and rows[1]["at"] == "2026-01-01T04:00:01+00:00"
    assert rows[2]["content"] == "after recovery" and rows[2]["at"] == "2026-01-01T04:00:02+00:00"
    # 备份与改写前整文件一致；迁移后的文件能被 SessionStore 加载。
    assert (backup / f"sessions/2026/01/{SESSION_ID}.jsonl").read_bytes() == before
    loaded = SessionStore(vault).load(SESSION_ID)
    assert [r.get("content") for r in loaded.records if r.get("type") == "message"] == ["after recovery"]
    assert loaded.damaged_tail_line is None


def test_session_u2028_content_survives_migration(tmp_path):
    # 正文含实际 U+2028 字符：str.splitlines 会拆坏记录；bytes 分行后必须原样保留。
    vault = tmp_path / "vault"
    backup = tmp_path / "utc-backup"
    session = vault / f"sessions/2026/01/{SESSION_ID}.jsonl"
    session.parent.mkdir(parents=True)
    content = "one\u2028two"
    session.write_bytes(b"\n".join([
        json.dumps({"type": "meta", "id": SESSION_ID, "started_at": "2026-01-01T12:00:00"}).encode(),
        json.dumps({"type": "message", "role": "user", "id": "m-1",
                    "content": content, "at": "2026-01-01T12:00:02"},
                   ensure_ascii=False).encode("utf-8"),
    ]) + b"\n")
    before = session.read_bytes()
    assert content.encode("utf-8") in before  # 文件里是实际 Unicode 字符，不是转义文本

    result = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai",
                     "--apply", "--backup-dir", backup)
    assert result.returncode == 0, result.stderr

    after = session.read_bytes()
    assert after.count(b"\n") == before.count(b"\n")  # 记录没有被拆成两行
    assert content.encode("utf-8") in after
    rows = [json.loads(line) for line in after.splitlines()]
    assert len(rows) == 2
    assert rows[0]["started_at"] == "2026-01-01T04:00:00+00:00"
    assert rows[1]["content"] == content and rows[1]["at"] == "2026-01-01T04:00:02+00:00"
    loaded = SessionStore(vault).load(SESSION_ID)
    assert [r.get("content") for r in loaded.records if r.get("type") == "message"] == [content]


def test_session_unmarked_invalid_utf8_aborts_whole_batch(tmp_path):
    # 未标记的非法 UTF-8 坏行：整批中止，同批本需转换的正常文件也不被改写。
    vault = tmp_path / "vault"
    backup = tmp_path / "utc-backup"
    session = vault / "sessions/2026/01/other.jsonl"
    session.parent.mkdir(parents=True)
    session.write_bytes(b"\n".join([
        json.dumps({"type": "meta", "id": "s-other", "started_at": "2026-01-01T12:00:00"}).encode(),
        b"{\xff\r\n",
        json.dumps({"type": "message", "role": "user", "id": "m-1",
                    "content": "no marker here", "at": "2026-01-01T12:00:02"}).encode(),
    ]) + b"\n")
    memory = write(
        vault / "memories/one.md",
        "# One\n\n"
        + render_record(
            "Synthetic text", Core(id="mem-1", source="test", status="active", confidence=1),
            extensions=[("syvrio.recorded_at", "2026-01-01T12:00:00")],
        )
        + "\n",
    )
    before = (session.read_bytes(), memory.read_bytes())

    preview = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai", "--dry-run")
    assert preview.returncode != 0
    assert "unmarked corrupt line" in preview.stderr
    assert (session.read_bytes(), memory.read_bytes()) == before

    result = migrate("--vault", vault, "--source-timezone", "Asia/Shanghai",
                     "--apply", "--backup-dir", backup)
    assert result.returncode != 0
    # 两份源文件都未改写；备份目录未创建（整批判定先于写入）。
    assert (session.read_bytes(), memory.read_bytes()) == before
    assert not backup.exists()


def test_session_migration_preserves_cr_only_separators(tmp_path):
    vault = tmp_path / "vault"
    session = SessionStore(vault).create()
    original = (
        b'{"type":"meta","started_at":"2026-01-01T12:00:00"}\r'
        b'{"type":"message","role":"user","at":"2026-01-01T12:00:01","content":"synthetic"}\r'
    )
    session.path.write_bytes(original)
    backup = tmp_path / "backup"
    result = migrate(
        "--vault", vault, "--source-timezone", "Asia/Shanghai", "--apply", "--backup-dir", backup
    )
    assert result.returncode == 0, result.stderr
    assert session.path.read_bytes().count(b"\r") == 2
    assert b"\n" not in session.path.read_bytes()
    loaded = SessionStore(vault).load(session.id)
    assert loaded.started_at == "2026-01-01T04:00:00+00:00"
    assert loaded.messages[0]["at"] == "2026-01-01T04:00:01+00:00"
    assert loaded.messages[0]["content"] == "synthetic"
    assert (
        backup / "sessions" / session.path.relative_to(vault / "sessions")
    ).read_bytes() == original
