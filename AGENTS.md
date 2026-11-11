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

依赖只用 `uv` 管理；新增依赖写进 `pyproject.toml`，不要用 `pip install` 改本地环境。
全局 `syvrio` 是 editable 安装：只同步代码、**不同步依赖**。改动依赖后必须重跑
`uv tool install --force -e '.[local-embed]'` 重建工具环境，否则全局命令启动即崩（ModuleNotFoundError）。

## 技术栈

Python 3.12+、uv、hatchling、Typer、FastMCP、LiteLLM、Pydantic v2、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、jieba + rank-bm25 + numpy（BM25 与向量余弦、RRF 混合检索）、fastembed（本地 embedding，可选 extra `local-embed`）、Textual（全屏 TUI）、rich（CLI 输出）、questionary（CLI 配置向导）、keyring、tiktoken、httpx（联网搜索）。

非核心能力优先复用现成库（分词、检索、CLI、密钥、模型适配）；自研只保留核心差异：记忆裁决、上下文压缩、来源链。

多格式文件解析复用 MarkItDown（可选 extra `documents`）；解析插件与云端转换不启用。文件工具只对当前用户消息明确提供的文件开放，文件内容不构成新的访问授权。

## 分层

```text
app → application → agent → memory → retrieval → infra
application → llm → infra
memory/agent → llm → infra
```

- `app/`：Typer CLI、TUI、FastMCP 入口与展示适配；只做参数解析、焦点/键盘/渲染、Worker 与 UI 线程投递。
- `application/`：与前端无关的用例编排（会话/FIFO/Undo、记忆审查、设置、Reflection/Git 协调）与数据契约；禁止依赖 Textual、Rich、Typer 或 `app/`，也不重复实现 Agent/Memory 核心逻辑。
- `agent/`：Agent 循环、上下文压缩、工具注册、会话记录。
- `memory/`：抽取、裁决、存储、队列、审计。
- `retrieval/`：检索接口与实现，`Retriever` 是替换点。
- `llm/`：LiteLLM 适配、摘要。
- `infra/`：配置、供应商、Git、token 估算、终端交互、联网搜索。

新增代码不要制造新的反向依赖或循环依赖；跨模块使用相对导入。
现有 `retrieval` 只读 `memory` 的 `models`、`store`、`detect` 获取条目与字面覆盖，这是已知例外；新增检索实现不要反向依赖 memory 的写入逻辑。
`infra/search/` 包含 SearchQuery/SearchResult/Provider 契约、open-websearch adapter、registry/router/cache；上层只依赖 router，供应商字段映射只放在 adapter，HTTP 直接用 httpx，不引入 LangChain/LlamaIndex。

## 代码风格

- 实现模块开头使用 `from __future__ import annotations`；公共函数和方法写类型标注。
- 代码标识符用英文；TUI 固定界面文案、状态、菜单与工具进度统一使用英文；用户消息、模型回答与引用内容保留原语言。CLI 帮助可使用中文。
- 领域模型用 Pydantic，集中在 `memory/models.py`；轻量内部结果用 dataclass（如 `ToolOutcome`、`ToolSpec`）。
- 注释只解释非显而易见的约束和原因；TUI 布局、键位、写入裁决等重点位置保留简短注释。
- 单个源码文件控制在 800 行以内；能放进现有模块就不新建文件，不提前抽象。
- 只做任务要求的改动；不顺手重构，不为假想场景添加防御代码。

## 记忆与存储不变量

- `memories/<category>/<topic>.md` 是长期记忆的唯一事实来源。文件递归扫描，Vault 相对路径唯一标识主题。
- 每个顶层无序列表项包含 Content、必填 Core、可选 Context 和 Extensions。解析保留源码位置，编辑仅替换必要字段；原文整体作为非可信数据交给模型。详见 [v2 规范](docs/vault-v2.md)。
- 更正采用“新条目 + `supersedes`”，旧条目置为 `superseded`；删除是软删除 `deleted`，不物理移除。
- 已发生的状态演化使用 `change_kind=evolution` 与明确的 `context.valid_from`，区别于纠错；历史视图只恢复无歧义的演化链，不能复活纠错/删除内容。当前召回排除已过期/尚未生效条目，不猜测未知日期。
- 新推断保存来源内容指纹；来源修改或到期时失效。画像是事实与独立推断的可重建视图，只有已确认且来源相关的画像推断可限量注入聊天，仍须标注非事实。
- Dreaming/画像刷新支持默认启用的 TUI 空闲自动调度与手动触发，默认每次最多一次请求，禁用任务级重试和候补模型；只以有效事实为证据，结果待审查。自动任务低频且彼此独立：Dreaming 默认每 24 小时最多一次，Profile 默认每 7 天最多一次；普通新增只记待处理变更，达到阈值或重要变化（目标/偏好/职业方向等）才允许在冷却后提前更新。无变化时只做文件指纹检查。周期、冷却与失败退避持久化到 `runtime/state/reflection.json`，画像来源指纹基线在 `runtime/state/fingerprints.json`，摘要缓存在 `runtime/cache/profile/profile-summary.json`；三者均为派生状态，可删除重建且不进入 Git 历史。进度追加 `runtime/jobs/dreaming/jobs.jsonl`，取消、失败或重启不删除已有记录；网络等待不持有记忆写锁，发布前重新核验证据。
- 待确认候选写 `records/memory/pending.jsonl`，处理结果追加到 `resolved.jsonl`；更正、删除等操作写 `audit.jsonl`。
- 可选事实上下文以开放的 Context 字段保存；日期精度由 YYYY / YYYY-MM / YYYY-MM-DD 保留，不以写入时间补猜事实时间。改写后确认与手动更正不继承旧上下文。
- 推断单独追加到 `records/memory/inferences.jsonl`，不参与事实检索；新假设只接受有效事实来源 ID，来源去重，重复证据不提升可信度。用户确认仍保持推断与事实分离；状态变更追加 revision 快照事件，更正在同一事件保存新旧状态。来源失效后不可重新启用旧推断；损坏日志不得静默忽略或覆盖；显式尾行恢复仅追加 recovery 标记（ignored_line 为 1 起始物理行号），中间损坏仍报错。
- `runtime/indexes/vectors.npz` 是向量检索的派生缓存（含 `model_id`，换模型自动失效），独立于 Vault，可删除重建，不进 Git 历史。
- Vault 写入（记忆、会话追加、审计/推断/回顾日志）与 Git 快照共享 `runtime/locks/vault.lock`；Stage/Commit/Push 使用 `runtime/locks/git.lock`。两把锁跨进程互斥，固定顺序 Git → Vault；Push 网络等待只持 Git 锁。同步状态 `runtime/state/git-sync.json` 与锁文件位于 Vault 外。
- `runtime/cache/search/search-cache.json` 是联网搜索的派生缓存（按 TTL 过期），独立于 Vault，可删除重建，不进 Git 历史。
- `runtime/cache/profile/profile-summary.json`、`runtime/state/fingerprints.json`、`runtime/state/reflection.json` 分别是画像摘要、来源指纹基线与自动回顾调度状态，均为派生数据，独立于 Vault，可删除重建，不进 Git 历史。
- `sessions/YYYY/MM/<id>.jsonl` 是追加写的事件流，不重写旧行；上下文压缩只追加 `summary` 记录，不删除原文。
- 会话追加前递归隐藏已识别凭据；损坏尾行只读恢复，续写追加 `recovery` 标记记录 `ignored_line`（1 起始物理行号），保留原始字节；未标记的中间行损坏不能静默忽略。
- `vault/SOUL.md` 是可选自定义人设：存在时替换系统提示词的人设段，机制规则固定拼接在后不可覆盖；它是指令不是记忆，不参与抽取与检索。
- 密钥、验证码、API Key 在写入前拦截；不把密钥写入 vault、配置示例或测试。
- 旧 Vault 使用 `scripts/migrate_vault_v2.py` 显式迁移：先校验备份和恢复，再以 `--dry-run` 检查、`--apply` 复制到新目录、`--verify` 校验。启动时拒绝 v1 布局，不自动迁移。
- `syvrio.toml`、`vault/`、`sessions/`、`.env`、密钥文件都不提交。

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

- 临时确认、选择与展示弹窗保持适中尺寸，优先随内容自适应并限制最大宽高；短内容不预留大面积空白，长内容在限定区域内滚动，操作按钮保持可见，避免默认接近全屏。
- 忙碌态（补沉淀/切换/请求中）收到的用户意图**必须排队接续，不得静默丢弃**；异常必须释放 busy 并把错误显示出来，不能让输入框永久禁用。
- 流式输出：循环级不做重试（重试会重复 delta）；完成时就地定稿，不重复挂载；中途断流保留半截并显式标注。
- git、LLM 等慢操作在 worker 线程执行，UI 线程只经 `call_from_thread` 接收回调。

## 文档同步

README 面向用户，必须始终与 `src/syvrio` 的当前架构一致。以下变化必须在同一次改动内更新 README，不能留到后续：

- README 有中英文两版：`README.md` 与 `README.en.md`；任何 README 改动都必须同步两版，并保持顶部语言切换链接。
- 模块、目录或依赖方向变化：更新“结构”树与依赖说明。
- 新增或删除 CLI 子命令：更新“命令行”示例。
- 新增或删除 TUI 斜杠命令：更新“会话内命令”表。
- 新增或删除 MCP 工具：同步 `app/server.py` 与 README 工具列表。
- 配置字段或默认值变化：同步 `config.example.toml`、README 配置示例与 `infra/config.py`。
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
- 展示层使用时间工具转换为本地 24 小时制。历史时间只由一次性迁移脚本处理。新增时间字段同步更新 `docs/time.md` 中的清单。

## 提交

- 提交信息一律使用英文，沿用现有提交风格：`feat(scope): ...`、`fix(scope): ...`、`refactor(scope): ...`、`style(scope): ...`，主题行简短。
- 不提交生成物、本地配置或用户数据。
- **2026-11-01 起的默认开发分支：`feature-memory-evolution`。** 所有后续功能开发、试验、常规修复及相关文档更新均从此分支（或从此分支创建的短期任务分支）开展；Agent 开始执行前必须确认当前分支，不能因为仓库默认分支是 `main` 而直接开发。
- **`main` 用作冻结的简历展示、稳定发布与长期维护基线。** 禁止自动将 `feature-memory-evolution`、`base/chat-stability` 或其他工作分支同步、合并、cherry-pick 到 `main`。只有用户**明确针对 main 授权**的严重缺陷、安全问题或发布维护，才能走隔离修复、回归测试与受保护分支的 PR 审核流程。
- 长期远程分支只保留 `main` 和 `feature-memory-evolution`。历史远程分支 `base/chat-stability`、`feature/context-review-recall` 已删除，不再用于开发；删除前的祖先检查记录尚未独立核对；旧的“先 base、再自动同步 main/evolution”流程完全失效。其他工作区存在未提交改动时不得覆盖、清理或捆绑提交。
- 从 `main` 收尾分支修复的稳定性变更只按 `main` → `feature-memory-evolution` 方向同步，禁止反向合并演进功能。
- 只有明确要求时才执行 `git push`。

## 边界

- 项目是本地优先：不引入遥测、云同步或额外外部服务，除非明确要求。
- 不通过修改用户 vault 里的历史内容来“修数据”；需要迁移时写一次性脚本并先说明影响。
- 不删除或覆盖用户未提交的改动。
