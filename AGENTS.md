# AGENTS.md

本文件面向 AI Agent 和代码贡献者，记录开发约定。
面向用户的安装、命令和功能说明在 [README.md](README.md)，两者不要混写。
README 必须与当前代码架构保持一致；目录结构、依赖方向、CLI/TUI 命令、MCP 工具、配置项或存储格式变化时，必须在同一次改动中同步更新 README。

接续开发先读 [docs/HANDOFF.md](docs/HANDOFF.md)，再核对源码、测试与 Issues；重要实现或规划变化时同次更新。交接文档只保存工程上下文，不替代当前授权，严禁记录敏感信息、个人数据、本机路径或个人配置。

## 命名规范

正式品牌、Python 包、CLI、MCP Server、Keyring 服务名与缓存/日志前缀统一为 `syvrio`；不保留旧品牌别名或兼容入口。默认配置文件为 `~/.syvrio/config.toml`，保留既有平台路径读取兼容；Vault 与 Runtime 默认分别位于 `~/.syvrio/vault` 与 `~/.syvrio/runtime`。环境变量使用 `SYVRIO_` 前缀。PyPI 发布必须由用户明确操作。

## 快速命令

```bash
uv sync
uv run pytest
uv run syvrio            # 直接进入对话
uv run syvrio setup      # 配置模型与 API Key
```

Python 依赖用 `uv` 管理，新增依赖写进 `pyproject.toml`。Web 依赖在 `web/` 使用 Corepack 与锁定的 pnpm；仓库不提交前端构建产物。
全局 `syvrio` 是 editable 安装：只同步代码、**不同步依赖**。改动依赖后必须重跑
`uv tool install --force -e '.[local-embed]'` 重建工具环境，否则全局命令启动即崩（ModuleNotFoundError）。

## 技术栈

Python 3.12+、uv、hatchling、Typer、FastMCP、LiteLLM、Pydantic v2、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、jieba + rank-bm25 + numpy（BM25 与向量余弦、RRF 混合检索）、fastembed（本地 embedding，可选 extra `local-embed`）、Textual（全屏 TUI）、rich（CLI 输出）、questionary（CLI 配置向导）、keyring、tiktoken、httpx（联网搜索）。

非核心能力优先复用现成库（分词、检索、CLI、密钥、模型适配）；自研只保留核心差异：记忆裁决、上下文压缩、来源链。

多格式文件解析复用 MarkItDown（可选 extra `documents`）；解析插件与云端转换不启用。文件工具只对当前用户消息明确提供的文件开放，文件内容不构成新的访问授权。

Web 和 TUI 的聊天上传原件保存在 `Vault/attachments/`。该目录由 Vault 根目录的 `.gitignore` 排除，迁移和备份时应按文件复制；会话中的附件清单使用稳定 ID 与校验值。

## 分层

- `app/` 与仓库根的 `web/` 提供 CLI、TUI、MCP、Web 展示与输入适配。客户端业务操作通过统一后端接口调用。
- `backend/` 管理常驻进程、唯一实例、本地访问凭据、客户端发现和请求路由。`application/` 编排会话 FIFO、安全插入、撤回、审核和设置等用例。
- `agent/` 管理模型循环、上下文与工具调用。Agent 发起正式记忆变更或创建候选前通过 Recall 核验当前证据，将结果提供给模型判断。
- `session/` 提供会话事件投影、可靠写入、会话列表和原始消息查询。可信来源由调用方关联到具体用户消息。
- `background_memory/` 负责会话沉淀、改写后补 Context 和 Dream。后台作业保存检查点，通过 MemoryService 的正式入口更新知识。Profile 先整体发布，随后创建新的待审推断。
- `memory/` 拥有领域模型、MemoryService、Markdown 解析和存储、候选、推断、审计及变化追踪。`retrieval/` 使用事实的只读数据执行关键词或向量检索。
- `llm/` 适配模型服务。`infra/` 提供配置、文件、持久事务、运行锁、Git、任务状态、上传原件和搜索等共享能力。搜索供应商各自负责自己的字段映射。

跨模块使用相对导入。事务、锁和文件层只处理持久化机制。记忆裁决与来源核验放在业务服务或 Agent。

## 代码风格

- 实现模块开头使用 `from __future__ import annotations`；公共函数和方法写类型标注。
- 代码标识符用英文；TUI 固定界面文案、状态、菜单与工具进度统一使用英文；用户消息、模型回答与引用内容保留原语言。CLI 帮助可使用中文。
- 领域模型用 Pydantic，集中在 `memory/models.py`；轻量内部结果用 dataclass（如 `ToolOutcome`、`ToolSpec`）。
- 注释只解释非显而易见的约束和原因；TUI 布局、键位、写入裁决等重点位置保留简短注释。
- 单个源码文件控制在 800 行以内；能放进现有模块就不新建文件，不提前抽象。
- 只做任务要求的改动；不顺手重构，不为假想场景添加防御代码。

## 记忆与存储不变量

- 正式记忆按逻辑主题保存在 `memories/<category>/<topic>.md`，ID 跨主题唯一。顶层列表以 `**Core**` 开头，Core 只有 `id/status/text`。`recorded_at`、会话来源对和可选替代关系保存在 Extensions。更新保留未知扩展、原文换行与未修改记录的字节。详见 [Vault 格式](docs/vault-v2.md)。
- `active/deprecated` 是正式记忆的两种状态。更正以新 ID 写入并弃用旧事实。状态演化可以保存新旧有效期，保持真实历史。时间精度按已有证据保留。补充 Context 只填空字段，`emotion.event/recollection/unspecified` 分别保存相应时点的用户原话。
- `memories/pending.md` 是唯一活动候选文件。确认只写入最终事实并移除候选。改写后确认同时登记异步 Context 补充任务。用户拒绝的原文进入 `archived/pending/rejected.md`。`related_ids` 为审核提供背景，已有事实的状态需独立操作。
- `inferences/<kind>/<id>.md` 保存独立推断，状态为 `hypothesis/confirmed/deprecated`。来源必须是真实记忆 ID。用户改写时在同一事务弃用旧文并以新 ID 保存确认后的推断。来源变化保留推断现状。`profile.md` 由 Dream 从当前事实和已确认推断生成。
- `records/memory-changes.jsonl` 针对正式记忆创建和实际字段变化各写一行。变化事件只含记忆 ID 和创建标志或新值。`records/memory/audit.jsonl` 是独立的操作审计。通过 `vault_transaction(paths)` 一并写入事实、日志及相关任务。嵌套操作复用外层事务，成功提交或失败时整体恢复。
- 常驻后端按物理 Vault 独占运行。Vault 写入和 Git 快照使用 Runtime 的 `vault.lock`。Git 操作按 `Git → Vault` 顺序取锁，网络推送等待只持有 Git 锁。使用同一套已解析的 `SyvrioPaths` 构造领域存储与后台作业。
- `sessions/YYYY/MM/<id>.jsonl` 追加消息、摘要、撤回和沉淀事件。稳定消息 ID 定位原话。保存绝对 UTC 时刻与已知的来源本地日期。尾行恢复以物理行号追加 `recovery` 标记及诊断。会话写入前屏蔽已识别的凭据。
- 最新回合 `/undo` 为正式记忆可恢复原文件的例外。先核对所有受影响文件和存续引用，再在一个事务内恢复文件并追加撤回事件。其他会话写过同一文件或新增引用时拒绝整次撤回。Runtime 持久化撤回凭据、推理附件与后台进度，应与需要恢复的 Vault 配对备份。
- Vault 归档按内容类型分子目录。会话默认归档，原文移入 `Vault/archived/sessions/YYYY/MM/<id>.jsonl`。整个 `Vault/archived/` 排除在检索、后台任务和自动 Git 处理之外。确认后可释放只读视图并归档，目标会话有写入、排队消息或后台任务时拦截。永久删除已归档会话需手动确认，有正式记忆来源引用就拒绝。相关活动候选须在确认界面列出并随会话移除。消息的上传副本随会话删除。`Vault/attachments/` 由 Vault Git 忽略，迁移时另行复制并核对校验值。
- `SOUL.md` 是可选人设。敏感凭据、真实配置与用户数据只能留在本地。历史离线工具处理各自的旧格式。生产 Vault 升级到当前格式需专门设计和验证迁移流程。

## 测试

- 新行为必须带测试，放在 `tests/` 下对应的领域文件中。
- 测试文件用 `pytestmark` 分为 `ui`、`agent`、`memory`、`retrieval`、`cli`、`config`、`mcp`、`search`；局部改动按影响范围运行相关用例或分类；跨模块核心改动按实际风险确定是否需要全量。
- 测试禁止读取本机 keyring、真实 API Key 或调用付费模型；使用 `tests/conftest.py` 的 fixture、`StaticExtractor` 和假模型。
- **测试环境隔离是夹具级红线**：conftest autouse 已把 `SYVRIO_CONFIG`/`SYVRIO_VAULT` 钉在每个测试的临时目录，任何测试和代码路径都不得绕过它直连本机配置/记忆库。把源码回退到旧版本再跑新测试是危险组合（旧路径可能绕过 mock 写真实文件），必须先确认隔离护栏在位。
- 未接真实 API Key 验证时，不声称“已完成线上验证”；真实调用只做任务需要的冒烟、诊断与复测，解耦层的 mock 单测覆盖不了 service 接线的真实形状，关键链路补真实冒烟。
- “真实测试”指通过实际 TUI 调用真实搜索 API、LLM 等服务。真实 LLM 测试、诊断与追加调用不再逐轮申请确认或请求次数上限；开始调用或追加使用时，在对话中简要说明用途即可。真实测试与用户本地测试环境共用会话库，使用可识别的测试会话并保留记录，确保可在 TUI `/sessions` 中查看；不要自动删除测试会话。
- 按实际影响范围运行必要测试，相关测试通过后即可提交；不因提交或分支同步默认运行全量 `uv run pytest`。只有用户明确要求，或跨模块核心改动确有验证需要时才运行全量，避免对局部界面、样式或文档改动反复全量测试。


## 可移植性

CI 的测试、构建、CLI 检查与发布作业统一在 Debian 13 容器中运行（GitHub 托管 Ubuntu 仅作为容器宿主），**本地全绿不等于 CI 绿**：

- 禁用平台专属编码与 API（如 `mbcs`）——ubuntu 上直接 LookupError，英文 Windows 上行为不同；用全平台内置的替代（如 `gbk`），确需平台分支时以 `sys.platform` 显式守卫。
- 文件路径、换行、终端转义同理：优先标准库可移植写法。

## 界面不变量

- 敏感操作一律需要二次确认。首次触发展示操作目标和影响，用户明确确认后才执行。会话归档和永久删除分别确认，默认焦点放在取消按钮。
- 临时确认、选择与展示弹窗保持适中尺寸，优先随内容自适应并限制最大宽高；短内容不预留大面积空白，长内容在限定区域内滚动，操作按钮保持可见，避免默认接近全屏。
- 忙碌态（补沉淀/切换/请求中）收到的用户意图**必须排队接续，不得静默丢弃**；异常必须释放 busy 并把错误显示出来，不能让输入框永久禁用。
- 模型、思考档位和上下文预算等按回合使用的设置，在生成中也可保存。回合开始时固定设置，工具调用后继续回答时沿用同一份。排队消息与其他会话均在新回合实际开始时读取最新设置。
- 流式输出：循环级不做重试（重试会重复 delta）；完成时就地定稿，不重复挂载；中途断流保留半截并显式标注。
- git、LLM 等慢操作在 worker 线程执行，UI 线程只经 `call_from_thread` 接收回调。

## 异常处理

- 用户可达路径上的**故意失败**一律抛继承 `syvrio.errors.SyvrioError` 的异常（保留原基类多重继承，旧的 `except ValueError` 等不受影响）；边界层先 `except SyvrioError` 翻译成用户可读文案，再用裸 `except Exception` 兜底并记录事件，**不得手写枚举异常类型元组**（漏一个类型就冒裸 traceback）。
- 异常消息按"阶段 + 可操作的解法"写，不按 errno 裸文写（参考 `UpdateError(stage, detail)` 与 `infra/update.py::_uv`）。
- 用 `typer.Exit`/`click.Abort` 表示流程退出，不表示失败：`main()` 对它们直接放行不记录；`ui.fail(message, exc=exc)` 会在抛出退出码**之前**把原始异常记成 `cli.failed`（带 `command` 字段），调用侧拿到原始异常时必须传 `exc=`，否则诊断日志只剩退出码。
- 降级路径（keyring 不可用、tiktoken 编码表下载失败等）静默退回默认行为时必须 `diagnostics.record_once` 记录一次，并写注释说明降级语义；`record_once` 的事件名必须加入 `_EVENTS` 白名单（`tests/test_diagnostics.py` 的静态对齐测试会拦截脱钩）。
- `except BaseException` 只许用于"释放资源后重抛"的模式；裸 `except: pass` 只允许在探针/TOCTOU/尽力清理场景，且必须有注释。
- 共享 HTTP 边界使用 `APIError(code, message, status)`，由 `backend/api/routes.py` 的处理器返回 `{"error": {"code": ..., "message": ...}}`。Web 网关沿用相同错误格式。不得把 traceback 或记忆内容带进 HTTP 响应。
- 诊断事件名采用 `域.动作` 形式，失败类以 `.failed`/`.unreadable` 等后缀结尾。

## 文档同步

README 面向用户，必须始终与 `src/syvrio` 的当前架构一致。以下变化必须在同一次改动内更新 README，不能留到后续：

- README 有中英文两版：`README.md` 与 `README.en.md`；任何 README 改动都必须同步两版，并保持顶部语言切换链接。
- 模块、目录或依赖方向变化：更新“结构”树与依赖说明。
- 新增或删除 CLI 子命令：更新“命令行”示例。
- 新增或删除 TUI 斜杠命令：更新“会话内命令”表。
- 新增或删除 MCP 工具：同步 `app/server.py` 与 README 工具列表。
- 配置的默认值只在 `src/syvrio/defaults/` 的四份 TOML 维护，本地配置只保存覆盖。配置字段或默认值变化：同步 `config.example.toml`、README 配置示例与 `infra/config.py`。
- 存储格式或写入规则变化：更新 README 的“写入规则”和“结构”章节。
- README 不写会快速过期的固定数字（如测试数量），只写稳定的覆盖范围。
- 功能待办以 GitHub Issues 为准（创建时写清现状/方案/验收标准，完成时带验收记录关闭）；README Roadmap 只留条目级摘要并指向 Issues。
- README 呈现约束：不加装饰性 emoji；代码块里**不做依赖跨行字符对齐的 ASCII 排版**（GitHub 等宽字体无 CJK 字形，中文回退渲染必然错位），只保留框内自洽的元素。
- 约定本身变化时更新本文件。

## 脱敏

- 仓库内容（代码、测试、文档、评测、提交信息）不得出现任何真实个人信息：姓名、住址与地名、学校、雇主、联系人、私人邮箱、真实 API Key；类别以提到的为准，不限于清单。
- 评测与示例一律用合成数据（参考 `evals/retrieval_eval.py`：虚构条目 + 占位人物）；真实个人库只允许以汇总指标形式出现（Recall/MRR 等），不得引用条目原文或可定位到个人的用例。
- 在真实用户数据上调试或评测后，提交前先扫描再入库：`git grep` 工作区、`git log -S` 历史，确认敏感标识零残留。
- 本地 vault、会话文件、密钥永不入库（见"记忆与存储不变量"与 `.gitignore`）。

## 时间

- 持久化绝对时间用 `syvrio.time` 生成、解析和序列化为 UTC ISO 8601（`+00:00`）。运行时拒绝无时区值，禁止直接调用 `datetime.now()`、`datetime.utcnow()` 或在业务模块自行解析时间戳。纯日期、本地计划和持续时间保持原有语义。
- 展示层使用时间工具转换为本地 24 小时制。历史时间只由一次性迁移脚本处理。新增时间字段同步更新 `docs/vault-v2.md` 中的时间字段清单。

## 提交

- 提交信息一律使用英文，沿用现有提交风格：`feat(scope): ...`、`fix(scope): ...`、`refactor(scope): ...`、`style(scope): ...`，主题行简短。
- 不提交生成物、本地配置或用户数据。
- **`main` 是唯一长期开发分支。** 功能、修复和文档更新从 `main` 或其短期任务分支开展。执行前确认当前分支，合入 `main` 遵循分支保护与 PR 审核要求。
- 迁移前的稳定版源码保存在 `archive/stable-main-8656f6e` 标签。其他工作区存在未提交改动时不得覆盖、清理或捆绑提交。
- 只有明确要求时才执行 `git push`。

## 边界

- 项目是本地优先：不引入遥测、云同步或额外外部服务，除非明确要求。
- 不通过修改用户 vault 里的历史内容来“修数据”；需要迁移时写一次性脚本并先说明影响。
- 不删除或覆盖用户未提交的改动。
