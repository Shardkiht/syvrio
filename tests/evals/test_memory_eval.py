from __future__ import annotations

import json
import os
import sys
from collections import Counter
from types import SimpleNamespace

import pytest

from evals import field_lookup_eval, memory_eval, short_facts_eval
from evals.retrieval_eval import load_data
from evals.sandbox import eval_environment
from syvrio.infra.storage.paths import SyvrioPaths
from syvrio.memory.store import MemoryStore

pytestmark = pytest.mark.memory


@pytest.mark.parametrize("case", memory_eval.CASES, ids=lambda case: case.name)
def test_synthetic_memory_regression(case, tmp_path):
    assert memory_eval.run_case(case, tmp_path / "synthetic") is None


def test_contextual_recall_synthetic_comparison(tmp_path):
    result = memory_eval.run_contextual_eval(tmp_path / "contextual")
    assert result["contextual"] == {"Recall@1": 1.0, "Hit@3": 1.0, "Hit@5": 1.0, "MRR@5": 1.0}
    assert result["baseline"]["Recall@1"] < result["contextual"]["Recall@1"]
    assert result["baseline"]["MRR@5"] < result["contextual"]["MRR@5"]


def test_evaluation_preserves_inherited_runtime_and_vault(tmp_path, monkeypatch):
    outside = SyvrioPaths(tmp_path / "existing-vault", tmp_path / "existing-runtime")
    outside.bind()
    existing = {
        "SYVRIO_VAULT": outside.vault_dir,
        "SYVRIO_RUNTIME": outside.runtime_dir,
        "SYVRIO_CONFIG": tmp_path / "existing-config.toml",
    }
    for key, path in existing.items():
        monkeypatch.setenv(key, str(path))
    index = outside.index_dir / "vectors.npz"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"synthetic index")
    binding = outside.state_dir / "vault-binding.json"
    previous = binding.read_bytes()

    for name in ("first", "second"):
        root = tmp_path / name
        assert memory_eval.run_case(memory_eval.CASES[0], root) is None
        current = json.loads((root / "runtime/state/vault-binding.json").read_bytes())
        assert current["vault"] == str((root / "vault").resolve())
    memory_eval.run_contextual_eval(tmp_path / "filters")
    with pytest.raises(RuntimeError), eval_environment(tmp_path / "failing"):
        raise RuntimeError
    assert all(os.environ[key] == str(path) for key, path in existing.items())
    assert index.read_bytes() == b"synthetic index"
    assert binding.read_bytes() == previous
    assert not outside.vault_dir.exists()


def test_memory_cli_rejects_a_failed_filter_contract(monkeypatch):
    monkeypatch.setattr(memory_eval, "CASES", [])
    monkeypatch.setattr(
        memory_eval, "run_contextual_eval", lambda _path: {"contextual": {"Recall@1": 0.0}}
    )
    assert memory_eval.main() != 0


def test_short_field_legacy_scores_each_frozen_question_with_all_targets(tmp_path):
    with eval_environment(tmp_path) as vault:
        short_facts_eval.seed_vault(vault, short_fields=False)
        ids = {entry.id for entry in MemoryStore(vault).entries()}
    cases = short_facts_eval.legacy_cases()
    _, frozen = load_data()
    assert len(cases) == len({query for query, _ in cases}) == len(frozen)
    assert any(len(targets) > 1 for _, targets in cases)
    assert all(targets and targets <= ids for _, targets in cases)
    assert not any(ident.startswith("short-") for ident in ids)


@pytest.mark.parametrize("evaluation", [short_facts_eval, field_lookup_eval])
def test_field_benchmarks_call_recall_using_both_configured_limits(evaluation, monkeypatch):
    calls = Counter()

    class CaptureService:
        retrieval_mode = "hybrid"
        retriever = SimpleNamespace(
            vector=SimpleNamespace(sync=lambda: None, degraded=False, _mark_degraded=lambda: None)
        )

        def __init__(self, config):
            self.config = config
            self.store = MemoryStore(config.vault)

        def current_facts(self):
            return self.store.entries()

        def recall(self, query):
            calls[query, self.config.retrieval.default_limit] += 1
            return []

    monkeypatch.setattr(evaluation, "MemoryService", CaptureService)
    monkeypatch.setattr(sys, "argv", ["benchmark"])
    evaluation.main()
    questions = (
        [q for _, q in short_facts_eval.SHORT_CASES]
        + [q for q, _ in short_facts_eval.legacy_cases()]
        if evaluation is short_facts_eval
        else [q for _, q in field_lookup_eval.CASES]
    )
    assert calls == Counter((query, limit) for query in questions for limit in (7, 10))
