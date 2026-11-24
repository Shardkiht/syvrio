"""填充合成 Vault，评估召回工具序列化后提供的证据。"""

from __future__ import annotations

import json
import platform
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.metadata import version
from unittest.mock import patch

from syvrio.infra.tokens import estimate_tokens
from syvrio.memory.schema import Core, render_record
from syvrio.memory.service import MemoryService

if __package__:
    from .retrieval_metrics import RankedResults
else:
    from retrieval_metrics import RankedResults

MODEL = "BAAI/bge-small-zh-v1.5"


def run_metadata(method: str) -> dict:
    from syvrio.infra.tokens import _encoding

    packages = {"fastembed", "onnxruntime", "numpy"} if method == "hybrid" else set()
    encoding = _encoding()
    if encoding is not None:
        packages.add("tiktoken")
    return {
        "python": platform.python_version(),
        "packages": {name: version(name) for name in sorted(packages)},
        "token_estimator": getattr(encoding, "name", "character heuristic"),
    }


def seed(service: MemoryService, corpus: list[dict]) -> list[str]:
    if service.current_facts():
        raise ValueError("Seeding requires a fresh synthetic Vault.")
    ids = [row.get("id", f"fixture-{i}") for i, row in enumerate(corpus)]
    if any(not isinstance(ident, str) or not ident.strip() for ident in ids) or len(
        set(ids)
    ) != len(ids):
        raise ValueError("Frozen corpus IDs must be unique nonempty strings.")
    by_topic = defaultdict(list)
    for key, item in zip(ids, corpus, strict=True):
        record = render_record(
            Core(id=key, status="active", text=item["text"]),
            extensions=[("recorded_at", "2024-01-01T00:00:00Z")],
        )
        by_topic[item["category"]].append(record)
    for category, records in by_topic.items():
        path = service.store.topic_path(category)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(f"# {category}\n\n" + "\n".join(records))
    entries = service.current_facts()
    if len(entries) != len(ids) or {e.id for e in entries} != set(ids):
        raise ValueError("The loaded Vault does not match the frozen corpus IDs.")
    return ids


def require_healthy(vector) -> None:
    if vector is not None and vector.degraded:
        raise RuntimeError("Local embedding degraded; no vector or hybrid score is valid.")


@contextmanager
def guard_vector(vector) -> Iterator[Callable[[], None]]:
    """即使后续查询成功并清除了降级标记，该次运行仍判为无效。"""
    if vector is None:
        yield lambda: None
        return
    require_healthy(vector)
    with patch.object(vector, "_mark_degraded", wraps=vector._mark_degraded) as failures:

        def check() -> None:
            if failures.called:
                raise RuntimeError("Local embedding degraded during the evaluation.")
            require_healthy(vector)

        yield check
        check()


def evidence_ids(results: list[dict], frozen_text: dict[str, str]) -> list[str]:
    ids = []
    for row in results:
        ident = row.get("id")
        if ident not in frozen_text or row.get("text") != frozen_text[ident]:
            raise ValueError(f"recall evidence does not match the frozen text for {ident!r}")
        if ident in ids:
            raise ValueError(f"Duplicate recall evidence ID: {ident!r}")
        ids.append(ident)
    return ids


def tool_search(tools, query: str, frozen_text: dict[str, str]) -> RankedResults:
    outcome = tools.execute("recall", {"query": query})
    if not outcome.ok:
        raise RuntimeError(f"recall failed for {query!r}: {outcome.display}")
    # 解析作为工具消息发送的序列化证据，以覆盖模型实际收到的内容。
    results = json.loads(outcome.content)["results"]
    return RankedResults(evidence_ids(results, frozen_text), estimate_tokens(outcome.content))
