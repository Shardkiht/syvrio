"""在包含超过 1,200 条固定记录的 Vault 中评估短字段检索。"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from syvrio.memory.schema import Core, render_record
from syvrio.memory.service import MemoryService

if __name__ == "__main__" and not __package__:
    from retrieval_eval import load_data
    from retrieval_fixture import guard_vector
    from retrieval_metrics import LIMITS, target_indices
    from sandbox import eval_environment, local_hybrid_config
else:
    from evals.retrieval_eval import load_data
    from evals.retrieval_fixture import guard_vector
    from evals.retrieval_metrics import LIMITS, target_indices
    from evals.sandbox import eval_environment, local_hybrid_config

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
    ("name", "我指的是姓名"),
    ("name", "我叫什么名字 姓名 名字 称呼"),
    ("name", "用户姓名 真实名字 如何称呼"),
    ("age", "我多大了 年龄"),
    ("hometown", "我的家乡是哪里 籍贯 老家 家乡"),
    ("work", "我从事什么工作 职业 工作领域"),
    ("device", "我用的设备型号是什么 设备 型号"),
)


SHORT_FIELD_MARKERS = tuple(text.split("：")[0] for _, _, _, text in SHORT_FACTS)


def legacy_cases() -> list[tuple[str, set[str]]]:
    """每个固定查询只计分一次，取所有目标中的最佳名次。"""
    _, cases = load_data()
    return [
        (
            case["query"],
            {f"legacy-{target:04d}" for target in target_indices(case)},
        )
        for case in cases
    ]


def seed_vault(vault: Path, *, short_fields: bool = True) -> int:
    topics = defaultdict(list)
    titles = {}
    corpus, _ = load_data()
    for index, item in enumerate(corpus):
        text = item["text"]
        if short_fields and any(marker in text for marker in SHORT_FIELD_MARKERS):
            continue
        topics["legacy-" + item["category"]].append((f"legacy-{index:04}", text))
    if short_fields:
        # 这组独立数据为每个待查字段指定了唯一的归属和值。
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
            render_record(
                Core(id=ident, status="active", text=text),
                extensions=[("recorded_at", "2024-01-01T00:00:00Z")],
            )
            for ident, text in entries
        ]
        (root / f"{topic}.md").write_text(
            f"# {titles.get(topic, topic)}\n\n" + "\n".join(records), encoding="utf-8"
        )
    return sum(map(len, topics.values()))


def measure(service: MemoryService, cases: list[tuple[str, set[str]]], details: bool) -> None:
    depth = service.config.retrieval.default_limit
    ranks = []
    durations = []
    with guard_vector(service.retriever.vector) as check:
        for query, targets in cases:
            start = perf_counter()
            hits = service.recall(query)
            durations.append((perf_counter() - start) * 1000)
            check()
            if len(hits) > depth:
                raise ValueError(f"Short-field recall exceeded the configured limit of {depth}")
            rank = next((i for i, hit in enumerate(hits, 1) if hit.id in targets), None)
            ranks.append(rank)
            if details:
                print(f"default_limit={depth} {query} rank={rank}")
    print(f"default_limit={depth} cases={len(ranks)}", end=" ")
    for k in (1, 3, 5, depth):
        count = sum(r is not None and r <= k for r in ranks)
        print(f"hit@{k}={count}/{len(ranks)}", end=" ")
    mrr = sum(1 / r for r in ranks if r is not None) / len(ranks)
    print(f"mrr@{depth}={mrr:.4f} median_ms={median(durations):.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="syvrio-short-facts-") as temporary:
        short = [(query, {f"short-{key}"}) for key, query in SHORT_CASES]
        for group, cases in (("short", short), ("legacy", legacy_cases())):
            with eval_environment(Path(temporary) / group) as vault:
                count = seed_vault(vault, short_fields=group == "short")
                service = MemoryService(local_hybrid_config(vault))
                ids = {entry.id for entry in service.current_facts()}
                for query, targets in cases:
                    if not targets or not targets <= ids:
                        raise ValueError(f"Missing target in the {group} fixture: {query}")
                if service.retrieval_mode != "hybrid":
                    raise SystemExit("requires local-embed and its configured model")
                start = perf_counter()
                with guard_vector(service.retriever.vector):
                    service.retriever.vector.sync()
                print(
                    f"{group} records={count} index_seconds={perf_counter() - start:.3f}",
                    flush=True,
                )
                for limit in LIMITS:
                    service.config.retrieval.default_limit = limit
                    measure(service, cases, args.details)
                    if group == "short" and args.details:
                        # 宽泛的身份问题没有唯一正确的姓名记录。
                        with guard_vector(service.retriever.vector):
                            probe = service.recall("我是谁")
                        print(
                            f"default_limit={limit} unscored 我是谁 ids={[hit.id for hit in probe]}"
                        )


if __name__ == "__main__":
    main()
