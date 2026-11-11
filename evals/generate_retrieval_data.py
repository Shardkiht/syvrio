"""用真实 LLM 生成检索评测语料与题库，冻结为 JSON 以便复现。

生成结果写入 ``evals/data/retrieval_corpus.json`` 与
``evals/data/retrieval_cases.json``，``retrieval_eval.py`` 只读取这两个文件，
评测时不再调用 LLM——同一份冻结数据可在任意分支复现，保证横向对比一致。

全部内容为虚构占位（人物甲/某市/编造型号），不含真实个人信息；真实库只用于
``--real-vault`` 的聚合分布对照，不把真实条目喂给模型。

    # 小探针：只生成少量，检查格式与分布
    uv run python evals/generate_retrieval_data.py --probe --real-vault ~/.syvrio/vault-v2

    # 全量：740 条语料 + 50 题
    uv run python evals/generate_retrieval_data.py --corpus 740 --cases 50 --real-vault ~/.syvrio/vault-v2
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
CORPUS_FILE = DATA_DIR / "retrieval_corpus.json"
CASES_FILE = DATA_DIR / "retrieval_cases.json"

CATEGORIES = [
    "food",
    "housing",
    "career",
    "learning",
    "identity",
    "health",
    "schedule",
    "devices",
    "entertainment",
    "finance",
    "relationships",
]

CORPUS_PROMPT = """你在为一个检索评测构造**完全虚构**的个人记忆库语料。
生成 {n} 条中文短条目，要求：
- 内容全部虚构：不得包含任何真实个人的具体信息（真实姓名、联系方式、证件号、银行卡号、车牌、精确住址等）；人名用“人物甲/乙/丙/丁/戊”，需要编号时写“某编号”。
- 可自然地提及常见品牌、城市、产品、软件作为生活场景（真实专有名词没问题），体现个人记录的日常感。
- 以中文为主：汉字应占约六成字符，英文单词、型号、数字合计约四成；可夹带英文产品名/术语，不要写整句英文。
- 每条**一行**，不要换行，也不要把多条事实拼进一条。
- 长度：大多数 45–75 字（中位约 65）；少量 20–45 字或 100–150 字，不要超过 160 字；另外要有几条极短字段行（如“姓名：人物乙”“年龄：28 岁”）。
- 自然书写即可，约三成六条目含“的”，不必刻意回避。
- 约四成条目含冒号字段式（如“型号：X”“偏好：……”“截止：某月”）。
- 覆盖类别：{cats}，尽量均匀。
- 条目之间不要重复，不要输出编号、解释或代码块标记。
只输出 JSON 数组，元素形如 {{"category":"food","text":"……"}}。"""

CASES_PROMPT = """下面是若干条**虚构**的个人记忆条目（编号从 1 开始）。为每条生成一个自然的**中文口语问题**，要求：
- 有明确指向性：必须带上该条目的主题/对象/关键线索（涉及的软件、物品、地点、时间、项目名、人物等），让人能对号入座；禁止“最近怎么样”“有什么安排”“进展如何”这类空泛提问。
- 不要把答案直接抄进问题：例如条目是“离上线还有 9 天”，问“上线倒计时那个项目现在什么状态”即可，不要写出“9 天”。
- 像本人回忆时的口吻，简短自然，不要提及“条目/记忆/事实/上面”。
只输出 JSON 数组，元素形如 {{"index":1,"question":"……"}}，不要解释或代码块标记。

{listing}"""


# 生成期脱敏过滤：只拦真正像个人信息的内容（邮箱/URL/手机号/身份证号/车牌）。
# 真实品牌、城市、产品名等专有名词允许出现——它们不指向特定个人。
_IDENT = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.-]+|\b1[3-9]\d{9}\b|\d{17}[\dXx]")
_PLATE = re.compile(
    r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5}"
)


def _identifiable(text: str) -> bool:
    return bool(_IDENT.search(text) or _PLATE.search(text))


def llm_config():
    from syvrio.infra.config import LLMConfig

    return LLMConfig(
        provider="spark",
        base_url="https://maas-api.cn-huabei-1.xf-yun.com/v2",
        model="spark-x2.6",
        api_key_env="SPARK_API_KEY",
        timeout=120.0,
        num_retries=1,
    )


def _chat(cfg, prompt: str, max_tokens: int = 4000) -> str:
    from syvrio.llm import complete

    resp = complete(cfg, [{"role": "user", "content": prompt}], max_tokens=max_tokens)
    return resp.choices[0].message.content or ""


def _parse_array(text: str) -> list:
    lo, hi = text.find("["), text.rfind("]")
    if lo != -1 and hi > lo:
        try:
            return json.loads(text[lo : hi + 1])
        except json.JSONDecodeError:
            pass
    items = []
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            items.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return items


def gen_corpus(cfg, total: int, batch: int, seed: int, workers: int = 4) -> list[dict]:
    rng = random.Random(seed)
    out: list[dict] = []
    seen: set[str] = set()

    def run(n: int, cats: str) -> list:
        try:
            return _parse_array(_chat(cfg, CORPUS_PROMPT.format(n=n, cats=cats), max_tokens=8000))
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        while len(out) < total:
            todo = min(workers, max(1, (total - len(out) + batch - 1) // batch))
            tasks = [
                (min(batch, total - len(out)), "、".join(rng.sample(CATEGORIES, 6)))
                for _ in range(todo)
            ]
            before = len(out)
            for arr in ex.map(lambda t: run(*t), tasks):
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    cat = str(item.get("category", "")).strip() or rng.choice(CATEGORIES)
                    text = str(item.get("text", "")).strip().replace("\n", " ").strip()
                    if not (4 <= len(text) <= 511) or text in seen or _identifiable(text):
                        continue
                    seen.add(text)
                    out.append({"category": cat, "text": text})
                    if len(out) >= total:
                        break
            print(f"  corpus: {len(out)}/{total} (+{len(out) - before})", flush=True)
            if len(out) == before:
                break
    return out[:total]


def gen_cases(
    cfg, corpus: list[dict], count: int, batch: int, seed: int, workers: int = 4
) -> list[dict]:
    rng = random.Random(seed + 1)
    eligible = [i for i, c in enumerate(corpus) if len(c["text"]) >= 16] or list(range(len(corpus)))
    rng.shuffle(eligible)
    targets = eligible[:count]

    def run(chunk: list[int]):
        listing = "\n".join(f"{j + 1}. {corpus[t]['text']}" for j, t in enumerate(chunk))
        try:
            return chunk, _parse_array(
                _chat(cfg, CASES_PROMPT.format(listing=listing), max_tokens=4000)
            )
        except Exception:
            return chunk, []

    chunks = [targets[i : i + batch] for i in range(0, len(targets), batch)]
    cases: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk, arr in ex.map(run, chunks):
            by_index = {}
            for item in arr:
                try:
                    by_index[int(item["index"])] = str(item["question"]).strip()
                except (KeyError, TypeError, ValueError):
                    continue
            for j, t in enumerate(chunk):
                q = by_index.get(j + 1)
                if q and not _identifiable(q):
                    cases.append({"category": corpus[t]["category"], "query": q, "target": t})
            print(f"  cases: {len(cases)}", flush=True)
    return cases


def stats(corpus: list[dict]) -> dict:
    texts = [c["text"] for c in corpus]
    lens = sorted(len(t) for t in texts)

    def pct(p):
        if not lens:
            return 0
        i = min(len(lens) - 1, int(round(p / 100 * (len(lens) - 1))))
        return lens[i]

    def cjk_ratio(t):
        return sum(1 for c in t if "\u4e00" <= c <= "\u9fff") / len(t) if t else 0

    ratios = [cjk_ratio(t) for t in texts]
    return {
        "entries": len(texts),
        "len_min": lens[0] if lens else 0,
        "len_p25": pct(25),
        "len_median": pct(50),
        "len_p75": pct(75),
        "len_p90": pct(90),
        "len_max": lens[-1] if lens else 0,
        "len_mean": round(statistics.mean(lens), 1) if lens else 0,
        "pct_with_de": round(sum("的" in t for t in texts) / len(texts), 3) if texts else 0,
        "pct_with_colon": round(sum("：" in t for t in texts) / len(texts), 3) if texts else 0,
        "pct_mixed": round(sum(0.05 < r < 0.95 for r in ratios) / len(texts), 3) if texts else 0,
        "mean_cjk_ratio": round(statistics.mean(ratios), 3) if ratios else 0,
    }


def real_vault_stats(vault: Path) -> dict:
    from syvrio.memory.store import MemoryStore

    store = MemoryStore(vault)
    return stats([{"text": e.text} for e in store.entries()])


def print_compare(gen: dict, real: dict | None) -> None:
    print("\n生成语料统计：", json.dumps(gen, ensure_ascii=False))
    if real:
        print("真实库统计：  ", json.dumps(real, ensure_ascii=False))
        keys = [
            "len_median",
            "len_p90",
            "pct_with_de",
            "pct_with_colon",
            "pct_mixed",
            "mean_cjk_ratio",
        ]
        print("\n质量对照（生成 / 真实）：")
        for k in keys:
            print(f"  {k:16} {gen[k]} / {real[k]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM 生成检索评测语料与题库（冻结为 JSON）")
    ap.add_argument("--corpus", type=int, default=740)
    ap.add_argument("--cases", type=int, default=50)
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--probe", action="store_true", help="小探针：10 条语料 + 3 题")
    ap.add_argument("--cases-only", action="store_true", help="只重做题库，复用已有语料")
    ap.add_argument("--real-vault", type=Path, default=None, help="真实库路径，用于聚合分布对照")
    args = ap.parse_args()

    # httpx 会把 NO_PROXY 里的 [::1] 误当端口，运行前规整掉。
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

    total, count = (10, 3) if args.probe else (args.corpus, args.cases)
    cfg = llm_config()
    if args.cases_only:
        corpus = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
        print(f"复用已有语料 {len(corpus)} 条", flush=True)
    else:
        print(f"生成语料 {total} 条（batch={args.batch}）…", flush=True)
        corpus = gen_corpus(cfg, total, args.batch, args.seed, args.workers)
    print(f"生成题库 {count} 题…", flush=True)
    cases = gen_cases(cfg, corpus, min(count, len(corpus)), args.batch, args.seed, args.workers)

    gen = stats(corpus)
    real = real_vault_stats(args.real_vault) if args.real_vault else None
    print_compare(gen, real)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CORPUS_FILE.write_text(json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8")
    CASES_FILE.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {len(corpus)} 条语料 -> {CORPUS_FILE}")
    print(f"写入 {len(cases)} 题 -> {CASES_FILE}")


if __name__ == "__main__":
    main()
