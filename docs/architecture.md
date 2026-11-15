# Syvrio Evo 架构设计

- 适用分支：`feature-memory-evolution`
- 文档状态：现行技术基准
- 更新日期：2026-11-15

## 分层与运行入口

`app → application → agent / memory → retrieval / llm / infra`。`memory` 装配检索器和模型抽取器。检索层只读记忆条目和存储接口，用于索引构建及命中证据。Web 以 HTTP/SSE 和浏览器状态为边界，TUI 通过 Textual 交互，CLI 与 FastMCP 按各自入口直接复用适用服务。

| 位置 | 职责 |
| --- | --- |
| `web/`、`app/web/` | React 页面、静态资源、FastAPI 路由、SSE、Host/Origin 检查、Web 会话生命周期 |
| `app/cli.py`、`app/tui.py`、`app/server.py` | Typer 命令、TUI 与 Vault 互斥入口、FastMCP 工具注册 |
| `application/` | `ApplicationService` 装配会话、记忆、设置、反思和 Git 用例；命令目录描述入口能力和确认要求 |
| `agent/` | `AgentRuntime` 编排召回、上下文、流式工具调用、会话记录、Undo 和补沉淀 |
| `memory/` | Schema V2、存储、抽取与裁决、审核和审计；时间视图、推断事件、反思及画像 |
| `retrieval/`、`llm/` | BM25、向量与 RRF；LiteLLM 协议、模型发现、流式聚合和摘要 |
| `infra/` | 路径、配置、锁、Git、诊断、搜索抓取、PyPI 更新及系统服务切换 |

## 关键数据流

1. **对话**：入口校验会话后绑定活动轮次。Runtime 写入用户记录，基于问题选择自动召回和上下文策略，再调用当前模型。选定的只读搜索与抓取工具可以并行；写入工具保持有序。正文、思考、工具和终止状态分别进入事件流。
2. **会话**：Web 连接关闭时释放订阅，后台继续完成该轮并持久化。重进页面从历史恢复。会话切换将沉淀交给后台；在模型抽取后、写入前检查会话是否已打开或改动。成功后推进游标，失效或失败保留待处理消息。TUI 与 Web 共用会话用例，界面分别管理输入队列和展示。
3. **记忆**：候选经过敏感检测、去重、冲突与置信度裁决。可采信事实写 Markdown；待确认进入 JSONL 队列。修订、状态演化、删除和审计保持来源链。推断保存在独立事件流，依有效来源进入审查和画像。
4. **召回**：BM25 和可选语义向量经 RRF 合并。关键词侧提供短字段的字面证据；时间、地点和情绪等约束进一步筛选。向量失败时使用可用的关键词结果。网页搜索和抓取在独立路由内处理预算、缓存和供应商退路。
5. **反思**：`ReflectionEngine` 对已沉淀证据执行有预算的 `profile` 或 `dream` 批次，产生待审查推断和衍生摘要。TUI 负责空闲调度；CLI 和 Web 命令可显式触发。来源变化时，摘要按指纹标记失效并刷新。

## 存储和时间

| 位置 | 内容 |
| --- | --- |
| `Vault/memories/<category>/<topic>.md` | 正文、Core、Context、Extensions；事实 ID 稳定 |
| `Vault/sessions/YYYY/MM/<id>.jsonl` | 追加式会话、工具结果、摘要、撤销与沉淀游标 |
| `Vault/records/memory/` | 待确认、处理结果、审计和推断记录 |
| `Runtime/session-reasoning/` | 本机推理附件；会话只持有引用标记 |
| `Runtime/indexes/`、`cache/` | 向量索引、搜索缓存、画像摘要 |
| `Runtime/state/`、`jobs/`、`logs/` | Vault 绑定、配额、反思状态、后台任务和本地诊断 |

Vault 与 Runtime 相互独立，Runtime 在 Git 工作树之外。绝对时间持久化为 UTC ISO 8601，界面按本地 24 小时制显示。事件日期、有效期的日历精度独立于写入时间。见 [Vault V2](vault-v2.md) 和 [时间规范](time.md)。

## 锁与资源生命周期

- `client.lock` 管理 Web/TUI 的整实例互斥。退出时先协调活动轮、后台补沉淀和正在执行的命令，再释放客户端锁。同步模型调用的等待由供应商超时和重试上限约束。
- `vault.lock` 保护会话与记忆持久化，`git.lock` 串行 Git 操作。模型抽取在写锁外进行，提交前在锁内复核状态。单文件写入可原子替换，多文件操作依赖现有异常恢复和备份。
- LiteLLM 请求使用当前显式配置的模型；旧的自动候补路径已移除。流式完成、取消和错误时释放本次请求拥有的连接。外部共享客户端由其所有者管理。
- API Key 由环境变量或系统凭据提供，模型发现缓存按供应商、端点和凭据隔离。思考回传仅在同一受支持模型路由与协议范围内发生，Runtime 推理附件不进入 Vault Git 或沉淀输入。
- Web 默认为 loopback。Host/Origin 检查阻止跨站请求，远程访问的认证与网络边界见 [Web 指南](web.md)。

## 构建、更新与分支同步

- Debian 13 执行 Ruff、Python 测试、前端构建和测试、分发包内容检查。Vite 资源须注入 sdist 和 wheel。Evo 的 PyPI 版本使用 `rcN` 预发布。
- `syvrio update` 从 PyPI 按频道筛选 wheel，验证摘要，准备版本目录并请求用户 systemd 切换。健康检查失败走现有恢复路径。发布和部署各自验收。
- Foundation 与 Evo 共享会话传输、锁、时间、模型适配和构建契约。稳定线保留独立记忆实现；Evo 的时间演化、推断审查和反思按明确边界晋级。合并前验证测试、存储兼容性、隐私和回退路径。

[索引](README.md) · [产品需求](PRD.md) · [路线图](ROADMAP.md)
