"""用完整标注的合成事实表评估实际使用的召回工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from syvrio.agent.tools import build_tools
from syvrio.infra.config import Config, EmbeddingConfig, RetrievalConfig
from syvrio.infra.session import SessionStore
from syvrio.memory.service import MemoryService

if __package__:
    from .retrieval_fixture import MODEL, guard_vector, run_metadata, seed, tool_search
    from .retrieval_metrics import LIMITS
    from .sandbox import eval_environment
else:
    from retrieval_fixture import MODEL, guard_vector, run_metadata, seed, tool_search
    from retrieval_metrics import LIMITS
    from sandbox import eval_environment

DATA = Path(__file__).parent / "data" / "recall_gold.json"


def build_fixture(data: dict) -> tuple[list[dict], list[str | None], dict[str, str]]:
    """只将记忆正文写入 Vault，所属评分项仅保存在评分依据中。"""
    records = []
    slots = []
    values = {}
    fields = data["fields"]
    if not fields or len(set(fields)) != len(fields):
        raise ValueError("Fact fields must be unique and nonempty")
    for entity, period, row in data["batches"]:
        for field, value in zip(fields, row, strict=True):
            slot = f"{entity}/{period}/{field}"
            if slot in values or not value.strip():
                raise ValueError(f"Duplicate slot or empty fact: {slot}")
            values[slot] = value
            records.append(
                {"category": entity, "text": f"{entity}的{period}登记，{field}为{value}。"}
            )
            slots.append(slot)
    for slot, value, text in data["notes"]:
        if slot in values or len(slot.split("/")) != 3 or not value.strip() or value not in text:
            raise ValueError(f"Invalid annotated note: {slot}")
        values[slot] = value
        records.append({"category": "练习手记", "text": text})
        slots.append(slot)
    for slot, text in data["alternative_records"]:
        if slot not in values or not all(
            part in text for part in (values[slot], *slot.split("/")[:2])
        ):
            raise ValueError(
                f"Alternative record is not grounded in its entity, period and value: {slot}"
            )
        records.append({"category": slot.split("/")[0], "text": text})
        slots.append(slot)
    for text in data["background"]:
        records.append({"category": "说明", "text": text})
        slots.append(None)
    if len({row["text"] for row in records}) != len(records):
        raise ValueError("Duplicate memory text")

    cases = data["cases"]
    if not cases or any(len({c[key] for c in cases}) != len(cases) for key in ("id", "query")):
        raise ValueError("Expected a nonempty set of unique question IDs and queries")
    for case in cases:
        required, misleading = (set(case[key]) for key in ("required", "misleading"))
        if (
            not case["query"].strip()
            or (not required) != case.get("unanswerable", False)
            or len(required) != len(case["required"])
            or len(misleading) != len(case["misleading"])
            or required & misleading
            or (required | misleading) - values.keys()
        ):
            raise ValueError(f"Invalid evidence labels for {case['id']}")
    return records, slots, values


def score(case: dict, returned: list[str], catalog: dict[str, str | None], k: int) -> dict:
    if len(returned) > k or len(set(returned)) != len(returned) or set(returned) - catalog.keys():
        raise ValueError("Tool returned unknown, repeated or excess memory IDs")
    required, misleading = set(case["required"]), set(case["misleading"])
    relevant_ids = {key for key, slot in catalog.items() if slot in required}
    ranks = [i for i, key in enumerate(returned, 1) if key in relevant_ids]
    wrong = [i for i, key in enumerate(returned, 1) if catalog[key] in misleading]
    found = {catalog[returned[i - 1]] for i in ranks}
    result = {
        "ids": returned,
        "relevant_records": len(relevant_ids),
        "relevant_returned": len(ranks),
        "missing_slots": sorted(required - found),
        "first_relevant": ranks[0] if ranks else None,
        "first_misleading": wrong[0] if wrong else None,
    }
    if not required:
        return result
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(k, len(relevant_ids)) + 1))
    result.update(
        precision_at_k=len(ranks) / k,
        precision_returned=len(ranks) / len(returned) if returned else 0.0,
        recall_at_k=len(ranks) / len(relevant_ids),
        ndcg_at_k=sum(1 / math.log2(i + 1) for i in ranks) / ideal,
        fact_coverage=len(found) / len(required),
        complete=found == required,
    )
    return result


def summarize(rows: list[dict], k: int) -> dict:
    answerable = [row["runs"][str(k)] for row in rows if row["gold_values"]]
    empty = [row["runs"][str(k)] for row in rows if not row["gold_values"]]
    conflicts = [
        row["runs"][str(k)] for row in rows if row["gold_values"] and row["misleading_slots"]
    ]
    by_fact_count = {"1": [], "2_to_7": [], "over_7": []}
    for row in rows:
        count = len(row["gold_values"])
        if count:
            group = "1" if count == 1 else "2_to_7" if count <= LIMITS[0] else "over_7"
            by_fact_count[group].append(row["runs"][str(k)]["complete"])
    return {
        "answerable": len(answerable),
        **{
            key: mean(row[key] for row in answerable) if answerable else None
            for key in (
                "precision_at_k",
                "precision_returned",
                "recall_at_k",
                "ndcg_at_k",
                "fact_coverage",
            )
        },
        "complete": sum(row["complete"] for row in answerable),
        "any_hit": sum(row["first_relevant"] is not None for row in answerable),
        "complete_by_required_facts": {
            group: {"questions": len(flags), "complete": sum(flags)}
            for group, flags in by_fact_count.items()
        },
        "conflict_questions": len(conflicts),
        "misleading_before_correct": sum(
            row["first_misleading"] is not None
            and (row["first_relevant"] is None or row["first_misleading"] < row["first_relevant"])
            for row in conflicts
        ),
        "unanswerable": len(empty),
        "unanswerable_with_decoy_labels": sum(
            not row["gold_values"] and bool(row["misleading_slots"]) for row in rows
        ),
        "unanswerable_with_candidates": sum(bool(row["ids"]) for row in empty),
        "unanswerable_with_labeled_decoy": sum(
            row["first_misleading"] is not None for row in empty
        ),
        "mean_tool_tokens_all_questions": mean(row["runs"][str(k)]["tool_tokens"] for row in rows),
    }


def evaluate(data_path: Path, method: str) -> dict:
    raw = data_path.read_bytes()
    data = json.loads(raw)
    records, slots, values = build_fixture(data)
    rows = [
        {
            "id": c["id"],
            "query": c["query"],
            "gold_values": {key: values[key] for key in c["required"]},
            "misleading_slots": c["misleading"],
            "runs": {},
        }
        for c in data["cases"]
    ]
    with (
        TemporaryDirectory(prefix="syvrio-recall-gold-") as tmp,
        eval_environment(Path(tmp)) as vault,
    ):
        config = Config(
            vault=vault,
            retrieval=RetrievalConfig(
                method=method,
                embedding=EmbeddingConfig(backend="local", model=MODEL),
            ),
        )
        config.search.enabled = False
        config.fetch.providers = []
        service = MemoryService(config)
        if service.retrieval_mode != method:
            raise RuntimeError(f"Requested {method}, got {service.retrieval_mode}")
        ids = seed(service, records)
        catalog = dict(zip(ids, slots, strict=True))
        texts = {key: row["text"] for key, row in zip(ids, records, strict=True)}
        vector = service.retriever.vector if method == "hybrid" else None
        with guard_vector(vector):
            if vector is not None:
                vector.sync()
            for k in LIMITS:
                config.retrieval.default_limit = k
                tools = build_tools(service, SessionStore(service.paths).create(), config=config)
                for row, case in zip(rows, data["cases"], strict=True):
                    hit = tool_search(tools, case["query"], texts)
                    row["runs"][str(k)] = {
                        **score(case, hit.ids, catalog, k),
                        "tool_tokens": hit.tool_tokens,
                    }
        environment = run_metadata(method)
    small, large = map(str, LIMITS)
    changes = defaultdict(list)
    for row in rows:
        before, after = row["runs"][small], row["runs"][large]
        row["same_top7"] = before["ids"] == after["ids"][: LIMITS[0]]
        row["gained_slots"] = sorted(set(before["missing_slots"]) - set(after["missing_slots"]))
        row["lost_slots"] = sorted(set(after["missing_slots"]) - set(before["missing_slots"]))
        if row["gold_values"]:
            if before["complete"] != after["complete"]:
                changes["gained_complete" if after["complete"] else "lost_complete"].append(
                    row["id"]
                )
    return {
        "schema_version": 1,
        "environment": environment,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "method": method,
        "embedding_model": MODEL if method == "hybrid" else None,
        "records": len(records),
        "slots": len(values),
        "limits": list(LIMITS),
        "metrics": {str(k): summarize(rows, k) for k in LIMITS},
        "gained_complete": changes["gained_complete"],
        "lost_complete": changes["lost_complete"],
        "same_top7": sum(row["same_top7"] for row in rows),
        "catalog": {key: {"slot": slot, "text": texts[key]} for key, slot in catalog.items()},
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keyword", action="store_true", help="Run keyword retrieval without local embeddings"
    )
    parser.add_argument(
        "--json", type=Path, help="Save scores, ground truth and returned IDs for every question"
    )
    args = parser.parse_args()
    report = evaluate(DATA, "keyword" if args.keyword else "hybrid")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"cases", "catalog"}},
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.json:
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
