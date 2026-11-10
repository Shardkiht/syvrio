# 检索评测

## 记忆解析与裁决回归

```bash
uv run python evals/memory_eval.py
```

`memory_eval.py` 使用固定合成模型输出和独立临时 vault，覆盖普通抽取结果、低置信与敏感候选、更新/冲突、重复、日期精度与非法日期、生效区间、未知相对时间、事件/居住地点、显式/推断情绪及凭据拦截。对应案例也在 `tests/test_memory_eval.py` 中随 pytest 和 CI 执行。

同时输出固定上下文查询对照（历史/当前、事件日期、地点、显式情绪）的 Recall@1 与 MRR@5。

指标为各类固定案例的通过情况；它验证程序的解析、裁决与存储契约，**不衡量真实模型理解自然语言的准确性**，不代表长期聊天效果。推断去重、隔离和来源边界另由 `tests/test_inference.py` 覆盖。没有真实 vault、Key、付费模型或额外服务依赖。

## 检索基准

可复现的检索质量基准：语料与题库由真实 LLM 一次性生成后**冻结为 JSON 提交**，评测时只读固定文件、不再调用 LLM，因此不同分支逐字节同一批数据，对比 keyword（BM25）/ vector（本地向量）/ hybrid（RRF 融合）三路。

## 运行

```bash
uv sync --extra local-embed
uv run python evals/retrieval_eval.py               # 全部三路（读 evals/data/*.json）
uv run python evals/retrieval_eval.py --keyword     # 只跑 BM25（离线）
uv run python evals/retrieval_eval.py --failures    # 打印未过 @3 的用例
uv run python evals/generate_retrieval_data.py --corpus 1000 --cases 50 \
    --real-vault ~/.syvrio/vault-v2                 # 重新生成数据（真实 LLM，需 SPARK_API_KEY）
uv run python -m evals.short_facts_eval --details  # 大规模短字段回归
uv run python -m evals.field_lookup_eval           # 完整标签与重叠标签回归
```

## 方法

- **语料**：1000 条合成条目，由真实 LLM 一次性生成后冻结为 `evals/data/retrieval_corpus.json`。内容为虚构生活记录（占位人物、编造数值），生成时过滤邮箱/URL/手机号/身份证号/车牌等个人信息标识；真实品牌、城市等专有名词允许出现，但不指向任何真实个人。
- **题库**：50 题，冻结为 `evals/data/retrieval_cases.json`，以**目标条目 ID** 判命中（不是子串）。混合真实问法：指向性明确的主题复述、弱指向口语改写（“我上次……来着”）、字段查询（“我什么血型/我今年多大”）、数字干扰与低词面重叠；字段题用 `targets` 接受同标签任一条目。
- **指标**：任一目标命中率记 Recall@3 / Recall@5（更准确地说是 Hit@K），MRR@10 为前十条内首个命中目标的倒数排名。

生成器见 `evals/generate_retrieval_data.py`（需真实 LLM 与 `SPARK_API_KEY`）；它不参与评测与 CI，仅用于离线产出或更新冻结数据。

短字段评测在原有合成数据上加入字段式姓名、年龄、家乡、职业、设备型号，以及同领域和带数字的干扰项。它通过 `MemoryService.recall` 跑混合检索，分别报告原题、短字段和整体的 Hit@1/3/5、MRR@10、预热查询中位耗时，以及冷建索引耗时。该评测直测检索层，未调用 LLM 改写或自动召回。

字段匹配评测使用另一组固定合成语料，覆盖重叠标签、同名字段的上下文区分、英文标签和普通文本。除 Hit@1/3 外，还单独统计较短标签错误排到第一的次数。

## 最新重跑（2026-11-10，LLM 生成冻结数据）

控制变量：两分支运行同一份 `evals/retrieval_eval.py`，读取同一份冻结的 `evals/data/*.json`（1000 条语料 / 50 题，含主题复述、口语改写、字段查询、数字干扰、低词面重叠）；解释器 CPython 3.12.14，本地 `BAAI/bge-small-zh-v1.5`，top-k=10。被测代码 main `703e969`（0.3.4）与 evolution 当前工作区。

语料特征（生成语料自身，冻结可复现）：1000 条，长度中位 47、P90 66，汉字占比 0.596，含冒号 46.2%，含“的”62.7%，中英混排 98.1%；生成时已过滤邮箱/URL/手机号/身份证号/车牌等个人信息模式。

main（`703e969`）：

| 检索 | Recall@3 | Recall@5 | MRR@10 |
|---|---:|---:|---:|
| keyword | 84% | 84% | 0.820 |
| vector | 88% | 96% | 0.837 |
| hybrid | 88% | 96% | 0.857 |

evolution（当前工作区）：

| 检索 | Recall@3 | Recall@5 | MRR@10 |
|---|---:|---:|---:|
| keyword | 84% | 84% | 0.820 |
| vector | 88% | 96% | **0.847** |
| hybrid | **90%** | 96% | **0.862** |

分类 Recall@3（两分支 keyword/vector 逐项相同，hybrid 仅 entertainment 不同）：

| 分类 | keyword | vector | hybrid（main / evolution） |
|---|---:|---:|---:|
| entertainment | 86% | 71% | 71% / 86% |
| learning | 100% | 83% | 83% |
| housing | 60% | 100% | 100% |
| finance | 83% | 100% | 100% |
| food | 100% | 100% | 100% |
| identity | 71% | 86% | 86% |
| career | 100% | 100% | 100% |
| health | 83% | 83% | 83% |
| schedule | 67% | 67% | 67% |
| devices | 100% | 100% | 100% |
| relationships | 100% | 100% | 100% |
| technology | 100% | 100% | 100% |

结论：keyword 两分支逐项相同；vector 召回相同、MRR@10 高 0.010；evolution 的 hybrid 仅 Recall@3 +2pp（88%→90%）、MRR@10 +0.005。逐题看只多救回 1 题（Roland FP 90X 键盘：查询含 ASCII 型号词，字面闸保留精确命中），其余 hybrid 失败题的目标不在 keyword/vector 任一候选池内，融合无法补回。50 题中 1 题即 2pp，单分类仅 2–7 题时 1 题可达 14–50pp，因此分支差异在表面上很小。未发现指标回退。

字段查询已并入 50 题（identity 分类 71%/86%/86%）：含标签词的“邮箱前缀/偏好语言/常住城市/血型”三路都过；“我今年多大”因不含“年龄”而 keyword 失败；“我叫什么名字”被“昵称”条目干扰，三路都失败。数字干扰与低词面重叠题多数通过，失败集中在语义相近条目的排序。

## 历史（2026-10-30，旧题集）

范围：main 与 evolution，同一解释器与本地 `BAAI/bge-small-zh-v1.5` 模型；456 条合成记忆 / 27 个固定用例。旧脚本按可接受子串判命中，指标为 Hit@K（当时称 Recall@K），不能与当前按目标 ID 判分的结果直接比较。两分支指标相同。

| 检索 | Hit@3 | Hit@5 | Hit@10 | MRR@10 |
|---|---:|---:|---:|---:|
| keyword | 74.1% | 74.1% | 74.1% | 0.722 |
| vector | 92.6% | 92.6% | 96.3% | 0.913 |
| hybrid | 92.6% | 92.6% | 96.3% | 0.913 |

结论：未发现基础检索命中率回退；混合检索在合成改写题上有效。旧题集与当前 1000 条冻结语料、50 题的题集不同，数字不可直比。


## 历史结果（2026-10-25，BAAI/bge-small-zh-v1.5 本地推理）

| 检索方式 | Recall@3 | Recall@5 | MRR@10 |
| --- | --- | --- | --- |
| keyword | 67% | 67% | 0.667 |
| vector | 89% | 89% | 0.815 |
| hybrid | **89%** | **89%** | 0.815 |

分类 Recall@3：hybrid 在原词 / 身份上与向量单路持平且由两路融合保障；数字类从 50% 升到 75%（hybrid 增加 keyword 命中的字面覆盖闸：查询的 CJK bigram 在命中条目覆盖率 < 0.3 视为虚匹配，不参与 RRF——此前干扰项的 BM25 高名次会把向量路的正确结果挤下去）。跨语言 50% 是 bge-small-zh 的模型局限，agent 查询改写已要求非中文问题补充中文说法，根治需换 multilingual embedding 模型。

## 旧题集观察（不代表当前分组指标）

- 身份类超短条目（如"姓名：X"）受小节前缀稀释和同义词（姓名↔名字）影响，三路 Recall@3 均低。
- 数字类小样本（4 例）下 hybrid 会被共享前缀的同小节条目挤占。
