from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("ordinary", [r"C:\data", r"\\server\share\data"])
def test_windows_runtime_compares_extended_and_ordinary_names(ordinary):
    from pathlib import PureWindowsPath

    from syvrio.infra.storage.paths import _comparison_path

    extended = (
        "\\\\?\\UNC\\" + ordinary[2:] if ordinary.startswith("\\\\") else "\\\\?\\" + ordinary
    )
    root = PureWindowsPath(ordinary) / "runtime"
    extended_root = PureWindowsPath(extended) / "runtime"
    assert _comparison_path(root / "jobs" / "dreaming").is_relative_to(
        _comparison_path(extended_root)
    )
    assert _comparison_path(extended_root / "jobs" / "dreaming").is_relative_to(
        _comparison_path(root)
    )
    assert not _comparison_path(PureWindowsPath(extended) / "runtime-other").is_relative_to(
        _comparison_path(root)
    )


def test_runtime_validation_checks_the_resolved_child(tmp_path, monkeypatch):
    from syvrio.infra.storage.paths import SyvrioPaths

    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    paths.validate()
    invalid = paths.jobs_dir / "dreaming"
    resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        return tmp_path / "outside" if path == invalid else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="outside Runtime"):
        paths.validate()


@pytest.mark.skipif(os.name != "nt", reason="模拟 Windows 解析不存在子目录时返回的扩展盘符路径")
def test_runtime_accepts_extended_name_for_missing_windows_child(tmp_path, monkeypatch):
    from syvrio.infra.storage.paths import SyvrioPaths

    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    missing = paths.jobs_dir / "dreaming"
    resolve = Path.resolve

    def extended(path, *args, **kwargs):
        result = resolve(path, *args, **kwargs)
        if path != missing or str(result).startswith("\\\\?\\"):
            return result
        return (
            Path("\\\\?\\UNC\\" + str(result)[2:])
            if str(result).startswith("\\\\")
            else Path("\\\\?\\" + str(result))
        )

    monkeypatch.setattr(Path, "resolve", extended)
    paths.validate()
