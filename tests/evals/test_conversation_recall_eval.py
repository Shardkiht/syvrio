from __future__ import annotations

import json

import pytest

from evals.conversation_recall_eval import (
    DATA,
    RecordingModel,
    evaluate,
    evidence_score,
    load_corpus,
)
from syvrio.agent.context import MEMORY_TEMPLATE
from syvrio.agent.recall import memory_references
from syvrio.agent.registry import ToolRegistry
from syvrio.infra.retrieval import vector
from syvrio.memory.models import RecallEntry


def test_conflicts_and_unanswerable_cases_score_only_labeled_evidence():
    case = {"required": ["one", "two"], "forbidden": ["near-match"]}
    score = evidence_score(case, ["one", "near-match", "two"])
    assert score["all_required"] and not score["forbidden_before_target"]
    assert score["forbidden_before_all_evidence"]
    missing = evidence_score(case, ["near-match"])
    assert not missing["all_required"] and missing["forbidden_before_target"]
    neutral = evidence_score({"required": [], "forbidden": ["near-match"]}, ["related-context"])
    assert neutral["target_first"] is None and neutral["forbidden_hits"] == []


def test_conversation_eval_uses_production_prefetch_and_configured_tool_limits(monkeypatch):
    fixture = json.loads(DATA.read_text(encoding="utf-8"))
    execute = ToolRegistry.execute
    explicit_calls = []

    def observe_execute(registry, name, arguments):
        if name == "recall":
            explicit_calls.append(arguments.copy())
        return execute(registry, name, arguments)

    monkeypatch.setattr(ToolRegistry, "execute", observe_execute)
    report = evaluate()
    assert explicit_calls == [
        {"query": case["query"]} for case in fixture["cases"] for _ in (7, 10)
    ]
    rows = {row["id"]: row for row in report["rows"]}
    assert rows["followup-positive"]["triggered"]
    assert "潮纹组" in rows["followup-positive"]["auto_query"]
    assert rows["current"]["auto_limit"] == 3 and rows["historical"]["auto_limit"] == 7
    assert rows["multiple-evidence"]["required"] == ["glider-material", "glider-weight"]
    assert rows["chitchat"]["auto"]["returned"] == []
    assert report["trigger"]["unexpected_injection"] == 0
    assert any(len(r["explicit_10"]["returned"]) > 7 for r in rows.values())
    for row in rows.values():
        assert row["explicit_7"]["returned"] == row["explicit_10"]["returned"][:7]
        assert len(row["explicit_10"]["returned"]) <= 10


def test_hybrid_run_rejects_results_even_after_embedding_recovers(monkeypatch):
    queries = []

    def intermittent_query(self, text):
        queries.append(text)
        if len(queries) == 1:
            raise RuntimeError("Synthetic query embedding failure")
        return [1.0, 0.0]

    monkeypatch.setattr(vector, "RETRY_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(vector.LocalEmbedder, "available", staticmethod(lambda: True))
    monkeypatch.setattr(
        vector.LocalEmbedder, "embed", lambda self, texts: [[1.0, 0.0]] * len(texts)
    )
    monkeypatch.setattr(vector.LocalEmbedder, "embed_query", intermittent_query)
    with pytest.raises(RuntimeError, match="embedding degraded"):
        evaluate("hybrid")
    assert len(queries) > 1


@pytest.mark.parametrize(
    ("records", "error"),
    [
        ([("one", "text")], None),
        ([("missing", "text")], "frozen text"),
        ([("one", "changed")], "frozen text"),
        ([("one", "text"), ("one", "text")], "Duplicate recall evidence ID"),
    ],
)
def test_auto_injected_evidence_is_verified_from_the_model_message(records, error):
    model = RecordingModel({"one": "text"})
    entries = [RecallEntry(id=key, topic="samples", text=value) for key, value in records]
    message = {"role": "user", "content": MEMORY_TEMPLATE.format(memory_references(entries))}
    if error:
        with pytest.raises(ValueError, match=error):
            model.chat([message], [])
    else:
        model.chat([message], [])
        assert model.ids == ["one"]


@pytest.mark.parametrize(
    ("required", "forbidden"),
    [(["unknown"], []), (["one", "one"], []), ([], ["one", "one"]), (["one"], ["one"])],
)
def test_conversation_fixture_rejects_invalid_labels_on_load(tmp_path, required, forbidden):
    case = {"id": "test", "required": required, "forbidden": forbidden}
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"facts": {"one": "text"}, "cases": [case]}), encoding="utf-8")
    with pytest.raises(ValueError, match="evidence labels"):
        load_corpus(path)
