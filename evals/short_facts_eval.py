"""Measure short-field retrieval on a deterministic Vault with over 1,200 records."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from evals.retrieval_eval import load_data
from syvrio.infra.config import Config
from syvrio.memory.schema import Core, render_record
from syvrio.memory.service import MemoryService

SHORT_FACTS = (
    ("name", "identity", "人物基本资料", "姓名：虚构人物甲"),
    ("age", "identity", "人物基本资料", "年龄：41 岁"),
    ("hometown", "identity", "人物基本资料", "家乡：示例岬"),
    ("work", "work", "工作经历", "职业：纸艺修复师"),
    ("device", "devices", "设备档案", "设备型号：ORBIT-Z17"),
)
SHORT_CASES = (
    ("name", "姓名"),
    ("name", "我叫什么名字"),
    ("name", "我是谁"),
    ("name", "我指的是姓名"),
    ("name", "我叫什么名字 姓名 名字 称呼"),
    ("name", "用户姓名 真实名字 如何称呼"),
    ("age", "我多大了 年龄"),
    ("hometown", "我的家乡是哪里 籍贯 老家 家乡"),
    ("work", "我从事什么工作 职业 工作领域"),
    ("device", "我用的设备型号是什么 设备 型号"),
)


SHORT_FIELD_MARKERS = tuple(text.split("：")[0] for _, _, _, text in SHORT_FACTS)


def corpus_entries() -> list[tuple[int, str, str]]:
    """冻结语料里本身就埋了同类短字段（姓名/年龄/职业…），会直接挤占 top3。

    本评测要验证的是「长文本干扰下短字段仍在前三」，所以先剔除同字段的竞品，只留下
    无关长文。返回值带原始下标，`legacy-NNNN` 仍按冻结语料顺序编号，用例的 target 不变。
    """
    corpus, _ = load_data()
    return [
        (index, item["category"], item["text"])
        for index, item in enumerate(corpus)
        if not any(marker in item["text"] for marker in SHORT_FIELD_MARKERS)
    ]


def legacy_cases() -> list[tuple[str, str, str]]:
    """冻结用例的 target 是语料下标，换成 seed_vault 生成的 legacy-NNNN 记录 ID。"""
    _, cases = load_data()
    rows: list[tuple[str, str, str]] = []
    for case in cases:
        for target in case.get("targets") or [case["target"]]:
            rows.append((case["category"], case["query"], f"legacy-{target:04d}"))
    return rows


def seed_vault(vault: Path) -> int:
    topics = defaultdict(list)
    titles = {}
    for index, topic, text in corpus_entries():
        topics["legacy-" + topic].append((f"legacy-{index:04}", text))
    for label, topic, title, text in SHORT_FACTS:
        topics[topic].append((f"short-{label}", text))
        titles[topic] = title
    distractors = (
        "录入培训资料时，要核对客户姓名、名字的读音和联系方式，不能在表格遗漏备注。",
        "设备采购样表记录产品型号和测评排名，清单是同事设备，不是自用设备。",
        "旧小说讨论人物的身份是谁，演员的名字与舞台称呼不同。",
        "志愿活动登记参与年龄、职业和来自哪里的统计字段；不要在公开汇总表列个人信息。",
        "家乡旅行专题收录不同地区的民俗，老家和籍贯在表单里是两个字段。",
        "每个工作小组会核算材料数量、到货日期和设备清单，会议之后修订统计。",
        "读书档案题名是古桥修复，馆藏作者名单在书目附页，原文件注明登记日期。",
        "偏好：社区活动用可重复使用的工具，作品编号与提交记录对应。",
        "学习笔记提到年龄分布、报名日期、地区和设备使用年限。",
        "工作安排：展览布置和标签印刷由不同团队负责。",
    )
    for index in range(780):
        variant = distractors[index % len(distractors)]
        topic = f"archive-{index % 13:02}"
        text = f"第 {index + 200} 号合成档案。{variant}"
        if index % 6 == 0:
            text += "\n\n附录记录项目的排期、协作方式和复核流程，用于提供长文本干扰。"
        topics[topic].append((f"distractor-{index:04}", text))

    root = vault / "memories" / "general"
    root.mkdir(parents=True)
    for topic, entries in topics.items():
        records = [
            render_record(text, Core(id=ident, status="active", source="eval", confidence=1.0))
            for ident, text in entries
        ]
        (root / f"{topic}.md").write_text(
            f"# {titles.get(topic, topic)}\n\n" + "\n".join(records), encoding="utf-8"
        )
    return sum(map(len, topics.values()))


def metrics(ranks: list[int | None], durations: list[float]) -> dict[str, float]:
    return {
        **{
            f"hit@{k}": sum(r is not None and r <= k for r in ranks) / len(ranks) for k in (1, 3, 5)
        },
        "mrr@10": sum(1 / r if r is not None and r <= 10 else 0.0 for r in ranks) / len(ranks),
        "median_ms": median(durations),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="syvrio-short-facts-") as temporary:
        root = Path(temporary)
        count = seed_vault(root / "vault")
        service = MemoryService(Config(vault=root / "vault", runtime=str(root / "runtime")))
        if service.retrieval_mode != "hybrid":
            raise SystemExit("requires local-embed and its configured model")
        start = perf_counter()
        service.retriever.vector.sync()
        if service.retriever.vector.degraded:
            raise SystemExit("local embedding could not build the index")
        print(f"records={count} index_seconds={perf_counter() - start:.3f}", flush=True)
        rows = defaultdict(list)
        checks = [("short", query, f"short-{key}", []) for key, query in SHORT_CASES] + [
            ("legacy", query, ident, []) for _, query, ident in legacy_cases()
        ]
        for group, query, ident, subs in checks:
            start = perf_counter()
            hits = service.recall(query, 10)
            ms = (perf_counter() - start) * 1000
            rank = next(
                (
                    i
                    for i, hit in enumerate(hits, 1)
                    if (hit.id == ident if ident else any(s in hit.text for s in subs))
                ),
                None,
            )
            rows[group].append((rank, ms))
            rows["overall"].append((rank, ms))
            if args.details:
                print(group, query, rank)
        for group, measures in rows.items():
            print(group, {k: round(v, 4) for k, v in metrics(*zip(*measures, strict=True)).items()})


if __name__ == "__main__":
    main()
