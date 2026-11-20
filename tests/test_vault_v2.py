from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from syvrio.agent.session import SessionStore
from syvrio.infra.config import Config, RetrievalConfig
from syvrio.memory.schema import SchemaError, parse_document
from syvrio.memory.service import MemoryService
from syvrio.memory.store import MemoryStore

EXAMPLE = """# Synthetic records

* **Core**
  * `id`: `mem-001`
  * `status`: `active`
  * `text`:
    ~~~~
    An **exhibit** and a [link](https://example.test).

    - an ordinary nested bullet
      - another bullet
    ~~~
    **Core**
    * `id`: `not-a-record`
    ~~~
    ~~~~

  **Context**
  * `event_time`: `2024-06`
  * `location`: `Synthetic Harbor`
  * `emotion.event`: `delighted`
  * `emotion.recollection`: `nostalgic`

  **Extensions**
  * `custom.marker`: `cobalt-fox`
  * `custom.marker`: `second-marker`
  * `entities.person`:
    * `name`: `Sample Agent`
    * `detail`:
      * `role`: `curator`
  * `recorded_at`: `2026-01-01T00:00:00+00:00`

* **Core**
  * `id`: `mem-002`
  * `status`: `active`
  * `text`: `An _unrelated_ item.`

  **Extensions**
  * `recorded_at`: `2026-01-01T00:00:00+00:00`
"""


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_parser_and_status_edit_preserve_raw_records(tmp_path, newline):
    store = MemoryStore(tmp_path)
    path = store.topic_path("custom/events")
    path.parent.mkdir(parents=True)
    raw = EXAMPLE.replace("\n", newline)
    path.write_bytes(raw.encode())
    doc = store.document(path)
    assert doc.raw_markdown == raw
    first, second = doc.records
    assert first.entry.raw_markdown == raw[first.start : first.end]
    assert first.entry.source_path == "memories/custom/events.md"
    assert first.entry.line == 3 and first.entry.end_line < second.entry.line
    assert first.entry.text.startswith("An **exhibit** and a [link]")
    assert "- another bullet" in first.entry.text and "`not-a-record`" in first.entry.text
    assert first.entry.context.event_time == "2024-06"
    assert [(n.key, n.value) for n in first.entry.extension_fields[:2]] == [
        ("custom.marker", "cobalt-fox"),
        ("custom.marker", "second-marker"),
    ]
    assert first.entry.extension_fields[2].children[1].children[0].value == "curator"
    assert not second.entry.context_fields and second.entry.extension_fields[0].key == "recorded_at"
    assert store.set_status("mem-001", "deprecated")
    assert (
        path.read_bytes() == raw.replace("`status`: `active`", "`status`: `deprecated`", 1).encode()
    )
    assert store.document(path).records[1].entry.raw_markdown == second.entry.raw_markdown


def test_store_reports_non_utf8_memory_file(tmp_path):
    store = MemoryStore(tmp_path)
    path = store.topic_path("custom/events")
    path.parent.mkdir(parents=True)
    original = "# 合成记录\n".encode("gbk")
    path.write_bytes(original)

    with pytest.raises(SchemaError, match=r"events\.md: invalid UTF-8.*Save"):
        store.entries()

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("`status`: `active`", "`status`: `other`", "invalid memory record"),
        ("`text`:", "`comment`:", "invalid memory record"),
        ("`status`: `active`", "`id`: `mem-3`", "duplicate"),
        ("  **Context**", "  **Core**", "out-of-order"),
        ("`custom.marker`: `second-marker`", "`custom.marker` = `bad`", "invalid field"),
        ("  * `custom.marker`: `second-marker`", "  * `custom.marker`:", "empty field"),
    ],
)
def test_parser_rejects_malformed_record(old, new, problem):
    with pytest.raises(SchemaError, match=problem):
        parse_document(EXAMPLE.replace(old, new, 1), "events.md", "events")


def test_retrieval_indexes_only_fact_location_and_emotion(tmp_path):
    config = Config(vault=tmp_path / "vault", retrieval=RetrievalConfig(method="keyword"))
    service = MemoryService(config)
    path = service.store.topic_path("events")
    path.parent.mkdir(parents=True)
    path.write_text(EXAMPLE, encoding="utf-8")
    entry = service.store.find("mem-001")
    assert entry.index_text == "\n".join((entry.text, "Synthetic Harbor", "delighted", "nostalgic"))
    assert entry.embedding_text == entry.text
    for query in ("exhibit", "Synthetic Harbor", "delighted", "nostalgic"):
        hits = service.recall(query, limit=1)
        assert hits[0].id == "mem-001"
    assert not service.recall("curator")
    assert not service.recall("cobalt")

    before = path.stat()
    path.write_text(EXAMPLE.replace("delighted", "excited"), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert service.recall("excited")[0].id == "mem-001"
    assert not service.recall("delighted")
    service.store.set_status("mem-001", "deprecated")
    assert not service.recall("excited")


def test_pending_markdown_is_excluded_from_facts_and_index_fingerprint(tmp_path):
    store = MemoryStore(tmp_path)
    store.ensure()
    fingerprint = store.fingerprint()
    pending = store.root / "pending.md"
    pending.write_text("Unreviewed draft", encoding="utf-8")
    assert store.entries(include_inactive=True) == []
    assert store.fingerprint() == fingerprint
    with pytest.raises(SchemaError, match="reserved"):
        store.document(pending)


def test_relative_topics_disambiguate_equal_stems(tmp_path):
    store = MemoryStore(tmp_path)
    a = store.append("work/notes", "synthetic first")
    b = store.append("education/notes", "synthetic second")
    assert a.topic != b.topic and store.entries(a.topic) == [a] and store.entries(b.topic) == [b]
    with pytest.raises(ValueError, match="(?i)ambiguous"):
        store.entries("notes")


def test_runtime_binding_invalidates_derived_data_without_vault_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("SYVRIO_RUNTIME")
    runtime = tmp_path / "runtime"

    def app(vault):
        return MemoryService(
            Config(vault=vault, runtime=str(runtime), retrieval=RetrievalConfig(method="keyword"))
        )

    first = app(tmp_path / "first")
    first.init_vault()
    entry = first.note("synthetic personal note")
    cache = first.paths.index_dir / "vectors.npz"
    cache.parent.mkdir()
    cache.write_bytes(b"old")
    summary = first.paths.cache_dir / "profile" / "profile-summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text("{}", encoding="utf-8")
    assert not (first.config.vault / ".syvrio").exists()

    second = app(tmp_path / "second")
    assert second.paths.runtime_dir == first.paths.runtime_dir
    assert not cache.exists() and not summary.exists()
    assert first.store.find(entry.id) == entry
    shutil.rmtree(runtime)
    reloaded = app(first.config.vault)
    assert reloaded.store.find(entry.id) == entry
    assert (reloaded.paths.lock_dir / "vault.lock").is_file()
    assert (reloaded.paths.state_dir / "vault-binding.json").is_file()


@pytest.fixture
def migrator():
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_vault_v2.py"
    spec = importlib.util.spec_from_file_location("migration_v2_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_checks_recovery_hashes_and_history(tmp_path, migrator, monkeypatch):
    src = tmp_path / "source"
    dst = tmp_path / "合成记忆库"
    run = dst.with_name(dst.name + "-runtime")
    (src / "memories").mkdir(parents=True)
    (src / "sessions").mkdir()
    (src / ".syvrio").mkdir()
    (src / ".hidden").write_bytes(b"unchanged")
    (src / ".合成文件").write_bytes(b"unchanged")
    (src / "memories" / "one.md").write_text(
        "# One\n\n- 2026-02-01T01:02:03 — A note  `id=mem-one` `status=active` "
        "`source=chat` `confidence=0.95` `future_field=synthetic`\n",
        encoding="utf-8",
    )
    (src / "memories" / "two.md").write_text(
        "# Two\n\n- 2026-02-01T01:02:03 — A legacy note\n",
        encoding="utf-8",
    )
    dated = b'{"type":"meta","id":"session-1","started_at":"2026-02-01T01:02:03"}\r\n'
    undated = b'{"type":"message","role":"user","content":"Synthetic history"}\n'
    (src / "sessions" / "session-1.jsonl").write_bytes(dated)
    (src / "sessions" / "session-2.jsonl").write_bytes(undated)
    event = b'{"action":"evolve","old_entry":"mem-one","new_entry":"mem-two"}\n'
    (src / ".syvrio" / "audit.jsonl").write_bytes(event)
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    head = (src / ".git" / "HEAD").read_bytes()
    before = migrator.inventory(src)
    args = ["--source", str(src), "--destination", str(dst)]
    assert migrator.main([*args, "--dry-run"]) == 0
    assert not dst.exists() and not run.exists()
    rename = os.rename

    def interrupt(path, dest):
        if dest == dst:
            raise OSError("interrupted after Runtime publish")
        return rename(path, dest)

    with monkeypatch.context() as patch:
        patch.setattr(migrator.os, "rename", interrupt)
        with pytest.raises(SystemExit, match="1"):
            migrator.main([*args, "--apply"])
    assert not dst.exists() and run.is_dir()
    assert migrator.main([*args, "--apply"]) == 0
    assert migrator.main([*args, "--apply"]) == 0
    assert migrator.main([*args, "--verify"]) == 0
    assert before == migrator.inventory(src)
    assert (dst / ".git" / "HEAD").read_bytes() == head
    git_root = subprocess.check_output(
        ["git", "-C", str(dst), "rev-parse", "--show-toplevel"],
        encoding="utf-8",
    )
    assert Path(git_root.strip()) == dst
    assert (dst / "records" / "memory" / "audit.jsonl").read_bytes() == event
    report = json.loads((run / migrator.REPORT).read_text(encoding="utf-8"))
    assert report["source"] == before
    assert report["counts"]["memory"] == 2 and report["counts"]["manual_review"] == 1
    dated_path = next((dst / "sessions").rglob("session-1.jsonl"))
    assert dated_path.read_bytes() == dated
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_timestamps_to_utc.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--vault",
            str(dst),
            "--source-timezone",
            "UTC",
            "--apply",
            "--backup-dir",
            str(tmp_path / "timestamp-backup"),
        ],
        check=True,
        capture_output=True,
    )
    from syvrio.memory.legacy_schema import parse_document as old_document
    from syvrio.memory.legacy_schema import source_fingerprint

    def old_entry(stem):
        path = next((dst / "memories").rglob(f"{stem}.md"))
        return old_document(path.read_text(encoding="utf-8"), str(path), stem).records[0].entry

    entry = old_entry("one")
    assert any(
        node.key == "syvrio.future_field" and node.value == "synthetic"
        for node in entry.extension_fields
    )
    marker = next(n.value for n in entry.extension_fields if n.key == "migration.evidence_v1")
    assert source_fingerprint(entry) == marker
    assert source_fingerprint(entry.model_copy(update={"status": "superseded"})) != marker
    assert old_entry("two").id.startswith("mem-legacy-")
    sessions = SessionStore(dst)
    assert sessions.load("session-1").started_at == "2026-02-01T01:02:03+00:00"
    assert sessions.load("session-2").path.read_bytes() == undated
    assert {row["id"] for row in sessions.list()} == {"session-1", "session-2"}
    assert sessions.load("session-2").path.parent.name == "undated"
    (dst / ".hidden").write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="1"):
        migrator.main([*args, "--verify"])
