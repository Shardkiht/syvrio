# 开发交接

## PyPI 发布频道

- PyPI 共用 `syvrio` 包名和 `workflow.yml` Trusted Publisher（环境 `pypi`）。稳定线从 main 发布正式版本；Evo 工作流只允许从 `feature-memory-evolution` 手动触发发布，且会拒绝正式版本号。Evo 发版时同步更新 `pyproject.toml`、`src/syvrio/__init__.py` 和 `uv.lock`，使用 PEP 440 预发布版本，例如 `0.5.2rc1`。
- GitHub Release 的预发布标记不影响 PyPI 的版本选择。普通用户按默认规则安装正式版，Evo 用户安装指定的预发布版本。

## Web 会话切换与延迟沉淀

- Web 新建、切换及 `/new` 共用 Application 的快速切换路径。单个后台 worker 合并重复调度，沿用 TUI 的三条消息阈值；未处理的会话由启动补记接手。
- Runtime 在模型调用前拍摄会话快照，写入前核对会话是否仍未打开、记录是否有变化。失败、停机或快照过期时保留待抽取消息；同一 Runtime 不并行执行两次补记。Web 停机等待写入者退出后才释放客户端锁。
- 浏览器串行提交导航，旧历史响应不会覆盖新选择。同步抽取发出后只能等待供应商返回或传输超时与重试结束，停机期间保持客户端锁。

## LiteLLM 流式传输资源

- 无候补的 OpenAI 兼容流复用 LiteLLM 的传输构造，并关闭本次调用拥有的传输。配置候补且启用 aiohttp 时，各次尝试借用同一请求拥有的会话；SDK 客户端、凭据与模型仍由 LiteLLM 选择。外部共享客户端沿用原所有权。
- 本地 HTTP 覆盖主模型成功、实际候补、重试、失败、超时和取消；真实 Spark 在隔离 Web/TUI 验证了主模型失败后的候补。其他供应商不保证使用同一传输。

- 自更新入口为 `syvrio update`。它按本地频道从 PyPI 选 wheel，在独立环境准备新版，然后通过用户 systemd 切换并检查健康状态。首次部署见 Web 文档。

## Evo 工程规范

- CI 使用锁定版本的 Ruff 检查 Python 源码、测试、脚本和评测。全库 formatter 差异单独检查，历史排版按模块渐进收敛。
- SessionStore 列表和补沉淀扫描复用已发现的会话路径。会话列表在截断前按最近有效消息排序，Web 和 TUI 共用此顺序。
- 供应商设置延迟导入终端向导依赖，应用层可独立导入。LiteLLM 传输、系统凭据和可选解析器继续按需加载。

## Reasoning 流式展示与协议

- LiteLLM 的 reasoning 文本与正文、工具参数分开聚合，结构化片段单独保留。思考开始和结束按单次模型请求计时。TUI 正文前显示最新三行，正文开始自动折叠。手动展开优先保留阅读状态，历史恢复默认折叠。
- 新会话记录通过关联标记读取 Runtime 本机思考附件，原文不会进入 Vault Git 历史或沉淀输入。中断段保留可见的部分文本，重放时跳过该段。压缩按用户轮次切断，思考仅计入可回传记录的预算。
- DeepSeek 官方端点保留带工具请求产生的思考历史。百炼 DeepSeek 1017 的回传范围限制为本轮，真实协议行为需单独验收。Spark、Qwen、硅基流动与未知端点的思考用于本机展示。OpenAI Responses 的 opaque items 属于另一套传输协议，当前 Chat Completions 入口按已返回字段处理。
- Spark MaaS 真实终端在隔离合成 Vault 中验证流式思考、正文折叠、历史展开及独立滚动、两次连续 recall、思考中取消与重启恢复，以及关闭思考后的普通回复。非流式真实请求同时验证了思考、正文和用量。DeepSeek 官方与百炼 1017 已核对 LiteLLM 实际请求转换，在线验收缺少可用凭据。

## 审查确认缺陷修复

- Vault 拒绝包含中文凭据赋值的输入；Git 暂存复检失败时恢复原索引，包括部分暂存的内容。配置保存保留全部反射调度字段，诊断日志兼容撤销路径传来的错误文本。
- 混合检索在缺少字面信号或向量无命中时保留关键词结果，只有明确的字段查询或有文本证据的限定词才享受字段优先级。相同模型 ID 下向量维度变化会尝试重建索引。
- 流式工具调用按索引或调用 ID 归并，非法参数终止该次模型请求。对话卡片和推断详情将动态文本按字面展示。
- 会话中断后，历史回放会在发给模型的上下文里为缺失的工具结果补上执行状态未知的回执，保留已有结果。原始会话记录保持追加式存储，恢复时不会重跑工具。
- Vault 记忆 Markdown 的 UTF-8 解码错误通过 SchemaError 报告相对路径和重新保存提示。迁移测试显式解码 UTF-8 的 JSON 与 Git 路径输出，并覆盖非 ASCII 内容和路径。

## 会话正文布局

- 宽终端的用户消息和助手回复共用左对齐阅读列，上限为 100 个终端字符格。窄窗口按可用宽度换行。助手角色行、正文和统计行留出分隔，实时回复与历史回放使用同一套样式。

## Pending 审查界面响应

- `/pending` 采用与 `/inferences` 一致的列表和详情布局，窄终端上下排列。正文可独立滚动，编辑在详情区用可换行的多行输入框完成，Ctrl+S 保存并确认，Esc 取消编辑。队列读写仍由独立 Worker 执行，成功后原位更新计数与列表，写入失败时保留草稿。
- Windows 终端使用独立合成 Vault 验收翻页、取消编辑、多行确认、拒绝和正常退出，核对 UTF-8 正文与审查记录。该链路为本地读写。
- 合成候选的 Textual 挂载实测（各三次中位数）：400 条约 876 ms → 95 ms，1,000 条约 2,463 ms → 102 ms。交互回归在延迟确认和拒绝期间执行 `/help`，释放写入后核对结果与下一条候选。

## 模型目录由供应商 API 发现

- `/model` 和 CLI 设置向供应商的 `/models` 发现模型 ID。目录只使用 API 返回值；首次打开可先显示加载状态，后续可复用按供应商、端点和凭据隔离的缓存。手动模型输入仍为独立入口。
- `/model` 中按 Ctrl+P 可配置各供应商的 Base URL，保存于 `[llm].provider_urls`。保存端点后可立即重新发现，选择模型时才切换当前模型及地址。
- `/model` 用 `[llm].recent_models` 保存最多五个供应商和模型 ID 对，目录中匹配的型号按最近切换时间置顶。完整模型 ID 搜索仍优先匹配精确结果。

## 单次搜索的上游并行

- 搜索路由给首选请求 250 毫秒领先窗口，仍在等待时并行派发候补，单次最多 4 个、进程最多 16 个。仍按质量门槛早停，compare 至少需要两家实际贡献。配额在派发前计数，已经发出的请求按自身超时收尾。
- 超过总等待预算时只返回已收集的候选，截断结果不进缓存。OpenWebSearch 和 SearXNG 的部分失败诊断按执行线程保存，避免重叠请求串报。

## 精确字段召回快路径

- 自动召回识别直接询问单个明确字段的问法。只有 BM25 字段索引的精确标签匹配、命中唯一且事实当前有效时，直接注入带来源的原文。多个字段、追问、改写同义词或存在冲突时继续使用原检索链路。

## 同轮记忆写入合批

- 单次模型响应内，相邻且时间来源兼容的 `memorize` 最多 8 条共享一次抽取和裁决。每条调用仍有对应的工具回执，第一条返回入库汇总，后续回执引用它。其他工具以及需要保留整条原文时间锚点的混合输入沿用原有顺序。

## 生成期间的命令与延迟日志

- 生成期间的回车与命令面板走同一分流。设置面板立即可用，进行中的聊天模型步骤使用本轮快照。导入使用独立 Worker；手动压缩等待当前回合完成，优先于排队消息。
- Runtime 的 `logs/latency.log` 使用本地轮转写入。按进程、轮次和模型步骤记录召回、上下文、流式首包/推理/正文、工具及内部同步请求，便于区分模型等待和工具等待。日志只写允许的事件与数值字段。

## Spark MaaS 思考模式

- Spark 的 `/think` 提供 `enabled`、`auto`、`disabled`，保存在 `[llm].spark_thinking`。旧配置未设置时沿用 Spark 默认开启。流式和非流式请求均使用 MaaS 的 `thinking.type`，备用模型请求不继承该字段。其他模型继续使用 `reasoning_effort`。

## 工具并行与召回改写时限

- 模型一次返回的连续 `search` / `fetch_web` 调用最多 8 个同时执行。其他工具保留顺序并隔开并行批次，工具结果仍按模型返回的调用顺序记录与回传。搜索缓存的写入在同一进程内串行合并。
- 自动召回改写最多等待 4 秒，使用独立的无重试请求。超时后使用原问题和有界上下文继续检索。主模型的流式超时、重试和后续依赖轮次沿用原配置。

## WSL 远程输入法光标

- PowerShell 经 Windows OpenSSH 启动 WSL 中的 TUI 时，xterm 终端可使用原生光标。输入框聚焦后先移动终端光标到实际插入点，再显示光标，供客户端输入法定位候选框。普通 Linux 和无相应能力的终端继续使用自绘光标。
- 长输入在粘贴、移动光标、调整窗口或发送清空时，先同步横向滚动，再发布插入点坐标。回归覆盖英文和中英混排，WSL 的 xterm 伪终端实测也核对了长文本粘贴、左移、发送清空和下一次输入的光标列。

## 短字段召回

- 自动召回保留原问句，独立搜索改写词或追问上下文后合并候选。原问句中明确命中的字段优先，主动 recall 和 CLI 继续共用检索服务。
- 关键词检索为独立短字段建立标签索引。标签须匹配完整词边界，重叠时优先匹配较长标签，互不重叠的标签可同时命中。候选截断前保留明确匹配的字段，同字段记录仍由上下文相关性排序。混合检索在精确字段组和普通组内分别按 RRF 排序，普通 BM25 候选仍受覆盖率过滤。
- 向量输入为短字段加入确定性语境和原有 Markdown 标题。关键词输入、引用 Markdown 和来源位置保持原样。向量缓存根据实际向量输入的哈希增量更新。
- `python -m evals.short_facts_eval` 使用合成数据复现原词、同义词扩展和同领域干扰。原数字类评测仍独立保留。

## AnySearch 与 Parallel MCP 网页抓取

- `[fetch].providers` 可独立加入 `anysearch` 和 `parallel_mcp`。两者无 Key 时可匿名使用，设置对应环境变量后发 Bearer；与 Search 共用原有额度池，默认每日 20 次。未启用的现有配置不受影响。
- AnySearch 使用 REST `/v1/extract`；Parallel 使用统一 MCP 2.0 `web_fetch`，原有独立 Search MCP 保留。两者沿用公网 URL 校验、按序回退和正文截断；上游未披露跳转地址时保留请求地址。
- 在隔离测试 Vault 中使用真实终端和 Spark `spark-x2.6` 分别验收：搜索返回公开页面，Fetch 请求到达对应新服务，抓取正文随工具结果回到模型，模型回答后可继续输入并正常退出。两家 Fetch 均完成带 Key 的真实冒烟。

## Memory Schema v2 / Vault Layout v2

- 当前存储、配置、迁移和回滚契约见 [v2 规范](vault-v2.md)。以下历史章节的 v1 路径只作为当时记录。
- CommonMark 块树负责识别记忆及附属段落；源位置用于局部修改。检索使用正文和扩展信息，模型收到带来源的完整原始片段。
- `SyvrioPaths` 解析独立的 Vault 和 Runtime，持久化记忆记录进入 `records/memory`，缓存、运行状态、进度与锁保留在 Runtime。
- 验收使用隔离的迁移副本核对记忆与会话 ID、原始 JSONL、Git 对象以及既有推断指纹。真实 TUI 用合成数据接通 Spark `spark-x2.6`，验证原文负载、手改后索引更新、会话重启恢复与 Runtime 隔离。
- Spark 画像刷新需要给推理和 JSON 正文同时保留输出额度；在当前窗口预算内为该供应商预留更多完成 tokens，保留单次请求约束。
- 回归：全量 pytest 1330 passed、6 skipped（原有条件）；工具描述复核 85 passed。真实 PTY 逐项通过 A–G，包含 Spark 画像缓存实际落盘。

## 搜索显式引擎列表配置化（2026-11-06）

- 移除代码内固定的 3 引擎上限；新增 `search.max_search_engines`，默认 `0` 表示不限制，写入 `config.example.toml`、TUI 生成的配置与中英文 README。
- Agent `search` 工具与 MCP `search` 共用该配置；只有值大于 0 时才限制，错误信息随配置值变化。自动回退的 `max_engines`（默认 3）语义不变，仍只控制首选引擎失败后的候补尝试数。
- 验证：`tests/test_setup.py`、`tests/test_web_search.py`、`tests/test_server.py` 164 passed；新增默认接受 4 个引擎、配置上限拒绝超限、配置往返保留字段的回归。

## Application Layer 生命周期收口（2026-11-06）

- `ConversationUseCases.end_turn()` 现在必须传入有效 `turn_id`；`None`、过期或不匹配均返回 `False`，不修改任何请求状态。`run_turn()` 不再用 `active_turn_id or 0` 兜底，没有活动 Turn 时返回 `failed` 且不伪造 ID。
- 错误处理拆分为三类：`_finish_chat_error`（只收尾聊天 Turn）、`_finish_navigation_error`（只释放导航状态，必要时丢弃失去目标上下文的 pending command）、`_background_error`（模型/Context 等普通后台任务只展示错误并恢复自身 UI）。会话切换失败不再释放正在生成的请求。
- Turn 终态确认只剩 `_finish_on_ui()`：校验 Turn ID、渲染结果、异常时记录 `tui.finalize_failed`，确认成功后只触发一次 `generation.finished` 和一次 FIFO 接续。`_finish_reply` / `_finish_chat_error` / `_finish_cancelled` / `_apply_undo_outcome` 不再各自释放状态或调度队列。
- 新增确定性测试覆盖 `end_turn(None)`、无活动 Turn 的 `run_turn`、生成中会话切换失败、模型加载失败、Context/Compact/Import 失败隔离、单次终态确认与过期回调不消耗队列。真实 TUI/LLM 验收记录见交付报告。

## Application Layer 稳定性修复（2026-11-06）

- P1 会话加载异常：`run_turn()` 现在把目标会话加载纳入统一 try，加载失败返回 `failed` TurnResult（保留目标 session_id、无 user_message_id 时不执行 Undo），记录 `chat.failed`，UI 收尾后恢复输入与队列。
- P1 生命周期：请求状态由 Application 独占。`begin_turn()` 分配 turn_id，Worker 完成只返回结果，不释放状态；UI 完成渲染后调用 `end_turn(turn_id)` 确认终态，过期回调无法清理新请求。UI 收尾异常通过 `_finish_on_ui()` 的 finally 释放，避免永久 busy。Worker 完成到 UI 确认之间提交的新消息继续按 FIFO 排队。
- P2 队列所有权：UI 通过 `take_next()`、`queue_snapshot()`、`take_pending_command()`、`take_pending_navigation()`、`resume_after_undo()` 操作；不再直接 pop/append 内部队列。`ChatApp._STATE_ALIASES` 仅保留旧 UI 测试兼容访问，属于后续技术债。
- P2 架构测试：AST 依赖检查按文件包路径解析 `node.level`，可识别 `..app`、`...app` 与 `from ..app import widget`；新增临时合成模块验证绝对导入、相对导入、多级相对导入和合法导入。
- 新增 `tests/test_application_lifecycle.py` 覆盖 Worker 完成待确认、过期回调、会话加载失败、切换/取消/Undo 竞争、快速连续提交；UI 回归补充过期 ack 与收尾异常释放。真实 TUI/LLM 验收记录见本次交付报告。

## Application Layer 解耦（2026-11-06）

- 新增 `src/syvrio/application/`：`contracts.py`（DTO）、`conversation.py`（FIFO/请求绑定/Undo/会话生命周期）、`memory.py`（Pending/推断审查/导入/画像）、`settings.py`（模型/供应商/推理/上下文）、`reflection.py`、`git.py` 与 `service.py` 组合入口。依赖方向为 `app → application → agent/memory/llm/infra`；应用层不依赖 Textual、Rich、Typer 或 `app/`。
- `ChatApp`/Mixin 的业务调用已改为应用层用例；UI 仅保留 Widget、焦点、渲染、Worker 与 `call_from_thread`。为兼容既有 UI 测试，`ChatApp` 保留 `runtime/service/sessions` 只读属性与队列状态别名，但不再直接存储或调用这些核心对象。
- `app/actions.py` 改为 `application/settings.py` 的旧签名适配器，不保留重复实现。Conversation 队列、请求中会话/用户消息绑定、Undo 目标与撤销后暂停队列均由应用层维护；确认/拒绝/编辑、来源摘要、导入批次与 Profile 读取也改由应用层组合。
- 新增不启动 Textual 的 `test_application_conversation.py`、`test_application_memory.py`、`test_application_settings.py` 与 AST 依赖边界 `test_application_architecture.py`。真实 TUI/真实 LLM 验收结果见本次交付报告。
- 残余耦合：TUI 仍以 `ChatApp.runtime/service/sessions` 只读属性供旧测试读取；会话列表展示仍使用 `session_ui.visible_sessions` 进行界面分组；Reflection/Git 的定时器仍由 TUI 生命周期触发（业务判定与运行已走应用层）。

## TUI 焦点与性能专项回归（2026-11-06）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），基线 `ea163a3`；本轮改动仅在工作区，未 commit、未 push。
- `/model` 首次打开此前在 Worker 里先算完 LiteLLM 能力标签才挂屏（本机冷启动 3.7–9.2s）；改为先用基础标签开屏、后台刷新补全能力标签并批量挂载。搜索框内按 ↑↓ 只移动列表高亮，不再把焦点移到列表，输入过滤可连续进行；后台刷新按值保留搜索过滤和高亮项，不再把键盘选择重置到第 1 项。启动时后台预热当前模型元数据，预热完成后 `/model` 约 0.2–0.8s、`/context` 0.01s 开屏。
- `/context` 此前在 UI 线程同步读取模型元数据（冷启动实测阻塞 3.4–6.7s）；改为 Worker 构建选项，加载期间事件循环保持响应（最大间隔 <0.1s），重复触发只保留一个等待；失败显示错误并解除加载锁。
- `/inferences` 此前每次确认/拒绝/编辑都清空并逐条重建 ListView；改为文本型 OptionList + 按 id 增量同步（只删/改变化项），保留选中与详情。400 条整表重建 2.56s→0.07–0.12s，1000 条 11.7s→0.14–0.25s；单次确认 UI 同步 2–8ms，后端读取/失效校验仍为 O(n)（400 条约 20–40ms），日志解析不是主要瓶颈。
- 实测：400 条 Pending 时确认按键到反馈 4.95s→0.44s（测试器轮询口径；核心阶段约 50ms）；后台刷新后选中项与键盘焦点保持。
- 测试：`tests/test_chat_app.py` 191 passed；`tests/test_agent.py`、`tests/test_inference.py`、`tests/test_context.py` 96 passed。测试侧另修复 Windows GBK 下 Git 日志读取（显式 UTF-8）及 Git 同步启动、会话栏布局的时序等待。
- 隔离真实应用冒烟（实际 `ChatApp` + 磁盘配置 + 临时合成 Vault，非原生终端）：`/model` 0.4–0.8s 开屏，↑↓ 保持搜索框焦点并移动高亮，后台补全后高亮与焦点保持；`/context` 0.01–0.07s；`/inferences` 400 条开屏 0.19–0.27s，连续 5 次确认约 0.1–0.4s，编辑保存成功且事实数不变；全程 0 次模型调用。
- 未读取真实 Vault/Keyring，未调用真实模型、搜索或远端 Git；预览仅使用合成数据。Textual SVG 导出的中文字形受回退字体影响，真实终端字形未重新人工验证。
- 后续修正（同日晚）：`/model` 的线上 `/models` 只返回当前一个模型时，旧的“用线上列表替换预设”会让选择器只剩一项、↑↓ 看起来无效；改为线上列表与供应商预设合并。模态屏打开时焦点在布局前建立，硬件光标停在旧坐标（左上角）闪烁；改为布局后再次聚焦并校正 `cursor_position`，关闭模态屏后恢复到输入框插入点。`tests/test_chat_app.py` 相关 31 passed，`tests/test_agent.py -k model_options` 4 passed。

## 二级界面统一与高风险操作保护（2026-11-05）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），基线 `dfec0a2`；本轮改动与本记录在同一提交中，推送目标由用户明确授权。
- `/profile` 改为固定标题、真实缓存状态/元信息、单控件正文滚动与固定操作提示；短内容保持紧凑，长内容和窄终端独立滚动。查看仍只读摘要缓存，不调用模型或扫描完整画像。
- `/inferences` 统一为 C Confirm / R Reject / E Edit；来源摘要改为 Worker 内一次事实库读取并建索引，审查后显示结果和剩余 Pending 数，编辑保存失败保留草稿并可直接重试。
- Git 同步继续使用界面内 `Ctrl+S`，确认展示提交/Push 影响和可用数量，默认 Cancel，显式选择 Synchronize 才启动既有 Worker；自动调度和锁逻辑未改。会话永久删除改为 `Ctrl+D` 或 Delete 按钮，默认 Cancel、Esc 取消、当前/生成中会话限制不变，文件删除失败保留界面与数据。Provider 移除默认 Cancel，失败不宣称成功。
- TUI `/import <path> --apply` 在文档解析和模型调用前进行一次批次确认，说明费用与部分完成保留语义；Preview 和正式导入写入/错误处理逻辑未改。Sessions 补标题与鼠标删除入口，详情弹窗在窄屏缩短提示，未重写其它正常界面。
- 验证：完整 `tests/test_chat_app.py` 185 passed；Agent/CLI/会话/文档组 281 passed，记忆/推断/Git 组 356 passed、1 skipped（Windows 符号链接权限），检索/搜索/Markdown 组 449 passed，合计 1271 passed、1 skipped、1 个既有 Pydantic 警告。`git diff --check` 通过。20 张合成 SVG 与 6 张代表 PNG 保存在本地忽略的 `logs/ui-preview-release/`。自动化测试未读取真实 Vault/Keyring，未调用真实模型、搜索或远端 Git 服务。
- 真实 TUI 冒烟：使用独立测试 Vault 与 Spark，共 5 次模型调用，真实对话、Profile 摘要、正式 Import、推断编辑/确认及 Git/Session 取消均通过，可识别测试会话保留。实测发现搜索框聚焦时 `Ctrl+D` 被 Input 消费，已改为该界面内的 priority binding 并补回归；未执行真实远端同步或 Provider 删除。真实 LLM 规则已改为开始或追加使用时说明用途，不再逐轮申请次数确认。
- 已知边界：Headless Pilot 与 SVG/PNG 已覆盖 120×36、100×30、80×24、60×20；真实模型验证仅覆盖当前 Spark 配置，原生终端字体、IME 和鼠标拖选仍未人工验证。

## Evolution Review 收尾补强（2026-11-05）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），基线 `2444491`；本轮改动仅在工作区，未 commit、未 push。
- 迁移 CLI：`main()` 在 `--apply` 且 `keyring_complete=False` 时返回 `2`，并在 stderr 说明文件迁移已完成且保留、Keyring 需人工核对；`MigrationError` 仍返回 `1`。`_print_report()` 不再在 `already_migrated` 时提前返回，已迁移 Vault 的再次执行仍显示 Keyring 结果。dry-run、`--skip-keyring` 和完全成功的返回码不变，输出不包含凭据值。
- Git 提交：`GitRepo.commit()` 在真正提交前只对索引执行敏感路径终检，复用 `staged_paths()` 与 `SENSITIVE_PATTERNS`；命中抛 `GitError(kind="sensitive")`，不删除文件、不 reset 索引。未暂存的工作区敏感文件不影响独立 Commit，`_require_own_repo()` 保护保持不变。
- 验证：`tests/test_migrate_to_syvrio.py` 19 passed；`tests/test_git_sync.py` 66 passed、1 skipped（Windows 无符号链接权限）。按任务约定未重复全量测试；未读取真实 Vault/Keyring，未调用真实模型或搜索 API。

## Evolution 安全边界与写入一致性修复（2026-11-05）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），审查基线 `5d7b0e9`；本轮改动仅在工作区，未 commit、未 push。
- Git 根目录：`GitRepo.is_repo` 改为要求 `git rev-parse --show-toplevel` 与 Vault 路径（realpath/normcase 归一）一致。Vault 位于父仓库子目录时，初始化、提交、同步、Push 统一拒绝并返回 `foreign_repo`，不会再误用父仓库的索引/remote；相对路径与符号链接 Vault 仍正常识别。
- 敏感文件：首次 `init_vault` 与日常提交共用 `_stage_all_checked()`，暂存前后各检查一次；`sensitive_paths()` 同时覆盖工作区与已暂存内容，`staged_paths()` 使用 `git diff --cached --name-only -z` 避免空格/非 ASCII 引号绕过。新增 `*.env`、`*.env.*` 规则，覆盖 `keys.env`/`secrets.env`/`credentials.env`。拒绝时不删除文件、不清空暂存区。
- 演化事务：`evolve()` 用既有 `rollback_files()` 覆盖事实主题文件、`audit.jsonl` 与 `inferences.jsonl`；追加新事实、旧状态更新、审计写入、推断失效任一步异常都按字节恢复。
- 迁移脚本：标签改写失败、校验失败、删除旧元数据后的配置迁移失败都会统一恢复记忆文件、元数据目录与本次新建配置；恢复使用备份的复制而非移动，备份保持独立可用。Keyring 在文件迁移完成后执行，失败只报告部分进度，不回滚已完成的文件迁移。`resolve_vault()` 修复 `SYVRIO_VAULT=""` 被当成当前目录的问题。
- Profile 状态：`schedule_overview()` 暴露 `pending_inferences`；`/profile status` 在 `0 pending changes` 时仍明确显示 `inference batch pending`，不虚构推断数量。
- 验证：全量 `PYTHONUTF8=1 HF_HUB_OFFLINE=1 uv run pytest -q` 为 1235 passed、1 skipped（本机无符号链接权限）、1 个既有 Pydantic 警告，耗时 726 秒；`git diff --check` 通过。新增测试均使用临时 Vault、本地 bare remote、模拟时间与假 Keyring；未读取真实 Vault/凭据，未调用真实模型或付费 API。
- 已知边界：跨进程写入仍依赖 advisory lock；文件恢复只覆盖普通 Python 异常，不覆盖进程强杀/断电；Keyring 本身没有事务语义，部分失败需人工核对。

## Syvrio 品牌迁移完成（2026-11-04）

- 唯一 Python 包与 CLI 为 `syvrio`；`src/quiet_memo` 兼容包、`qm`/`quietmemo` 别名及旧导入适配器已删除。
- 配置统一为 `syvrio.toml`；用户配置目录 `~/.config/syvrio/`（Windows `%APPDATA%\syvrio\`）；默认 Vault `~/.syvrio/vault/`；Vault 元数据目录 `.syvrio/`；环境变量仅 `SYVRIO_CONFIG`、`SYVRIO_VAULT`；Keyring 服务名 `syvrio`。
- MCP Server、TUI 助手名、缓存/日志/临时目录前缀统一为 `syvrio`；转义标签前缀改为 `~syvrio-escaped~`，迁移工具负责旧标签转换。
- 新增 `scripts/migrate_to_syvrio.py`：默认 dry-run，`--apply` 先备份再迁移，校验记忆 ID/条目、会话、JSONL 与元数据哈希；冲突停止，重复执行幂等，锁文件不搬运，可重建缓存不迁移。
- PyPI `syvrio` `0.3.3` 已发布；本次只修复品牌与发布配置，不触发新的 PyPI 发布或版本升级。
- 验证：全量 `pytest -q` 为 1185 passed、6 skipped；`uv build` 的 wheel/sdist 仅包含 `syvrio`；旧名扫描仅剩迁移工具、迁移文档与历史评审证据。
- 真实数据：旧 Vault 元数据已迁移到 `.syvrio`；12 个记忆文件、1114 条条目与 ID、5 个会话保持一致，pending/audit/inferences/reflection 行数逐项核对；迁移前备份保留。Keyring 后端不可用，凭据仍由私有环境文件提供并已迁入 `syvrio` 配置目录。
- 历史章节中的旧名称/路径仅作当时证据，不代表当前有效路径。

## Profile 推断批次待办生命周期（2026-11-04）

- 分支 `feature-memory-evolution`，审查基线 `1303ce9`；提交已 rebase 到 Syvrio 重命名后的 `53df23f` 之上。
- 根因：`record_run()` 在 Profile 成功时无条件清零 `fact_digest`、`pending_changes` 与 `pending_important`；当自动预算被摘要刷新占用、`result.remaining > 0` 时，剩余推断批次没有任何持久化待办，之后没有事实变化就不会再自动运行。
- 修复：新增持久化字段 `profile.pending_inferences`，摘要更新与推断完成分开判断。摘要成功但仍有未处理批次时保留待办，全部批次完成才清除；`completed`/`prepared` 增量记录继续避免重复提交已处理事实。
- 调度：`check_due()` 与 `plan()` 都只在常规 168 小时周期或失败重试到期时因剩余批次唤醒，24 小时冷却与单次 1 请求限制不变；重启后待办仍在。失败、取消、来源变化不清空待办；空 proposals 批次正常标记完成。
- `ReflectionResult.completed` 记录本次完成批次数，使没有新请求但发布了已校验 prepared 缓存的手动运行也能同步自动调度状态。
- 回归：`tests/test_reflection.py` 覆盖摘要占用预算、周期恢复、多次执行归零、重启、冷却与退避、手动/预校验批次、空结果以及失败/取消/来源变化。测试使用临时 Vault、Mock 模型与模拟时间，未读取真实 Vault/Keyring、未调用真实 LLM。

## 画像摘要、命令面板滚动与低频回顾调度（2026-11-04）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），基线 `8c91143`；本轮改动仅在工作区，未 commit、未 push。
- `/profile` 卡顿根因：旧实现每次查看都在写锁内遍历全部有效事实与推断，把每条事实和来源 ID 拼成单个聊天卡片。合成 5000 条事实时数据构建约 456ms、文本约 493KB；Textual 挂载该单卡超过 120 秒仍未完成。现在 `/profile` 只读 `.syvrio/profile-summary.json` 派生摘要并在限高弹窗中滚动显示，5000 条合成事实下读取约 0.4–3ms，约 2800 字摘要弹窗稳定约 10ms；不调用模型、不遍历完整画像。
- 画像摘要：新增 `ProfileSummaryResponse` 与 `ReflectionEngine.profile_summary()`；`/profile refresh [n]` 在摘要缺失/过期时用最多 1 次请求生成摘要，剩余 n-1 次继续推断批次；`n=0` 不发请求，事实未变化时跳过摘要。摘要输入按去重后的有效事实量决定参考长度（<30 条 100–300 字到 >1000 条 1000–3000 字），按主题轮转覆盖并在 token 上限内截断，包含日期时只使用已有 `valid_from`，不猜测事实时间；只纳入已确认画像推断，未确认假设不进入摘要。摘要正文不写回事实/推断，不把旧摘要作为新证据，来源变化时立即标记过期，生成失败或取消时保留旧摘要并标记状态。
- 缓存与隔离：`.syvrio/profile-summary.json` 保存摘要正文、生成时间、模型、统计和来源 ID/签名；`.syvrio/profile-fingerprints.json` 保存上次成功画像的来源指纹基线；`.syvrio/reflection-state.json` 保存跨重启调度状态。三者均为派生数据，vault 内 gitignore，可删除重建。摘要签名同时覆盖记忆文件元信息与推断日志；事实变化立即过期，但不会立即触发 LLM。
- 自动调度：移除“每 300 秒轮换 Dreaming/Profile、每次启动重置 20 次预算”。Dreaming 默认每 24 小时最多自动一批、每批最多 1 次请求；Profile 默认每 7 天最多自动更新一次，普通新增只记录待处理变更，变更达到 `profile_min_changes`（默认 20）或目标/偏好/职业方向等重要变化时，可在 24 小时冷却后提前触发。TUI 每 5 秒只做状态与文件元信息检查，事实扫描和变更判断在 Worker；无变化不扫描完整记忆、不请求模型。失败按 1/2/4…小时退避，上限 24 小时；手动 `/profile refresh` 与 `/dream` 可绕过自动周期但仍遵守命令预算。状态损坏或缺失安全回退，不产生请求风暴。
- 命令面板：`CommandPalette` 改为外层限高、内层 `VerticalScroll`；↑↓ 移动时自动滚动高亮项，支持鼠标滚轮与单击执行，保留两列对齐和字面方括号/尖括号参数。80×24 与 120×40 下都不遮挡输入框；新增最后一条命令可见、向前回滚、前缀过滤、点击执行、滚轮与初始高亮测试。
- 测试：`tests/test_reflection.py` 新增摘要预算/缓存/失效/损坏/取消/失败/动态长度/调度/退避等用例，`tests/test_chat_app.py` 新增画像弹窗、后台刷新与命令面板滚动/点击/尺寸用例。相关测试使用临时 Vault、Mock 模型与模拟时间；未读取真实 Vault/Keyring、未调用真实 LLM 或付费 API。
- 已知边界：真实模型生成的摘要语义质量、7 天/24 小时周期在真实时钟下的长期行为、原生终端滚轮与拖选仍需人工验收；摘要来源引用不展示在 UI，仅保存在缓存中；画像上下文仍沿用既有“相关已确认推断最多 300 tokens”机制，完整摘要不注入聊天。

## 回顾调度一致性与画像摘要预算复审（2026-11-04）

- 分支 `evolution`，审查基线 `093cd16`；本轮仅在工作区修改，未 commit、未 push、未创建 PR。
- 重要变化只由本轮新增、更正或删除的事实判定；历史中已存在的重要事实不再把普通变更升级。调度计划的待办标记只累加不重置，`remaining_batches` 跨重启保留，部分 Dreaming 成功不清空剩余批次。自动失败退避在轻量检查与计划阶段同时生效，成功后清除 `next_retry_at`。
- 轻量检查纳入摘要缓存缺失/损坏/过期与已确认画像推断变化，仍只读状态和文件元信息；完整事实扫描与模型调用留在 Worker。
- 画像摘要证据签名只覆盖有效事实与参与生成的已确认画像推断；生成前后签名一致才发布，未确认假设不再阻止旧摘要重新签名。摘要输入按完整 payload（事实、推断、统计、提示词、输出与余量）预算，主题统计超限时压缩为汇总项，窗口不足在请求前明确失败且不消耗预算。聊天上下文直接筛选相关有效已确认画像推断，不再构建完整画像。
- 回归：`tests/test_reflection.py` 新增重要变化判定、摘要唤醒、部分 Dreaming、退避、证据签名、请求期间变更、预算与上下文选择用例；相关测试使用临时 Vault、Mock 模型和可控时间，未读取真实 Vault/Keyring、未调用真实 LLM。

## 搜索超时缓存与 /undo 边界收尾（2026-11-03）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），基线 `8d54e1e`；本轮改动仅在工作区，未 commit、未 push。
- P1 早停超时：`SearchRouter` 新增 `_timed_out(started)`；Provider 返回后先归一化并保留候选，再检查总预算。即使结果能通过 `quality_gate()`，预算耗尽时也立即以 `timed_out=True` 收尾、不再尝试查询变体或后续 Provider，并且不写缓存。Provider 抛出超时/其他异常后也复用同一预算判定。compare 截断使用候选池实际贡献来源计算 `verification`，不虚构来源；auto/strict 同规则。
- P2 失败诊断去重：新增 `_dedupe_failures()`，按 `(provider, kind, detail)` 稳定去重并保留首次顺序；同时用于缓存命中合并与 `_final_outcome()`，避免无效 Provider 在首次搜索和缓存命中时重复出现，也不会把同类不同原因的失败误合并。
- P2 多会话回填：`_pending_undo_restore` 改为 `dict[session_id, text]`。切回会话只消费该会话的待回填；同一会话的新撤销覆盖尚未应用的旧文本；输入框已有未发送内容时不静默覆盖，通知并保留待回填，之后输入为空再应用；成功应用后立即移除。
- P2 撤销失败队列：`_finish_undo` 区分 success / no_target / failed。成功且有排队消息时沿用暂停策略；无目标或加载/写入失败时不暂停队列，Worker 已退出后调用 `_drain_pending_intent()` 接续已排队消息，并给出 “No generated turn to undo.” / “Undo failed: …” / “queued message(s) resumed.” 提示。无绑定消息时仍不会回退撤销更早回合。
- 测试：`uv run pytest -m search -q` 为 376 passed；`PYTHONUTF8=1 uv run pytest tests/test_chat_app.py -q` 为 145 passed；`tests/test_agent.py tests/test_session.py tests/test_server.py` 为 104 passed。全量离线套件 1111 passed / 1 failed：唯一失败为既有 `test_sidebar_items_are_compact_and_clickable` 的焦点就绪时序，单测复跑通过，与本轮改动无直接关联。默认 GBK 控制台下涉及 UTF-8 子进程输出的 Git 界面用例仍需 `PYTHONUTF8=1`。
- 未验证：未调用真实 LLM、付费 API 或公网搜索；未读取真实 Vault/Keyring。全部使用临时 Vault、Mock Provider、模拟时钟与模拟 Worker。
- 剩余边界：Provider 同步阻塞时仍无法被线程事件即时打断；撤销只能等 Worker 退出。进程崩溃不会执行撤销。缓存仍是单文件派生数据，多进程并发的最后写入者生效，未引入事务。测试侧会话切换后的焦点就绪存在独立时序抖动，未修改产品逻辑。

## /undo 即时撤销与搜索缓存可靠性修复（2026-11-03）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），基线 `4129ca6`；本轮改动仅在工作区，未 commit、未 push，远端的 `main` 未改动。
- `/undo` 根因：`_submit_during_ask` 只放行 `SAFE_COMMANDS`，生成期间的 `/undo` 会进入全局 FIFO；`runtime.undo` 只能针对 `self.current` 的最后一条用户消息，既无法在等待期取消请求，也可能在切换会话后误撤销其他会话。修复为即时控制命令：`_request_undo` 在进入 FIFO 前先设置现有 `GenerationCancelled` 事件，记录目标会话与 Worker 实际追加的用户消息 ID（`runtime.ask` 新增 `on_user_message` 回调），Worker 退出后才执行撤销。重复 `/undo` 在撤销进行中直接忽略；旧版本排进队列的 `/undo` 一次性清除；普通排队消息保留但暂停，直到用户下一次主动发送非命令消息后才恢复接续。
- 撤销时机与边界：`_finish_undo` 始终从磁盘重新加载目标会话再追加 `undo` JSONL 事件，避免切回时载入的内存对象落后于后台写入；只撤销绑定的那条用户消息及其后的部分/最终回复和工具记录，不启动新请求、不自动发送队列。目标就是当前视图时重载并回填原文到输入框；目标在后台时只记录待回填，切回后再重载回填，不修改当前会话的视图和输入，其他会话的后续对话也不会清掉这个待回填项。若没有绑定到具体用户消息（例如请求在落盘前失败），不会回退撤销更早的对话。`/undo` 与自然完成的竞态下，只要撤销已请求，最终回复不会保留。
- 搜索缓存：`CACHE_VERSION` 升到 2，缓存条目新增 `strategy`、`attempted`、`contributors`、`verification`、`failed` 诊断与上限 20 条的元数据。`CachedSearch`/`get_record` 在命中时恢复这些字段；compare 的 `verification` 为 `complete`/`incomplete`/`unknown`，缺失字段的 compare 条目一律视为 `unknown`，绝不默认完成；v1 旧缓存整体安全失效。缓存键继续区分 provider 顺序、策略和尝试序列，auto/compare/strict 不串用。
- 截断：`_final_outcome` 统一 `truncated = budget_exhausted or timed_out`，覆盖 `max_requests`、全局 `timeout_budget`（含单次请求耗尽剩余预算、收尾时复核）等路径；截断结果保留候选并写明确提示，但一律不写缓存。compare 因预算/超时未能完成第二家时同样不缓存；仅因普通 provider 失败或空结果而只有一家贡献时，按 `incomplete` 语义缓存并在命中时恢复原诊断。普通 provider 失败但其他来源正常完成仍可缓存。截断收尾的验证状态从候选池的实际贡献来源计算，已有多家贡献时不会误报为“仅 0 家”。
- 测试：搜索套件 `uv run pytest -m search -q` 为 370 passed；`tests/test_agent.py tests/test_session.py` 102 passed；完整 `tests/test_chat_app.py`（`PYTHONUTF8=1`）为 140 passed。新增用例覆盖：生成期 `/undo` 不进入 FIFO、重复点击只撤销一次、切到其他会话只撤销原会话、其他会话继续对话后仍能回填、无绑定消息不得撤销上一轮、撤销后队列暂停并可恢复、自然完成与取消竞争；compare 缓存恢复 complete/incomplete/unknown、未完成时的失败诊断、同一 URL 不算独立来源、策略缓存隔离、预算截断不缓存且验证状态如实。默认 GBK 控制台下涉及 UTF-8 子进程输出的 Git 界面用例需设置 `PYTHONUTF8=1`，属于本机终端编码环境问题。
- 测试同步：设置输入框回填值会异步触发 `Input.Changed`；测试在结束前等待该消息处理完，避免界面卸载后查询命令面板造成偶发 `NoMatches`，产品逻辑未改。
- 未验证：未调用真实 LLM 或收费搜索 API，未读取真实 Vault/Keyring；全部使用临时会话、模拟 Worker 与离线 Provider。真实 cancellation 延迟、跨进程缓存写入竞争与终端人工验收仍未覆盖。撤销仍只保证单进程内时序；进程崩溃或强制退出不会执行撤销。

## SearXNG、多 Provider 调度与搜索质量修复（2026-11-03）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`），基线 `4ac7839`；本轮改动未 commit、未 push。
- SearXNG：新增 `infra/search/searxng.py`，以 `GET /search?q=...&format=json` 接入自托管实例，整个实例视为一个 Provider。校验 http(s) 地址（允许本机/私网、拒绝凭据与查询串）、不跟随重定向、响应体上限 2 MiB、单次超时取 `searxng_timeout` 与剩余预算较小值；映射 `title/url/content/publishedDate/engine`，过滤特殊对象与非法 URL，按统一 `normalize_url` 去重并限制条数；`unresponsive_engines` 转为 `last_partial_failures`，零结果且内部引擎全失败时抛 `ProviderError`。403 明确提示在 `settings.yml` 的 `search.formats` 启用 json，连接失败提示检查实例与地址。
- 配置与向导：`[search]` 新增 `searxng_base_url` / `searxng_timeout`；`searxng` 默认不启用、无 Key，默认本地额度 `(0,0)`；`qm setup` 可填写/修改地址、启停 Search 和调整优先级，不探测或启动用户服务。
- 多 Provider 调度：`SearchQuery` 新增 `providers` 与 `strategy`，保留 `preferred_provider` 兼容。Router 校验名称、去重、最多 8 个，只接受已启用且注册成功的来源；`auto` 按推荐顺序执行并追加配置剩余来源，质量达标早停；`compare` 不因第一家达标而停止，预算允许时至少完成两家，未完成时在 `hint` 明确写“交叉验证未完成”及失败类型；`strict` 只调用明确列出的来源，空列表拒绝。`SearchOutcome` 新增 `attempted` 与 `strategy`，Agent 与 MCP 工具均开放 `providers` / `strategy`。
- 缓存与保护：缓存键升到 `search-v7`，包含策略、推荐列表、实际尝试顺序等；`max_requests`、`timeout_budget`、`UsageLimiter` 与请求失败回退均保持共享，LLM 参数不能绕过。
- 质量修复：`quality.py` 提取首字母大写专有名词短语（如 “Clash of Clans”、“Town Hall”），短语标题命中加权；含短语时强命中要求标题至少命中一个完整短语且核心词覆盖过半，避免只命中部分单词的代理/跑题结果早停。URL 规范化统一到 `base.normalize_url`（www.、utm_*、普通锚点合并，SPA 路由片段保留），Parallel 内部去重改用同一规则。日期统一由 `parse_published_date` 校验，非法或明显未来日期返回 `None`。`search` 工具与 MCP 文档补充官方公告/媒体报道/个人爆料/推测的区分要求。
- 测试：`uv run pytest -m search -q` 为 359 passed；`tests/test_agent.py tests/test_server.py tests/test_setup.py` 为 115 passed。`tests/test_chat_app.py` 两个 Git 界面用例在默认 GBK 控制台下因 subprocess 读取 UTF-8 git 输出失败，设置 `PYTHONUTF8=1` 后通过，属于本机终端编码环境问题，不是本轮搜索改动。
- 未验证：未发起真实 SearXNG/AnySearch/Parallel 请求，未实测真实搜索质量对比；SearXNG JSON 字段、`publishedDate` 形状、`unresponsive_engines` 真实组合仍待授权冒烟。`scripts/smoke_search_apis.py --live` 可用但需先报告目标、关键词、次数与额度消耗。

## AnySearch 与 Parallel MCP 搜索扩展（2026-11-03）

- 分支 `evolution`（跟踪 `origin/feature-memory-evolution`）。新增 `infra/search/anysearch.py`（REST，`POST /v1/search`，可选 Bearer）与 `infra/search/parallel_mcp.py`（固定 `https://search.parallel.ai/mcp`，FastMCP Streamable HTTP，工具 `web_search`，可选 Bearer）。两者都支持匿名使用，无 Key 时不发送认证头；提供 Key 时只加 `Authorization: Bearer`。
- 注册：`router._OPTIONAL_KEY_PROVIDERS` 记录环境变量名（`ANYSEARCH_API_KEY` / `PARALLEL_API_KEY`），`build_registry` 无论有无 Key 都注册，区别于无 Key 即跳过的严格外部厂商。AnySearch 的 `zone` / `language` 来自 `[search]` 的 `anysearch_zone` / `anysearch_language`（默认 `cn` / `zh-CN`）。
- 集成：复用 `JsonApiClient`、`SearchRouter` 的 provider/引擎回退、质量门控与缓存、`UsageLimiter`（默认 daily=20、monthly=0，仅本地请求计数）、`qm setup`（可匿名启用、录入可选 Key、清除 Key 恢复匿名）、`scripts/smoke_search_apis.py` dry-run 与 `config.example.toml`。
- 错误映射：AnySearch 业务 `code != 0` 归为 BAD_RESPONSE；Parallel 的 401/403 归 AUTH、402/429 归 QUOTA、超时归 TIMEOUT、其余异常归 NETWORK，detail 做长度截断。MCP 每次搜索独立建立并关闭会话；调用线程已有事件循环时改在线程池执行 `asyncio.run`。
- 测试：新增 `tests/test_anysearch_parallel.py` 19 项（MockTransport、假异步 client、真实内存 FastMCP server），覆盖匿名/Key、鉴权与配额错误、超时、结构化与文本结果解析、去重/截断/日期过滤、router 回退与本地额度、配置默认值；`tests/test_setup.py` 补匿名启用与清除 Key 两项。相关搜索/配置/安装套件 348 passed（2 个既有 Pydantic 警告）。
- 未验证：本轮未发起任何真实 AnySearch/Parallel 请求，也未核对线上 `tools/list` schema；MCP 传输层交由 FastMCP，离线测试只走内存 server，未验证真实 SSE/HTTP 帧。Parallel 的 `web_fetch` 未实现。
- 国内网络：两服务均在境外，连通性与延迟需自行确认；失败按既有规则回退到本地 open-websearch，任何上游失败都不写缓存。
- 本次未 commit、未 push，改动保留在工作区。

## Git 维护功能增强（2026-11-02）

- 分支 `feature-memory-evolution`。完成 TUI `/git` 管理界面：真实仓库状态、Remote/Upstream、未提交文件、未推送提交、最近 Commit/Push、自动同步配置、最近结果与错误；`Ctrl+S` 仅在界面内打开确认，`Enter` 执行，`Esc` 取消，后台 Worker 分阶段反馈。关闭界面不取消执行，重复触发被拒绝。底部模型/上下文/Pending 状态栏未加入 Git 信息。
- Commit/Push 解耦：有变更才提交，无变更时仍可独立补推；Push 失败保留本地 Commit，下次只补推；禁止 Force/Pull/Merge/Rebase/Reset；远程分叉停止自动推送；未配置 Remote/Upstream 显示设置提示。手动与每日同步均使用固定提交信息，不调用 LLM；`qm commit` 与 `auto_push` 旧语义保留。
- 每日默认本地 03:00 的 TUI 进程内调度，支持修改时间、关闭、启动补执行、按日期去重；网络/超时失败按 `retry_interval_minutes` 补推，认证/分叉/未配置不频繁重试。状态持久化到 vault 内 `.syvrio/git-sync-state.json`（gitignore），最近成功 Push 时间不从 Commit 推断。
- 并发：新增 `.syvrio/vault.lock`（记忆、会话 JSONL、审计/推断/回顾日志与 Git 快照）和 `.syvrio/git.lock`（Stage/Commit/Push），均为进程内重入、跨进程互斥，进程崩溃由 OS 释放；固定顺序 Git → Vault。Push 网络等待只持 Git 锁，不阻塞聊天和记忆写入。SessionStore 与回顾任务日志已接入同一 Vault 锁。
- 新增 `infra/locking.py`、`infra/gitsync.py`、`app/git_screen.py`、`app/chat_git.py`；增强 `infra/gitops.py`（超时、非交互、状态/上游/未推送查询、错误分类、敏感路径检查），扩展 `infra/config.py` `[git]` 配置。中英文 README、`config.example.toml` 与结构树同步。
- 验证：新增 31 项 Git 同步/调度/并发测试与 6 项 TUI 交互测试，使用临时 vault、本地 bare remote 和模拟时钟，不触碰真实记忆库或外部远程。全量 `pytest -q`（启用项目 env.sh 的 fastembed 缓存并设置 `HF_HUB_OFFLINE=1`）为 994 passed、2 skipped，耗时 101.07 秒；此前未设置缓存路径时 fastembed 会尝试联网下载，属于环境问题。
- 已知边界：文件锁依赖本地文件系统 advisory lock，NFS/SMB 多主机共享同一 vault 不保证跨主机互斥；TUI 未运行时不会主动定时，只由下次启动补偿；外部 Git 操作可能改变引用，界面状态以本地读取为准；终端 XON/XOFF 可能拦截 `Ctrl+S`，界面提供点击同步行作为替代。
- 已提交 `ca53b1e` 并推送到 `origin/feature-memory-evolution`（2026-11-02）；本地 HTTPS 访问 GitHub 不通，推送使用同一仓库的 SSH 地址完成。

## CI 环境与失败修复（2026-11-02）

- 用户指定远程测试环境统一 Debian：演进分支的测试与发布 workflow 均使用 Debian 13 slim 容器、Python 3.12；Ubuntu runner 仅作为 Docker 宿主。安装 Git、CA 证书与 Node 所需 C++ 运行库后再 checkout。保留测试、构建和 CLI help 检查，未修改冻结 main 的 workflow。
- Actions run 41368204299：Ubuntu job 通过；Windows job 在会话切换 UI 测试失败（934 passed、4 skipped、1 failed），CLI help 步骤因前序失败跳过，不是 CLI 启动错误。
- 修复测试同步条件：导航标志结束不代表 Textual 异步移除完成；等待目标会话、导航就绪与实际回复控件状态，并等待完整流式缓冲，保留清空和切回重放断言。局部相关 3 项通过，扩大会话切换/流式/导航回归 18 项通过；uv build 和 qm --help 均通过，YAML 结构校验通过，未调用真实模型或搜索。
- 旧章节中的双平台 CI 描述属于历史，不代表当前演进分支工作流。新 Debian 容器的执行结果须以推送后的 Actions 为准。

## 当前开发分支与冻结约定（2026-11-01）

- **长期远程保留 `main` 与 `feature-memory-evolution`**；历史 `base/chat-stability`、`feature/context-review-recall` 已不在远程，审核期间可存在待清理的短期 PR 分支。删除前的祖先检查记录未独立复核。历史章节中的 base 开发、自动同步 main、提交前默认全量测试均已失效。常规开发应在 evolution 或由其派生的短期任务分支进行；main 保留 GitHub 默认分支身份。
- **后续默认开发分支：`feature-memory-evolution`**。所有新功能、常规问题修复、实验和相应文档工作从此分支（或从它派生的任务分支）开展。执行前确认当前 branch，不在默认的 `main` 上直接开发。
- **`main`：PR #15 合并后冻结展示源码，PR #16 为经授权的发布元数据维护；此后仅用于简历展示、稳定发布与长期维护**。禁止自动将演进分支、历史 `base/chat-stability` 分支向 `main` 同步或合并；只有本人明确点名授权的必要稳定性/安全修复才可走独立修复与受保护分支 PR 流程。
- 历史冻结前源码评审基线为 `main@b016ccbf`，详见 [review-2026-11-01-freeze.md](review-2026-11-01-freeze.md)，不得将其当作当前 HEAD。PR #15 合并提交 `316a030` 对应 `portfolio-2026-11-01`；PR #16 合并提交 `22217c2` 对应 `v0.3.3`。历史 base 同步安排全部作废。
- PR #15 已合并，`portfolio-2026-11-01` 指向 `316a030`；PR #16 已合并，`main` 当前 HEAD 为 `22217c2`，`v0.3.3` Tag 和 GitHub Release 均已存在。两次 PR 的 Windows/Ubuntu CI 均通过。PyPI `0.3.3` 本轮未通过官方发布记录独立复核；原生终端/IME/系统剪贴板仍待人工验收。

- **PR #16 发布维护已同步（2026-11-02）**：本分支通过 `main@22217c2` → `feature-memory-evolution` 单向合并吸收 PR #16 的四处版本变更：`pyproject.toml`、`__init__.py`、`open_websearch.py` 的 MCP clientInfo、`uv.lock`，统一为 `0.3.3`；中英文 README 保留演进功能说明，修正已过期的“尚未同步”描述。合并提交为双父提交，保留 `main` 的完整祖先历史以及本分支原有功能；未将 evolution 反向合入 main。此前 Windows CI 的会话切换异步测试仍应独立复核，合并行为不代表测试已修复。

## 历史接续记录（2026-10-30；不可作为当前入口）

本节保留 2026-10-30 当时的接续快照，不能作为 2026-11-02 的分支 HEAD、发布版本或任务优先级依据；下方均为按时间保留的历史工程记录。后续实现已改变部分旧边界；交接记录不替代当前会话授权。

- 远程目标 `feature-memory-evolution`，本地功能分支 `codex/feature-memory-evolution`；主目录 main 保持独立，不自动合并。当前功能基线 `8b8d507`；main 的命令精简基线 `a7777c0`。
- TUI 默认空闲回顾：补记结束后空闲 60 秒，Dreaming/画像轮转，间隔 300 秒，每次启动自动预算 20 次；配置可关闭，cancel 暂停，resume 恢复。进程退出后无守护任务。预算耗尽不会由 resume 自动补满，手动执行使用单独预算。
- 画像跨全部当前有效事实提炼，兼容中文与自定义主题；事实展示只纳入常见主题及有效画像推断引用的来源。仅已确认且来源与本轮召回相关的画像推断注入上下文。Dreaming 推断仍仅供审查。
- TUI 会话入口仅 /sessions；/resume、/recall、/remember 已删除，Agent recall/memorize 与 CLI 管理功能保留。
- 检索重跑已完成，两分支基础三路检索命中率一致；完整汇总及方法在 [评测说明](../evals/README.md)。固定合成题集与模型生成真实题集分开统计，不能把生成题集当人工金标准，也不能与旧题集成绩直接作涨跌比较。
- 时间有效期探针中 evolution 排除过期/未来事实，main 未实现该筛选。无个人记忆意图的通用问题仍常得到非空结果，下一步优先评估召回门控；本轮未验证最终回答是否误用记忆，不能声称已有无关记忆拒答能力。
- 两边固定记忆解析/裁决均 24/24；evolution 上下文对照 5 例均通过。最近功能全量回归 600 passed。真实数据、查询、来源ID、模型输出和本机配置均只在本地忽略目录，不写入交接文件或提交。
- 本次文档核对提交前全量回归 600 passed（281.52 秒），git diff --check 通过；仅更新文档，未改产品行为。
- 建议后续先做后台状态/配置交互与召回门控评测；复杂时序重排、推断情绪和跨客户端主动召回仍未完成。功能待办按 GitHub Issues 维护。

发布历史日期：2026-10-29。代码版本 `0.3.2`，发行标记使用 `v0.3.2`；基础功能参照 `444fe0f`，文件导入与模型选择界面参照 `bef5199`。包版本、锁文件与 MCP 客户端标识保持一致；远端发布结果以 GitHub Release、Actions 与 PyPI 为准。

更新日期：2026-10-31。代码版本 `0.3.2`，发行标记使用 `v0.3.2`；基础功能参照 `444fe0f`，文件导入与模型选择界面参照 `bef5199`。包版本、锁文件与 MCP 客户端标识保持一致；远端发布结果以 GitHub Release、Actions 与 PyPI 为准。

## 搜索质量优化（2026-10-31，base/chat-stability）

按《联网搜索质量优化实施方案》分阶段完成 P0（Phase 0-3），逐阶段提交：Phase 0/1 `d3ec502`、Phase 2 `79ccbb0`、Phase 3 `87a66fd`。

- 离线基准：`evals/web_search_eval.py`（33 条合成查询、固定候选池、PoolTransport mock；`--save/--compare/--json`），基线快照 `evals/web_search_baseline.json`。合成数据零个人信息，可公开提交。
- QueryAnalysis（`infra/search/quality.py`）：core/context/intent 三层分离——core 只来自原始查询并决定相关性；context 低权重消歧（0.5/0.25），不能单独令结果通过质量门控；时间词（今天/最新/latest…）与事件词（发布/官宣/更新…）不进 core。拉丁词元词边界匹配（`tibo` 不命中 `tibonacci`），CJK 按受控停用词/时间/事件词表轻量切分并保留短语序，≥4 字片段补二元词元；无高置信实体时 `ambiguous=True`。
- 渐进式搜索 + QualityGate：每次请求向上游拉 `fetch_limit = min(30, max(10, limit*3))` 条，进统一 `CandidatePool`（URL 去重、www. 宿主归一、保留各引擎名次证据与更完整标题/摘要）。早停条件：≥2 条标题级强命中，或单条标题覆盖全部 core 词，或单实体导航型查询 1 条强命中；都不满足才继续变体/候补引擎。低相关候选收尾时原样返回并附提示，核心词命中路径按成功语义缓存，零核心命中路径短缓存。
- 超时预算：`SearchQuery.timeout` 由 router 按剩余预算下发（`round(remaining,3)`），HTTP transport 逐请求生效、MCP transport 取 `min(self.timeout, timeout)`；预算计时仍用 `time.perf_counter`。
- 确定性 ranker：`ScoreBreakdown` 逐特征贡献（标题核心 3/词元、摘要 1/词元、全核心覆盖 +2、intent 0.5/词、context 0.5/0.25、引擎内名次 1.2/0.6/0.3、跨引擎交叉 +1.0/+1.5、时效查询有 published +0.3）；平分保持首次入池顺序；无日期不伪造。
- engine 归属：`_map_result` 逐条读取 daemon 的 `engine`/`source` 字段，router 只回填空值（不再一律覆盖成槽位引擎）；daemon `partialFailures` 有部分结果时转成结构化 `SearchFailure`（`provider.last_partial_failures` 通道），不再静默吞掉。
- 缓存键升到 v7（排序策略变化即换键）；低相关候选短缓存、核心命中正常 TTL、失败永不入缓存的政策不变。
- 质量对比（同一固定候选池 vs 基线）：nDCG@5 0.7387→0.8944（+21% 相对），Success@5 0.5455→0.7273，P@5 0.1879→0.2364，误接受率 0.1515→0.0606；平均请求数 1.48→2.30（弱命中查询按设计多试候补，强命中仍单请求早停）。全量 653 测试绿。
- 遗留（P1 待办）：`SearchQuery.lang/freshness` 是否被 open-websearch daemon 支持尚未核实，透传前先验证；`SearchDiagnostics` 结构化诊断未做；模糊实体泛匹配误接受（如 se5「wjx-handler ai」命中 handler/ai 泛词）为词法门控的已知边界，按任务书不引入 LLM rerank。

## 免费联网搜索增强（2026-10-31，base/chat-stability）

- 引擎候补链：首选引擎空结果、结果与查询核心词不相关或命中过弱（质量门控未达标）或请求失败时，按 `search.fallback_engines`（默认 baidu→duckduckgo→sogou）自动降级，最多 `search.max_engines`（3）个引擎，共享 `search.timeout_budget`（25s）超时预算且单次请求超时不超过剩余预算；显式 `search_engine` 保持单次请求语义不变。引擎回退只对声明 `engines` 能力的 provider 生效（OpenWebSearchProvider 两种 transport 均已声明），provider 级降级顺序保留。
- 查询变体：`search`/MCP `search` 新增 `context` 参数（对话中确认的专有名词，最多 5 个）；原始查询无有效结果时确定性派生最多 2 个变体（追加 context 词项，不改写原拼写），不调用 LLM。
- 缓存分级：真空结果短缓存 5 分钟、「今天/最新/latest」类时效查询成功结果 10 分钟、低相关候选 10 分钟；任何上游失败一律不入缓存。`SearchCache.set` 支持按条目覆盖 TTL（不超过实例 TTL）；键版本见上方"搜索质量优化"（当前 v7）。
- 结果处理：统一候选池去重合并后按确定性 ranker 排序截断（见上方"搜索质量优化"）；`SearchResult.engine` 记录实际来源引擎并进入 payload；零核心命中时如实提示"候补引擎未找到…以下为候选"。
- 工具上限：search 工具 limit 硬上限 10→15（schema 同步）；默认值仍来自 `search.max_results`。
- 计时注意：Windows `time.monotonic` 分辨率约 15.6ms，搜索预算计时用 `time.perf_counter`，勿改回。
- 已知上游限制：open-websearch 无验证码识别（上游弹验证码表现为空结果或跑题内容）；bing 对小众英文账号（如 thsottiaux）可能返回无关内容，依赖 baidu 候补兜底；baidu 返回的是跳转链接（baidu.com/link?url=...，QM 不主动解析，需经 fetch_web）；fetch_web 偶发 daemon HTTP 502。真实冒烟 7 条查询对比与 LLM 端到端在 be6d632 前后完成（冒烟脚本在系统临时目录，不入库）。

## 多厂商搜索适配（Phase B，2026-10-31，base/chat-stability）

- 外部厂商：`baidu_search`（qianfan，双鉴权头可配置待冒烟定夺）、`tavily`、`exa`（type 固定 auto）、`you`（X-API-Key，日期字段未核实保持 None）、`volcengine`（global/custom 双模式）。注册表在 `router._EXTERNAL_PROVIDERS`，Key 走环境变量（如 `BAIDU_SEARCH_API_KEY`），未配置 Key 静默跳过；适配器全部经 `http_client.JsonApiClient`（非 2xx 一律抛 `ProviderError`，错误 detail 只含状态码不回传响应体）。全部 MockTransport 测试覆盖，真实冒烟未做。
- 请求预算：`search.max_requests`（默认 9）为一次搜索跨所有 provider 共享的请求次数上限；查询变体只在第一家 provider 上执行。耗尽时返回池内最佳候选并附截断提示，该结果**不写入缓存**（缓存会丢截断提示，下次搜索应重新请求）。`ProviderErrorKind.BUDGET_EXHAUSTED` 表示本地预算耗尽，与厂商账户配额 `QUOTA`（402 等）严格区分。
- 交叉来源统计：`CandidatePool` 排名证据为 `(provider, engine, rank)` 三元组，corroboration 按 distinct (provider, engine) 计数——外部厂商 engine 为空时凭 provider 身份参与统计；`SearchResult.engine` 语义不变（上游引擎名）。
- 火山引擎 Query 官方上限 100 字符：超限抛 `ProviderError(BAD_RESPONSE)`，不静默截断。
- 待办（v1.2 方案卡 09-14）：独立 FetchRouter 与四家 Fetch 适配、`qm setup` 搜索 Key 菜单、配额保护、文档同步；五家统一受控真实冒烟（每家 ≤10 次）待授权执行，冒烟后固化 Baidu 鉴权头、You.com 日期字段、火山 Custom NeedContent 的真实行为。
- FetchRouter（卡 09）：抓取调度从 `SearchRouter.fetch` 抽到 `infra/search/fetch_router.py`，SearchRouter 保留兼容入口内部委托；新增 `[fetch]` 配置（`providers`，默认 `["open_websearch"]`，与 search.providers 独立，旧配置缺省即原行为）；agent 的 `fetch_web` 工具名/参数/返回结构不变，显式注入 search_router 的旧 DI 路径保留。
- 四家 Fetch（卡 10）：`TavilyFetchProvider`（/extract，Bearer，单 URL Basic）、`ExaFetchProvider`（/contents，x-api-key，body 恰为 {urls, text:true}）、`YouFetchProvider`（/v1/contents，X-API-Key，{urls, formats:["markdown"]}，响应形状二手资料待冒烟）、`VolcengineFetchProvider`（/search_api/fetch，字段名按 Search 同族约定待冒烟）。正文经 `truncate_fetch_content`（base.py）截断；空正文/失败抛 ProviderError 由 FetchRouter 回退。注册表 `fetch_router._EXTERNAL_FETCH_PROVIDERS`，Key 环境变量与对应 Search 相同。
- Key 读取（卡 11）：`config.read_api_key`（env 优先、Keyring 回退）统一供 search/fetch registry 与 setup 使用；`qm setup` 顶层分「模型与 LLM」/「搜索与网页抓取」两个向导（providers.py 的 run_setup/_run_llm_setup/run_search_setup），官方厂商不自动启用、无 Key 拒绝启用，TOML 不落 Key。测试用假 ask + 假 questionary + 假 keyring 脚本化交互。
- 额度限制（卡 12）：`infra/search/usage.py` 的 `UsageLimiter`（`search.usage_limits.<provider>`：period daily/monthly + max_calls，max_calls<=0 视为不限制）；Search/Fetch 同厂商共用一个计数池，计数在实际发起请求前记账（失败保守计数），状态在 vault `.syvrio/usage.json` 原子落盘，读前按 mtime 重载以兼容两个路由实例；达上限的失败分类是 `quota`（detail 注明本地上限，与厂商配额语义一致但 detail 可区分）。setup 启用官方厂商时询问每日上限（默认 20）。本地计数不是服务商账单上限。
- 集成与冒烟（卡 13）：`tests/test_multi_provider.py` 覆盖五 Search + 四 Fetch 注册矩阵、Search/Fetch 顺序独立、无 Key 不注册不发请求、search/fetch_web 工具协议、冒烟脚本 dry/live（假件注入）；`scripts/smoke_search_apis.py` 默认 dry 零请求，`--live` 才对单一厂商发 ≤max-requests（默认 1）次真实请求，报告脱敏。真实网络冒烟仍未执行，待验证：百度鉴权头、You.com Contents 响应形状、火山 Fetch 字段名与 Custom NeedContent、各家 count 上限。

## 忙碌态解耦与会话一致性（2026-10-31，分支 base/chat-stability）

- **历史流程（已废止，不得执行）**：当时基础修复使用 `base/` 前缀，再分别合入 main 与 evolution。现行开发仅在 evolution（或其短期派生分支）；禁止自动向冻结 main 同步。
- 等待 LLM 期间输入保持可用、会话切换立即执行；等待期提交（消息与 /命令）进入全局 FIFO 队列，按提交顺序执行且任意时刻至多一个请求（并发第二个请求会被 exclusive worker 协作取消且 runtime.ask 无重入保护，故只能排队）；导航/只读命令（/new、/sessions、/exit 等）等待期直接执行。旧的全局 busy 布尔拆为 navigation_busy 与 ask_active 两态，输入框只在导航期间禁用。
- 数据一致性：`runtime.ask` 支持显式 session 并新增 on_session 回调；`_auto_recall`/`_flush_memories` 显式绑定请求会话，不再读 self.current；`last_plan` 带 session 标识，状态栏不串会话；后台任务执行时从磁盘新鲜加载会话对象，完成后若目标是当前会话则 reload 治愈内存快照；切走/新建时对生成中的会话延迟打 closed/distill_deferred 标记（保证排在最后的 assistant 记录之后）；生成期间跳过后台补沉淀，留给启动补记；删除守卫拒绝删除生成中或被队列引用的会话——物理删除后后台追加会原路重建僵尸文件（已用临时库脚本实证）。
- 切回生成中的会话：磁盘重建负责已完成轮次，界面只重放未落盘的活跃段（已完成段必然已随 assistant 记录落盘，天然去重）。
- 输入光标：原生光标仅用于支持 DECSCUSR 的终端（WT_SESSION/WT_PROFILE_ID/TERM_PROGRAM，conhost 退回自绘）；`on_resize` 重算 app.cursor_position，修复布局变化（折叠侧栏、窗口缩放）后硬件光标停在旧坐标的问题。rich 宽度表与终端在 emoji/歧义宽度字符上的渲染差异仍需真实 PowerShell 人工验证，headless 测试无法覆盖。
- 测试：新增 10 个回归（跨会话全局 FIFO 验收场景、等待期输入可用、立即切换、流式重放、关闭标记顺序、删除守卫、补沉淀跳过、光标终端门控与重算等）；同步修稳两个既有负载抖动测试——启动补记可能晚于测试打点触发 refresh_sessions（干扰计数与点击旧控件），driver.write 全量捕获会被帧渲染写入污染 `writes[-1]` 断言。
- 验证：全量 `uv run pytest` 592 passed 连续两轮；分支未合并、未推送。

## 环境变量污染事件与抓取超时（2026-10-31）

- **污染事件**：`QUIET_MEMO_CONFIG`/`QUIET_MEMO_VAULT` 曾被 `setx` 持久化进 Windows 用户注册表（本应只在手动测试启动器进程内注入），导致此后所有新进程（含 TUI 会话）加载测试配置与测试 vault，期间的会话沉淀写入了测试库。已从注册表删除这两个变量。教训：多环境隔离只能用进程级环境变量或启动器包装，禁止 `setx` 等持久化写入；已在运行的长驻程序需重启才脱离旧环境。
- **数据处置**：污染窗口期写入测试库的沉淀按用户决定留在测试库，不抢救、不回滚。定位（2026-10-31 拍板）：本地一切（vault、本地 personal-memory 克隆）按测试环境对待，当前记忆标准源是远程 personal-memory 仓库，终态 vault 单源化未到。
- **抓取超时**：真实网络排查确认抓取目标站响应 24-42 秒且上游间歇 502，原默认 `search.fetch_timeout=25s` 会先于上游掐断约一半尝试；默认提升至 45s，config 数据类、加载 fallback、setup 模板、示例配置、双语 README 与测试同步（`6c24553`）。已有配置文件若显式写 25.0 需手动改为 45.0；未写该键则自动获得新默认。
- **搜索侧结论**：当次 search 失败为 Bing 间歇 TLS 失败后回退 Baidu 撞验证码，属上游瞬时问题，未改代码；daemon 日志中 request-filtering-agent 的 CIDR 告警是已捕获的良性警告（即此前记录的上游地址族比较问题）。
- fetch_timeout 相关提交未经本轮本地全量回归（由用户直接提交推送），以 CI 双矩阵结果为准；后续改动仍遵循"全量 pytest 绿了才提交"。

## 检索评测重跑（2026-10-30）

- main 已完成合成库与真实库三路检索评测：456 条合成记忆/27 个固定用例，882 条真实有效事实/24 个模型生成问题。合成题集为固定用例；真实题集由模型生成，不属于人工标注金标准。
- 基准结果：合成 keyword/vector/hybrid 的 MRR@10 为 0.722/0.913/0.913；真实三路为 0.889/0.814/0.842，真实 Hit@5 均为 95.8%。固定记忆解析与裁决用例 24/24。
- 有效期探针中 main 仍返回过期和未来事实，时间过滤尚未实现；无个人记忆意图的通用问题仍会得到非空结果，尚未验证最终回答是否误用。后续优先评估召回门控。
- 私有题集、逐题明细和原始耗时只在本地忽略目录，不写入交接文件或提交；本次未修改产品代码或个人库。方法与完整结果见 `evals/README.md`。

## 0.3.2 发布核对

- 版本覆盖 `pyproject.toml`、`__init__.py`、`uv.lock` 根包与联网搜索 MCP 客户端标识；新增功能说明已同步双语 README，Release 汇总从 `v0.3.1.1` 以来的变更。
- 本地已构建 wheel 与源码包，检查版本、`documents` extra 和新增模块均正确，未包含本地配置、vault、会话、缓存或虚拟环境。
- 启动自动提交和补沉淀的 UI 测试改为等待后台任务完成再验证，不再依赖固定刷新次数或短时轮询；功能断言保持原样。
- 发布前全量：`uv run --frozen --offline --extra documents pytest -q`，536 passed；`uv lock --check --offline` 通过。双平台 CI 和 PyPI 发布状态需通过远端记录核对。
- Release published 触发 `.github/workflows/workflow.yml` 自动测试、构建并通过 OIDC 发布 PyPI；仅推送版本提交或标签不会发布。

## 模型与供应商选择界面

- `/model` 列表仅展示模型，供应商操作不再作为模型条目混排。模型面板底部提示 `Ctrl+P` 打开独立管理菜单（添加 / 更新 Key / 移除），`Ctrl+R` 刷新，`Ctrl+N` 输入自定义模型名；管理菜单按 Esc 返回模型列表。
- `ModelPickerScreen` 复用现有选择器，单独约束模型列表高度，为搜索框和快捷键提示留空间。`ChatApp` 仅在模型选择器中重定向 Ctrl+P，其他界面仍使用 Textual 原命令面板。后台模型列表刷新仍校验原选择器身份，不能覆盖已打开的供应商管理界面。
- 本次只调整界面编排，保留配置、凭据存储与模型切换逻辑；使用隔离 UI 测试验证，不再消耗真实模型请求。
- 验证：模型选择、供应商管理、返回、快捷键、后台刷新隔离及两种窗口尺寸均通过。早期界面分类回归出现过现有启动自动 Git 提交用例超时，相关 12 项复测通过；提交前最终全量 `uv run --frozen --offline --extra documents pytest -q` 为 536 passed。若再次遇到后台 Git 时序波动，应先检查任务实际完成状态。

## 文件分析与导入

- 用户可粘贴/拖入绝对文件路径并用自然语言要求分析或导入记忆。带空格路径需引号，拖放路径转换取决于终端。仅本条用户消息中显式给出的支持格式文件可用；不沿用历史消息或文件内路径授权，不开放目录读取。
- Agent 工具 `read_file` 按字符游标分页并限制输出 token；`import_file` 每次抽取一块，默认 dry-run，用户明确要求写入长期记忆时才设 apply。返回下一块游标，步数用尽须说明未完成。预览和实际导入都会产生模型抽取请求，用户应了解费用。
- `/import <path> [--apply]` 与 CLI `qm import` 支持当前层目录；TUI 在 worker 运行，默认预览，失败释放 busy 并接续待切换意图。只读取/分析文件不会自动把全文作为个人记忆存储。
- `infra/documents.py` 读取常见文本，复杂格式复用 MarkItDown 可选 `documents` extra；20 MiB 文件上限，未启用插件、云端解析、OCR 或音视频转写。JSON/JSONL 保留原始角色和时间信息，不承诺专用客户端语义适配或跨块完整性。
- `agent/file_tools.py` 隔离文件路径与本轮解析缓存；文件内容脱敏后进入模型工具结果，导入保留文件名及块编号。工具只提供给内置 Agent，不扩展对外 MCP server。
- 新行为使用合成文件、固定抽取和假模型验证。2026-10-29 经授权使用 Spark 做了两轮共 10 次真实请求（达到本轮上限），分别验证显式指定工具、只提供路径与自然语言意图的 read_file / import_file 链路，以及助手建议未被误写为用户事实；保留独立测试库及会话，不读取个人库。不继承本轮调用预算，后续真实测试须按当前授权执行。最终测试记录以本轮实际结果为准。
- 文件导入初轮全量验证为 530 passed；合并模型选择界面优化后的提交前最终全量为 536 passed。真实本地转换覆盖 HTML、DOCX、XLSX、PPTX，其他格式通过统一适配契约测试，不能当作每一种真实文件均已验证。已有两处异步界面测试改为等待目标状态；命令面板按最长用法计算列宽。新增解析依赖已安装到项目环境，日常启动器使用当前项目源码；相关功能纳入 0.3.2。

## 基础版本范围（444fe0f）

本轮实现 [#10](https://github.com/Shardkiht/Syvrio/issues/10)、[#11](https://github.com/Shardkiht/Syvrio/issues/11) 的基础部分，并建立 [#14](https://github.com/Shardkiht/Syvrio/issues/14) 的首批合成解析与裁决回归。未提高包版本，也未发布到 PyPI。

## 已实现

- 独立 `.venv` 与锁定依赖可安装，支持离线缓存；仓库提供 VS Code pytest 发现和调试配置。
- `MemoryContext` 包含可选事件日期、生效起止日期、时间原话、地点类型和显式情绪。日期支持年、月、日精度，检查非法日期和倒置区间。
- 普通抽取与导入使用同一上下文契约。缺少可靠时间锚点时只保留相对时间原话；语气推断不写入情绪事实。
- 上下文写在 Markdown 行尾 `context` JSON 标签中，沿用标签转义，旧格式无需迁移。候选排队、普通确认和 resolved 记录保留上下文；改写后确认及手动更正清除旧上下文。
- 正文和新增上下文共同经过凭据拦截。条目 ID、来源、软删除与替代关系保持现有语义。
- `MemoryService.propose_inference()` 只把假设追加到 `.syvrio/inferences.jsonl`，新记录为 `hypothesis`，包含类型、来源 ID 和带时区的生成/更新时间。
- 来源去重且必须是有效事实；同类型、同规范化文本、同证据集合重复登记返回原记录。推断不能支持另一推断，不进入 Markdown 事实库或普通召回。
- `evals/memory_eval.py` 提供固定合成输出的解析、冲突和上下文回归，并通过 `tests/test_memory_eval.py` 接入 pytest/CI；推断边界由独立领域测试覆盖。
- 离线验证发现并修复 token 截断在极小预算下超限的问题；字符估算分支按最终预算截断，tokenizer 分支避免截断产生无效 Unicode。
- 正式增加讯飞 Spark MaaS provider：配置向导、密钥环境变量、模型预设与 OpenAI 兼容调用；适配其采样默认值、输出上限以及 JSON 提示词约束。
- 修复 AI Markdown 回复无法选择复制：保留单控件绘制，通过带文本坐标的 Content 渲染选择高亮；覆盖流式、历史回复与窗口缩放。恢复会话成功不再弹出通知。

## 主要入口

| 文件 | 职责 |
| --- | --- |
| `memory/models.py` | 上下文和推断领域模型、字段校验 |
| `memory/extractor.py` | 普通抽取/导入的上下文契约 |
| `memory/store.py` | `context` 标签的读写与转义 |
| `memory/service.py` | 裁决、确认与推断证据检查 |
| `memory/inference.py` | 独立推断日志的追加和读取 |
| `evals/memory_eval.py` | 离线合成解析与裁决回归 |

以上源码路径相对 `src/quiet_memo/`，评测路径相对仓库根目录。

## 复现与调试

```bash
uv sync --frozen
uv run pytest
uv run pytest -m memory
uv run python evals/memory_eval.py
```

缓存齐全时可使用 `uv sync --frozen --offline`。网络受限环境可以下载与 `uv.lock` 对应的官方 wheel 并校验哈希，再用 `uv pip` 安装到项目 `.venv`；不要用全局包路径替代最终测试环境。共享缓存不可写时，项目默认的 `.cache/uv` 可独立使用。

VS Code 选择 `.venv` 后可从测试面板下断点，或使用 `.vscode/launch.json` 的当前文件/全量配置。不要运行真实 `qm` 会话验证测试：夹具会隔离配置、vault 和凭据，调试测试时也应保留这些护栏。

当前受限 Windows 执行环境禁止回环 TCP，默认 asyncio 在 `socketpair` 初始化时阻塞。测试专用开关 `QUIET_MEMO_TEST_PIPE_LOOP=1` 使用本地管道唤醒 Proactor，保持正常异步调度与跨线程回调；它不改变应用运行时或网络权限。普通终端与 CI 不必启用。当前环境同时用 `UV_FROZEN=true` 避免全局镜像配置触发重新解析依赖，用 `UV_OFFLINE=true` 复用已准备的锁定依赖。

## 本机手动测试

手动测试使用独立虚拟环境与配置，包以 editable 方式指向项目源码；修改保存后重启即可测试。不在交接文档保存本机路径。

测试 vault 是用户指定记忆库的独立副本，拥有自己的 Git 历史，无远程仓库；只复制长期记忆与人设，没有复制旧会话。新会话仅追加到副本的 `sessions/`。启动检查已验证原库文件哈希未变。

启动器通过进程环境变量隔离配置、vault、凭据与 tokenizer 缓存；凭据和个人配置仅存本地，不入库。

## 验证记录

- 本地 Windows / Python 3.12.14，独立 `.venv`，126 个运行/测试依赖符合锁定版本，未借用全局工具环境。
- `uv sync --frozen --offline`：通过。
- `uv run pytest -q -x --tb=short -o faulthandler_timeout=45`：478 passed，147.57 秒；使用上述受限 Windows 测试环境开关。
- `uv run python evals/memory_eval.py`：24/24 通过。
- `git diff --check`：通过。

Spark 真实接口合成数据冒烟：流式回复、带时间/地点的记忆抽取、`read_topic` 工具结果回传均通过，共 4 次请求。当前环境不能由 Python 直接建立网络连接，因此保留项目 LiteLLM/OpenAI 的请求序列化和响应解析，通过本地文件转交已授权网络客户端发送原始请求；这验证了 API 接线和响应形状，未验证普通终端的直连网络或逐帧延迟。测试凭据和冒烟产物均在本地忽略路径；未发送个人记忆。

普通 Windows 事件循环与 Ubuntu 的最终验证以 GitHub Actions 双矩阵结果为准。

## 当前剩余规划

- #10：已补原始消息时区偏移、逐消息抽取锚点与确定的相对日/月/年解析；复杂自然语言时间依赖抽取模型，未知或无时区旧记录不猜测。推断情绪仍待实现。
- #11：已补 CLI/TUI 查看、确认、拒绝、更正、来源失效、追加状态事件和显式损坏尾行恢复。来源审查与操作共用写锁；更正产生新假设，确认不变成事实。合入前保持 Issue 开放。
- #12：已实现事件日期范围、地点、显式情绪筛选，CLI/Agent/MCP 共用服务入口，先筛选再截断；无筛选保留原行为。当前/历史视图与显式演化已在功能分支实现；复杂时序重排仍待完善。
- #14：在程序契约回归之外增加真实模型语义抽取评估、时间演化、无关注入率、过时事实与推断误用指标。任何真实模型冒烟先约定预算。
- Dreaming 与画像基础功能已在独立功能分支实现，仍需长期语义评测与自动调度；跨客户端主动召回继续按 Issue 推进。

用户要求：本轮基础功能验证通过并提交后，另开 feature 分支集中完成较难的后续功能，供用户评估后合入；不得自动合并该 feature 分支。

## 独立功能分支进行中

分支 `codex/feature-memory-evolution` 基于 v0.3.2；主工作目录保持发布版本。用户授权先完成推断审查/来源失效、相对时间/时区、时间地点条件检索。不得自动合并。实现和验证结果以分支实际状态为准；未做真实模型调用。新增 CLI/TUI 推断审查和 recall 筛选参数，MCP/Agent recall 同步筛选。推断确认保持独立，状态追加可追溯；日志损坏时停止写入；CLI --recover-tail 仅显式恢复损坏尾行，保留全部字节并追加恢复标记。推断 TUI 的读写在 worker 执行，失败释放 busy 并接续待切换意图。后续状态见下方增量记录；跨客户端主动召回仍待实现。

开发测试须使用功能分支专属虚拟环境及配置/记忆库；Git worktree 不隔离用户配置，不能直接用默认配置启动功能分支的真实聊天。当前只运行夹具隔离的合成测试；没有读取或迁移个人库，没有真实模型请求。

本分支验证：推断/日期/Agent 专项回归 99 项通过；推断界面正常操作与异常恢复均通过。合成 200 条记忆，关键词条件检索预热后连续 10 次，耗时中位数 26.88 ms、最大 37.65 ms；仅代表本机该合成规模，不代表向量模型或大库性能。最终全量回归 560 passed；随后补充的故障注入与界面复测 23 passed（其中包含新增的两条审计保留用例，不与全量数量简单相加）。git diff --check 通过。所有测试记录与合成评测文件保留在忽略目录，不删除。

推断日志写入失败的边界：事实更正/软删除先保留审计，再同步推断失效；若失效追加失败，下次审查重新校验来源并补记。尾行恢复仅显式执行，不改写原文件。旧版 0.3.2 不理解 revision/recovery 推断事件，功能分支必须使用独立测试库，不能让两个版本共写同一库。本分支独立开发，现有主目录仍为发布版本；提交与推送状态以 Git 记录为准，合并须由用户评估。当时约定每次提交前运行全量测试；现行以 AGENTS.md 的按影响范围选测规则为准。

本次提交前完整回归：562 passed。模型搜索测试改为等待列表的最终可观察状态，保留原筛选断言，避免将异步移除/挂载尚未完成的一帧误判为失败。


## 后续高优先级功能（2026-10-29）

前三项已在 `368d5b3` 提交并推送到 `codex/feature-memory-evolution`，提交前全量 562 passed。用户随后明确授权自行 commit/push 并继续剩余高优先级开发；不授权自动合并主分支或再次发版。以下为该功能分支后续实现，最终提交状态以 Git 为准。

- #12：`qm evolve`、Agent/MCP evolve 区分状态变化与错误更正；当前召回排除已过期/未生效事实，历史日期和完整演化视图使用关键词快照，不污染向量缓存。删除/纠错不会复活前驱。明确分离的有效期不互判冲突，仍保留敏感/低可信候选审查。
- #9：CLI/TUI/MCP profile 从当前偏好、目标、约束、习惯、日程事实和独立画像推断重建；纯查看不调用模型。显式 refresh 产生待审查推断；只有来源相关、已确认的画像可进入聊天，最多 300 tokens。新来源指纹支持稳定 ID 下的手动正文修改失效；旧无指纹推断不能追溯历史编辑。
- #8：CLI dream 与 TUI /dream 显式启动增量回顾。默认一次请求，上限 20，禁用重试/候补模型，单次超时最多 60 秒；窗口至少 2048 tokens。TUI 运行期间可聊天/换会话；支持状态、取消、重启接续，预算 0 可发布已校验缓存而不请求模型。失败保留会话和任务日志，发布前重新校验证据。互斥仅覆盖同服务实例；不支持多个进程并发回顾同一库。
- 新增 `memory/reflection.py` 与 `app/chat_reflection.py`；领域模型仍集中 models.py。`reflection-jobs.jsonl` 追加批次摘要/候选/状态，截断尾行追加恢复标记，中间损坏停止。新推断类型 reflection 与 source_fingerprints、条目标签 change_kind 都不兼容旧版读取；仍须用独立测试库，不与 0.3.2 共写。
- 没有实现无人值守自动调度、原始会话直接回顾、整库所有关系穷举或跨客户端主动召回。既有会话沉淀先产生事实。真实模型语义效果未验证，本轮零真实请求，没有使用个人库。
- `evals/memory_eval.py` 新增合成时间/地点/情绪 Recall@1 与 MRR@5 对照；它验证筛选和历史视图契约，不代表真实用户准确率。新增回归覆盖预算、取消、失败接续、证据失效、画像注入与 TUI 可交互性。
- 中英文 README、命令表、MCP 列表、结构树与 AGENTS 同步更新。Issues 在功能分支验收/合入及剩余范围完成前保持开放。

最终提交前验证：全量 `uv run --frozen --offline --extra documents pytest -q` 为 591 passed（238.48 秒）；使用夹具隔离和测试专用管道事件循环。首轮 589 passed / 1 failed 暴露命令面板嵌套方括号转义问题，已改为直接组合 Content 文本与样式，并在完整复测中通过。画像日志故障不再阻断事实召回与聊天，错误可见。合成上下文评测：baseline Recall@1=0.2、MRR@5=0.5，contextual 均为 1.0；仅 5 个固定合成用例。变更文件的本机路径/凭据模式扫描无命中，git diff --check 通过。全部记录保留在本地忽略目录。此次仅本地 Windows 验证，尚无该提交的跨平台 CI 结论。

## 全项目审查后续

远程分支已由用户改名为 `feature-memory-evolution`，推送必须明确指定目标。审查覆盖 app/agent/memory/retrieval/llm/infra 的主要调用与异常路径；修复候选日志失败导致丢失、损坏向量缓存导致检索异常、缓存清理异常外溢、搜索默认引擎未传递、回顾缺失 proposals 被误判成功。时间写入收回 MemoryService；回顾改用公开服务方法，服务使用可重入写锁保持组合操作一致性。没有新增依赖或读取个人库。

本轮完整回归：594 passed，430.50 秒；git diff --check 通过。仅隔离合成测试，零真实模型请求。测试记录保留；本轮未验证跨平台 CI。

## Spark 真实数据诊断

用户明确授权真实 Vault 只读输入及 Spark 调用；诊断输出单独保存在本地忽略目录，不提交任何条目原文或个人配置。三次真实请求中一次超时、两次成功；复现确认异常类型为请求超时，失败批次保留，重跑后进度推进。原记忆文件哈希一致。画像预设主题无匹配，因此未触发该入口，不宣称画像真实数据验证通过，也未处理完整记忆库。错误分类补充 timeout/validation/storage/request，只记录类别与固定文案，不保存异常原文。

提交前全量回归：595 passed（331.73 秒）。真实数据原文与诊断输出均未暂存，测试和诊断记录保留。

## 自动沉淀定位修正

用户明确要求自动运行：TUI 默认启用 Dreaming/画像空闲轮转，补记结束后开始，空闲 60 秒、间隔 300 秒、每次启动自动请求预算 20；可配置关闭、暂停恢复。后台输出仍须审查，不自动修改事实。此前文档中的“仅手动、自动调度未实现”描述被本节取代。退出进程无后台守护。

自动调度提交前完整回归：597 passed（365.38 秒）；覆盖忙碌延后、暂停/恢复、请求预算耗尽、配置关闭和输入草稿不触发。git diff --check 通过。没有新增真实模型请求或改写个人库。功能仅在 evolution 分支生效，主目录启动器仍指向发布版本。


## TUI 命令精简

按用户要求同步 main 和 evolution：仅保留 /sessions 会话入口，移除 /resume、/recall、/remember 的面板与分发及闲置处理函数。自然语言记忆仍通过 Agent recall/memorize 工具，CLI 管理入口保留。导入完成回调改为按用途命名；中英文 README 同步。

命令精简验证：evolution 全量 598 passed（441.86 秒）。 测试记录保留，git diff --check 通过。

## 简单优先：画像主题兼容

画像提炼移除英文主题白名单，跨全部有效事实筛选；输入带主题帮助理解。画像事实视图仅展示常见中英文主题与有效画像推断引用的来源，拒绝/失效后自定义主题来源不再留在画像；事实本身不变。后续可优先补配置界面与后台进度展示，复杂跨客户端召回继续后置。

画像主题兼容验证：全量 600 passed（385.63 秒）；真实 Vault 只读快照进行一次 Spark 画像请求，正常结束并生成 6 条来源校验通过的待审查候选。仅完成一个批次，不代表整库完成；原始记忆文件哈希未变。原文、候选和诊断文件仅在本地忽略目录保存，不进入提交。

命令精简验证：main 全量 537 passed（375.37 秒）；首轮临时目录缺失导致的准备错误已修复并完整复测。 测试记录保留，git diff --check 通过。

## 搜索基础兼容同步（2026-10-30）

- 搜索与抓取属于两分支共用基础能力，修复同步 main 与 feature-memory-evolution；记忆演化、画像和自动回顾仍仅在功能分支领先，不合并整条功能分支。
- 域名解析默认兼容代理合成 DNS 段 `198.18.0.0/15` 与 `2001:2::/48`，自动启动的搜索 daemon 继承相同 `FAKE_IP_CIDRS`。显式设置覆盖默认值，空值关闭；已有外部 daemon 不自动重启，需手动同步环境。
- 合成 DNS 例外只用于域名解析结果，直接填写非公网 IP 仍拒绝，混合返回中存在其他私网地址仍拒绝，重定向终点继续复验。双语 README 与回归测试同步。
- 开发使用功能分支自己的虚拟环境与空测试库，显式覆盖继承的配置变量；全局命令继续引用 main。开发配置与入口均在忽略目录，不保存个人库或真实凭据。
- 本次验证：main 全量 556 passed；功能分支全量 619 passed。直接公开网页请求返回 HTTP 200，URL 校验通过；未运行经 daemon 的端到端抓取，测试环境未发现可用 npx 或运行中的服务。零模型请求，专属测试临时目录已清理。
- 后续已补齐 Node/npm/npx 与 open-websearch，自动启动改为优先已安装命令，缺失时才回退 npx。真实服务冒烟两分支均返回搜索结果并抓取到公开页面正文，取代上方“尚未验证”的旧边界；测试进程和日志已清理。open-websearch 2.2.0 的混合 IPv4/IPv6 CIDR 比较存在上游问题，本机安装包已修正为同地址族比较；此修补不在 Python 包内，升级上游服务时需复核。
- 安装与命令选择调整后的最终全量回归：main 558 passed，feature-memory-evolution 621 passed；真实搜索/抓取两个分支均通过，零模型请求，测试临时目录均已清理。
- Spark 联网 Agent 真实冒烟：main 2 次请求、feature-memory-evolution 3 次请求，共 5 次，关闭重试和候补模型。该次冒烟只向模型提供联网工具，未覆盖记忆工具与联网工具混合注册的生产场景。两边均由模型选择 search/fetch_web，实际执行搜索与两次网页抓取，工具结果回传后生成带来源链接的回答；使用空临时库，事实写入数为 0。直接抓取覆盖 tibo.cc、Python asyncio 文档、GitHub 仓库页、MDN HTTP 文档、中文维基百科，两个分支均成功；知乎公开主题页远端 HTTP 403，上游服务将其包装为 502，属于远端访问限制而非本地 DNS 拦截。未做浏览器渲染或登录态访问。测试服务已停止，临时会话与日志已清理；本轮未改产品代码。

- 两分支自动召回增加轻量个人话题/连续追问门控，通用问题跳过预取与查询改写；模型主动 recall 保留。规则存在隐式个人意图漏判边界，不将关键词判断视为完整语义分类。


## 请求进度与状态栏

- 发送后立即在模型名称旁显示活动条、阶段和经过秒数；上下文用量独立右对齐。活动条表示忙碌，不代表完成百分比。
- Agent 的可选 on_progress 回调在上下文准备、模型请求及工具执行前通知界面；工具开始与结果分别展示，异常仍走原有收尾释放 busy。
- 两分支同步基础体验；按需召回跳过通用问题，模型自身首字等待仍取决于供应商。
- 最终隔离全量回归（2026-10-31）：main 572 passed，feature-memory-evolution 635 passed；覆盖工具执行前进度、两种窗口宽度、状态栏卸载后的定时刷新。此次界面改动未做真实模型延迟基准，不将加载提示视为模型耗时缩短。


## TUI language and search errors

- Fixed TUI labels and progress are English; model/user content retains its original language.
- Search accepts JSON-encoded engine arrays from model arguments. Empty results with upstream partialFailures now raise an error and are not cached; default Bing HTTP failures fall back to Baidu once, explicit engines remain unchanged.
- Reproduced upstream Bing TLS failure returned in partialFailures with HTTP 200. Live tool smoke on both branches returned three results for default search and JSON-encoded Baidu arguments; temporary vaults removed, no model calls. Cache keys are versioned to bypass legacy false-empty entries.
- Validation: full isolated suite main 580 passed / evolution 643 passed; after the cache-key update, all 81 search tests passed on each branch. Fixed TUI string literals are checked for unintended Chinese text; user and model content are unaffected.


## Windows IME cursor

- Windows interactive TUI input uses a native blinking bar cursor, leaving IME composition placement to the terminal; headless and other platforms retain the painted cursor. Blur/unmount hide the native cursor and reset its shape. Native IME behavior requires manual terminal verification; headless tests only cover cursor control, editing coordinates and cleanup.


## Review follow-up: queued targets and search budget

- Pending commands retain the full session-bound queue item and recheck its target after intervening navigation; synchronous completion resumes FIFO processing. Pending targets are covered by the session deletion guard.
- Search budget exhaustion stops further requests while retaining collected candidates with the existing candidate hint and timeout diagnostics.
- Validation: targeted queue/budget regressions 5 passed; full isolated suite 617 passed. No real model or network requests were used.


## Input caret end spacing

- BarInput removes Textual Input's extra end-of-value scroll cell from the terminal caret coordinate; scrolling retains its existing padding. Empty input and trailing user spaces retain their actual insertion positions. Regression coverage includes Chinese, mixed ASCII/CJK, middle edits and literal trailing spaces.
- Validation: insertion-coordinate cases and native cursor checks passed; full isolated suites base 623 passed, evolution working tree 651 passed. Real terminal/IME rendering remains a manual check.


## Generation cancellation and visible send queue

- Esc stops the active generation, preserving the draft and partial response in the append-only session. Open palettes/modals consume Esc first. TUI streaming uses cancellable async transport inside the worker, including first-token waits; no second request starts until cleanup completes.
- Enter during generation appends to the session-bound FIFO, shown above the input. Completion or cancellation drains it automatically. The queue is process-local, not persisted across exit.
- Active synchronous context preparation/tools finish safely; remaining tools receive cancelled results to keep tool-call pairs valid. Completed writes are not rolled back. UI text remains English.

- Validation: final full isolated suites base/main 632 passed, evolution working tree 672 passed. Installed LiteLLM async stream wiring tested with local mock responses, including repeated requests; cancellation covers first-token waits, partial output, draft preservation and tool-result pairing. No live model/network verification or evaluation-data changes. Evolution retains its pre-existing uncommitted work and memory-specific behavior.


## Search / Fetch final wiring fixes

- Normal Agent tool construction builds FetchRouter independently of SearchRouter; explicit legacy router injection remains supported and its fetch compatibility entry shares the usage limiter.
- Request-count budget is checked before provider quota accounting. Failed dispatched requests still count conservatively.
- Official fetch adapters receive search.fetch_timeout; search adapters retain search.timeout. Setup exposes Fetch only for implemented fetch providers.
- You Contents parses the documented top-level array (https://you.com/docs/api-reference/contents), without livecrawl or additional formats. Search parsing and the shared JSON client remain unchanged.
- Offline comparison against the starting HEAD preserved all search metrics. That offline-only pass made no paid requests; the later authorized real API verification is recorded below.

- Validation: affected provider/setup/usage suite 121 passed; full isolated pytest 775 passed. Offline Search Eval unchanged: P@5 0.2364, Success@5/MRR@5 0.7273, NDCG@5 0.8944, false acceptance 0.0606, requests/query 2.303. Temporary baseline removed; evaluation inputs unchanged.


## Search / Fetch real API smoke follow-up (2026-11-01)

- Authorized smoke used process-local credentials and disposable empty vaults through normal build_tools / AgentRuntime paths. Five official Search providers returned results; Tavily, Exa, You Contents and open-websearch fetched public page content.
- Volcengine Fetch returned HTTP 200 but exposed an incorrect assumed response mapping. The official Fetch documentation and live response agree on Result.Data.StatusCode / ContentText / RedirectedUrl / Title. The adapter now reads those fields, rejects target-page HTTP errors, and preserves router URL checks and existing usage accounting.
- Corrected Volcengine Fetch succeeded through build_tools on a public page. A confirmed Spark Agent run used two model requests, executed Tavily search and fetch_web, then produced a linked answer; the temporary vault contained no memory files. Mock regressions cover the documented response, redirects, truncation, empty content and target HTTP 403/404/500. Official reference: https://docs.volcengine.com/docs/networked-search/doubao-search-web-analysis-fetch?lang=zh
- Smoke scope does not establish all count limits, Custom search behavior, dynamic-page rendering or every target website. Credentials were read only; no user vault or persistent configuration was changed.
- Final validation: provider suite 70 passed; full isolated pytest 778 passed (one existing Pydantic warning). Search Eval retained P@5 0.2364, Success@5/MRR@5 0.7273, NDCG@5 0.8944, false acceptance 0.0606 and requests/query 2.303. Evaluation inputs were unchanged; disposable smoke directories were removed.


## Base synchronization (2026-11-01)

- main fast-forwarded to base/chat-stability c6d8aee. Evolution integrates the same base while retaining its temporal memory, profile and reflection features. Search / Fetch implementation matches base.
- The paused merge was snapshotted and rebuilt against the latest base. Original local README, reflection UI and reflection-test changes remain outside the merge commit. The original preservation stash remains available.
- Adapted the corrupt-profile recall test to use an explicit personal question under the automatic-recall gate; its corruption and fact-retrieval assertions remain unchanged.
- Final isolated full suites: main 778 passed; evolution working tree with retained local edits 841 passed. Both Search Eval runs retain the same quality and request metrics. One intermittent native-cursor test failed during a prior run, passed its focused retry and the final full suite; no product change was made for it. No live API calls or evaluation-data changes were made for synchronization.


## Review follow-up: committed English reflection UI (2026-11-01)

- Committed the reflection UI English labels previously present only in the local working tree; normalized the status line punctuation. No reflection logic or user/model content changed.
- Validation used a disposable archive of the committed evolution tree with only this source fix overlaid, excluding all unrelated local README and reflection-test edits: full pytest 841 passed, one existing dependency warning. The archive was removed after testing.
- Search result-count clamping and loss of low-relevance hints on cache hits were reproduced with synthetic providers during review; left unchanged pending user review. No live API calls or user-vault access.

## Search review fixes (2026-11-01)

- Explicit search limits override the configured default up to 15. Cached result hints are recomputed using the same query analysis, retaining low-relevance warnings without changing cache storage. Regression tests use synthetic providers and verify no second upstream request on cache hits.
- Validation: search suite 242 passed, full base suite 782 passed. Search Eval unchanged. Normal build_tools live smoke returned 15 You results and repeated search used cache without another request; fetch succeeded. Spark Agent completed search/fetch and a linked answer in two model requests. Three You requests were added (five including prior smoke), temporary vault removed; no user vault or persistent credentials changed.


- Evolution review follow-up also pins the profile-test proposal to its intended source ID instead of depending on randomly ordered IDs.
- Final evolution validation: full isolated suite 845 passed, one existing dependency warning; this tested the exact source/test contents prepared for commit, with unrelated README edits stashed. Evolution live build_tools smoke also returned 15 You results, reused cache without another request, and fetched the public example page. Across both environments this follow-up used five You requests (seven including prior smoke) and two Spark model requests. Search Eval unchanged; temporary smoke vaults removed.

## Dual-period usage protection and official quota pauses (2026-11-01)

- Defaults live in config.DEFAULT_USAGE_LIMITS. Local calendar day/month counters are independent and shared by Search/Fetch. UsageLimiter.acquire atomically checks and records under a process-wide path lock; persistence reloads under that lock. Cross-process transactional accounting is intentionally unsupported.
- Legacy TOML overrides defaults; legacy usage windows retain only the known count. The unknown other period is labeled; new requests count in both periods. No invented historical totals. Pauses persist alongside counts; manual resume does not reset usage, and reliable timestamps expire on access.
- Setup quota management shows defaults/effective caps, usage/remaining, unknown historical periods and pause reasons; edits accept zero, reset uses centralized defaults, manual resume clears only official pauses.
- Official evidence checked: Baidu search response contract https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy and AI Search errors https://ai.baidu.com/ai-doc/AppBuilder/4m8sgh88y distinguish QUOTA_USER_DAILY_REQUEST, QUOTA_USER_DAILY_FREE, BILLING_INSUFFICIENT_BALANCE from RATE_LIMIT_*_QPS. The specified local daily 50 is not a claim about official free pricing.
- Tavily https://help.tavily.com/articles/8645538886-understanding-http-errors documents 432 plan / 433 PAYG limits versus ordinary 429. Exa https://exa.ai/docs/admin/error-codes documents 402 NO_MORE_CREDITS / API_KEY_BUDGET_EXCEEDED / TEAM_BUDGET_EXCEEDED; payment-protocol challenges and unknown tags are not persistent quota evidence.
- You https://you.com/docs/using-the-api/error-code-reference and https://about.you.com/resources/web-search-api-authentication-header document keyed 402 Insufficient credits; payment challenges are excluded. Volcengine Fetch https://docs.volcengine.com/docs/networked-search/doubao-search-web-analysis-fetch?lang=zh documents 10406 free quota and 10412 search package exhaustion; no unverified generic Search codes are added.
- Exact account reset instants/timezones were not confirmed by those sources. Recognized provider quota pauses therefore require manual resume, including Baidu daily codes; no midnight/month-start is guessed. The limiter supports expiry for a reliable timestamp when one is supplied. Official pauses are distinct from local cap failures; unknown failures retain fallback without a persistent pause.
- Only safe machine error codes are retained from HTTP failure payloads; full upstream error responses/messages are not exposed. No real API calls, key access or user-vault modifications were performed during offline validation.
- Final offline validation: 817 passed, one existing Pydantic warning. Search Eval retained P@5 0.2364, Success@5/MRR@5 0.7273, NDCG@5 0.8944, false acceptance 0.0606 and requests/query 2.303. Inputs unchanged. Real TUI/API validation is a separate user-approved phase; sessions must remain visible in the shared local test environment.


- Evolution integrates the dual-period quota and official-pause base update while preserving memory-specific configuration and behavior.
- Quota-update merge validation: evolution full isolated pytest 880 passed (one existing dependency warning); search implementation matches main/base bc661bb and Search Eval metrics are unchanged. Unrelated local README edits were excluded from validation and the merge commit. No real API calls were made during synchronization.

## TUI Markdown rendering (2026-11-01)

- Reproduced the quoted-CJK emphasis example with the installed Rich/markdown-it parser: CommonMark punctuation flanking leaves double-star markers literal beside CJK text. Reply-only compatibility adjusts double-star delimiter flags at CJK/punctuation boundaries, preserving markdown-it pairing, code/escapes and full-document reference context. Stored Markdown is untouched.
- Scoped reply styles replace magenta/underlined headings with left-aligned slate-blue/neutral headings. Heading-to-body spacing and code-block vertical padding are reduced; code uses restrained dark syntax styling and preserves source indentation/trailing spaces. Rich continues to handle lists, tables, quotes, rules and terminal link metadata.
- The Content conversion remains the single rendering/selection source. Copy still means rendered display text, including display wrapping, not original Markdown. Exact duplicate ChatReply updates reuse the visual; stream coalescing and session storage are unchanged. No new rendering dependency, model/API call or user-vault access.
- Local visual validation includes Textual SVG screenshots and equivalent Rich before/after PNGs for 80/120/160 columns (reply widths 47/87/127). The synthetic sample uses 39→35 lines at 80 columns, 38→34 at 120/160. Terminal fonts, italic support, soft-wrapped code copying and link activation remain terminal-dependent; no new link interaction or copy mode was added.
- Validation: UI suite 128 passed; focused final Markdown suite 25 passed; full isolated pytest 842 passed with one existing dependency warning. Truecolor Textual screenshots explicitly override process-local NO_COLOR for visual inspection only; 80/120/160-column SVGs confirm slate heading color and no literal emphasis markers in the sample. CJK and existing assistant selection/Ctrl+C tests pass; stored Markdown remains byte-for-byte unchanged. No user configuration, Vault, credentials or APIs were accessed.

- Markdown merge validation: evolution full isolated pytest 905 passed with one existing dependency warning. Reply Markdown rendering/theme match main/base 3dd03ca, while evolution retains its existing command-panel and memory functionality. Local README edits remain outside the merge commit; no real API calls were made for this merge.

## Full pending review content (2026-11-01)

- Pending action review no longer truncates the item to 44 characters. PickScreen accepts optional plain-text detail in an independently scrollable area; the expanded card uses up to 90% width / 85% height with a bounded width. Approval/edit/rejection actions remain below the body.
- Detail mode supports mouse-wheel scrolling and PgUp/PgDn without moving the action selection; other pickers retain their existing layout and bindings. Markup-like content is literal. Pending list rows remain concise summaries; review and edit receive the full original text.
- Synthetic UI regressions cover 80x24, 120x40 and 160x50 layouts, pagination, cancellation, full-text approval, full-text edit defaults and rejection. No API calls, user-vault access or memory/session format changes.
- Validation: focused pending suite 9 passed; full isolated base suite 848 passed with one existing dependency warning. Synthetic rendered SVG checks at all three sizes confirm mouse-wheel scrolling, the final body line and visible action labels. No real APIs or user data were used.
- Pending merge validation: full isolated evolution suite 911 passed with one existing dependency warning. Existing README changes remain unstaged; no real API calls were made.

## Compact pending confirmation (2026-11-01)

- Replaced fixed 85% dialog height with content-driven sizing. Width is capped at 76 columns / 90% of the terminal; the body grows with content up to 30% of terminal height, then scrolls independently. Short confirmations no longer reserve a large empty area; action controls remain visible.
- Regression coverage at 80/120/160 columns verifies one-line bodies remain one line with no scrollbar, while existing full-text pagination and action tests remain unchanged.
- AGENTS.md now records compact, content-driven temporary dialogs as a UI invariant, including bounded scrolling and visible action controls.
- Per user direction, AGENTS.md now requires tests selected by impact instead of automatic full-suite runs before commits/branch synchronization. Pending suite: 12 passed; Markdown suite: 25 passed on rerun. An earlier full run had 850 passed and one Markdown copy/wrapping failure; the repeated full run completed with 851 passed and one existing warning before the cancellation arrived. Subsequent verification is targeted only.


## Final stability fixes and freeze acceptance (2026-11-01)

- F1: validate complete JSON record shapes, retain direct-load errors and original bytes, and log per-file batch errors without aborting healthy session listing or consolidation. Existing torn-tail recovery is unchanged.
- F2: scoped in-memory snapshots compensate ordinary exceptions in manual approve/correct/reject/forget, including partial decision-log writes. Resolution is written before pending removal. Only affected topic/log paths are snapshotted under the existing writer lock; no storage format, durable journal or transaction framework is added. Failed rollback raises both errors. Process kill/power loss and simultaneous writers remain unsupported transaction boundaries, explicitly documented in both READMEs.
- F3: cross-process quota atomicity remains a documented limitation. F4: optional open-websearch latest fallback remains unpinned; no clean upstream version has been verified in this round, so no guessed pin is introduced. F5: exclusive session file creation closes the exists/write race. F6: portfolio tag identifies source independently of package 0.3.2 and its older Release; evaluation data and historical metrics are untouched.
- CI runs Windows/Ubuntu tests, build and CLI help, including pushes to evolution. All local verification uses temporary Vaults, static extractors and fake models. Native terminal IME/clipboard appearance remains a manual acceptance item; headless UI checks do not establish it.
- Build inspection found Hatch's default source archive included an existing untracked local script. The sdist now uses an explicit public project-content allowlist and excludes the entire one-off scripts directory; the local script itself was not opened or changed. Wheel package selection is unchanged.
- User requested the untracked script be moved outside the repository; it was moved with a matching SHA256, without opening its content. The source-archive allowlist remains as a packaging safeguard.
- Full validation exposed an existing Markdown copy assertion using a pre-scrollbar-layout snapshot. The assertion now compares clipboard text to the current rendered width after Ctrl+C; rendering and copy behavior are unchanged.
- Local final acceptance: 871 passed, 4 optional document tests skipped, one existing dependency warning; sync/build and disposable installed-wheel CLI succeeded. Temporary install environments were removed. Manual native terminal checks and remote PR/CI/tag/cleanup remain separate gates.
- Remote main b2057c4 CI failed only its Windows native-input cursor test: the after-refresh focus callback had not fired before inspecting captured writes. The test now waits for the actual cursor-show event with a bounded timeout and retains focus/active-state/sequence assertions; product cursor code is unchanged.
- PR Windows CI exposed another test readiness race: the request-active UI flag precedes worker-side session creation. The queue/cancellation test helper now waits for both an active request and an existing runtime session before dereferencing current; production request/queue behavior is unchanged.
- Dual-platform CLI smoke exposed a real English-Windows redirected-output failure: cp1252 could not encode Chinese help. The CLI entry point configures only non-TTY Windows stdout/stderr to UTF-8 in-process; native console handling and persistent environment stay unchanged. A real subprocess regression forces cp1252:strict and checks UTF-8 help output.


## Evolution absorbs the stable main snapshot

- Merged only main -> evolution after the stable main PR and final-SHA CI passed and the portfolio tag was pushed. Retain temporal retrieval, reflection, inference review/fingerprints and dated-session behavior; no evolution features enter main.
- Conflict resolution preserves inference validation/invalidation in manual correction and soft-delete. Existing evolution tests establish that completed source facts and their audit survive a later inference append failure; subsequent inference reads repair derived status. Accordingly source/audit writes receive main's exception rollback, while inference invalidation remains after that boundary. The existing source-audit failure regression is retained and strengthened; no inference format or commit semantics are changed.
- Shared queue keeps main's rollback and log-before-removal behavior plus evolution's existing log-failure test. Current branch/freeze instructions replace the obsolete base-first workflow; historical evolution planning remains recorded.
- Evolution merge validation: memory-evolution/reflection/inference/context/service suite 125 passed; focused TUI queue/pending/cancel/cursor suite 24 passed. Shared session/queue/Agent/CLI/Markdown regressions also passed before resolving the inference commit-boundary difference, and the affected service paths were rerun in the 125-test group. At that historical checkpoint, main was unchanged after tagging and evolution push CI was still the synchronization gate. Current release/branch status is recorded at the top.

## Evolution 视觉区分（2026-11-02）

TUI 改为深蓝灰面板、薄荷绿焦点/当前会话、淡紫角色/模型标签、暖金快捷键提示；回复 Markdown 同步配色。布局、键位、数据和命令行为保留。配色集中 chat_theme.py，固定 TUI 文案继续英文。只同步 evolution，不修改冻结 main。

视觉验证：相关焦点、当前会话、命令面板、弹窗布局与 Markdown 渲染 40 项通过；合成聊天、命令面板和会话选择器已导出预览，聊天配色已渲染检查。预览仅在本地忽略目录，未使用真实 Vault 或调用服务。主目录日常启动器已使用 evolution，重启即可加载新样式。

## 视觉偏好修正（2026-11-02）

用户明确反馈蓝色背景不好看，改回中性灰：主背景、侧栏、面板、消息气泡、选中行、弹窗与代码底色全部使用灰阶，边框与次要文字同步中性化；仅焦点/当前会话与少量角色标签保留强调色。此前深蓝灰配色已被本节取代。

## 对话界面层级调整（2026-11-02）

用户要求参考终端对话产品的设计，保留灰背景。当前使用单一暖色强调，欢迎内容聚合居中、首条用户消息清除欢迎态；用户内容改为左对齐全宽灰面板，助手与工具事件弱化边框和标签颜色。输入区变为平面面板，用左侧焦点线提示，底部显示模型/上下文/快捷键。保留输入提示上沿位置以兼容 IME。TranscriptView 清理时同时移除 home 样式，历史和实时对话共用排版。新增窄/宽窗口欢迎态转对话回归，旧配色章节仅为历史。未使用真实数据，合成预览仅保存在忽略目录。

界面验证：整组聊天/Markdown 回归首轮 144 passed、1 个会话弹窗弱标记断言失败；恢复当前项灰线与焦点暖线区分后，相关 6 项复测全部通过（含 80/120 列欢迎态转换、输入焦点和命令浮层）。欢迎页、对话页及弹窗均以合成会话导出预览，已检查实际渲染；未调用真实服务或使用参考截图中的内容。git diff --check 通过。

## 强调色确认（2026-11-02）

用户选择石墨灰背景与清晰天蓝强调 #6CB6FF，替换暖色，用于焦点、选中标记、链接及少量品牌文字。标题/正文/角色标签保留灰白，提示恢复灰色；布局不变。此前暖色说明仅为历史。

启动侧栏按用户要求默认收起，Ctrl+B/展开控件保持可用；会话切换不强制修改当前展开状态。

验证：配色相关 31 项通过；整组界面/Markdown 首轮 144 passed，1 个直接点击已隐藏侧栏按钮的旧测试失败。测试显式展开后，相关 5 项复测全部通过。实际天蓝/收起预览已检查，未调用真实服务。

## 首页实用内容与远程失败核对（2026-11-02）

用户提供的旧 Actions run 41384553579 在早期薄荷配色测试失败：当前会话灰标记预期与强调色实现不符，已在后续提交修正。最新 run 41397436953 的 Debian 测试/构建/CLI 均成功，不对旧失败记录反复重跑。

首页改为宽度受限、靠上显示的本地概况、最近三个非空会话、可编辑对话示例。HomePanel worker 读取当前有效事实与待审查数量、会话摘要元信息，UI 线程挂载；标题按纯文本处理。点击会话复用切换逻辑，点击示例只填输入，不自动请求模型。首页移除后忽略迟到结果，读盘失败有提示，窄窗口滚动不遮输入。实现仍在现有 chat_widgets.py，未新增依赖或读取个人库测试；预览仅使用合成资料。

首页验证：聊天界面/Markdown 整组 147 passed（226.70 秒），首批首页与焦点专项 8 passed。合成首页已渲染检查，最近会话和示例在常见窗口可见，窄窗口使用滚动；未调用真实模型/搜索或读取个人库。

## 首页最终字标：QuietMemo（2026-11-02）

用户明确拼写 QuietMemo，Q/M 大写、其余小写，小写字母较矮并基线对齐。首页移除最近会话、统计、示例与概况后台读取，仅保留五行块状 QuietMemo 字标和一句提示；固定39列，适配窄终端。Quiet灰白/Memo天蓝，灰色背景与默认收起侧栏保留。侧栏/窗口标题同步QuietMemo，包名和CLI不变。此前数据卡片首页已被简洁版本取代。

最终 QuietMemo 字标的50/120列显示、居中、欢迎态退出、侧栏切换及命令浮层相关7项通过，实际预览已检查。首页不再有数据读取worker，无新增依赖或真实服务调用。

## 当前本地视觉调整（2026-11-02，待用户确认）

用户反馈大字标蓝色太跳，QuietMemo 字标统一柔灰 #B0B0B0，保持Q/M大写与较矮小写。交互焦点/链接保留现有天蓝。用户明确要求不得擅自 commit 或 push：本次只保留本地改动与预览，后续提交、推送须有明确指令，不沿用此前自动提交推送安排。

## 本地字形细化（2026-11-02，未提交）

用户明确指出问题是实心像素字笔画糊在一起。当前将字标改为细线轮廓、扩大e/m/o字腔，仍拼写QuietMemo且Q/M更高、整体柔灰；51列不足时回退完整普通标题，避免裁切。继续仅本地修改和预览，未经明确要求不得提交推送。

## 本地选择与流式滚动修复（未提交）

用户反馈拖选时光标跳动、上滑会被流式输出拉回底部。TranscriptView 跟踪跟随意图，向上移动暂停、回底部恢复，真正滚动前再次检查拖选/选区，避免排队刷新抢回位置。选择正文时隐藏输入硬件光标并暂停绘制，点击输入或按键恢复。新用户消息与清空视图重置跟随；用户内容左线改天蓝。另修复首页移除时迟到字标回调找不到子控件的问题。本轮仍不得commit/push。

本地验证：流式/选择/光标/字标/欢迎态相关21项模拟交互测试通过，包含真实鼠标事件路径与模拟硬件光标；未进行原生终端人工拖选验收，未调用真实外部服务。

## 回复样式本地预览（未提交）

用户要求先提交推送已有选择/滚动/字标改动，已保存为559d1c2。后续回复样式先本地验证：Markdown设置明确的柔白正文基色，蓝色标题、提亮加粗、淡金行内代码、灰蓝引用、蓝色链接及语法高亮代码块。作用域仅回复Theme，不强制覆盖NO_COLOR；新增语义颜色回归。

## 阅读交互整链路复查（本地未提交）

用户再次反馈流式输出期间滚轮/鼠标位置异常。此前只在正文MouseDown暂停输入光标，遗漏被滚动控件消费的滚轮事件；同时只阻止scroll_end，仍每次替换Markdown，阅读/选择中的布局会变化。现由TranscriptView接收滚轮阅读意图并暂停光标，ChatReply在上滑、选区或滚动条抓取时保留当前渲染、缓存最新全文，滚到底部后flush；流式完成也使用同一缓冲规则，原始会话仍正常追加。Focus回调不再取消阅读暂停，防止后台完成时重现硬件光标。使用公开事件处理器由Textual继续默认滚动，避免手动super导致滚轮处理两次。新增连续更新、流式完成、选区复制、实际滚动条抓取的组合回归；未进行原生终端人工验收或真实服务调用。未经用户明确要求不得提交推送。

整链路复查验证：完整聊天UI/Markdown回归154 passed（213.27秒）；新增滚轮持续输出8次、最终定稿、焦点回调不重启硬件光标、选区文本复制稳定、真实滚动条抓取等模拟交互全部通过。未声称原生终端人工验收完成，当前改动（含之前本地回复语义配色）仍未提交推送。

## 本地诊断日志（未提交）

用户要求增加日志，并明确要求本机排障保留详细异常。新增 infra/diagnostics.py，由 qm 入口启动/关闭 QueueListener，默认配置目录旁 logs/quiet-memo.log 每行 JSON，2 MiB + 三个轮转备份，写盘在后台线程，满队列丢弃诊断事件以免阻塞界面。CLI 未处理异常、Textual 致命/worker 异常、聊天请求、会话切换/加载/扫描、启动补沉淀、用量估计、搜索启动与后台回顾失败接入；生成起止和阅读状态切换提供上下文。不拦截第三方全局日志，不修改会话/记忆格式。

按用户最新要求保留异常原因、链式堆栈、文件/函数/行号与源码片段，不采集局部变量或整段转存聊天/记忆；识别到的环境凭据、Bearer、常见密钥标签脱敏。异常仍可能引用业务内容或本机路径，只留本机且分享前检查；交接文档/仓库不收录实际日志。日志目录 gitignore，日志创建失败只提示，不影响主功能。此次没有读取真实库、调用真实模型或提交推送。

补充命令导入/压缩/推断审查/模型列表异常，输入光标暂停/恢复的屏幕坐标，以及生成耗时与线程标识。回归捕获了缓冲 Markdown 恢复时旧底部先滚动、新高度晚一帧出现的时序问题；TranscriptView 在 Resize 后且阅读跟随仍启用时按已更新高度立即滚动，暂停阅读不强制跟随。回归使用三种输出长度覆盖缓冲定稿后的底部位置，避免仅一种高度偶然通过。

验证：第一轮日志/聊天/Markdown/会话/回顾/CLI 相关回归 254 passed；后续命令与光标接入专项 28 passed。发现的缓冲布局时序问题修复后，滚动/拖选/首页/历史加载 15 passed（含三种输出长度），最终日志专项 7 passed（含轮转、链式异常脱敏、线程写入、入口接线与满队列）。未进行原生终端人工鼠标验收或真实外部服务调用。所有测试使用夹具隔离数据；既有本地改动保留，仍未 commit/push。

## 提交前复查与上游规划接续（2026-11-02）

整组 UI 复查曾出现 162 passed / 2 failed：窄终端字标断言早于布局完成，缓冲全文定稿后内容虚拟高度增加却没有对应的窗口 Resize。补充 virtual_size 监听，最终滚动回调仍检查跟随/选择意图；测试使用有限等待，并要求回复实际高度增长后再断言新底部，不放宽目标状态。相关专项 15 passed。

远程 036fe94 / 0fc4c56 只在中英文 README 新增长期规划 Issue #18“演化认知：纵向用户理解”：跨期证据区分稳定特征、短期波动、阶段变化与长期趋势，可审阅且可重建。它仍是规划，区别于已实现的单条事实时间演化和当前画像；本轮接续这些文档，不宣称实现此功能。

最终提交前验证：日志、完整聊天 UI 与 Markdown 回归 164 passed（284.27 秒）；此前会话、回顾、CLI 相关回归也已通过。未做原生终端人工验收或真实 API 调用。回复语义配色、流式阅读/光标修复与本地诊断一起提交，取代上文的未提交状态；后续仍只在用户明确要求时推送，实际提交与远程状态以 Git 为准。

## Syvrio Web 适配层（2026-11-09，本地未提交）

新增 `src/syvrio/app/web/`（FastAPI + Pydantic）与 `web/`（React + TypeScript + Vite）。Web 只是传输层：`WebBackend` 复用 `ApplicationService.conversation` 的 `begin_turn` / `run_turn` / `end_turn` / `request_cancel`，不重复实现 Agent 循环、Memory、Session 或模型适配；`src/syvrio/app/server.py` 仍是 MCP Server，未被覆盖。CLI 新增 `syvrio web --host/--port`，默认 loopback 单 Worker。

`TurnCallbacks` 由 `EventPump` 转成有序 SSE：每帧带单调递增 `seq` / SSE `id` 与稳定 `type`；`on_progress("Calling tool: X")` 转 `tool_start`，首个工具结果配对 `tool_end`；`on_thinking` 按模型请求维护 `phase`，避免不同工具轮次的思考串线，`record_id is not None` 时转 `reasoning_end` 用完整文本校准；`done` 用完整正文定稿。模型未返回 reasoning 时不产生任何推理事件，前端不解析 Provider 私有协议。前端用有序 `Segment[]` 表达 Thinking / Tool / Result / Answer，16ms 批量合并 delta；Thinking 实时只显示最新三行，正文开始折叠，展开后固定高度内部滚动。Markdown 经 `react-markdown`（不渲染 raw HTML）+ `rehype-sanitize` 双重边界。

并发边界：单进程共享一个 Runtime，同一时刻只允许一个活动 Turn；第二个 `POST /api/chat` 返回 `409 busy` 而非排队，取消必须带匹配 `turn_id`，浏览器断流只释放该 Turn。新增 `infra/locking.client_instance_lock()` 作为整进程独占标记：TUI `run_chat()` 与 Web `startup()` 都持 `runtime/locks/client.lock`，后启动的一方收到明确错误，避免两个客户端同时写同一 Vault；`vault.lock` / `git.lock` 仍只保护单次写入。安全边界为 Host / 写请求 Origin 白名单、忽略代理头、无宽松 CORS、会话 ID 正则校验、通用 500 不泄露堆栈、关闭 access log。

打包链路有一处必须保留的约束：`uv build` 默认先从源码构建 sdist、再**从 sdist 构建 wheel**，而暂存目录 `src/syvrio/app/web/static/` 被 Git 忽略，hatchling 的 VCS 规则会过滤普通 `include`。只配 wheel `artifacts` 会静默产出不含 Web 资源的包，因此 `pyproject.toml` 同时配置 sdist 的 `force-include`。该配置还带来反向保证：未先执行 `scripts/build_web.py` 时 `uv build` 直接报 `Forced include not found`。`scripts/build_web.py` 支持 `--check`（校验暂存产物）与 `--wheel`（校验实际 wheel 内 `index.html` + JS/CSS）；两个 CI 工作流按「构建 → `--check` → 前端测试 → `uv build` → `--wheel`」顺序执行，发布前拦截缺资源的包。PyPI 安装后不需要 Node.js。

验证：后端 Web API/SSE/并发/安全 18 passed；前端 reducer、SSE 客户端、组件交互 21 passed；前端生产构建通过；`uv build` 的 wheel 与 sdist 实测均含静态资源，缺资源反例构建失败符合预期。真实模型在隔离合成 Vault 下完成端到端验收：真实流式 reasoning（分段递增）、`reasoning_end → tool_start(recall) → tool_end → 新 reasoning → content_delta → done` 多阶段链路、会话恢复、停止生成（保留半截正文并标记 cancelled）、取消后 busy 复位；5 路并发实测 1 路正常 + 4 路 `409 busy`，过期 turn_id 取消不误伤。合成配置下也验证了有序 `start → progress → failed` 与不泄露堆栈。

仍未验收：WSL systemd 常驻、Tailscale Serve 远程联机、原生浏览器人工交互与其他 Provider 的 reasoning 协议差异；相关命令与检查清单写在 `docs/web.md`，不能当作已实测。首版不替代 TUI，未实现模型管理后台、Memory 管理界面、多用户与文件上传。README 中英文版、`config.example.toml`、CI 与部署文档已同步。所有改动保留在本地开发分支，未 commit、未 push。

## Web / TUI 命令与快捷键兼容（2026-11-09，本地未提交）

按补充要求，把 TUI `app/command_catalog.py` 的纯数据下沉为 `application/commands.py` 的单一事实来源，TUI 原入口改为薄转发（现有 `chat_app.py` / `chat_commands.py` / `chat_widgets.py` 与 `tests/test_chat_app.py` 调用点不变，`SLASH_COMMANDS` / `SLASH_SUBCOMMANDS` / `slash_matches()` 内容完全等价）。新增 `WebCapability` 能力标记（`server` / `client` / `unsupported` + `confirm` + `note` + `shortcut`）与 `CommandUseCases`，只复用 `ConversationUseCases` / `SettingsUseCases` / `MemoryUseCases` / `ReflectionUseCases` / `GitUseCases`，不含 Textual、Rich 或 HTTP 依赖。`application/__init__.py` 导出共享目录与命令用例。

后端新增 `GET /api/commands`（目录）与 `POST /api/commands`（结构化结果）。`/import`、`/setup` 明确 unsupported，浏览器路径绝不交给服务器读取；`/git`、`/pending`、`/inferences`、`/profile`、`/dream` 只读，同步 / push / 批准 / 任务控制仍留 TUI；`/exit`、`/quit` 是纯客户端「返回入口」，不会停后台 Server；`/undo` 前端二次确认、服务端独立复核 `confirmed`；未知 `/` 命令返回 `unknown`，`POST /api/chat` 也拒绝斜杠开头消息（`422 command_required`），双保险不转发给 LLM。忙期白名单除 `/undo`（生成中兼作取消）外保持为 TUI `SAFE_COMMANDS` 子集，有测试断言。`session_id` 与当前 Runtime 会话不一致返回 `409 session_mismatch`，避免旧标签页串会话。

快捷键映射以 TUI 实际绑定为准，纠了一处重要偏差：**TUI `Ctrl+P` 是命令面板**（`action_model_providers` → `action_command_palette`），不是模型入口；浏览器 `Ctrl+P` 是打印，不拦截。改为 `Ctrl+Shift+P` / `Alt+P` 打开命令面板、`Alt+M` 或点击顶栏模型名进入 `/model`，`Ctrl+B` / `Alt+B` 切侧栏，`Ctrl+Q` 不拦截、`/exit` 只返回入口。映射集中在 `web/src/state/shortcuts.ts`，`RESERVED_KEYS` 明确列出不劫持的浏览器键并在命令面板中展示替代组合键。输入框补全、命令面板、确认弹窗、选项面板分别落在 `Composer.tsx`、`CommandUI.tsx`、`state/commands.ts`，含 IME 组字保护（组字期间不触发 Enter / Tab / 快捷键）。

本轮还修了两个真实缺陷：`state/chat.ts` 的 `withLive()` 原先只看消息列表最后一条，生成中插入命令结果（info 消息）会把后续 delta 挂到新 assistant 气泡、原始流式消息永远不收尾；改为向后查找仍 `streaming` 的 assistant，`done` / `cancelled` / `failed` 也用同一索引收尾。`CommandUseCases._model/_think/_context` 在应用参数时不再返回 `options`，否则前端 `panelFor()` 会在选中模型/推理档位后立即重开面板；`/think` 的空选项以 `default` 别名传递。另修复生成中 slash 命令被 `Composer.submit()` 的 `busy` 判断挡住的问题，并在忙期提供可点击 `Run` 按钮；模型变更后刷新顶栏模型信息。

验证：完整 Python 回归首轮实为 `1 failed, 1477 passed, 5 skipped`（1440 秒），唯一失败是 `test_every_source_event_is_allowlisted`——Web 适配层新增的 7 个 `web.*` 诊断事件（`web.startup_hook_failed` / `web.shutdown_distill_failed` / `web.history_unreadable` / `web.switch_failed` / `web.worker_failed` / `web.turn_failed` / `web.unhandled`）未登记进 `infra/diagnostics.py::_EVENTS`，`record_event()` 会静默丢弃它们，导致 Web 侧异常无法进入诊断日志。已补齐白名单；修复后 `tests/test_diagnostics.py` 9 passed、`-m web` 32 passed、源码 `record_event` 事件集合与白名单差集为空（此前误报的 `1482 passed` 作废）。`-m web` 32 passed（原 18 + 命令目录/分发/安全 14）；前端 `46 passed`（5 个测试文件）、TypeScript `tsc --noEmit` 通过；`scripts/build_web.py` 生产构建与 `--check` 通过；`uv build --clear` 的 wheel 经 `--wheel` 校验确实含最新 `index.html` + JS/CSS。真实 Edge 浏览器端到端（隔离合成配置 + Keyring 打桩，未触碰真实 Vault / API Key / 真实模型）：目录 21 条、`/` 前缀与 `/model refresh` 子命令补全、Tab / Escape、候选框贴近输入框不遮挡、`/help` 列出命令与快捷键说明、`/new` 新建并选中会话、`/sessions` 展开侧栏、`Alt+M` 与顶栏点击均可打开 `/model`、`/undo` 二次确认并恢复焦点、未知命令与 `/import` 明确反馈且不泄露路径、`Ctrl+P` / `Ctrl+Q` 的 `defaultPrevented=false`（不劫持）、`Ctrl+Shift+P` 打开 21 行面板并过滤到 1 行、`Ctrl+B` 切侧栏、IME 组字不发送、390px 手机宽度候选框宽 366px 且侧栏收起；截图 `commands-desktop.png` / `commands-mobile.png` 已人工查看。双标签页实测：另一标签 `/new` 后旧标签收到 `This tab is viewing a different session. Refresh first.`、输入框未禁用、刷新后 `/help` 恢复正常、Server 仍健康。

明确未验收：本轮命令/快捷键验收使用合成模型配置，**没有再用真实 LLM 发起一次带命令的会话**（真实 LLM 的流式 / Reasoning / recall / 停止生成验收是上一轮在同一隔离 Vault 完成的）；WSL systemd 常驻、Tailscale Serve 远程联机、Firefox / Safari 与其他浏览器保留键差异、原生终端 TUI 人工操作仍未实测。本机是开发环境，未安装 systemd unit、未配置 Tailscale、未发布 PyPI、未开放公网端口。所有改动仍只在本地开发分支，未 commit、未 push。

## `/model` 空目录修复（2026-11-09，本地开发态实测后追加）

用户在本机实际测试时发现 `/model` 只输出 `Models:` 后接一大片空白，`/model refresh` 则假称「需要本地终端」。根因链：`SettingsUseCases.model_options()` 默认 `model_catalog(cached_only=True)`，而 `llm/client.py` 的模型目录是**进程内** `_MODEL_CACHE`（非磁盘缓存）；Web 进程刚启动时缓存为空，TUI 会在打开选择器后由后台 Worker 调 `model_catalog(refresh=...)` 补全，Web 没有该后台刷新，于是 `options=[]`，`"\n".join([])` 产出空行。修复在 `application/commands.py::_model()`：① 空目录时把当前配置的 `(provider, model)` 作为兜底选项插入，message 显式写明 `Current model:` 与「无缓存，用 /model refresh」；② `/model refresh` 真正执行 `model_catalog(refresh=True)`（单供应商 5 秒超时、异常返回明确错误），并按拉取结果给出诚实文案；③ 缓存为空时允许 `/model <当前provider>/<模型ID>` 显式切换（沿用 `apply_model()` 自带的 key / 元数据校验），未知供应商仍拒绝，不让提示变成空话。新增 3 项后端测试，`-m web` 35 passed；真实服务重启后复验：`/model` 显示 `Current model: spark/spark-x2.6` + 一行选项，`/model refresh` 返回 `Refreshed the provider catalog: 1 model(s) found.`。

## Web 会话栏排序（2026-11-09，用户实测反馈）

用户要求左侧会话栏只按「会话最后一条消息的发送时间」排序。根因：`Session.updated_at` 取 `self.records` 最后一条**任意**记录的 `at`，而 `title` / `summary` / 沉淀 / `undo` / `recovery` 等控制记录同样 append 带 `at`，重命名或后台沉淀就会把旧空会话顶到最前（实测隔离库里两个 0 消息空会话排在带消息会话之前，当前会话反而垫底）。修复：`Session` 新增 `last_message_at` property，从 `active_records()` 倒序找最后一条 `type == "message"` 的 `at`（undo 投影后），无消息回退 `started_at`；`SessionStore._listing_row()` 输出该字段但**不改** `list()` 的既有排序，TUI / CLI 行为保持；`WebBackend.list_sessions()` 按 `last_message_at` 倒序重排并把字段放进 `SessionInfo`。前端 `Sidebar.tsx` 本就按后端顺序渲染，无需改动。新增 1 项测试（构造 `2999` 年的 title 记录污染 `updated_at`，断言顺序仍按消息时间），`-m web` 36 passed、前端 `tsc --noEmit` 通过。注意 `SessionStore.list()` 的 `limit` 截断发生在旧排序之后，超过 `limit` 的极老会话理论上可能被截掉，当前 `limit=100` 的私有部署可忽略。

## 全量回归复跑结果与时间边界（2026-11-09）

补齐 `_EVENTS` 后启动的第二轮完整 Python 回归结果为 `1 failed, 1477 passed, 5 skipped`（1442 秒）。`test_every_source_event_is_allowlisted` 已不再失败，说明诊断白名单修复在全量套件中生效。唯一失败换成 `tests/test_chat_app.py::test_reasoning_stays_with_background_session`：新建会话后断言 `not app.query(ThinkingPanel)` 时面板仍在。该用例依赖最多 100 次 `pilot.pause()` 的有限等待，与 Web 适配层无调用关系；独立复跑 4 次全部通过（5.37s / 9.57s / 8.51s / 9.79s），`tests/test_chat_app.py -k reasoning` 4 passed，判定为偶发时序抖动，未修改产品代码、未放宽断言。

**时间边界**：这轮全量启动于 diagnostics 白名单修复之后，但早于 `/model` 空目录修复与 Web 会话栏排序修复，因此不覆盖这两处改动。按 AGENTS.md「按影响范围选测」的约定，最新代码以定向回归覆盖：`-m web` 36 passed（67.59s）、`tests/test_diagnostics.py` 9 passed、`tests/test_session.py` + `tests/test_application_conversation.py` 40 passed、TUI reasoning 组 4 passed、前端 `tsc --noEmit` 通过、`/api/health` 正常。如需覆盖最新代码的完整全量数字，须再单独跑一轮（约 24 分钟）；当前未擅自重跑，服务保持运行供用户实测。所有改动仍未 commit、未 push。

## Web 会话删除（2026-11-09，用户实测反馈）

用户问「怎么没删除会话选项」。核对确认：TUI 本来就有 `Ctrl+D` + 二次确认（`chat_widgets.py` 的 `action_delete_session` → `chat_app.py::confirm_delete_session` → `ConversationUseCases.delete_guard/delete_session`），Web 首版只做了新建/切换/恢复，漏了删除入口。接入方式：后端 `DELETE /api/sessions/{id}?confirmed=true` → `WebBackend.delete_session()`（`_validate_session_id` → `_require_idle` → 存在性 → `confirmed` 复核 → `delete_guard` → `delete_session`，OSError 记 `session.delete_failed`），`STATE_MODIFYING` 已含 DELETE 故 Origin 检查自动生效。前端 `Sidebar.tsx` 条目改为 `session-row`（主按钮 + 删除按钮，避免 button 嵌套），复用 `ConfirmDialog`（新增可选 title/confirmLabel/testId），`useChat.ts` 新增 `deleteTarget` / `requestDeleteSession` / `confirmDeleteSession` 并纳入 `handleEscape`；删除当前会话时先 `POST /api/sessions` 切到新空会话再删，等价于 TUI 的先 `/new` 再删，服务端守卫语义不变。新增 4 项后端测试（未确认 409、确认后删除并刷新、当前会话保护、未知/穿越/跨站/生成中 busy）与 4 项前端交互测试（取消不删、确认后刷新、当前会话先新建、拒绝后输入框可用）。验证：`-m web` 40 passed、前端 50 passed、`tsc --noEmit` 通过、`scripts/build_web.py` 生产构建通过；真实服务重启后 `DELETE` 未知 ID 返回 404、静态资源含新 UI，隔离 Vault 中 4 条真实会话一条未动。

## Web 设置面板（2026-11-09，用户反馈「前端添加个设置面板」）

用户要求前端补齐设置面板。此前只有一个仅管理供应商的 `ProvidersDialog`，`/setup` 也已从 `unsupported` 改为 `client`（`intent=open_settings`），但模型、思考强度、上下文窗口仍只能靠斜杠命令。本轮把对话框重构成 `web/src/components/SettingsPanel.tsx`，左侧分区 + 右侧内容：Model / Thinking / Context / Providers / About。

数据与写入口径：面板不保存设置副本，全部通过 `POST /api/commands` 走 `CommandUseCases`（`/model`、`/model refresh`、`/model <provider>/<model>`、`/think`、`/context`），与 TUI 使用同一套 `SettingsUseCases`；供应商分区抽成 `ProvidersSection.tsx`，沿用 `GET/POST /api/settings/providers`，Key 只写不读。Model 分区在目录为空时明确提示「无可缓存目录，可按 ID 切换」，不伪造选项；`/think` 的 `default` 别名双向映射。About 分区只展示 `/api/health` 白名单字段（版本、provider、model、busy、单客户端锁、静态资源）与 `SHORTCUT_HELP`，不渲染任何凭据。

入口：顶栏 `Settings`、`Alt+S`（新增绑定，写进 `KEY_BINDINGS` / `SHORTCUT_HELP`）或 `/setup`；`Escape` 关闭并恢复输入框焦点。`useChat` 新增导出 `refreshHealth`，模型切换后顶栏标签同步。删除当前供应商仍由服务端 `SettingsUseCases.remove_provider` 拒绝，面板把 `ok=false` 的原因原样显示在 `settings-error`。

验证：前端新增 `SettingsPanel.test.tsx` 5 项（模型列表与切换、Thinking、Context、Providers 分区、About 不泄露 `sk-`）与 App 级 2 项（顶栏点击 / `Alt+S` 打开，并确认读取走 `/model`、`/think`、`/context`）；`npx tsc --noEmit` 通过，前端 `56 passed`，`-m web` 59 passed，`scripts/build_web.py` 已把新产物同步到 `src/syvrio/app/web/static/`。文档同步 `docs/web.md`（新增「设置面板」小节，并修正 `/setup` 由 unsupported 变为 client 的旧描述）、README 中英文版（删除「/setup 未支持」「无模型管理后台」的过期说法）。仍未用真实浏览器做视觉验收，也未 commit / push。

## 移动端适配补齐（2026-11-10，用户询问「手机端浏览页面做了适配吗」）

原有实现只有「≤767px 侧栏变抽屉 + 输入框底部 safe-area」两处，其余是桌面布局直接照搬。实测 375px 虽无横向溢出，但存在真实问题：顶栏 4 个按钮把模型名挤到极窄；iOS 聚焦输入框会整页放大（字号 15px < 16px）；弹层用 `vh`，键盘弹出或地址栏收起后会超出可视区；触摸设备删除按钮依赖 hover；横屏安全区未处理。

本轮改动：

- `web/index.html`：viewport 增加 `interactive-widget=resizes-content`，新增 `theme-color=#111318`（暗色地址栏）。
- `web/src/App.tsx`：监听 `visualViewport.resize`，把可用高度写入 `--app-height`，`.app` 改为 `height: var(--app-height, 100dvh)`，软键盘弹出时输入框不被遮住；`scale !== 1`（双指放大）时跳过，避免缩放时压扁整页。连接状态文案包进 `.connection-label`，窄屏只保留状态圆点。
- `web/src/styles.css`：`-webkit-text-size-adjust: 100%`；`touch-action: manipulation`；消息区 `overscroll-behavior: contain`；顶栏/输入框/侧栏使用 `env(safe-area-inset-*)`；弹层与设置面板优先 `dvh`（保留 `vh` 回退）；移动端输入框统一 16px 防 iOS 缩放；触摸目标 34–44px；`@media (hover: none)` 让删除按钮常显（不再只靠宽度断点）；设置标签从横向滚动改为 `flex-wrap`，320px 下两行不裁切；新增 `≤380px` 顶栏压缩与 `max-height: 520px` 横屏规则。

验证：类型检查通过、前端 `56 passed`、`build_web.py` 重建 + `--check` 通过。用本机 Edge（Playwright `channel=msedge`，iPhone UA + 3x DPR）对 375×667、320×568、740×360 三种视口实测：`document.scrollWidth === innerWidth`（无横向溢出），顶栏 `scrollWidth === clientWidth`，模型名 375px 下 153px、320px 下 98px 均可见，输入框 `font-size: 16px`，composer 底边等于视口高度，控制台无报错；截图检查主界面、抽屉、设置面板、命令面板与 `/` 补全都未裁切。软键盘本身无法在桌面浏览器真实触发，`--app-height` 逻辑仅验证了初始化与 resize 路径，真机 iOS/Android 仍需人工确认。

文档同步：`docs/web.md` 新增「移动端适配」小节；README 中英文补充抽屉侧栏/软键盘/安全区/触摸目标。仍未 commit / push。

## 目录规范调整（2026-11-10，用户要求）

- **删除项目内 `.workbuddy/`**：用户明确禁止在仓库里单独创建此类目录。此前它只存一份 `memory/2026-11-09.md` 工作日志，内容与本文档 754–822 行重复，核对后直接删除，无信息丢失。工程上下文统一写 `docs/HANDOFF.md`（AGENTS.md 既有约定），不再另建隐藏目录。
- **`deploy/` 并入 `docs/`**：唯一文件 `syvrio-web.service` 移到 `docs/syvrio-web.service`，仓库根目录不再保留只有一个模板的 `deploy/`。同步更新 `docs/web.md`（3 处）、`README.md`（2 处）、`README.en.md`（2 处）的路径引用；`git grep deploy` 已无残留（仅剩英文 prose 里的普通词）。

## Web 主题、懒创建与交互收尾（2026-11-11，本地未提交）

- 设置面板新增 Appearance，支持 `auto/light/dark`；首屏脚本、系统主题监听和 `theme-color` 同步避免黑白闪烁。全局样式补齐双主题变量、焦点态、滚动条与视觉层级，移动端继续使用抽屉、安全区、`dvh` / `visualViewport` 和 16px 输入框。
- 桌面 `«` 收起侧栏已修复，展开状态写入 `localStorage`；跨移动端断点时抽屉会复位。切换会话由 `MessageList` 在 `useLayoutEffect` 中按会话 ID 即时定位底部，不再从顶部平滑滚动到底；同一会话的流式输出仍保持跟随。
- 新建会话只返回首页草稿：`/new` 与 `POST /api/sessions` 不再落盘，首条消息仍由下层 `AgentRuntime.ensure_session()` 创建 JSONL。只删除 `ConversationUseCases.ensure_session()` 薄封装，没有删除 Runtime 的必要创建逻辑。
- 自动验证：前端 TypeScript 与 65 项测试通过，相关后端回归 62/62，通过生产构建和 `build_web.py --check`。死代码扫描未确认新增垃圾；`vulture` 只报告框架回调形参，未误删。构建产物为 JS 312.04 kB（gzip 97.37 kB）、CSS 23.97 kB（gzip 5.17 kB）。
- 真实 Edge 最终补验：新建不提前增加会话记录；缺少 DeepSeek Key 时显示明确错误并恢复发送状态；当前会话删除的取消保留记录，确认后回到首页、列表移除且历史 API 返回 404。只删除本轮 `E2E_FAILURE_PROBE` 测试会话，未动既有会话。此前“首条消息无回复”的根因是进程没有 `DEEPSEEK_API_KEY`，不是 Web 流式回归；**真实模型成功回复本轮仍未验收**。服务直接运行在 `127.0.0.1:8787`，所有代码改动仍未 commit / push。

## 真实模型回复验收与配置覆盖坑（2026-11-11 收尾）

**根因**：用户全局配置 `~/.syvrio/config.toml`、`%APPDATA%\syvrio\syvrio.toml` 早已是 `provider = "spark" / spark-x2.6 / SPARK_API_KEY`（Keyring 中存在，35 字节），但仓库根目录还有一份被 `.gitignore` 忽略的本地 `syvrio.toml`（11-09 16:14 生成），把 `[llm]` 钉成 `deepseek / deepseek-flash / DEEPSEEK_API_KEY`，而该 Key 为空。`config.py::default_config_path()` 的优先级是 `SYVRIO_CONFIG` → **`Path.cwd()/syvrio.toml`** → `~/.syvrio/config.toml`，因此本地这份把 Spark 全局配置整个盖掉，表现为“模型没有回复”，实际是缺少 LLM API Key。

**处理**：把仓库本地 `syvrio.toml` 的 `[llm]` 改回 `spark / spark-x2.6 / SPARK_API_KEY`（该文件不入库，不影响他人）；未删除用户既有配置，也未打印任何密钥。排查同类问题时先跑 `load_config()` 看 `provider/model/api_key_env` 与 `api_key()` 是否为真，再看 `Path.cwd()` 是否有一份覆盖文件。

**真实 Edge 全流程验收（`http://127.0.0.1:8787`，provider=`spark`）**：新建对话时会话行 20 → 20（不提前落盘）；发送首条消息后 21（落盘），助手气泡渲染出真实回复（`SYVRIO / 我目前处于X2.6系列版本…`），历史接口返回 2 条记录、无 `error-banner`；再走 UI 删除（确认弹层 → `Delete permanently`）后行数回到 20，历史接口 404。API 侧另外用流式 SSE 直接验证过一轮：`start → progress → reasoning_delta → content_delta(收到) → reasoning_end → done`。本轮只删除自建测试会话（`E2E_*` 与 `/api/chat` 冒烟产生的那条），既有 19 条会话一条未动；`.playwright-cli/` 与临时日志已清理。代码改动仍未 commit / push。

## 输入框高度与停止按钮（2026-11-11，用户反馈截图）

用户反馈「输入框这么小还要右侧滚动条」且问「目前有停止按钮吗」。原实现 `Composer.tsx` 有自动增高，但在 `box-sizing: border-box` 下把 `height` 直接设为 `scrollHeight`：该值含 padding 不含 border，写回后内容区仍少 padding + border 的高度，**空闲状态就溢出 1~2px**，于是 `overflow-y: auto` 常驻一条细滚动条；同时 `rows={1}` 让初始只有一行。

改动：

- `rows={3}` + `.composer textarea { min-height: calc(4.5em + 24px) }`（1.5 行高 × 3 + 上下 padding 22 + 边框 2），默认固定三行，字体变化时随 `em` 缩放。
- `Composer.tsx` 改为 `useLayoutEffect` 内 `fit()`：先把 `height` 置空取固有高度，用 `scrollHeight + (offsetHeight - clientHeight)` 计算含边框的真实需求，仅在超出固有高度时才设 `height`，上限取 CSS `max-height`（200px）；`overflow-y` 同步按「是否超过上限」切换 `hidden` / `auto`，并加 1px 容差防止逐键长高。
- `ResizeObserver` 只在**宽度**变化（侧栏收起、横竖屏）时重算，避免自己写高度触发循环。
- 新增 `web/src/components/Composer.test.tsx` 4 项：默认三行无滚动条、内容变高但未到上限仍隐藏、到上限才 `auto`、发送后回落三行。

实测（Edge，`127.0.0.1:8787`，字体 15px / 行高 22.5px）：空态 `height=92px`、`scrollHeight === clientHeight`、`overflowY=hidden`；灌 30 行后 `height=200px`、`overflowY=auto` 且可滚到底；清空后回到 92px。停止按钮实测：发送后 `Send` 变 `Stop`（走 `POST /api/chat/cancel`，`Esc` 同源），点击后恢复 `Send` 且输入框可用。前端 69 项测试、`tsc --noEmit`、`build_web.py --check` 均通过。

**会话清理（已与用户核实）**：收尾核对时发现 vault 少了 3 个会话文件（「我是谁」、`s-20261103-150724-62b0`「为啥感觉白茶特别耐泡」、`s-20261023-123045-852f` 空会话）。曾用 vault 自带的 git (`git checkout --`，HEAD 保留完整内容) 误恢复过一次；用户确认为本人删除后，按「空会话直接删掉」的要求走 `DELETE /api/sessions/{id}?confirmed=true` 清空：15 个 0 消息会话 + 上述 2 条有内容会话，共 17 个，剩余 2 条（`s-20261102-155826-de40` 8 条、`s-20261023-122812-4801` 33 条）。删除链路只 `unlink` 单个文件，无批量误删逻辑；因服务未开 access log，事件归属靠用户确认。vault 的 git 历史仍是最后的恢复手段。

## 界面布局参考 Kimi 重构（2026-11-11）

用户给出 Kimi 桌面截图，要求「界面布局参考 Kimi」。本轮只改前端结构与样式，后端接口与交互契约未动。

- **侧栏**（`Sidebar.tsx` + 新增 `Icons.tsx`）：Logo 方块 + 面板图标收起；新建会话是白底卡片并带 `/new` 提示；`Chats` 分组标题；会话行去掉 "N messages" 副标题、标题单行省略；底部头像 + 模型名 + 在线圆点。
- **顶栏**：改为「当前会话标题 + 右侧动作组」。Commands / Settings 变成图标按钮（`data-testid` 与 `aria-label` 不变），模型名做成无边框 chip，连接状态只留圆点，`Connected / Offline` 文字仍留在 DOM（`clip-path` 视觉隐藏）供测试与读屏使用。
- **空态**：`.main.is-empty` 下把 `thread` 垂直居中，Logo 方块 + `Syvrio` 大字 + 副标题；输入框卡片居中，下方是 `/model` `/status` `/help` `/sessions` 快捷 chip，点击填入命令。
- **会话态**：消息列与输入框统一 720px（`--content`）；用户消息右对齐 18px 圆角气泡，助手为纯文本并去掉 You / Syvrio 标签；工具事件从整行改为自适应宽度胶囊。
- **输入框**：外层 `.composer-card` 20px 圆角 + 柔和阴影，`:focus-within` 显示焦点环；内部 textarea 三行起、无边框；底部工具行左侧 `+` 打开命令面板，右侧为深色圆形发送 / 停止按钮（图标 + `aria-label`，保留 `Send` / `Stop` 可访问名）。
- **主题**：新增 `--sidebar-bg`。浅色主区纯白、侧栏 `#f7f8fa`；深色侧栏 `#090b0e`、主区 `#0e1014`。
- **结构**：`MessageList` 与 `Composer` 始终挂在同一个 `.thread` 容器里，只切换容器对齐方式，避免发送首条消息时输入框被卸载重建。

验证：`tsc --noEmit`、前端 69 项测试、`build_web.py --check` 全部通过。Edge 实测 1280×800 与 390×844：首页卡片宽 720px 且居中；会话态滚动到底、用户气泡右对齐且圆角 18px；深色主题对比正常；手机无横向溢出（`scrollWidth = 390`）；抽屉与折叠侧栏均正常。真实发送一条消息（spark-x2.6 回复「收到」）确认 hero → 底部输入框切换无异常，验证后已删除该测试会话，原有 2 条会话未动。

### 顶栏图标尺寸上调（2026-11-11）

用户反馈顶栏图标偏小，随后补充「不是手机端的」「手机端也稍微放大点」。改动只在尺寸层面：

- 桌面：`.icon-button` 32→36px，`PanelIcon / CommandsIcon / SettingsIcon` 17→20（侧栏收起按钮同属该图标对，一并 20，避免收/起时尺寸跳变）。
- 手机（`max-width: 767px`）：`.icon-button` 34→38px，并用 `.icon-button svg { 22px }` 同步放大字形。

实测 Edge：桌面 36/20；390 与 360 宽均为 38/22，`scrollWidth` 等于视口宽（无横向溢出），标题与模型名仍保留 87–106px。前端 69 项测试通过。

## 非正文内容默认收起（2026-11-11）

用户要求「除了回复的正文内容，其他例如工具调用、思考过程、压缩后内容一律默认收起不要展开显示」。此前：思考段在流式期间会露出最后三行、工具调用常驻一行并直接带结果、压缩摘要以整段文字插在对话里。

统一为 `web/src/components/Collapsible.tsx`（胶囊摘要 + 箭头 + 可选正文），正文区统一 `max-height: 220px` 内部滚动，永远不会把对话撑高：

- **思考**：`reasoning_delta` 创建即 `collapsed: true`，`Thinking` 只看 `expanded`，流式时也只有「Thinking...」一枚脉冲点；结束后变「Thought for 3.2s」。原来按 `!done` 预览最后三行的逻辑与 `.thinking-live` 样式一并删除。
- **工具调用**：`MessageList` 中**相邻 tool 段合并为一个组**：单个显示工具名 + Done/Running/Failed；多个显示「N tool calls」。结果文本改为组内条目，点开才出现。
- **压缩摘要**：`ChatMessage.info` 增加 `folded` / `title`，`loadHistory` 里 `summary` 与 `role=summary` 记录都带 `folded: true`、标题「Summarized context」。命令结果仍是普通 `info-line`，不折叠。
- **顺带修掉重复**：历史里 `role=tool` 是紧跟 assistant 的独立记录，原实现既在 assistant 的 `tool_calls` 画胶囊、又画一行「fetch_web ok」，且结果正文丢失。现在 `loadHistory` 按顺序把 tool 记录的 content 配回调用（新增 `Segment.tool.ok`），独立行不再出现；仅当前面没有 assistant 时才退化成折叠 info。

验证：`tsc --noEmit`、前端 **72 项测试**（新增「多次工具调用合并成组」「工具结果配回调用」「摘要标记 folded」3 项，改写工具与实时思考 2 项）、`build_web.py --check` 全部通过。Edge 实测：

- 历史会话（31 turn）：折叠行 17（6 组 + 9 单 + 2 摘要），默认 `.fold-body` = 0，`.info-line` = 0；展开工具组可见 `fetch_web Failed` 与错误正文，展开摘要可见原文。
- 实时发送「只回复两个字：收到」：流式期间 `thinking-body` = 0、label「Thinking...」；完成后「Thought for 0.3s」，正文「收到」正常。折叠行宽 720 = 内容列宽（原先摘要卡会撑到 820）。
- 手机 390：折叠行宽 142–163px、`Expand` 文字隐藏、`scrollWidth` = 390 无溢出。
- 测试会话已删除，原有 2 条会话未动。

**遗留可见**：`Markdown.tsx` 不支持 GFM 表格，历史里的表格仍以 `| 字段 | 值 |` 原文显示。原因见下节（当时的「自研解析器」判断是错的）。

## GFM 表格（2026-11-11）

先更正上一节末尾的判断：「47 行自研解析器」不成立。`Markdown.tsx` 一直是 `react-markdown` v9 的薄封装（`git log -- web/src/components/Markdown.tsx` 只有 `e173518` 一个提交，从未自研），47 行只是因为原来只自定义了代码块一个组件。表格不渲染的真正原因只有一个：**没有启用 GFM 语法插件**，而表格不属于 CommonMark。

改动（全部走通用生态，没有新增自研解析逻辑）：

- `web/package.json` 新增 `remark-gfm@4.0.1`（`corepack pnpm add`，`pnpm-lock.yaml` 同步）。
- `Markdown.tsx`：`remarkPlugins={[remarkGfm]}`；新增 `components.table`，把表格包进 `.table-wrap` 以提供横向滚动。`rehype-sanitize` 保留，原始 HTML 依旧不解析（remark-gfm 不含 raw HTML 能力）。
- `styles.css`：表格从 `display: block; overflow-x: auto` 的 hack 改为容器圆角边框 + 行列分隔线 + 表头 `var(--panel-2)` 底且 `nowrap`；顺带补 `del`、任务列表 checkbox 样式。
- 新增 `web/src/components/Markdown.test.tsx` 5 项：表头/表体结构、表格必须位于 `.table-wrap` 内、表格与段落混排、删除线、原始 HTML 不进入 DOM。

验证：`tsc --noEmit`、前端 **77 项测试**（72 + 5）、`scripts/build_web.py` 构建全部通过。Edge 实测历史会话：

- 7 个表格全部渲染为真实 `<table>`（表头含 功能/说明、层/组件/职责、设计模式、理念、标题/链接、字段/值），`tbody` 共 47 行，页面文本中 `|-------|` 原文消失。
- 桌面：圆角容器、灰底表头、单元格左对齐。
- 390×844：`document.scrollWidth - clientWidth = 0`，表格自身 `scrollWidth = clientWidth = 360` 无裁切。
- 人为把表格撑到 783px：`.table-wrap` 可横向滚动（783/360），页面横向溢出仍为 0。

副作用（已清理）：为验证表格，曾在会话 `s-20261102-155826-de40` 追加一轮「Reply with ONLY a markdown table…」测试消息（文件 76→86 行）。验证后已 `git checkout --` 回滚到 vault HEAD，文件回到 76 行、会话 53 条、末条为原有 summary 记录；页面复查 6 个表格 / 44 行、无测试消息残留、console 0 错误。

## Review 修复（2026-11-11）

对上一批未提交改动做了一次完整 review，逐项修掉发现的问题。

**工具失败状态贯通（此前只在历史加载时才对）**

- `TurnCallbacks` 新增 `on_tool: Callable[[bool], None]`，`runtime.ask` / `_run_ask` 在 `emit(outcome.display)` **之前**回调，保证「状态 → 文本」成对到达。
- `EventPump.on_tool` 收集结果状态，`on_event` 发 `tool_end` 时带上 `ok`；`_flush_tools` 后清空残留，避免下一个工具继承错误状态。
- `StreamEvent` 增加 `ok: bool | None`，前端 `tool_end` 分支写入 `segment.ok`。实测 `fetch_web http://127.0.0.1:1/` 会立刻渲染 `Failed`，展开可见 `{"error": "抓取地址不能指向私网或本机地址"}`。

**其余修复**

- `/new`：`refresh_history` 的旧逻辑会先读一次上一会话历史再重置为空视图，白跑一次请求；改为 `new_session` 分支直接重置，只保留 `refresh_sessions`。
- 工具结果配对：由「按位置」改为「先按 `tool_name`、未命中再按顺序」，避免缺记录时后续结果整体错位。
- 折叠状态统一：`toggleThinking` → `toggleSegment`，`Segment.tool` 增加 `expanded`，工具行与思考行共用一条状态路径；多工具组以首成员标志为准，展开时整组同步。
- 主题：`applyTheme(mode, persist = true)`，启动恢复传 `persist: false`，用户没选过就不会在 `localStorage` 里留下 `syvrio.theme`；系统主题变化回调同样不写入。
- `Collapsible` 补 `aria-controls` + `useId`，与 `aria-expanded` 配对。
- CSS 去重：`.composer .run-command` 的 `min-width` 合并回主块、`.settings-nav button.active` 背景改 `--accent-soft` 并删掉后置覆盖、删掉重复的 `.markdown th` 背景。
- 文档：`docs/web.md` 的侧栏描述由 `«` / `Sessions` 文字按钮改为图标按钮并补 `tool_end` 的 `ok` 字段；`README.md` 的 `Ctrl+B` 措辞与实现对齐。

**review 撤回项**：原以为 `session_id is None` 且后端 `current` 非空会被 `_check_session_match` 拒绝，复查后不成立——条件是 `session_id is not None and session_id != current`，null 不会触发 409，故未改动。

验证：`tsc --noEmit`、前端 **79 项**（新增工具失败标注、按名配对 2 项）、`test_web_api.py` + `test_web_commands.py` **47 项**、`test_application_conversation.py` 8 项、`build_web.py` 全部通过。Edge 实测：历史会话 6 表格 / 44 行 / 17 折叠行且默认全收起；Appearance 选 Dark 后 `data-theme=dark`、`theme-color=#0e1014`、`aria-checked=true`，未选择时 `localStorage` 为 `null`。验证用的临时会话已删除，原有 2 条会话未动。

## fetch_web 502 排查（2026-11-11）

用户截图报 `Fetch failed: fetch: unavailable: open_websearch: open-websearch HTTP 502`。结论：**两层原因，一层是环境，一层是真代码缺陷**。

### 第一层：502 是连接层错误，不是 daemon 业务错误

daemon 的业务错误一律是 JSON（`{"status":"error","error":{"code":...}}`）；截图里的 `open-websearch HTTP 502` 来自 `_post` 对 `status >= 500` 的统一包装，而此时返回体其实是 WorkBuddy 沙箱代理的纯文本 `upstream connect failed: ... (os error 10061)`。`127.0.0.1:3000` 当时已无监听（`netstat` 无 LISTENING），代理连不上上游就把自己的 502 转了出来，被 syvrio 当成 daemon 502 上报。

同一时间点，shell 里手工 `curl` 到 3000 又正常——**说明 8787 那个进程（17:10 启动）持有的网络上下文已与当前环境脱节**。重启服务后立刻恢复，`tool_end` 变 `ok: true`。所以用户判断「不像代码问题」是对的，主因不是代码。

### 第二层：真实代码缺陷——fake-IP 豁免对顺序敏感

open-websearch 2.2.0 `build/utils/urlSafety.js`：

```js
function isAllowedFakeIp(address) {
  if (isIP(address) === 0 || config.fakeIpCidrs.length === 0) return false;
  try {
    const parsed = ipaddr.parse(address);
    return config.fakeIpCidrs.some((cidr) => parsed.match(ipaddr.parseCIDR(cidr)));
  } catch { return false; }
}
```

`ipaddr` 的 `IPv6.match(IPv4 网络)` 会抛 `cannot match ipv6 address with non-ipv6 one`，被外层 `catch` 吞掉后整体返回 **false**。于是只要 IPv4 段排在前面，**所有 IPv6 fake-IP 永远得不到豁免**。而 syvrio 原默认值 `"198.18.0.0/15,2001:2::/48"` 正是 v4 在前。对照实验（注入 DNS 返回 `2001:2::59`）：

| `FAKE_IP_CIDRS` | 结果 |
|---|---|
| `2001:2::/48,198.18.0.0/15` | PASS |
| `198.18.0.0/15,2001:2::/48` | BLOCKED（`resolves to private IP(s) (2001:2::59)`） |

Clash Verge 的 TUN 在 IPv6 下返回 `2001:2::xx`，所以只要同时开着代理 + IPv6 fake-IP，fetch 必然 502，与 daemon 是否健康无关——这是用户笔记本上也可能踩到的地雷。

### 改动

- `base.py`：`DEFAULT_FAKE_IP_CIDRS` 改为 `"2001:2::/48,198.18.0.0/15"`；新增 `order_fake_ip_cidrs()`，把 IPv6 段排到 IPv4 之前（注释写清上游缺陷）。
- `daemon.py`：spawn 时不再只 `setdefault`，而是**无条件重排** `FAKE_IP_CIDRS`，用户自定义配置同样受保护；空串保持「显式关闭豁免」的原语义。
- `tests/test_web_search.py`：参数化扩到 5 组（含 v4 在前、带空格、空串），新增 `test_order_fake_ip_cidrs_puts_ipv6_first`。

### 验证

- 对照实验：`node build/index.js serve` 手工启动（v6 在前）→ `fetch-web` 返回 200 抓到 example.com；npx 在项目目录启动同样 200。
- `ensure_daemon` 干净环境启动后 `fetch-web` 200（1.56s），`FetchRouter.fetch()` 端到端拿到 `Example Domain`。
- 8787 重启后端到端：`tool_end {"text":"Fetched example.com","ok":true}`。
- `test_web_search.py` 125 项通过；验证用会话已删除（剩原有 8 / 33 条两条）。

### 遗留

`_spawn_daemon` 与运行中的 Web 服务可能同时 ensure 同一个端口，先 bind 失败的进程会留下「banner 打印后消失」的假象（`health` 检测到的其实是对方）。当前只影响开发期观感，未改动。

## 0.5.0 发版（2026-11-11）

版本号 `0.4.0.dev0` → `0.5.0`（`pyproject.toml`、`src/syvrio/__init__.py`、`uv.lock` 自引用条目）。`uv lock --check` 通过，`python -m syvrio --version` 输出 `Syvrio 0.5.0`。本轮合并的改动按主题分四个提交：Web 布局与折叠、工具状态贯通、GFM 表格、搜索 fake-IP 豁免。

发版前修掉一个既有红：`evals/short_facts_eval.py` 仍 `from evals.retrieval_eval import CASES, build_corpus`，而上一提交（`99d8632`）已把检索评测改成冻结 JSON（`load_data()` 返回 corpus/cases，target 为语料下标）。该导入直接 ImportError，`tests/test_vector.py::test_large_corpus_keeps_explicit_short_fields_without_local_model` 必然失败。

修法（不改评测基准数据）：

- `corpus_entries()` 从 `load_data()` 取语料并保留原始下标，`legacy-NNNN` 仍按冻结顺序编号，用例 target 不变。
- 冻结语料本身埋了同类短字段（`姓名：人物乙`、`年龄：28 岁`、`职业：UI 设计师` 等 30 条），会直接挤占 top3，使「长文干扰下短字段仍在前三」的前提失效。种子阶段剔除这些同字段竞品，只留无关长文，语料 1000 → 970、记录 1785 → 1755，仍满足 `>= 1200`。
- `legacy_cases()` 把 target 下标换成记录 ID，`main()` 的 legacy 分组改用 ID 命中。

验证：

- 前端 `tsc --noEmit` + **79 项**测试通过；`scripts/build_web.py` 构建并 `--check` 通过（bundle `index-Dz8N7HaU.js`）。
- 后端全量 **1513 passed / 5 skipped / 1 failed**（失败即上述 evals 导入）；修复后 `tests/test_vector.py` **29 项**全绿。
- Edge 端到端：`fetch_web` 实时帧 `ok:true`，回复渲染为 `Field | Value` 表格（2 行），3 个折叠行默认全收起、展开可见正文、`aria-controls` 指向的节点存在、无 error banner。
- 验证用会话 `s-20261111-181106-9383` 已删除，原有 8 / 33 条两条会话未动。
