# Syvrio 重构：目标架构与字段契约

> 更新：2026-11-19 · **目标设计，尚未落地** · 本次重构执行 Agent：**GPT 6.2 Astra**  
> 关联：[Issue #30](https://github.com/Shardkiht/Syvrio/issues/30)。本文优先于已经被修正的旧设计；当前 `main` 实现、AGENTS、HANDOFF 可能仍描述旧架构。  
> **范围**：只记录重要架构决定、确定的字段及不可破坏的业务约束。技术细节由执行 Agent 在审查源码后确定；本文件的更新不代表授权直接修改代码、真实 Vault、Issue 或发布版本。

## 一、整体架构：完整前后端分离

**已确定**：统一常驻 Syvrio Backend，TUI、Web、CLI/MCP 只作为交互/协议适配客户端。后端统一拥有业务状态、AgentRuntime、Session、Vault 写入及各类定时后台任务；客户端全部退出后后端仍应独立运行，不依赖请求或 UI 生命周期。

```text
交互 / 适配层
  ├─ Textual TUI
  ├─ React + TypeScript + Vite Web（含手机）
  ├─ CLI
  └─ MCP adapter
        │ 统一后端 API、请求结果、流式事件
        ▼
Syvrio Backend（独立常驻，统一会话/状态/写入所有权）
  ├─ Application / UseCases（会话、审核、导入、设置、Git 等）
  ├─ AgentRuntime + ToolRegistry（对话、Recall、工具调用）
  ├─ Background Memory（后台记忆任务组）
  │   ├─ Session Distillation（游标补记忆）
  │   ├─ Dream（完整 Profile + 新 Inference）
  │   ├─ Vault Maintenance（既有知识维护，后续）
  │   └─ Pending Context Fill（异步补全 Context）
  └─ Git Sync 等非记忆周期任务
        │
        ▼
Memory / Session / Retrieval / LLM / Stores
        │
        ▼
Infra（任务执行、定时、取消、状态/恢复、锁、原子 I/O、配置、日志）
        │
        ▼
Vault（事实来源）+ Runtime（任务状态、索引、缓存）
```

### 分层职责

| 模块 | 负责 | 不负责 |
|---|---|---|
| **前端 / 适配端** | 人机交互、快捷键、命令输入、展示、界面级状态、网络协议转换 | **任何后端任务调度**；业务队列/重试/定时器/恢复、持有业务 AgentRuntime、绕过服务直接写 Vault |
| **Application** | 组合用例、统一应用接口、跨模块协调；会话 FIFO/取消/Undo、审核、导入、Git、设置各用例自管内部流程 | 成为所有任务的超级调度器；领域语义和文件内部实现 |
| **AgentRuntime** | 对话 LLM 循环、ToolRegistry 调用、记忆写前强制 Recall、可信会话来源上下文 | 后台补记忆、Dream/Maintenance/Git 定时调度 |
| **MemoryService / Stores** | 记忆业务操作、确定性参数/并发校验、合法写入及持久化 | 用语义相似阈值替 Agent 判断事实冲突 |
| **Session** | 会话模型/有效记录投影、JSONL 存储、摘要和沉淀游标、会话查询 | LLM 记忆提取与后台任务生命周期 |
| **Background Memory** | 自主管理记忆类后台用例（Distillation、Dream、Maintenance、Context Fill） | Git 同步、聊天排队；要求所有子任务每轮一起执行 |
| **Infra** | 可复用的任务执行和触发机制、取消、状态持久化/Checkpoint、锁、原子写入、路径及诊断 | 决定业务触发条件、记忆内容或维护判断 |

### 前后端职责硬性约束

1. **所有后端任务调度必须在后端**。包括自动 Dream/Profile、Session 补沉淀、Vault Maintenance、Git 定时同步、异步 Context 补全以及今后新增的周期任务。任务的到期判定、队列、优先级、互斥、重试、恢复、取消与资源预算均不能依赖 TUI `set_interval`、Textual Worker、浏览器定时器或 Web 请求触发。前端断开不能取消本应持续的后台任务。
2. **前端可以发起用户明确请求**（聊天、手动 Dream、取消任务、修改设置），但只向后端提交请求；任务是否接受、如何调度/执行及其进度由后端负责。前端允许本地动画、局部刷新、输入防抖，但不得将这些 UI 计时器用于驱动后端业务。
3. 后端**常驻且独立于客户端**：统一处理业务与后台任务。多个 TUI/Web 会话可连接同一后端；同一聊天 Session 的并发轮次、切换与 Undo 必须受控。不能把前后端分离退化为“前端各自初始化一套 AgentRuntime”。
4. **只保留现有界面能力，不强制重写外观**：TUI 延续 Textual 的交互、键位、`/` 命令、Thinking 折叠/滚动、会话展示；Web 延续现有 React/TypeScript/Vite、FastAPI 适配及 REST/SSE 流式体验。重点重构调用与状态边界，CLI/MCP 同样遵循统一核心业务语义。离线初始化/显式迁移工具若确需直接文件操作，必须采用受控独占模式。
5. 统一后端必须明确单 Vault 进程/业务所有权、既有 `client.lock/vault.lock/git.lock` 的新语义；持久化任务进度、停止/重启恢复、写前证据版本核对，以及 Undo、后台 Memory 写入、Git 快照之间的一致性。不能仅凭某个文件锁就宣称解决跨进程并发。
6. 应通过稳定 API/事件模型让前端获取任务状态、进度、错误和取消结果；网络中断与客户端重连不能使实际后端任务消失。配置切换后后端的模型/密钥和运行中任务版本也需按一致性规则处理。
7. **实现未定**：具体本机 IPC/HTTP、守护进程启动方式、自动拉起/升级方案、客户端多会话事件订阅及权限认证。执行时优先复用现有 FastAPI/REST/SSE 与系统服务能力，不为“分离”引入复杂分布式架构；如涉及真正的产品行为选择，先停下来确认。

建议逻辑目录（**不是强制按文件名创建新类**）：

```text
app/                  Textual、Web API 适配、CLI、MCP
application/          用例与后端应用入口
agent/                前台 Agent 与工具注册
session/              Session、Store、Query
background_memory/    记忆任务管理与各子任务
memory/               MemoryService、领域模型、Stores、Change Tracking
retrieval/  llm/      检索、模型适配
infra/                共享任务/文件/状态/锁/配置/诊断
web/                  既有 React 前端
```

## 二、已确定的领域模型与字段

这些是**重构目标模型**，当前源码中的 Candidate、旧 Entry、旧 Reflection 状态不代表新字段契约。

```python
class MemoryDraft(BaseModel):
    text: str
    topic: str
    context: MemoryContext | None = None

class PendingItem(MemoryDraft):
    question: str
    related_ids: list[str] = Field(default_factory=list)

class MemoryEntry(MemoryDraft):
    id: str
    status: Literal["active", "deprecated"] = "active"
    recorded_at: datetime

class MemoryUpdate(BaseModel):
    id: str
    context: MemoryContext | None = None
    status: Literal["active", "deprecated"] | None = None

class RecallEntry(MemoryDraft):
    id: str
    status: Literal["active", "deprecated"]

class Inference(BaseModel):
    id: str
    text: str
    kind: str
    source_ids: list[str]
    status: Literal["hypothesis", "confirmed", "deprecated"] = "hypothesis"
    created_at: datetime
    updated_at: datetime
```

**MemoryContext：**

| 字段 | 确定含义 |
|---|---|
| `event_time` | 事件发生时间 |
| `valid_from`、`valid_to` | 事实有效期开始、结束 |
| `location` | 地点 |
| `emotion` | 用户明确表达的情绪，不推测 |
| `emotion_at` | 情绪关联的时间语义，有可靠依据才填写 |

- 时间精度允许 **`YYYY`、`YYYY-MM`、`YYYY-MM-DD`、空值**；部分精度不是确切的月初/年初，不得推断未知具体日期。`recorded_at` 是写入时间，不参与事实时间筛选。
- `MemoryDraft` 是 Agent 已整理好的事实内容；`topic` 负责分类与文件定位。删除旧 Candidate 的 `title/confidence/sensitive/kind/reason` 等字段及 Context 的 `time_text/location_kind/emotion_source/source`；来源类型不属于 MemoryContext。
- Memory 状态仅 `active/deprecated`；不物理删除。`MemoryUpdate` 只可**补齐原本为空的 Context 子字段**或修改 `status`，不能原地覆盖已有 `text/topic` 或非空 Context；事实纠错采用新事实 + 旧事实弃用，真实状态变化应尊重历史事实。
- `PendingItem.question` 是用户必须确认的疑问，`related_ids` 是关联记忆 ID，不能作为自动弃用指令；队列编号、创建时间和来源属于程序元数据。
- Inference `kind` 动态分类；`source_ids` 只指向 Memory ID，不得引用其他 Inference。`confirmed` 仍然是推断，不等于 Memory；Dream 新推断默认 `hypothesis`。**人工编辑 Inference：旧记录保留并设为 `deprecated`，编辑后新建不同 ID、默认 `confirmed` 的推断**，保留来源，不另建版本快照或变更事件日志。

## 三、Vault 格式与数据边界

| 数据 | 目标位置及性质 |
|---|---|
| Memory | `vault/memories/**/*.md`，按 `topic` |
| Pending | `vault/memories/pending.md`，仅活动待确认条目 |
| Pending 拒绝归档 | `vault/archived/pending.md`，原始条目只读留存，不作为事实 |
| Inference | `vault/inferences/**/*.md`，按 `kind` |
| Profile | 唯一 `vault/profile.md`，Dream 可整体原子重建 |
| Sessions | `vault/sessions/YYYY/MM/*.jsonl`，保留原始消息/摘要/沉淀事件 |
| Audit / Change Tracking | JSONL；Memory 变更日志可采用 `vault/records/memory-changes.jsonl` |
| 任务状态、检索索引、缓存 | Vault 外 Runtime；状态可恢复、索引可重建 |

- Memory、Pending、Inference 都采用 Markdown **Core / Context / Extensions**；不独立设置 Content，正文位于 `Core.text`。**Memory Core 仅 `id/status/text`**，`topic` 由文件路径确定；其他类型保留各自字段。
- 会话衍生的 Memory Extensions **必须保存真实的 `session_id/message_id`**；外部导入可无。来源由可信代码绑定，LLM 不编造。Session 的 `distilled/up_to` 游标不是逐条 Memory 来源 ID。
- Extensions 仅供程序追溯，**不进入普通 Recall、`read_topic`、BM25 或 Embedding**；不能把 Markdown 原始内部元数据全文交给 LLM。原始对话只存 Session JSONL。
- Session 摘要边界 `covered_records` 与沉淀进度 `distilled/up_to`、`distilled_ids` 相互独立；保留当前 JSONL 事件语义、Undo 投影、损坏尾行恢复，避免无意义迁移。
- 旧 Vault 迁移须先备份，执行显式迁移、核验事实条目与历史/引用，不能使用真实私有 Vault 进行自动重构测试，也不得将凭据及个人信息写入公开仓库。

## 四、记忆工具、Recall 与 Pending

### Agent 与 MemoryService

- Agent LLM 根据当前对话和 Recall 判断记忆价值、重复、矛盾、事实变化或纠错；重要语义不明确时主动询问用户，非关键 Context 缺失可留空。Agent 已整理 `MemoryDraft` 后，**不再另调 LLM 抽取同一事实**。
- Agent 发起的 Memory 写入、修改或 Pending 创建，由 AgentRuntime **强制前置 Memory Recall**；创建 Pending 还必须查询活动 Pending List。允许在操作内容、证据版本及上下文均未变化时复用有效 Recall，变化或上下文压缩后必须重新 Recall。
- Agent 工具：`recall`、`memorize`、`transition`（替代 evolve）、`correct`、`deprecate`（替代 forget）。MemoryService **直接实现**对应业务方法，内部统一通过 `add(draft)` 和 `apply(add?, updates?)` 可靠落盘；ToolRegistry 负责工具通用注册与分发，不另造独立的记忆 Handler。
- Service 进行 ID、状态、Context 合法性、路径、凭据、确定性幂等及并发校验；**不以 BM25/向量分数、confidence 或敏感信息标签代替语义决策**。凭据如 API Key、密码、Token、私钥应拦截；普通私人事实不因敏感标签被简单禁止（仍保持 Vault 私有）。

### Recall 字段契约

```python
recall(
    query: str,
    limit: int = 7,
    time_range: ... = None,
    location: str | None = None,
    emotion: str | None = None,
) -> list[RecallEntry]
```

- `query` 必需；无 `view`，`time_range` 统一按 `event_time` 或 `valid_from/valid_to` 的事实时间范围筛选，不以 `recorded_at` 筛选；部分精度按可能时间区间处理。
- 结构化返回 `RecallEntry = MemoryDraft + id/status`，包含完整 Core/Context 的 LLM 可见部分，**不包含 Extensions、分数、行号、文件路径或原始 Markdown**。
- BM25 索引文本：**正文 + 地点 + 情绪**；向量 Embedding：**仅正文**；所有内部 Extensions 与时间元数据不拼入检索文本。需通过既有检索评测验证 Hit@3、Hit@5 和 MRR，而非预设提升。
- RecallContext 是 Runtime 的操作、目标 ID、payload 指纹、证据版本、上下文代次等验证状态，不是 LLM 事实。无需额外 Recall 专属 Token 预算、固定裁剪或 LLM Reranker；交给现有对话上下文管理。

### Pending 审核

- Pending **仅在 Agent 经 Recall/必要澄清仍无法可靠决定、且需要用户之后确认时**产生；不是低置信候选池、Service 错误兜底。普通 Recall 与 Dream 不把 Pending 当事实；Confirmed Inference 只可选参考。
- 用户可确认、**只编辑正文**后确认、拒绝；`question` 只读。确认时正式 Memory 成功写入对应 Topic 后，才移除活动 Pending；拒绝则保留原条目到 `archived/pending.md`。不为已确认条目另建 Pending 历史副本，Pending 审核也不写独立 Audit。
- 文本**未实际改变**的确认复用原 Context，不额外调用 LLM。文本**确实改变**后，先以最终正文和**空 Context** 写入 Memory 并完成审核，再向 Background Memory 提交一次异步 Context 提取；后台仅补原本为空的字段，经 Service 校验。失败不能回滚已确认事实，也不能覆盖后续修改。
- `related_ids` 不自动引发对旧 Memory 的纠错/弃用；归档拒绝记录不作为事实证据。

## 五、后台记忆任务、Change Tracking

**Background Memory 是独立后台业务模块，不是 Application 内的超级调度入口。** 它管理记忆相关的周期/触发任务；通用执行和状态恢复来自 Infra。

| 子任务 | 主要输入 | 目标 |
|---|---|---|
| **Session Distillation** | Session 未处理用户消息及各自 ID | 增量补记忆，复用 Recall、Agent 语义判断和 MemoryService |
| **Dream** | 有效 Memory、可用的 confirmed Inference | **先**生成完整 Profile，**再**批量发现新的 hypothesis Inference |
| **Vault Maintenance** | Change Tracking 的 Memory 变化及最新 Vault 内容 | 定期检查已有 Memory、Inference、Pending、Profile、引用与索引的冲突、过时、重复和错误 |
| **Pending Context Fill** | 用户实际改写并确认后的 Memory | 后台额外一次 LLM 提取缺失 Context，校验并提交 |

### Session Distillation

- 保留 `distilled/up_to` 增量游标、`distilled_ids`，同一消息已处理不重复提取；后台扫描、Context 压缩和会话结束的触发需求都提交到**后端统一任务入口**，不在 TUI/Web 实现不同的提取逻辑。
- 按消息提取真实来源 ID；处理成功后推进游标，**即使没有值得保存的 Memory 也可推进**；写入失败或中断不跳过未处理内容。摘要成功与记忆沉淀成功互不等价。
- SessionStore 只负责可靠会话数据，SessionQuery 只负责待处理发现；提取 LLM 与任务进度归后台模块。旧 `extract_candidates/adjudicate_extracted` 阈值链路不再作为目标设计。

### Change Tracking（只追踪 Memory）

- 只在 Memory 创建或实际字段变化时追加事件；不追踪 Inference、Profile，也不进行语义维护。不复制整条 Memory 或旧值；新增仅 ID，更新仅变化字段及**新值**，包括可覆盖的 `status`。

```jsonl
{"id":"mem-001","change":"created"}
{"id":"mem-002","change":{"status":"deprecated"}}
{"id":"mem-003","change":{"context":{"valid_to":"2026-11"}}}
```

- `id` 是 Memory ID，`change` 是 `"created"` 或变更字段对象。任务消费游标、失败重试、读取进度属于后台运行状态，**不写进事件正文**；文件路径和持久化一致性留待实施验收。不要恢复 `inference-changes.jsonl`。

### Dream 与 Maintenance 的边界

- Dream **只生成**完整 `vault/profile.md` 和**新的** Inference；不修改旧 Inference，不负责既有知识质量维护。生成新 Inference 不写 Change Tracking。发布前核验生成所依据的当前证据版本；Profile 可原子重建并覆盖人工编辑。
- Maintenance 以后定期启动，**依据追踪到的 Memory 变化**查询关联和语义相关的旧 Memory/Inference，检查内在矛盾、失效和重复，复用已有业务工具修复；对新增 Memory，不能只依靠旧推断 `source_ids`，还需做相关知识检索。必要时可提出 Dream 重建 Profile 的需求，但不自行生成。
- **当前阶段不实现 Maintenance 语义自动维护**；Maintenance 尚未上线时，来源变化**不自动弃用、暂停或排除 confirmed Inference**。
- **保留后续方案参考（不删除）**：未来可由 Memory 变更触发/批量调度 Inference 增量复核；在维护任务具备可靠审核、恢复与重试能力后，可以考虑**复核期间暂时排除受影响的 confirmed Inference**（不改原状态、不自动弃用），复核后再恢复、弃用或以新 Inference 替换。是否启用保护策略留到 Maintenance 设计时确认；不得当作当前需求提前落地。
- 三项记忆子任务可按需单独运行，也可未来组合成“补齐 Session → 检查变化 → 维护知识 → 必要时 Dream”流程；不意味着每次运行必须全部调用 LLM。

## 六、实施约束与需用户确认的决策

**执行 Agent：GPT 6.2 Astra。**

- **已确定的字段/语义/模块边界必须遵守**；代码组织、接口传输、系统服务部署方式等实施细节可以在审查现状与测试结果后合理优化，但不要制造大型框架或无意义兼容层。
- **允许、并要求遇到实质性事实不确定或架构冲突时暂停实施，向用户准确说明发现的源码事实、影响和可行方案，得到确认后再继续**。尤其是需要改变已定字段、完整前后端分离目标、Vault 写入/并发语义、历史数据迁移规则、已有 TUI/Web 用户体验，或存在真实数据损坏风险时。普通技术细节与确定性修复可自行处理，无需频繁打断。
- 保留已有 TUI/Web 的交互能力、快捷键和流式体验；新前端业务请求应通过后端执行。不将本次重构当作 UI 重设计；前端技术选型无需用户掌握，实现 Agent 对技术负责。
- 涉及存储格式、Session/Inference/Pending 迁移时，先备份，使用**显式迁移脚本及合成 Vault 验证**，核对原文、状态、引用与索引；不使用、提交或泄露真实私人 Vault。测试不得读取真实 API Key 或本地敏感数据。
- 测试核心业务/跨进程任务、恢复与取消、并发 Session/Undo、TUI/Web/CLI/MCP 兼容；必要时增量验证后全量测试。实现过程中同步维护 AGENTS、HANDOFF、README 等与实际代码有关的文档；设计文档不应虚称已完成。
- **本文仅为设计文档更新**：不自动开展重构、推送实现、删除分支、运行真实 Vault 迁移或发布 PyPI。后续实施、合并、部署和版本发布须遵从用户另行授权。
