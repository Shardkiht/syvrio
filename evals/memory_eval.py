from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from syvrio.infra.config import Config, RetrievalConfig
from syvrio.memory.extractor import StaticExtractor, parse_extraction
from syvrio.memory.service import MemoryService

if __name__ == "__main__" and not __package__:
    from sandbox import eval_environment
else:
    from evals.sandbox import eval_environment


@dataclass(frozen=True)
class Case:
    name: str
    text: str = "合成事实"
    expected: str = "admitted"
    question: str | None = None
    context: dict | None = None
    seed: str | None = None


# 固定模型输出，评估解析、存储与裁决；不冒充真实模型的语义抽取准确率。
CASES = [
    Case("明确事实"),
    Case("重要歧义", expected="queued", question="是哪一种偏好？"),
    Case("普通私人事实", text="去年购入一辆合成单车"),
    Case("变化待澄清", expected="queued", question="这一变化从何时开始？"),
    Case("资料归属存疑", expected="queued", question="这段经历是谁的？"),
    Case("完全重复", text="喜欢纸质阅读", seed="喜欢纸质阅读", expected="duplicates"),
    Case("不同事实", text="喜欢阅读纸质杂志", seed="喜欢纸质书"),
    Case(
        "数字冲突",
        text="每周阅读 5 次",
        seed="每周阅读 2 次",
        expected="queued",
        question="哪段时间？",
    ),
    Case(
        "否定冲突",
        text="不喜欢纸质阅读",
        seed="喜欢纸质阅读",
        expected="queued",
        question="以前还是现在？",
    ),
    Case("凭据拦截", text="密码是 synthetic-only", expected="blocked"),
    Case("年份精度", context={"event_time": "2024"}),
    Case("月份精度", context={"event_time": "2024-06"}),
    Case("日期精度", context={"event_time": "2024-02-29"}),
    Case("无效日期", context={"event_time": "2023-02-29"}, expected="invalid"),
    Case("未知相对时间", text="去年去过合成展馆"),
    Case("生效区间", context={"valid_from": "2024-06", "valid_to": "2025"}),
    Case("倒置区间", context={"valid_from": "2025", "valid_to": "2024"}, expected="invalid"),
    Case("事件地点", context={"location": "合成展馆"}),
    Case("居住地点", context={"location": "合成居所"}),
    Case("空白地点", context={"location": "  "}, expected="invalid"),
    Case("事件当时", context={"emotion": {"event": "很开心"}}),
    Case("回想时", context={"emotion": {"recollection": "感到遗憾"}}),
    Case("两个时点", context={"emotion": {"event": "紧张", "recollection": "释然"}}),
    Case("情绪时点未知", context={"emotion": {"unspecified": "五味杂陈"}}),
    Case(
        "推断情绪拒绝",
        context={"emotion": {"inferred": "似乎不开心"}},
        expected="invalid",
    ),
    Case(
        "上下文凭据拦截",
        context={"location": "密码是 synthetic-only"},
        expected="blocked",
    ),
]


def run_case(case: Case, root: Path) -> str | None:
    with eval_environment(root) as vault:
        service = MemoryService(Config(vault=vault, retrieval=RetrievalConfig(method="keyword")))
        return _check_candidate(case, service)


def _check_candidate(case: Case, service: MemoryService) -> str | None:
    seed = service.note(case.seed, topic="synthetic") if case.seed else None
    candidate = {
        "text": case.text,
        "topic": "synthetic",
        "context": case.context,
        "question": case.question,
        "related_ids": [seed.id] if seed and case.question else [],
    }
    try:
        extraction = parse_extraction(json.dumps({"memories": [candidate]}, ensure_ascii=False))
    except ValueError as exc:
        return None if case.expected == "invalid" else f"解析失败：{exc}"
    if case.expected == "invalid":
        return "应拒绝无效上下文，但解析成功"
    result = service.import_text(case.text, extractor=StaticExtractor(extraction))
    counts = {
        key: len(getattr(result, key)) for key in ("admitted", "queued", "duplicates", "blocked")
    }
    if counts.get(case.expected) != 1 or sum(counts.values()) != 1:
        return f"预期 {case.expected}，实际 {counts}"
    if seed and case.expected == "queued" and seed.id not in result.queued[0].related_ids:
        return "冲突来源 ID 丢失"
    if result.admitted:
        entry = service.store.find(result.admitted[0].id)
        if entry.context != extraction.memories[0].context:
            return "上下文落盘后发生变化"
    if case.expected == "blocked" and service.store.entries():
        return "被拦截内容仍然落盘"
    return None


def main() -> int:
    failures = []
    with TemporaryDirectory(prefix="syvrio-memory-eval-") as directory:
        for index, case in enumerate(CASES):
            error = run_case(case, Path(directory) / str(index))
            if error:
                failures.append((case.name, error))
        metrics = run_contextual_eval(Path(directory) / "contextual")
    print(f"合成解析与裁决回归：{len(CASES) - len(failures)}/{len(CASES)} 通过")
    for name, error in failures:
        print(f"失败：{name}：{error}")
    print("固定模型输出；不调用真实模型，不衡量自然语言语义抽取效果。")
    print("结构化过滤契约：已知时间、地点或情绪条件，最多返回 5 条。")
    print(json.dumps(metrics, ensure_ascii=False))
    return int(bool(failures) or metrics["contextual"]["Recall@1"] != 1.0)


def run_contextual_eval(root: Path) -> dict:
    """小型合成对照，比较不带上下文的关键词检索与明确时间/地点/情绪条件。"""
    with eval_environment(root) as vault:
        service = MemoryService(Config(vault=vault, retrieval=RetrievalConfig(method="keyword")))
        return _check_filters(service)


def _check_filters(service: MemoryService) -> dict:
    from syvrio.memory.models import MemoryContext, MemoryDraft

    old = service.store.append(
        "preferences", "合成偏好早期状态", context=MemoryContext(valid_from="2020-01-01")
    )
    new = service.transition(
        old.id,
        MemoryDraft(
            topic="preferences",
            text="合成偏好后期状态",
            context=MemoryContext(valid_from="2024-01-01"),
        ),
        previous_valid_to="2023",
    )
    target = service.store.append(
        "events",
        "合成展览活动",
        context=MemoryContext(
            event_time="2024-06",
            location="合成展馆甲",
            emotion={"event": "期待", "recollection": "怀念"},
        ),
    )
    other = service.store.append(
        "events",
        "合成展览活动",
        context=MemoryContext(
            event_time="2025-06",
            location="合成展馆乙",
            emotion={"unspecified": "遗憾"},
        ),
    )
    cases = [
        ("合成偏好", {"time_range": {"start": "2022", "end": "2022"}}, old.id),
        ("合成偏好", {"time_range": {"start": "2025"}}, new.id),
        ("合成展览", {"time_range": {"start": "2024", "end": "2024"}}, target.id),
        ("合成展览", {"location": "合成展馆甲"}, target.id),
        ("合成展览", {"emotion": "期待"}, target.id),
        ("合成展览", {"emotion": "怀念"}, target.id),
        ("合成展览", {"emotion": "遗憾"}, other.id),
    ]
    result = {"cases": len(cases), "limit": 5}
    for name in ("baseline", "contextual"):
        ranks = []
        for query, filters, expected in cases:
            hits = (
                service.retriever.search(query, 5)
                if name == "baseline"
                else service.recall(query, 5, **filters)
            )
            ids = [hit.id for hit in hits]
            ranks.append(ids.index(expected) + 1 if expected in ids else 0)
        result[name] = {
            "Recall@1": ranks.count(1) / len(ranks),
            "Hit@3": sum(0 < rank <= 3 for rank in ranks) / len(ranks),
            "Hit@5": sum(0 < rank <= 5 for rank in ranks) / len(ranks),
            "MRR@5": sum(1 / rank for rank in ranks if rank) / len(ranks),
        }
    return result


if __name__ == "__main__":
    raise SystemExit(main())
