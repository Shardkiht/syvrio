from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from syvrio.infra.storage.paths import RuntimeBindingError, SyvrioPaths
from syvrio.memory.models import MemoryContext
from syvrio.memory.store import MemoryStore


def test_index_inputs_and_pending_are_independent(service):
    entry = service.store.append(
        "events",
        "A synthetic exhibit",
        context=MemoryContext(
            location="Synthetic Harbor", emotion={"event": "delighted", "recollection": "nostalgic"}
        ),
    )
    assert entry.index_text == "\n".join((entry.text, "Synthetic Harbor", "delighted", "nostalgic"))
    assert entry.embedding_text == entry.text
    for query in ("exhibit", "Harbor", "delighted", "nostalgic"):
        assert service.recall(query)[0].id == entry.id
    from syvrio.memory.models import MemoryDraft

    before = service.store.revision()
    service.create_pending(MemoryDraft(topic="events", text="unreviewed"), "Confirm?", [])
    assert service.store.revision() == before and service.recall("unreviewed") == []
    service.store.set_status(entry.id, "deprecated")
    assert not service.recall("delighted")


def test_relative_topics_disambiguate_equal_stems(tmp_path):
    store = MemoryStore(tmp_path)
    a = store.append("work/notes", "synthetic first")
    b = store.append("education/notes", "synthetic second")
    assert a.topic != b.topic and store.entries(a.topic) == [a] and store.entries(b.topic) == [b]
    with pytest.raises(ValueError, match="(?i)ambiguous"):
        store.entries("notes")


def test_runtime_binding_preserves_paired_state_and_refuses_another_vault(tmp_path):
    from syvrio.backend.maintenance import prepare_vault_runtime

    paths = SyvrioPaths(tmp_path / "first", tmp_path / "runtime")
    marker = paths.state_dir / "vault-binding.json"
    marker.parent.mkdir(parents=True)
    binding = json.dumps({"version": 2, "vault": str(paths.vault_dir)}, indent=2).encode()
    marker.write_bytes(binding)
    cache = paths.log_dir / "diagnostic.log"
    cache.parent.mkdir()
    cache.write_bytes(b"synthetic index")
    prepare_vault_runtime(paths)
    with pytest.raises(RuntimeBindingError, match="different Vault"):
        prepare_vault_runtime(SyvrioPaths(tmp_path / "other", paths.runtime_dir))
    assert marker.read_bytes() == binding and cache.read_bytes() == b"synthetic index"


@pytest.mark.parametrize(
    "contents", [b"{", b"null", b'{"version":1}', b'{"version":2,"vault":"relative"}']
)
def test_damaged_runtime_binding_is_preserved(tmp_path, contents):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    marker = paths.state_dir / "vault-binding.json"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(contents)
    with pytest.raises(RuntimeBindingError, match="Invalid Runtime binding"):
        paths.bind()
    assert marker.read_bytes() == contents


@pytest.mark.parametrize("ordinary", [r"C:\data", r"\\server\share\data"])
def test_windows_runtime_compares_extended_and_ordinary_names(ordinary):
    from pathlib import PureWindowsPath

    from syvrio.infra.storage.paths import _comparison_path

    extended = (
        "\\\\?\\UNC\\" + ordinary[2:] if ordinary.startswith("\\\\") else "\\\\?\\" + ordinary
    )
    root = PureWindowsPath(ordinary) / "runtime"
    extended_root = PureWindowsPath(extended) / "runtime"
    assert _comparison_path(root / "jobs" / "background-memory").is_relative_to(
        _comparison_path(extended_root)
    )
    assert _comparison_path(extended_root / "jobs" / "background-memory").is_relative_to(
        _comparison_path(root)
    )
    assert not _comparison_path(PureWindowsPath(extended) / "runtime-other").is_relative_to(
        _comparison_path(root)
    )


def test_runtime_validation_checks_the_resolved_child(tmp_path, monkeypatch):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    paths.validate()
    invalid = paths.lock_dir
    resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        return tmp_path / "outside" if path == invalid else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="outside Runtime"):
        paths.validate()


@pytest.mark.skipif(os.name != "nt", reason="模拟 Windows 解析不存在子目录时返回的扩展盘符路径")
def test_runtime_accepts_extended_name_for_missing_windows_child(tmp_path, monkeypatch):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    missing = paths.lock_dir
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
