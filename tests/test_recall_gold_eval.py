from __future__ import annotations

import json
import math

import pytest

from evals.recall_gold_eval import DATA, build_fixture, evaluate, score
from syvrio.agent.registry import ToolRegistry

pytestmark = pytest.mark.retrieval


def test_evidence_coverage_does_not_double_count_two_sources_for_one_fact():
    catalog = {"a": "location", "b": "location", "c": "colour", "d": "former", "n": None}
    case = {"required": ["location", "colour"], "misleading": ["former"]}
    row = score(case, ["d", "a", "b", "n"], catalog, 7)
    assert row["precision_at_k"] == 2 / 7
    assert row["precision_returned"] == 2 / 4
    assert row["recall_at_k"] == 2 / 3
    assert row["fact_coverage"] == 0.5
    assert row["missing_slots"] == ["colour"]
    assert not row["complete"]
    assert (row["first_misleading"], row["first_relevant"]) == (1, 2)
    assert row["ndcg_at_k"] == pytest.approx(
        (1 / math.log2(3) + 0.5) / (1 + 1 / math.log2(3) + 0.5)
    )
    empty = score({"required": [], "misleading": ["former"]}, ["n"], catalog, 7)
    assert empty["first_misleading"] is None
    assert "recall_at_k" not in empty


def test_frozen_labels_are_grounded_in_the_corpus():
    data = json.loads(DATA.read_bytes())
    build_fixture(data)
    data["cases"][0]["required"].append("missing slot")
    with pytest.raises(ValueError, match="evidence labels"):
        build_fixture(data)
    data["cases"][0]["required"].pop()
    data["alternative_records"][0][1] = "A conflicting value"
    with pytest.raises(ValueError, match="not grounded"):
        build_fixture(data)


def test_tool_returns_scored_evidence_on_both_default_settings(monkeypatch):
    calls = []
    execute = ToolRegistry.execute

    def observe(registry, name, arguments):
        if name == "recall":
            calls.append(arguments.copy())
        return execute(registry, name, arguments)

    monkeypatch.setattr(ToolRegistry, "execute", observe)
    report = evaluate(DATA, "keyword")
    assert report["limits"] == [7, 10]
    assert calls == [{"query": row["query"]} for _ in report["limits"] for row in report["cases"]]
    assert report["metrics"]["7"]["answerable"] + report["metrics"]["7"]["unanswerable"] == len(
        report["cases"]
    )
    assert report["catalog"]
    for k in ("7", "10"):
        metrics = report["metrics"][k]
        groups = metrics["complete_by_required_facts"].values()
        assert sum(group["questions"] for group in groups) == metrics["answerable"]
        assert sum(group["complete"] for group in groups) == metrics["complete"]
        assert (
            metrics["unanswerable_with_labeled_decoy"] <= metrics["unanswerable_with_decoy_labels"]
        )
    assert report["metrics"]["7"]["complete_by_required_facts"]["over_7"]["complete"] == 0
    for row in report["cases"]:
        for k in report["limits"]:
            run = row["runs"][str(k)]
            ids = run["ids"]
            assert len(ids) <= k
            found = {report["catalog"][i]["slot"] for i in ids}
            assert set(run["missing_slots"]) == row["gold_values"].keys() - found
            assert run["tool_tokens"] > 0
