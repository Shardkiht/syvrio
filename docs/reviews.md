# 历史代码评审

- 文档状态：历史记录（非当前行为基准）
- 更新日期：2026-11-17

这里保存两次按时间点完成的代码评审快照。正文保留当时的项目名称、路径、提交基线与测试结果，仅作历史证据；当前设计、命令和分支规则以[文档索引](README.md)、[架构](architecture.md)、[HANDOFF](HANDOFF.md) 与 [AGENTS](../AGENTS.md) 为准。

## quiet-memo 代码评审记录（2026-10-27）

> 历史评审快照。保留当时的项目名称、发现和验证结论；当前设计及命令请看[文档索引](README.md)。

> **历史记录提示（2026-11-04）**：本报告保留当时的项目名、路径与提交证据；其中 `src/quiet_memo`、`.quiet-memo` 等路径已不再有效，当前代码与路径以 `src/syvrio`、`.syvrio` 为准。

> 评审基线：`main` @ `86262cf`
> 评审范围：`src/quiet_memo` 全部模块、测试、配置示例、README 与 CI
> 评审方式：只读代码走查 + 全量测试验证，未修改任何源码
> 测试结果：`uv run pytest -q` → **272 passed**（137.04s）

---

### 结论摘要

| 编号 | 级别 | 问题 | 状态 |
|---|---|---|---|
| B1 | 高 | 敏感信息正则误杀正常记忆，且静默拦截 | 已修复 |
| B2 | 高 | 压缩前抽取失败后，那批用户消息永久不再抽取 | 已修复 |
| B3 | 中高 | 后台补沉淀队头阻塞，旧会话可能永远轮不到 | 已修复 |
| B4 | 中 | 切换/新建会话后状态栏沿用上一会话的上下文用量 | 已修复 |
| B5 | 中 | “改写后确认”先写原文再写更正，留下多余 superseded 记录 | 已修复 |
| B6 | 中 | 记忆 Markdown 非原子写，与检索并发时可能读到半写文件 | 已修复 |
| B7 | 中 | 向量后端失败后每次检索都重试全量 embedding | 已修复 |
| B8 | 中 | `/recall` 在 UI 线程同步检索，首次可能卡界面 | 已修复 |
| S1 | 中 | `fetch_web` 未限制协议/内网地址，存在 SSRF 面 | 已修复（纵深防御） |
| A1 | 低中 | UI 直接读取 store，绕过 service 接口 | 已修复 |
| A2 | 低中 | 同一批候选重复全量扫描，复杂度 O(n²) | 已修复 |
| A3 | 低中 | 侧栏/启动补记全量加载会话文件 | 已修复 |
| A4 | 低 | 工具线程池不可强杀，慢任务会占满 4 个 worker | 已修复 |
| D1 | 低 | `chat_app.py` 813 行，超过 800 行约定 | 已修复 |
| D2 | 低 | README 描述的侧栏键盘操作与实现不符 | 已修复 |
| D3 | 低 | 若干死代码 | 已修复 |
| D4 | 低 | 配置文件数值缺校验，错误输入抛原始 traceback | 已修复 |
| D5 | 低 | `set_status` 不更新 front matter 的 `updated` | 已修复 |

---

### 一、隐藏 bug

#### B1 敏感信息正则误杀正常记忆（高）

位置：[`src/quiet_memo/memory/detect.py:9`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/memory/detect.py#L9)

```python
SECRET_RE = re.compile(
    r"(api[_ -]?key|access[_ -]?token|secret|password|passwd|密码|口令|验证码|私钥)"
    r"\s*(是|为|[:：=])?\s*\S{4,}",
    re.I,
)
```

关键词后只要有 4 个以上非空白字符就命中，中文同样计入。实测（2026-10-27）：

| 输入 | 是否判为密钥 |
|---|---|
| 我的密码是 abc12345 | 是（正确） |
| API key: sk-xxxxx | 是（正确） |
| 密码忘记了怎么办 | **是（误判）** |
| 验证码一直收不到 | **是（误判）** |
| 这个软件的密码管理器好用 | **是（误判）** |
| secret 是 我心中的秘密 | **是（误判）** |

影响：命中后进入 `blocked` 而不是 `queued`（[`src/quiet_memo/memory/service.py:244`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/memory/service.py#L244)），TUI 对话中不会提示；用户以为已经记住，实际什么都没写。

建议：把关键词后的“值”限制为 ASCII 凭据形态，例如 `[A-Za-z0-9_\-.:/]{4,}`；或要求关键词与值之间必须出现 `是/为/:/=` 之一，且值不含 CJK。补充“密码忘了/验证码收不到”类反例测试。

#### B2 压缩前抽取失败 → 这批消息永久漏抽取（高）

位置：[`src/quiet_memo/agent/context.py:190`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/context.py#L190)

`_compact` 中 flush 失败只记录一条事件，随后仍然调用摘要器并追加 `summary` 记录（[`L208`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/context.py#L208)），该记录携带 `covered_records`。而后续沉淀游标以摘要边界为下界（[`src/quiet_memo/agent/session.py:141`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/session.py#L141)），因此被压缩掉的这批用户陈述此后不会再进入 `user_texts_after_distilled()`。

影响：LLM 抽取偶发失败（限流、超时）时，用户该轮陈述永久丢失，且没有任何重试入口。

建议：flush 失败时不要推进摘要覆盖边界，或把失败段落写入 `extraction_error` / 待补记队列，由启动补沉淀重试。

#### B3 后台补沉淀队头阻塞（中高）

位置：[`src/quiet_memo/agent/runtime.py:297`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/runtime.py#L297)

```python
for session in self.sessions.unconsolidated(limit, exclude=exclude):   # limit=3
    ...
    if len(texts) < min_new_messages:   # 切换会话时 = 3
        continue
```

先取最近 3 个“需要沉淀”的会话，再判断新消息数量是否达标。如果这 3 个会话都只有 1–2 条新消息，它们会长期占住名额被跳过，更老的未沉淀会话永远进不了窗口。

建议：先按 `min_new_messages` 过滤再截断；或在跳过时顺延补足下一批。

#### B4 会话切换后状态栏显示旧会话用量（中）

位置：[`src/quiet_memo/app/chat_app.py:463`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_app.py#L463)

- `AgentRuntime.start()`（[`runtime.py:70`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/runtime.py#L70)）与 `resume()`（[`runtime.py:85`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/runtime.py#L85)）都没有清空 `last_plan`。
- `_after_session_switch()` 直接调用 `update_status()`（[`chat_app.py:475`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_app.py#L475)），后者读取 `runtime.last_plan.estimated_tokens`。

影响：新建会话或恢复会话后，底部上下文进度条显示的是上一个会话的占用，直到发送下一条消息才纠正。`undo()` 反而清了 `last_plan`（[`runtime.py:389`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/runtime.py#L389)），行为不一致。

建议：`start()`/`resume()` 中重置 `last_plan = None`，或在切换后按新会话重新估算。

#### B5 “改写后确认”产生多余 superseded 记录（中）

位置：[`src/quiet_memo/app/chat_commands.py:138`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_commands.py#L138)

```python
entry = self.service.approve(item_id)          # 先写原文
if override and override != entry.text:
    entry = self.service.correct(entry.id, override, reason="人工改写后写入")
```

影响：vault 里多出一条“从未被用户确认过”的原文（状态 superseded），audit 多两条记录；用户只看到改写后的版本。

建议：给 `approve` 增加可选 `text_override`，直接以改写文本写入，不产生中间条目。

#### B6 记忆 Markdown 非原子写（中，推断）

位置：[`src/quiet_memo/memory/store.py:112`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/memory/store.py#L112)

```python
path.write_text(f"---\n{front}\n---\n\n{body.lstrip()}", encoding="utf-8")
```

`write_text` 先截断再写入。TUI 后台补沉淀写 vault 时，前台检索可能读到空文件或半行，表现为某主题瞬时检索为空/少条目。注意 `vectors.npz` 与 `search-cache.json` 都已使用临时文件 + `os.replace`（[`vector.py:169`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/retrieval/vector.py#L169)、[`cache.py:70`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/infra/search/cache.py#L70)），只有 Markdown 没有。

建议：统一改为“写 `.tmp` 后 `os.replace`”，并在失败时清理临时文件。

#### B7 向量后端失败后每次检索重试全量 embedding（中）

位置：[`src/quiet_memo/retrieval/vector.py:96`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/retrieval/vector.py#L96)、[`L116`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/retrieval/vector.py#L116)

异常时置 `_degraded = True`；`search()` 只要 `_degraded` 为真就重新 `sync()`。若 API Key 失效或额度耗尽，每个查询都会重新尝试嵌入所有未缓存条目。

影响：检索延迟持续升高、额度反复被打；用户只会感觉“搜索越来越慢”。

建议：degraded 状态加退避（例如 5 分钟内只重试一次），或直接降级 keyword 并在 `/status` 标注。

#### B8 `/recall` 在 UI 线程同步检索（中）

位置：[`src/quiet_memo/app/chat_commands.py:68`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_commands.py#L68)

`/recall` 直接调用 `service.recall(...)`。hybrid 首次检索会触发 `VectorRetriever.sync()`（加载 fastembed 或调用 embedding API），在 TUI 事件线程中可能卡住数秒甚至更久。同类问题：`/model` 首次打开会在 UI 线程 import litellm、查询 keyring（[`actions.py:20`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/actions.py#L20)）。

建议：`/recall` 走 `run_worker(..., thread=True)`，与 `_ask_work` 保持一致。

---

### 二、安全与合规

#### S1 `fetch_web` 存在 SSRF 面（中）

位置：[`src/quiet_memo/agent/tools.py:486`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/tools.py#L486) → [`src/quiet_memo/infra/search/router.py:141`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/infra/search/router.py#L141)

`fetch_web` 接受模型给出的任意 URL，本项目入口未校验协议与目标地址。模型可能被外部搜索结果中的提示注入诱导抓取 `http://127.0.0.1:...`、内网地址或云元数据地址（请求由本地 daemon 发出）。

补充核查（2026-10-28）：open-websearch 2.2.0 的 daemon 侧已实现 HTTP(S) 限制、私网/回环拒绝、代理模式下的逐跳 DNS 复验（`urlSafety.js`、`httpRequest.js`），因此这不是“已确认可利用的 SSRF”，而是本项目入口缺少纵深防护。修复仍按以下建议落地：

建议：
- 只允许 `http/https`；
- 解析后拒绝 `localhost`、回环、私网段、链路本地地址（含 `169.254.169.254`）；
- 对 `final_url` 做二次校验（防重定向绕过）。

#### 已确认正常项

- 仓库内无真实 API Key；`quiet-memo.toml` 已被 `.gitignore` 忽略（`.gitignore:10`）。
- 密钥走 keyring/环境变量，`write_config` 不写 Key（[`providers.py:283`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/infra/providers.py#L283)）。
- `evals/retrieval_eval.py` 为合成语料，不含真实个人信息。
- `read_topic` 只按主题名/标题匹配，不接受路径，无路径穿越。
- 会话 ID 有正则校验，`SessionStore._path` 拒绝非法 ID。
- 搜索缓存、向量缓存均在 vault 内并被 gitignore，属可重建派生数据。

#### 小问题：TOML 未转义

[`src/quiet_memo/infra/providers.py:286`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/infra/providers.py#L286) 将 vault 路径直接插进双引号字符串。路径含 `"` 时会生成非法 TOML。建议使用 `tomllib` 无法写回的前提下，至少对 `"` 和 `\` 做转义。

---

### 三、强耦合与架构

#### A1 UI 直接读取 store

[`src/quiet_memo/app/chat_commands.py:175`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_commands.py#L175) `/topics` 使用 `self.service.store.entries(topic=...)`，绕过 `MemoryService` 的公开接口。建议 service 提供 `topic_entries(name)`，界面层不依赖存储实现。

#### A2 同一批候选重复全量扫描

[`src/quiet_memo/memory/service.py:247`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/memory/service.py#L247) 每个 candidate 调 `has_text()`（全库读一遍），[`L250`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/memory/service.py#L250) 再调 `find_conflicts()`（再读一遍）。20 条候选 = 40 次全库读取。

建议：在一次 `_adjudicate` 内取一次 `entries()` 快照，增量维护“已写入”集合。

#### A3 会话列表全量加载

[`src/quiet_memo/agent/session.py:218`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/session.py#L218) `list()` 逐个加载并解析全部会话文件；`refresh_sessions()` 在启动、每轮回复、后台提交后都会调用。启动补记也在 UI 线程先跑 `unconsolidated()`（[`chat_app.py:223`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_app.py#L223)）。

建议：缓存会话摘要（id/title/updated/count），只对变更文件失效；启动扫描移入 worker。

#### A4 工具线程池不可强杀

[`src/quiet_memo/agent/tools.py:26`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/tools.py#L26) 全局 `ThreadPoolExecutor(max_workers=4)`；超时后 `future.cancel()` 只能取消未开始的任务。连续慢 `fetch_web` 会占满线程池，导致后续工具排队超时。建议为抓取类工具单独池，或改为可终止的子进程。

#### 重复的指纹函数

[`src/quiet_memo/retrieval/keyword.py:52`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/retrieval/keyword.py#L52) 与 [`src/quiet_memo/retrieval/vector.py:144`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/retrieval/vector.py#L144) 各有一份 `_file_fingerprint`，逻辑完全一致。可下沉到 `MemoryStore`，避免存储格式变化时两处同时修改。

---

### 四、风格、文档与一致性

#### D1 文件超长

[`src/quiet_memo/app/chat_app.py`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_app.py) 共 813 行，超过 AGENTS.md “单个源码文件控制在 800 行以内”。可先拆出 CSS 常量与 transcript 渲染。

#### D2 README 侧栏键盘描述与实现不符

[`README.md:130`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/README.md#L130) 与 [`README.en.md:130`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/README.en.md#L130) 写“左侧会话列表：↑↓ 选择、Enter 恢复、Del 删除”，但侧栏在 [`chat_app.py:194`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_app.py#L194) 被设为 `can_focus = False`（测试 [`test_chat_app.py:440`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/tests/test_chat_app.py#L440) 也固定了该行为），键盘事件无法进入侧栏；[`on_key` 的删除分支](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_app.py#L375) 要求 `view.has_focus`，实际不可达。

实际行为：侧栏仅支持鼠标点击切换；键盘操作在 `/sessions` 选择器内。建议二选一——修正 README，或让侧栏可聚焦并移除“焦点固定在输入栏”的约束。

#### D3 死代码

- [`src/quiet_memo/app/ui.py:17`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/ui.py#L17) `WARNING` 未使用
- [`src/quiet_memo/infra/layout.py:19`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/infra/layout.py#L19) `indent()` 未使用
- [`src/quiet_memo/agent/context.py:260`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/context.py#L260) `default_summary()` 未使用
- [`src/quiet_memo/llm/summarizer.py:71`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/llm/summarizer.py#L71) `SessionSummarizer` 兼容壳无引用
- [`chat_app.py:375`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/chat_app.py#L375) 侧栏 Del 分支不可达

#### D4 配置数值缺校验

[`src/quiet_memo/infra/config.py:165`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/infra/config.py#L165) 起大量 `int()/float()` 直接作用于 TOML 值。用户写 `context_window = "32k"` 会在启动时抛原始 traceback。建议统一捕获并提示“配置项 X 非法”。

#### D5 `updated` 元数据不随状态变化

[`src/quiet_memo/memory/store.py:261`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/memory/store.py#L261) `set_status` 不更新 front matter 的 `updated`。`correct` 因追加新条目会更新，`forget` 不会，导致 `topics().updated` 在软删除后仍是旧日期。

#### 其他小项

- `search_router = search_router or build_search_router(...) if enabled else None`（[`server.py:23`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/app/server.py#L23)、[`tools.py:105`](https://github.com/Shardkiht/syvrio/blob/86262cfdba11f30c7cb242dd3d105e25e0b7c1ef/src/quiet_memo/agent/tools.py#L105)）：优先级不直观，`enabled=false` 时连显式传入的 router 也会被丢弃。建议加括号。
- `_approve_locked` 不复查 `has_text`：待确认项在排队期间若已存在同文，会重复写入。
- `Session.messages` 只统计用户消息，侧栏 “N msgs” 不含助手回复；若产品语义是“总消息数”，需改名或改口径。

---

### 五、已确认良好的部分

- 分层依赖：`app → agent → memory → retrieval → infra`，未发现新增反向依赖或循环依赖。
- 忙碌态会话切换、新建会话、侧栏点击均会排队接续，不静默丢弃。
- 流式中断保留半截并显式标注；完成时就地定稿，不重复挂载。
- `undo` 通过追加事件实现，不重写 JSONL，且会让后续摘要失效。
- 敏感字段不写入配置；缓存文件均为派生数据且原子写。
- 测试夹具把 `QUIET_MEMO_CONFIG` / `QUIET_MEMO_VAULT` 钉在临时目录，且屏蔽 keyring 与真实 Key。
- CI 在 ubuntu-latest 与 windows-latest 双矩阵跑全量测试。

---

### 六、建议处理顺序

1. B1 敏感信息误杀（静默丢用户记忆）
2. B2 压缩 flush 失败漏抽取、B3 补沉淀队头阻塞（数据完整性）
3. B4 状态栏串会话、B8 `/recall` 阻塞 UI（可见体验）
4. B5 改写确认双写、B6 原子写、B7 degraded 重试
5. S1 `fetch_web` 地址限制
6. D1/D2 文档与约定、A1–A4 性能与耦合、D3–D5 清理

---

### 七、修复记录（2026-10-28）

分支：`fix/review-b1-b5`（提交 `873d49c`，已推送）
验证：`uv run --no-sync pytest -q` → **345 passed**（159.17s）

| 编号 | 修复方式 | 回归测试 |
|---|---|---|
| B1 | `SECRET_RE` 区分“明确赋值 / 空白分隔 / 无分隔”：明确赋值保留 Unicode 值；空白分隔只接受 ASCII 凭据形态；无分隔要求 ASCII 可打印字符 | `tests/test_detect.py` |
| B2 | 摘要覆盖边界与沉淀进度分离：`summary` 记录新增 `distilled_ids`，只确认成功 flush 的消息；失败仍压缩但保留待抽取；旧摘要按 `covered_records` 兼容 | `tests/test_context.py`、`tests/test_session.py`、`tests/test_agent.py` |
| B3 | `unconsolidated()` 先按 `min_new_messages` 过滤再截断；阈值随调用传入 | `tests/test_session.py`、`tests/test_agent.py` |
| B4 | `start()` / `resume()` 清空 `last_plan` | `tests/test_agent.py`、`tests/test_chat_app.py` |
| B5 | `approve(item_id, text_override=...)` 单次写入最终文本；空文本、凭据、重复内容在写入前拒绝；audit 记录 `text_override` | `tests/test_service.py`、`tests/test_chat_app.py` |
| B6 | `MemoryStore._write`、`PendingQueue._save` 改为同目录临时文件 + `os.replace`，失败清理临时文件并保留原文件 | `tests/test_store.py`、`tests/test_queue.py` |
| B7 | 向量同步/查询失败后进入 60s 冷却（可注入 clock），冷却内不再调用后端；降级时 Hybrid 直接回退关键词 | `tests/test_vector.py` |
| B8 | `/recall` 改走 worker；`/model` 的选项构造与真实模型刷新都移出 UI 线程 | `tests/test_chat_app.py` |
| S1 | 抓取入口校验 http(s)、localhost、私网/回环/链路本地地址与 DNS 解析结果，并对 `final_url` 复验；支持与上游同名的 `FAKE_IP_CIDRS` | `tests/test_web_search.py` |
| A1 | 新增 `MemoryService.topic_entries()`，`/topics` 不再直接读 store | `tests/test_chat_app.py` |
| A2 | 每批候选只取一次全量快照，增量维护 ID、文本与冲突集合；`append()` 支持传入已知 ID 集合 | `tests/test_service.py` |
| A3 | 会话列表按 `(mtime_ns, size)` 缓存摘要；补沉淀扫描缓存待抽取条数；启动/切换扫描均在 worker | `tests/test_session.py` |
| A4 | `fetch_web` 使用独立 `network` 线程池，慢抓取不再占满本地工具池 | `tests/test_web_search.py` |
| D1 | `chat_app.py` 降至 777 行（删除不可达分支与重复启动提交逻辑） | 全量测试 |
| D2 | README 中英文侧栏说明改为“点击/列表聚焦后键盘选择；删除与重命名在 `/sessions`” | 文档 |
| D3 | 删除 `WARNING`、`indent()`、`default_summary()`、`SessionSummarizer` 与侧栏 Del 分支 | 全量测试 |
| D4 | `load_config` 增加结构、类型、范围校验并抛中文 `ConfigError`；TOML 写入使用 JSON 字符串字面量转义 | `tests/test_setup.py` |
| D5 | `set_status` 同步更新 front matter 的 `updated` | `tests/test_store.py` |

#### 与原始建议的差异

- **B1**：原建议“值不含 CJK”会漏掉中文密码。实际规则改为：明确赋值（`是/为/:/=`）保留 Unicode，空白分隔只认 ASCII 凭据，无分隔只认 ASCII 可打印字符。`secret 是 我心中的秘密` 这类明确赋值仍保守拦截，已在 README 标注为启发式边界。
- **B2**：原建议“flush 失败不推进摘要边界”会导致压缩预算失效。改为把“摘要覆盖范围”和“已沉淀消息 ID”分开记录，两者都追加在 `summary` 事件中。
- **B7**：冷却时间取 60s（而非建议的 5 分钟），兼顾额度保护与恢复速度；`/status` 的检索模式仍由启动时构建结果决定。
- **S1**：上游 daemon 2.2.0 已有逐跳校验，本次是入口纵深防御，不宣称修复了可利用漏洞。
- **其他小项**：`_approve_locked` 已复查重复文本；`search_router` 的三元表达式已加括号，显式传入的 router 不再因 `enabled=false` 被丢弃。`Session.messages` 继续只统计用户消息，侧栏 “N msgs” 的产品语义未变。

### 附：复现命令

```bash
# 全量测试
uv run pytest -q

# 敏感信息误判复现
uv run python -c "
from quiet_memo.memory.detect import SECRET_RE
for s in ['密码忘记了怎么办', '验证码一直收不到', '这个软件的密码管理器好用']:
    print(bool(SECRET_RE.search(s)), s)
"
```

> 第一至六节为评审基线记录（基于 `86262cf`，行号可能漂移）；第七节为 2026-10-28 的修复与验收记录。

---

## Quiet-Memo main 冻结前代码评审（2026-11-01）

> 历史评审快照。保留当时的项目名称、发现和验证结论；当前设计及命令请看[文档索引](README.md)。

> **品牌迁移提示（2026-11-04）**：本报告沿用当时的 `Quiet-Memo` 名称与 `src/quiet_memo` 路径，仅作历史证据；当前项目为 `Syvrio`，源码路径为 `src/syvrio`，Vault 元数据目录为 `.syvrio/`。

> **历史评审／已结案提示（2026-11-02）**：本报告的 `b016ccbf`、`0.3.2` 和“F1/F2 待修”等均是冻结前的时点证据，不代表当前状态。后续 PR #15 已合并稳定性修复并形成 `portfolio-2026-11-01`（`316a030`）；PR #16 仅做发布准备，`main` 已到 `22217c2`，`v0.3.3` GitHub Release 已发布。两次 PR 的 Windows/Ubuntu CI 均通过；原生 Windows TUI 人工验收仍未确认。现行分支规则见 [AGENTS.md](../AGENTS.md) 和 [HANDOFF.md](HANDOFF.md)。以下历史评审正文保留原貌，禁止把其待办当成当前指令。

> 基线：`main@b016ccbf4e220db8e432da115f0ab4a0c677b89c`（本报告和分支规范的文档提交不改变该源码基线）。
> 方法：远端源码静态走查，核查相关单测、已有评审和 GitHub Actions 日志。**本轮未在本地重新运行 pytest、打包、真实 TUI 或真实模型 / API 调用**。
> 决策：后续默认在 `feature-memory-evolution` 开发；`main` 只做最终稳定性验收后冻结的简历展示基线，未经用户针对 main 明确授权不得自动同步或合并。

### 结论

- **有条件可冻结**：现有自动化测试与模块化程度可以支撑简历展示；静态走查未发现已证实的 P0 / P1 阻断项。
- 冻结前建议处理 F1、F2，或明确记录故障注入验收结果；F3 应在文档中注明接受或采取有限修复。
- 应在代码修复停止后完成一次实际安装/打包与终端人工检查；以固定 SHA/专用 portfolio tag 关联简历，不能把较早的 `v0.3.2` Release 直接视为本轮 `main` 快照。

### 验证与覆盖

- `main` 源码基线：`b016ccbf`；分支保护已开启。
- GitHub Actions：https://github.com/Shardkiht/quiet-memo/actions/runs/41281207052 。Windows 和 Ubuntu 两个 job 均 success；Ubuntu 日志：**846 passed, 5 skipped, 4 warnings**。警告来自依赖（jieba/Pydantic），不能等同于项目逻辑失败。
- `.github/workflows/test.yml` 执行 `uv sync --frozen` 与 `pytest -q`，没有在普通 push/PR CI 阶段单独运行 `uv build`；发布工作流 `workflow.yml` 有打包步骤。
- 已重点审阅：`agent/session.py`、`agent/runtime.py`、`agent/context.py`、`agent/tools.py`、`app/chat_flow.py`、`app/chat_app.py`、`app/chat_commands.py`、`memory/{service,store,queue,detect,models,extractor}.py`、`retrieval/{keyword,vector,hybrid}.py`、`infra/search/` 搜索/抓取/计数模块、`llm/client.py`、配置、测试及文档。属于重点静态审查，**不宣称逐行审完仓库所有文件**。

### 发现清单（按优先级）

#### F1 · P2 · 损坏会话隔离范围不足（建议冻结前修）

- `src/quiet_memo/agent/session.py:204-234`：`SessionStore.load()` 对中间行不可解码的 UTF-8 会抛 `UnicodeDecodeError`，符合“未标记中间损坏不能静默忽略”的数据不变量。
- 但 `session.py:278-307` 的 `_listing_row()`、`session.py:337-360` 的 `unconsolidated()` 只捕获部分异常（`JSONDecodeError`、`KeyError`），可能让**单个损坏文件导致整个会话列表或补沉淀扫描中断**。另外，合法 JSON 但非对象的记录在 `load():221` 访问 `record.get` 时可能抛 `AttributeError`。
- 当前 `tests/test_session.py` 已覆盖损坏尾行恢复和中间行加载失败；未看到“损坏会话与正常会话并存时列表/扫描仍可用”的针对性用例。
- 建议：保持单文件直接读取时报错和原始字节不变，在列表/扫描批量入口隔离单文件错误；用临时 vault 注入坏 UTF-8 中间行与非对象 JSON，并验证正常会话不受影响、错误不丢失。

#### F2 · P2 · 待确认状态跨文件更新的失败恢复（建议冻结前做故障注入）

- `src/quiet_memo/memory/queue.py:56-66`：`resolve()` 先从 `pending.jsonl` 移除项，再写 `resolved.jsonl`。如果后一步磁盘写失败或进程中断，待确认项已消失，但解决日志未写，影响审计追溯。
- `src/quiet_memo/memory/service.py:496-514`：`approve()` 依次写新记忆、修改旧状态、删除待确认项、写审计；`service.py:394-419` 的 `correct()` 同样先写新条目再停用旧条目。跨多个原子文件操作的中途故障可能留下重复有效记忆/部分完成状态。
- 已有 `test_approve_append_failure_keeps_previous_memory_active` 与 pending 保存失败测试，但与以上**中间步骤**对应的故障注入/崩溃恢复验证仍需补充。
- 建议：优先保证失败可检测、重试幂等及审计可补偿；如需引入大规模事务基础设施，本次宜记录残余风险、交由 evolution 处理。避免临时复杂重构。

#### F3 · P2 / 已知限制 · 厂商调用计数不保证跨进程原子性

- `src/quiet_memo/infra/search/usage.py:22-31,106-124` 使用进程内 `RLock`，Search / Fetch 同进程可共享，**多个 QM/MCP 进程并发时不能保证绝对不超限**。`docs/HANDOFF.md` 已明确这一边界。
- `tests/test_search_usage.py` 的并发验证是线程内实例，不等于跨进程事务测试。
- 冻结建议：保留功能并在说明中明确“本地保守限额，非供应商账单上限”；多实例严格计费保护属于 evolution 可选工作，不作为本轮冻结的强制重构。

#### F4 · P3 · 可选联网依赖未完全固定

- `src/quiet_memo/infra/search/open_websearch.py:259-264` 与 `infra/search/daemon.py:59-66` 均存在 `open-websearch@latest` 兜底启动路径。
- 使用该路径时，后续安装可能拉取不同上游实现，影响冻结版本的环境复现与兼容性。
- 建议固定已经验证的依赖版本，或者明确记录稳定运行环境并保留已安装命令优先逻辑。是否改动以兼容性回归为前提。

#### F5 · P3 · 极端并发下的会话创建占用

- `src/quiet_memo/agent/session.py:183-198` 为同秒随机 ID 加 `path.exists()` 循环，但最终使用 `write_text()`，不是独占创建。在**同时有多个进程创建完全相同 ID**的极端竞态下理论上可能覆盖文件。
- 常规 TUI 入口序列化且已有随机碰撞重试测试。无复现证据，冻结期可以接受；以后若支持多实例并发，可用独占打开补强。

#### F6 · 发布与简历展示版本一致性（发布前必核）

- `pyproject.toml` 为 `0.3.2`，公开 `v0.3.2` Release 指向较早发布过程；当前 `main` 已有后续变更。
- README 对当前特性描述、合成/真实检索结果口径、搜索评测范围应与固定源码一致；优先使用**专属 commit SHA / portfolio 标签**标识简历展示版本。
- `evals/README.md` 载明 456 合成记忆/27 用例的 BM25/Vector/Hybrid Hit@3 分别为 74.1% / 92.6% / 92.6%，882 真实有效事实/24 模型生成题分别为 95.8% / 91.7% / 91.7%。这不是同一检索方法优化前后的提升，也不能把小样本模型生成题写成人工标注的真实准确率。

### 已确认的正面设计

- 记忆主题 Markdown 使用同目录临时文件 + `os.replace()`；会话保留原始 JSONL，损坏尾行采用追加恢复标记，不覆写原始字节。
- 检索接口解耦，BM25 + 可选向量 + Hybrid，向量异常有冷却和关键词降级路径；部分关键算法有增量缓存与回归用例。
- TUI 生成期间输入、跨会话排队、Esc 取消、消息所属会话隔离、Markdown 复制等已有广泛 UI 测试。
- 搜索结果有 URL 稳定去重、provider 优先级交错、多个 provider 回退、超时/调用次数预算与外部资料的提示注入边界；Search 与 Fetch 使用独立顺序和共享本地厂商计数。
- 配置和凭据存储优先环境变量/系统 Keyring，仓库 .gitignore 排除本地 vault、会话、配置与密钥等敏感产物。

### 最终冻结验收建议

1. 修复/验证 F1 的批量异常隔离；为 F2 做故障注入，决定做小修还是明确接受剩余风险。
2. 按 `AGENTS.md` 要求运行受影响测试；对最终冻结版本跑一次 `uv sync --frozen`、`uv run pytest -q`、`uv build` 和 CLI 基础启动检查。真实 Windows TUI 输入/复制/切换应人工验收；未运行的步骤不得标已通过。
3. 不沿用历史真实 API 调用授权；本轮评审不调用付费模型、外部 Search API，也不读取个人 vault。
4. 确认最终稳定 SHA 和 CI 后再由用户决定标签/发布；**不要因本报告而自动修改业务代码、创建 Release 或合并 evolution 到 main**。
5. **后续所有开发从 `feature-memory-evolution` 开始**。本轮收尾完成后 `main` 不接受常规功能、架构探索或自动修复同步；特殊稳定修复需用户明确指定。

### 评审结论

`main@b016ccbf` 可作为稳定展示基线的候选，质量基础已较充分。**不等同于已经通过最终打包/真实终端验收，也不意味着风险为零。** 后续 Agent 应优先处理 F1 和 F2 这两个直接涉及数据可靠性的收尾项，完成验收后冻结。


### Local implementation and acceptance update

The final stability task independently reproduced F1/F2. Record validation and per-file diagnostics now isolate invalid sessions while direct load still fails and raw bytes stay unchanged. F2 uses scoped exception compensation for manual operations, with fault injection after state/log writes and retry checks, including cross-topic approval. It is deliberately not a durable transaction: forced termination, power loss, concurrent writers and rollback failure remain explicit limitations. F3 (cross-process quota atomicity) and F4 (optional latest upstream fallback) are accepted documented limits, without a new subsystem or unverified dependency pin. F5 now uses exclusive file creation, tested against an injected create-time race. F6 retains package version 0.3.2 and separates the portfolio source tag from the older release and historical evaluation cohorts; no evaluation inputs or metric values changed.

A build inspection also found default sdist inclusion of a pre-existing untracked local script. The archive now has a project-content allowlist; user-directed relocation preserved the original script outside the repository. Archive listings exclude local scripts/data/caches, and wheel CLI installation was checked in a disposable environment with locked dependencies. No real credentials, personal Vault, paid API or model calls were used. A flaky Markdown copy assertion was corrected to compare against the rendered width at copy time after scrollbar layout, rather than a stale snapshot.

Local validation: affected session/queue/service/Agent suite 155 passed; queue suite including explicit rollback-error reporting 8 passed; Markdown suite 25 passed; final default full suite 871 passed, 4 optional document tests skipped, one existing dependency warning. `uv sync --frozen` and `uv build` succeeded. Real Windows terminal/IME/OS clipboard interaction remains unaccepted manually; headless tests are not presented as manual verification. Remote PR, dual-platform CI, stable source SHA/tag and branch deletion are separate gates and must be recorded from actual results.

The pre-existing main Windows CI failure (run 41284275480) was independently inspected: native cursor refocus was asserted before its after-refresh event. The regression now awaits the captured cursor-show event and still checks active focus and cleanup. Product cursor behavior is unchanged; related input cases pass locally.
