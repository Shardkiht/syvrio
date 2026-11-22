"""Scoring and production-tool wiring for the synthetic, paired retrieval benchmark."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evals import retrieval_eval
from evals.retrieval_metrics import RankedResults, paired_evaluation, validate_data
from syvrio.retrieval import LocalEmbedder

pytestmark = pytest.mark.retrieval


def test_real_calls_and_one_vote_per_question():
    ids = [f"entry-{i}" for i in range(10)]
    cases = [
        {"query": "tail", "category": "a", "targets": [8, 9]},
        {"query": "lost", "category": "a", "target": 5},
        {"query": "reordered", "category": "b", "target": 7},
    ]
    high = {
        "tail": ids,
        "lost": ids[:5] + ids[6:],
        "reordered": [ids[7], *ids[:7], *ids[8:]],
    }
    calls = []

    def search(query, limit):
        calls.append((query, limit))
        result = ids[:7] if limit == 7 else high[query]
        return RankedResults(result, len(result) * 3)

    report = paired_evaluation(cases, ids, search)
    assert calls == [(row["query"], k) for row in cases for k in (7, 10)]
    paired = report["paired"]
    assert (paired["same_top7"], paired["different_top7"]) == (1, 2)
    assert (paired["gained_in_tail"], paired["gained_within_first_7"], paired["lost"]) == (1, 1, 1)
    assert report["cases"][0]["runs"]["10"]["rank"] == 9
    assert report["cases"][1]["dropped_ids"] == [ids[5]]
    metrics = report["metrics"]["10"]
    assert metrics["hit_counts"] == {"Hit@1": 1, "Hit@3": 1, "Hit@5": 1, "Hit@10": 2}
    assert metrics["hit_rates"]["Hit@10"] == 2 / 3
    assert metrics["MRR@10"] == pytest.approx((1 + 1 / 9) / 3)
    assert metrics["mean_tool_tokens"] == 29


@pytest.mark.parametrize(
    "labels",
    [
        {"target": -1},
        {"target": 1},
        {"target": True},
        {"targets": []},
        {"targets": [0, 0]},
        {"target": 0, "targets": [0]},
    ],
)
def test_target_labels_are_validated(labels):
    with pytest.raises(ValueError):
        validate_data(
            [{"category": "a", "text": "evidence"}], [{"category": "a", "query": "q", **labels}]
        )


def freeze(tmp_path, monkeypatch, corpus, cases):
    for key, rows in (("CORPUS_FILE", corpus), ("CASES_FILE", cases)):
        path = tmp_path / (key.lower() + ".json")
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(retrieval_eval, key, path)


def test_keyword_recall_tool_runs_against_frozen_synthetic_data(tmp_path, monkeypatch):
    # Keep this a fast integration test. The CLI benchmarks the complete corpus.
    corpus = [{"category": "items", "text": f"合成编号清单项目 {i} 的记录。"} for i in range(12)]
    cases = [{"category": "items", "query": "合成编号清单", "target": 0}]
    freeze(tmp_path, monkeypatch, corpus, cases)
    from syvrio.agent.registry import ToolRegistry

    original = ToolRegistry.execute
    calls = []

    def record(tools, name, arguments):
        calls.append(arguments.copy())
        return original(tools, name, arguments)

    monkeypatch.setattr(ToolRegistry, "execute", record)
    report = retrieval_eval.run(tmp_path, keyword_only=True)
    assert calls == [{"query": cases[0]["query"]}] * 2
    assert report["tool_limit_argument"] == {"7": "omitted", "10": "omitted"}
    assert set(report["results"]) == {"raw_keyword", "recall_tool_keyword"}
    row = report["results"]["recall_tool_keyword"]["cases"][0]
    for k in (7, 10):
        assert len(row["runs"][str(k)]["ids"]) == k
        assert row["runs"][str(k)]["tool_tokens"] > 0
    assert "tool_tokens" not in report["results"]["raw_keyword"]["cases"][0]["runs"]["10"]
    assert json.loads(json.dumps(report)) == report


def test_tool_evidence_checks_text_for_each_returned_id():
    tools = SimpleNamespace(
        execute=lambda *args: SimpleNamespace(
            ok=True,
            content='{"results": [{"id": "one", "text": "other record"}]}',
        )
    )
    with pytest.raises(ValueError, match="frozen text"):
        retrieval_eval.tool_search(tools, "query", {"one": "expected evidence"})


def test_query_embedding_failure_aborts_instead_of_reporting_hybrid(tmp_path, monkeypatch):
    corpus = [{"category": "items", "text": "合成编号清单"}]
    cases = [{"category": "items", "query": "合成编号", "target": 0}]
    freeze(tmp_path, monkeypatch, corpus, cases)
    monkeypatch.setattr(LocalEmbedder, "available", staticmethod(lambda: True))
    monkeypatch.setattr(LocalEmbedder, "embed", lambda self, texts: [[1.0, 0.0] for _ in texts])
    responses = iter(([1.0, 0.0], [float("nan"), 1.0]))
    monkeypatch.setattr(LocalEmbedder, "embed_query", lambda self, text: next(responses))

    with pytest.raises(RuntimeError, match="embedding degraded"):
        retrieval_eval.run(tmp_path, keyword_only=False)
