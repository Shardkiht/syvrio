"""在固定合成语料上对比前 7 条和前 10 条检索结果，无需对话模型。

python evals/retrieval_eval.py [--keyword] [--failures] [--json PATH]
python -m evals.retrieval_eval --json -  # 向标准输出写入机器可读的报告
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from syvrio.agent.tools import build_tools
from syvrio.infra.config import Config, EmbeddingConfig, RetrievalConfig
from syvrio.infra.session import SessionStore
from syvrio.memory.service import MemoryService

if __package__:
    from .retrieval_fixture import MODEL, guard_vector, run_metadata, seed, tool_search
    from .retrieval_metrics import LIMITS, RankedResults, paired_evaluation, validate_data
    from .sandbox import eval_environment
else:  # 按文档运行脚本时，evals/ 已在 sys.path 中。
    from retrieval_fixture import MODEL, guard_vector, run_metadata, seed, tool_search
    from retrieval_metrics import LIMITS, RankedResults, paired_evaluation, validate_data
    from sandbox import eval_environment

DATA_DIR = Path(__file__).parent / "data"
CORPUS_FILE = DATA_DIR / "retrieval_corpus.json"
CASES_FILE = DATA_DIR / "retrieval_cases.json"


def _read_frozen_data() -> tuple[list[dict], list[dict], dict[str, str]]:
    corpus_bytes, case_bytes = CORPUS_FILE.read_bytes(), CASES_FILE.read_bytes()
    corpus, cases = json.loads(corpus_bytes), json.loads(case_bytes)
    validate_data(corpus, cases)
    return (
        corpus,
        cases,
        {
            CORPUS_FILE.name: hashlib.sha256(corpus_bytes).hexdigest(),
            CASES_FILE.name: hashlib.sha256(case_bytes).hexdigest(),
        },
    )


def load_data() -> tuple[list[dict], list[dict]]:
    corpus, cases, _ = _read_frozen_data()
    return corpus, cases


def run(root: Path, *, keyword_only: bool) -> dict:
    with eval_environment(root):
        return _run(root, keyword_only=keyword_only)


def _run(root: Path, *, keyword_only: bool) -> dict:
    corpus, cases, hashes = _read_frozen_data()
    method = "keyword" if keyword_only else "hybrid"
    config = Config(
        vault=root / "vault",
        runtime=str(root / "runtime"),
        retrieval=RetrievalConfig(
            method=method,
            embedding=EmbeddingConfig(backend="local", model=MODEL),
        ),
    )
    config.search.enabled = False
    config.fetch.providers = []
    service = MemoryService(config)
    if service.retrieval_mode != method:
        raise RuntimeError(f"Requested {method}, got {service.retrieval_mode}. Check local-embed.")
    ids = seed(service, corpus)
    retriever = service.retriever
    vector = None if keyword_only else retriever.vector
    with guard_vector(vector) as check:
        retrievers = {"keyword": retriever}
        if not keyword_only:
            vector.sync()
            check()
            retrievers = {"keyword": retriever.keyword, "vector": vector, "hybrid": retriever}

        results = {}
        for name, engine in retrievers.items():

            def raw_search(query, limit, engine=engine):
                hits = engine.search(query, limit)
                check()
                return RankedResults([hit.id for hit in hits])

            results[f"raw_{name}"] = paired_evaluation(cases, ids, raw_search)

        session = SessionStore(service.paths).create()
        frozen_text = {ident: item["text"] for ident, item in zip(ids, corpus, strict=True)}
        tool_registries = {}
        packaged_default = config.retrieval.default_limit

        def chat_search(query, limit):
            # 两组调用都省略 limit，各自用内存中的默认值构建工具定义，
            # 避免写入配置文件。
            config.retrieval.default_limit = limit
            try:
                if limit not in tool_registries:
                    tool_registries[limit] = build_tools(service, session)
                result = tool_search(tool_registries[limit], query, frozen_text)
                check()
                return result
            finally:
                config.retrieval.default_limit = packaged_default

        results[f"recall_tool_{method}"] = paired_evaluation(cases, ids, chat_search)
    return {
        "schema_version": 1,
        "corpus_size": len(corpus),
        "question_count": len(cases),
        "multi_target_questions": sum("targets" in case for case in cases),
        "dataset_sha256": hashes,
        "environment": run_metadata(method),
        "embedding_model": None if keyword_only else MODEL,
        "limits": list(LIMITS),
        "packaged_tool_default": packaged_default,
        "tested_tool_defaults": list(LIMITS),
        "tool_limit_argument": {str(k): "omitted" for k in LIMITS},
        "score_scope": (
            "One vote per frozen question. First matching target ID wins, including any "
            "alternative for multi-target fields. Labels indicate retrieval, not answer correctness. "
            "All questions have at least one labeled target."
        ),
        "token_scope": (
            "estimate_tokens on serialized recall tool message content only. Excludes system "
            "prompt, tool schemas, conversation history and output. No LLM calls or query rewrite."
        ),
        "results": results,
    }


def print_report(report: dict, failures: bool) -> None:
    print(f"{report['corpus_size']} synthetic records; {report['question_count']} questions.")
    for name, result in report["results"].items():
        for limit in LIMITS:
            stats = result["metrics"][str(limit)]
            counts = " ".join(
                f"{k}={n}/{stats['questions']}" for k, n in stats["hit_counts"].items()
            )
            print(f"{name} k={limit}  {counts}  MRR@{limit}={stats[f'MRR@{limit}']:.3f}", end="")
            if "mean_tool_tokens" in stats:
                print(f"  mean_tool_tokens={stats['mean_tool_tokens']:.1f}", end="")
            print()
        print("  paired:", json.dumps(result["paired"]))
        if failures:
            for row in result["cases"]:
                rank = row["runs"]["10"]["rank"]
                if rank is None or rank > 3:
                    print(f"  [{row['category']}] {row['query']} (rank at k=10: {rank})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", action="store_true", help="Only run offline keyword retrieval")
    parser.add_argument("--failures", action="store_true", help="Show misses at rank 3 with k=10")
    parser.add_argument("--json", metavar="PATH", help="Write per-question JSON; use - for stdout")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="syvrio-eval-") as directory:
        report = run(Path(directory), keyword_only=args.keyword)
    if args.json != "-":
        print_report(report, args.failures)
    if args.json:
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.json == "-":
            print(payload, end="")
        else:
            Path(args.json).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
