"""检索评测：读取LLM生成并冻结的语料/题库，对比 keyword / vector / hybrid 三路。

语料与题库由 ``evals/generate_retrieval_data.py`` 用真实 LLM 一次性生成后冻结为
``evals/data/retrieval_corpus.json`` 与 ``evals/data/retrieval_cases.json``。本脚本
评测时**不再调用 LLM**，只读这两份固定文件，因此不同分支跑的是逐字节相同的同一批
数据，可复现、可横向对比。内容全为虚构占位，不含真实个人信息。

用例以“目标条目 ID”判命中：语料按顺序写入临时 vault 时记录每条对应的条目 ID，
命中该 ID 即算相关，避免用子串误判。

用法：
    uv run python evals/retrieval_eval.py            # 全部三路（需 local-embed extra）
    uv run python evals/retrieval_eval.py --keyword  # 只跑 BM25，离线可用
    uv run python evals/retrieval_eval.py --failures # 打印未过 @3 的用例
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from syvrio.infra.config import Config, LLMConfig, RetrievalConfig
from syvrio.memory.service import MemoryService
from syvrio.retrieval import KeywordRetriever
from syvrio.retrieval.vector import LocalEmbedder, VectorRetriever

DATA_DIR = Path(__file__).parent / "data"
CORPUS_FILE = DATA_DIR / "retrieval_corpus.json"
CASES_FILE = DATA_DIR / "retrieval_cases.json"


def load_data() -> tuple[list[dict], list[dict]]:
    if not CORPUS_FILE.exists() or not CASES_FILE.exists():
        raise SystemExit(
            "缺少冻结数据；先运行：uv run python evals/generate_retrieval_data.py"
        )
    corpus = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return corpus, cases


def evaluate(ids: list[str], retrievers: dict, cases: list[dict]) -> dict:
    results: dict[str, dict] = {}
    for name, retriever in retrievers.items():
        p3 = p5 = 0
        mrr = 0.0
        per_category: dict[str, list[bool]] = {}
        failures = []
        for case in cases:
            target_ids = {ids[t] for t in case.get("targets") or [case["target"]]}
            hits = retriever.search(case["query"], 10)
            ranks = [i + 1 for i, hit in enumerate(hits) if hit.id in target_ids]
            rank = min(ranks) if ranks else None
            ok3 = rank is not None and rank <= 3
            ok5 = rank is not None and rank <= 5
            p3 += ok3
            p5 += ok5
            mrr += 1.0 / rank if rank else 0.0
            per_category.setdefault(case["category"], []).append(ok3)
            if not ok3:
                top = hits[0].text[:36].replace("\n", " ") if hits else "无结果"
                failures.append(f"  [{case['category']}] {case['query']} → {top}")
        results[name] = {
            "p3": p3 / len(cases),
            "p5": p5 / len(cases),
            "mrr": mrr / len(cases),
            "per_category": per_category,
            "failures": failures,
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="检索评测（LLM生成并冻结的合成语料）")
    parser.add_argument("--keyword", action="store_true", help="只跑 BM25（离线）")
    parser.add_argument("--failures", action="store_true", help="打印未过 @3 的用例")
    args = parser.parse_args()

    corpus, cases = load_data()

    with tempfile.TemporaryDirectory(prefix="syvrio-eval-") as tmp:
        vault = Path(tmp) / "vault"
        config = Config(vault=vault, llm=LLMConfig(), retrieval=RetrievalConfig(method="keyword"))
        service = MemoryService(config)
        service.init_vault()
        ids: list[str] = []
        known: set[str] = set()
        for item in corpus:
            entry = service.store.append(
                item["category"], item["text"], source="eval", confidence=1.0, known_ids=known
            )
            known.add(entry.id)
            ids.append(entry.id)
        print(f"语料 {len(ids)} 条 · 用例 {len(cases)} 例\n")

        retrievers: dict = {"keyword": KeywordRetriever(service.store)}
        if not args.keyword:
            if not LocalEmbedder.available():
                raise SystemExit("未安装 local-embed extra，先用 --keyword 或 uv sync --extra local-embed")
            embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5")
            vector = VectorRetriever(service.store, embedder)
            retrievers["vector"] = vector
            retrievers["hybrid"] = _hybrid(service, vector)

        results = evaluate(ids, retrievers, cases)
        print(f"{'':<10}{'Recall@3':>10}{'Recall@5':>10}{'MRR@10':>9}")
        for name, r in results.items():
            print(f"{name:<10}{r['p3']:>9.0%}{r['p5']:>10.0%}{r['mrr']:>9.3f}")
        print("\n分类 Recall@3：")
        print(f"{'':<8}" + "".join(f"{name:>10}" for name in results))
        categories = [c["category"] for c in cases]
        for category in dict.fromkeys(categories):
            line = f"{category:<8}"
            for name, r in results.items():
                rows = r["per_category"][category]
                line += f"{sum(rows) / len(rows):>10.0%}"
            print(line)
        if args.failures:
            for name, r in results.items():
                if r["failures"]:
                    print(f"\n{name} 未过 @3：")
                    print("\n".join(r["failures"]))


def _hybrid(service: MemoryService, vector: VectorRetriever):
    from syvrio.retrieval import HybridRetriever

    return HybridRetriever(KeywordRetriever(service.store), vector)


if __name__ == "__main__":
    main()
