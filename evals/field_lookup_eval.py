"""Evaluate exact label boundaries on a fixed, synthetic field corpus."""

from __future__ import annotations

from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from syvrio.infra.config import Config
from syvrio.memory.schema import Core, render_record
from syvrio.memory.service import MemoryService

FIELDS = (
    ("port", "端口：4826"),
    ("mapping", "端口映射：外部 9364 转发到接口乙"),
    ("place", "地点：演示台"),
    ("handover", "交接地点：模型库西门"),
    ("rating", "密封等级：ZX-7"),
    ("cycle", "登记周期：每 11 周复核"),
    ("dispatch", "交付编号：RUNE-77"),
    ("safe-a", "保管地点：天琴组 北柜"),
    ("safe-b", "保管地点：琥珀组 南柜"),
    ("storage", "Storage key: AQUA-900"),
    ("reuse", "重复利用空罐前，检查内壁是否有裂纹。"),
    ("postcode", "邮编：75230"),
    ("postal-tool", "邮编工具：用于校验虚构地址的批量格式。"),
)
CASES = (
    ("port", "我配置的端口是多少？"),
    ("mapping", "端口映射规则是什么？"),
    ("handover", "交接地点定在哪？"),
    ("rating", "请确认一下我的密封等级"),
    ("cycle", "登记周期 复核间隔 什么时候复核"),
    ("dispatch", "交付编号是什么？"),
    ("safe-a", "天琴组的保管地点"),
    ("safe-b", "琥珀组 保管地点"),
    ("storage", "What is the storage key?"),
    ("reuse", "旧空罐还能用吗，使用前检查什么？"),
    ("postal-tool", "邮编工具有什么用途？"),
)
NESTED_LABELS = {"mapping": "port", "handover": "place", "postal-tool": "postcode"}


def seed(vault: Path) -> None:
    topics = vault / "memories" / "general"
    topics.mkdir(parents=True)
    for group in range(12):
        rows = (
            FIELDS
            if group == 0
            else [
                (
                    f"decoy-{group}-{i}",
                    f"样本 {group}-{i} 记录设备编号、复核周期、端口规则和文件交接安排。"
                    "另有独立的容器标签处理流程。",
                )
                for i in range(109)
            ]
        )
        records = [
            render_record(text, Core(id=ident, status="active", source="eval", confidence=1))
            for ident, text in rows
        ]
        (topics / f"{group:02}.md").write_text(
            "# 独立设备与工作资料\n\n" + "\n".join(records), encoding="utf-8"
        )


def main() -> None:
    with TemporaryDirectory(prefix="syvrio-field-eval-") as temporary:
        root = Path(temporary)
        vault = root / "vault"
        seed(vault)
        service = MemoryService(Config(vault=vault, runtime=str(root / "runtime")))
        if service.retrieval_mode != "hybrid":
            raise SystemExit("requires local-embed and its configured model")
        service.retriever.vector.sync()
        if service.retriever.vector.degraded:
            raise SystemExit("local embedding could not build the index")
        ranks = []
        durations = []
        nested_first = 0
        for expected, query in CASES:
            start = perf_counter()
            hits = service.recall(query, 10)
            durations.append((perf_counter() - start) * 1000)
            rank = next((i for i, hit in enumerate(hits, 1) if hit.id == expected), None)
            winner = hits[0].id if hits else ""
            ranks.append(rank)
            nested_first += winner == NESTED_LABELS.get(expected)
            print(f"{expected}: rank={rank} first={winner}")
        print(
            f"records={len(service.current_facts())} "
            f"hit@1={sum(rank == 1 for rank in ranks)}/{len(ranks)} "
            f"hit@3={sum(rank is not None and rank <= 3 for rank in ranks)}/{len(ranks)} "
            f"nested_label_first={nested_first}/{len(NESTED_LABELS)} "
            f"median_ms={median(durations):.1f}"
        )


if __name__ == "__main__":
    main()
