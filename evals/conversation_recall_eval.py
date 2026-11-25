"""用固定的合成对话评估自动记忆注入及显式工具调用返回的证据。

显式调用使用预先固定的独立查询，追问中的指代也已补全。
自动预取接收原始对话，并使用生产环境无密钥时的改写方式。
历史陈述和当前陈述都可能是有效事实。
评测按每个问题标注必要及误导条目的 ID，只评估检索结果。
"""

from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from syvrio.agent.runtime import AgentRuntime
from syvrio.agent.tools import build_tools
from syvrio.infra.config import Config, EmbeddingConfig, LLMConfig, RetrievalConfig, SearchConfig
from syvrio.infra.llm import AssistantTurn
from syvrio.infra.session import SessionStore
from syvrio.memory.service import MemoryService

if __package__:
    from .retrieval_fixture import MODEL, evidence_ids, guard_vector, tool_search
    from .retrieval_metrics import LIMITS
    from .sandbox import eval_environment, seed_topic
else:
    from retrieval_fixture import MODEL, evidence_ids, guard_vector, tool_search
    from retrieval_metrics import LIMITS
    from sandbox import eval_environment, seed_topic

DATA = Path(__file__).with_name("data") / "conversation_recall_cases.json"


def load_corpus(path: Path) -> dict:
    def unique_keys(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate JSON key in the frozen conversation fixture.")
        return result

    corpus = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    facts, cases = corpus["facts"], corpus["cases"]
    if not facts or not cases:
        raise ValueError("The conversation fixture needs facts and cases.")
    case_ids = set()
    for case in cases:
        ident = case["id"]
        if not isinstance(ident, str) or not ident.strip() or ident in case_ids:
            raise ValueError(f"Empty or duplicate conversation case ID: {ident!r}")
        case_ids.add(ident)
        required, forbidden = case["required"], case["forbidden"]
        if not isinstance(required, list) or not isinstance(forbidden, list):
            raise ValueError(f"Expected lists of evidence labels in {ident!r}")
        labels = required + forbidden
        if any(not isinstance(label, str) or label not in facts for label in labels):
            raise ValueError(f"Unknown evidence labels in {ident!r}")
        if len(set(labels)) != len(labels):
            raise ValueError(f"Duplicate or conflicting evidence labels in {ident!r}")
    return corpus


class OfflineLLMConfig(LLMConfig):
    def api_key(self) -> None:
        # 选用生产环境无密钥时的查询改写方式，避免读取凭据。
        return None


class RecordingModel:
    def __init__(self, frozen_text: dict[str, str] | None = None):
        self.frozen_text = load_corpus(DATA)["facts"] if frozen_text is None else frozen_text
        self.ids: list[str] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        records = []
        for message in messages:
            content = message["content"]
            if message["role"] == "user" and content.startswith("<long_term_memory>\n"):
                payload = content.split("\n", 2)[2].removesuffix("\n</long_term_memory>")
                records.extend(json.loads(payload)["results"])
        self.ids = evidence_ids(records, self.frozen_text)
        return AssistantTurn(content="收到。")


def evidence_score(case: dict, ids: list[str]) -> dict:
    required = set(case["required"])
    forbidden = set(case["forbidden"])
    target_ranks = [ids.index(item) if item in ids else len(ids) for item in required]
    first_forbidden = next((i for i, item in enumerate(ids) if item in forbidden), len(ids))
    return {
        "returned": ids,
        "required_hits": sorted(required.intersection(ids)),
        "all_required": required.issubset(ids) if required else None,
        "target_first": bool(ids and ids[0] in required) if required else None,
        "forbidden_hits": sorted(forbidden.intersection(ids)),
        "forbidden_before_target": (
            first_forbidden < min(target_ranks) if required and forbidden else None
        ),
        "forbidden_before_all_evidence": (
            first_forbidden < max(target_ranks) if required and forbidden else None
        ),
    }


def summarize(rows: list[dict], channel: str) -> dict:
    answerable = [row[channel] for row in rows if row["required"]]
    ambiguous = [score for score in answerable if score["forbidden_before_target"] is not None]
    unknown = [row[channel] for row in rows if row["expected_trigger"] and not row["required"]]
    # 无法作答的问题也可能检索到相关背景。只有标注的干扰项才算误导，
    # 其他返回的候选单独统计。
    return {
        "answerable": len(answerable),
        "all_required": sum(s["all_required"] for s in answerable),
        "target_first": sum(s["target_first"] for s in answerable),
        "required_hits": sum(len(s["required_hits"]) for s in answerable),
        "required_total": sum(len(row["required"]) for row in rows),
        "conflict_cases": len(ambiguous),
        "forbidden_before_target": sum(s["forbidden_before_target"] for s in ambiguous),
        "forbidden_before_all_evidence": sum(s["forbidden_before_all_evidence"] for s in ambiguous),
        "no_answer": len(unknown),
        "no_answer_with_candidates": sum(bool(s["returned"]) for s in unknown),
        "no_answer_with_forbidden": sum(bool(s["forbidden_hits"]) for s in unknown),
    }


def evaluate(method: str = "keyword") -> dict:
    corpus = load_corpus(DATA)
    with TemporaryDirectory(prefix="syvrio-conversation-eval-") as folder:
        root = Path(folder)
        with eval_environment(root) as vault, ExitStack() as observers:
            seed_topic(vault, "samples", corpus["facts"].items(), "合成样品")
            config = Config(
                vault=vault,
                runtime=str(root / "runtime"),
                retrieval=RetrievalConfig(
                    method=method, embedding=EmbeddingConfig(backend="local", model=MODEL)
                ),
                llm=OfflineLLMConfig(provider="custom", model="offline"),
                search=SearchConfig(enabled=False),
            )
            service = MemoryService(config)
            if method == "hybrid":
                if service.retrieval_mode != method:
                    raise RuntimeError("Hybrid evaluation requires the local embedding model.")
                vector = service.retriever.vector
                check = observers.enter_context(guard_vector(vector))
                vector.sync()
                check()
            store = SessionStore(service.paths)
            model = RecordingModel(corpus["facts"])
            runtime = AgentRuntime(service, model, store)
            rows = []
            for case in corpus["cases"]:
                runtime.start()
                for previous in case.get("previous", []):
                    runtime.ask(previous)
                with patch.object(service, "recall", wraps=service.recall) as observed:
                    runtime.ask(case["question"])
                if observed.call_count > 1:
                    raise RuntimeError("The automatic path made multiple searches for one turn.")
                args = observed.call_args.args if observed.called else ()
                row = {
                    "id": case["id"],
                    "required": case["required"],
                    "forbidden": case["forbidden"],
                    "expected_trigger": case["trigger"],
                    "triggered": observed.called,
                    "auto_query": args[0] if args else None,
                    "auto_limit": args[1] if args else None,
                    "auto": evidence_score(case, model.ids),
                    "explicit_query": case["query"],
                }
                for limit in LIMITS:
                    # 调用工具时省略 limit，以检验配置的默认值。
                    with patch.object(config.retrieval, "default_limit", limit):
                        tools = build_tools(service, runtime.current)
                        result = tool_search(tools, case["query"], corpus["facts"])
                    row[f"explicit_{limit}"] = evidence_score(case, result.ids)
                rows.append(row)

    positives = [r for r in rows if r["expected_trigger"]]
    negatives = [r for r in rows if not r["expected_trigger"]]
    return {
        "method": method,
        "embedding_model": MODEL if method == "hybrid" else None,
        "rewrite": "production no-key fallback (recent user text + current question)",
        "chat_model": "recording stub, no answer generation",
        "explicit_queries": "frozen standalone tool arguments, no limit supplied",
        "facts": len(corpus["facts"]),
        "cases": len(rows),
        "trigger": {
            "true_positive": sum(r["triggered"] for r in positives),
            "false_negative": sum(not r["triggered"] for r in positives),
            "true_negative": sum(not r["triggered"] for r in negatives),
            "false_positive": sum(r["triggered"] for r in negatives),
            "unexpected_injection": sum(bool(r["auto"]["returned"]) for r in negatives),
        },
        "scores": {
            "auto": summarize(rows, "auto"),
            **{f"explicit_{k}": summarize(rows, f"explicit_{k}") for k in LIMITS},
        },
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("keyword", "hybrid"), default="keyword")
    parser.add_argument("--details", action="store_true", help="Include the IDs from every call")
    options = parser.parse_args()
    report = evaluate(options.method)
    if not options.details:
        del report["rows"]
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
