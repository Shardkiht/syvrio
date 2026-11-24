"""固定合成检索基准中每个问题的得分。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from statistics import mean

LIMITS = (7, 10)


@dataclass(frozen=True)
class RankedResults:
    ids: list[str]
    tool_tokens: int | None = None


def target_indices(case: dict) -> list[int]:
    if ("target" in case) == ("targets" in case):
        raise ValueError("Each question must have either target or targets.")
    return case["targets"] if "targets" in case else [case["target"]]


def validate_data(corpus: list[dict], cases: list[dict]) -> None:
    if not corpus or not cases:
        raise ValueError("The frozen corpus and questions must both be nonempty.")
    for row in corpus:
        if not all(
            isinstance(row.get(key), str) and row[key].strip() for key in ("category", "text")
        ):
            raise ValueError("Every corpus entry requires a category and text.")
    queries = set()
    for case in cases:
        query = case.get("query")
        if not isinstance(query, str) or not query.strip() or query.strip() in queries:
            raise ValueError(f"Empty or duplicate query: {query!r}")
        queries.add(query.strip())
        if not isinstance(case.get("category"), str) or not case["category"].strip():
            raise ValueError(f"Missing category: {query}")
        targets = target_indices(case)
        if (
            not isinstance(targets, list)
            or not targets
            or any(type(i) is not int or not 0 <= i < len(corpus) for i in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError(f"Invalid target indices: {query}")


def paired_evaluation(
    cases: list[dict],
    corpus_ids: list[str],
    search: Callable[[str, int], RankedResults],
) -> dict:
    valid_ids = set(corpus_ids)
    rows = []
    for index, case in enumerate(cases):
        targets = [corpus_ids[i] for i in target_indices(case)]
        runs = {}
        for limit in LIMITS:
            result = search(case["query"], limit)
            ids = result.ids
            if len(ids) > limit or len(set(ids)) != len(ids) or not valid_ids.issuperset(ids):
                raise ValueError(f"Invalid returned IDs for question {index}, limit={limit}")
            rank = next((r for r, ident in enumerate(ids, 1) if ident in targets), None)
            runs[str(limit)] = {"ids": ids, "rank": rank}
            if result.tool_tokens is not None:
                runs[str(limit)]["tool_tokens"] = result.tool_tokens

        small, large = (runs[str(limit)] for limit in LIMITS)
        first_ids, next_ids = small["ids"], large["ids"]
        shared_prefix = first_ids == next_ids[: LIMITS[0]]
        if small["rank"] is None and large["rank"] is not None:
            change = "gained_in_tail" if large["rank"] > LIMITS[0] else "gained_within_first_7"
        elif small["rank"] is not None and large["rank"] is None:
            change = "lost"
        else:
            change = "retained_hit" if small["rank"] is not None else "missed_both"
        rows.append(
            {
                "case_index": index,
                "query": case["query"],
                "category": case["category"],
                "target_ids": targets,
                "runs": runs,
                "same_top7": shared_prefix,
                "added_ids": [i for i in next_ids if i not in first_ids],
                "dropped_ids": [i for i in first_ids if i not in next_ids],
                "hit_change": change,
            }
        )

    metrics = {}
    for limit in LIMITS:
        observations = [row["runs"][str(limit)] for row in rows]
        ranks = [o["rank"] for o in observations]
        hit_counts = {k: sum(r is not None and r <= k for r in ranks) for k in (1, 3, 5, limit)}
        summary = {
            "questions": len(rows),
            "hit_counts": {f"Hit@{k}": n for k, n in hit_counts.items()},
            "hit_rates": {f"Hit@{k}": n / len(rows) for k, n in hit_counts.items()},
            f"MRR@{limit}": mean(1 / r if r else 0 for r in ranks),
        }
        by_category = defaultdict(list)
        for row, rank in zip(rows, ranks, strict=True):
            by_category[row["category"]].append(rank is not None and rank <= 3)
        summary["category_hit_at_3"] = {
            category: {"hits": sum(hits), "questions": len(hits)}
            for category, hits in by_category.items()
        }
        if "tool_tokens" in observations[0]:
            summary["mean_tool_tokens"] = mean(o["tool_tokens"] for o in observations)
        metrics[str(limit)] = summary

    paired = {
        "same_top7": sum(row["same_top7"] for row in rows),
        "different_top7": sum(not row["same_top7"] for row in rows),
        **{
            change: sum(row["hit_change"] == change for row in rows)
            for change in (
                "gained_in_tail",
                "gained_within_first_7",
                "lost",
                "retained_hit",
                "missed_both",
            )
        },
    }
    return {"metrics": metrics, "paired": paired, "cases": rows}
