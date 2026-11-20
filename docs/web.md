# Syvrio Web

Web、Textual TUI、CLI 和 MCP 连接到同一常驻后端。每个会话有独立的运行时和发送队列。后端持有 Vault 的写入所有权，统一处理工具调用、撤销、自动沉淀及定时回顾。

```text
浏览器 → Web 网关 ─┐
TUI / CLI / MCP ──┴─ 常驻后端 → 会话 / 记忆 / 模型
```

浏览器使用 REST 和 SSE。网关沿用 Host 和 Origin 检查，并阻止访问仅供本机程序使用的管理接口。常驻后端只监听本机地址。首次连接会自动启动，退出客户端后继续运行。使用 `syvrio backend status` 查看状态，`syvrio backend stop` 安全停止。

## 开发与运行

PyPI wheel 已包含 Web 静态资源，安装并运行 `syvrio setup` 后，用 `syvrio web` 启动，浏览器打开 `http://127.0.0.1:8765/`。

从 Evo 源码运行需要 Python 3.12+、uv、Node.js 和 Corepack。首次使用先运行 `uv run syvrio setup`。

```bash
uv sync
uv run python scripts/build_web.py
uv run syvrio web
```

前后端联调时，可跳过静态构建。先在仓库根目录运行 `uv run syvrio web`，再用另一个终端启动 Vite：

```bash
cd web
corepack pnpm install --frozen-lockfile
corepack pnpm dev
```

打开 Vite 提示的地址（默认 `http://localhost:5173/`）；`/api` 会代理到 `127.0.0.1:8765`。未暂存静态资源时，直接访问后端根路径会返回 `503 assets_missing`，API 仍可用。

### 本地域名映射（浏览器只输域名）

`syvrio web --domain <域名>` 把 `127.0.0.1 <域名>` 追加到系统 hosts（先备份为 `hosts.syvrio-backup`，幂等、不覆盖已有映射），同时把该域名加入可信 Host / Origin 白名单。服务仍只监听 loopback，域名只是本机解析别名，不改变监听范围。

```bash
uv run syvrio web --domain syvrio.local --port 80
# 浏览器打开 http://syvrio.local/
```

- 只有 `--port 80` 才能省略端口号；Windows 绑定 80 通常无需提权，Linux / WSL 需要 root 或 `CAP_NET_BIND_SERVICE`。
- 写 hosts 需要管理员 / root 权限。权限不足时命令不会失败，而是打印需要手动添加的那一行；`--no-hosts` 只打印不写文件。
- 额外域名（Tailscale / 局域网主机名等）用 `SYVRIO_WEB_ALLOWED_HOSTS=host1,host2`；也可只靠环境变量、不写 hosts。
- 撤销映射可调用 `src/syvrio/app/web/hosts.py` 的 `remove_hosts_mapping()`，或手动删除 hosts 中对应行；备份保留在 hosts 同目录。
- 域名必须是纯主机名（如 `syvrio.local`），拒绝 URL、端口、路径、通配和 IP 字面量，避免把任意字符串放进 Host 白名单。

### 生产构建与 PyPI 打包

```bash
uv run python scripts/build_web.py                     # pnpm install + build + 复制 + 校验
uv run python scripts/build_web.py --check             # 只校验已暂存的静态资源
uv build --clear
uv run python scripts/build_web.py --wheel dist/*.whl  # 校验实际 wheel 内含静态资源
```

`scripts/build_web.py` 把 `web/dist` 复制进 `src/syvrio/app/web/static/`（该目录被 Git 忽略，不提交），并校验 `index.html` 确实引用了 JS/CSS 产物；构建失败或产物不完整会非零退出。`--wheel` 进一步检查打好的 wheel 里确实存在 `index.html` 和 JS/CSS 产物。

`pyproject.toml` 有两处配合，缺一不可：

- wheel 的 `artifacts = ["src/syvrio/app/web/static/**"]` 把产物打进 wheel；
- sdist 的 `force-include` 把同一个目录打进 sdist。**`uv build` 默认从 sdist 构建 wheel**，而 `static/` 被 `.gitignore` 忽略，普通 `include` 会被 hatchling 的 VCS 规则过滤掉；只配 `artifacts` 会静默产出不含 Web 资源的 wheel。`force-include` 同时保证：未先执行 `scripts/build_web.py` 时 `uv build` 直接报 `Forced include not found` 而非发布半成品。

通过 PyPI 安装后静态资源已在包内，运行时不需要 Node.js。CI 与手动发布流程都按「`scripts/build_web.py` → `--check` → 前端测试 → `uv build` → `--wheel`」顺序执行，最后一步在发布前拦截缺资源的包。

### 环境变量

| 变量 | 作用 |
| --- | --- |
| `SYVRIO_WEB_ALLOWED_HOSTS` | 允许的可信 Host/Origin，逗号分隔；Tailscale 访问时必须填入 `<机器名>.<tailnet>.ts.net` |
| `SYVRIO_WEB_STATIC` | 覆盖静态资源目录（仅用于调试） |

## API 与 SSE

所有响应统一错误格式：`{"error":{"code":"...","message":"..."}}`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 版本、供应商与模型、生成状态、活动轮次、静态资源状态 |
| GET | `/api/context-usage?session_id=<id>` | 会话上下文估算、配置上限与压缩阈值；省略 ID 表示空白草稿 |
| GET | `/api/sessions?limit=100` | 会话列表 |
| POST | `/api/sessions` | 回到新对话首页（返回 `draft=true`、`session_id=null`，不写文件；首条消息才创建会话） |
| POST | `/api/sessions/{id}/switch` | 切换/恢复会话 |
| GET | `/api/sessions/{id}/deletion-preview` | 读取随会话移除的待确认内容及本次清单版本 |
| DELETE | `/api/sessions/{id}?confirmed=true` | 确认删除，存在相关候选时通过 `pending_revision` 提交已查看的版本 |
| GET | `/api/sessions/{id}?limit=300` | 最近的结构化历史；可读取本机 Runtime 思考附件，默认折叠 |
| GET | `/api/sessions/{id}/messages/{message_id}/draft` | 读取最近回合的原文及附件清单供编辑；消息已变更时返回 409 |
| GET | `/api/settings/providers` | 供应商、端点和配置状态；不返回已保存的 Key |
| POST | `/api/settings/providers/{provider}` | 添加、更新或删除凭据，以及修改端点 |
| GET | `/api/commands` | 共享命令目录（名称、用法、能力标记、子命令、快捷键） |
| POST | `/api/commands` | 执行斜杠命令，返回结构化结果，不经过 LLM |
| POST | `/api/chat?receipt=1` | 将消息放入当前会话队列，返回 202 和回合 ID。省略参数时直接返回该回合的 SSE 流 |
| GET | `/api/chat/queue` | 查询本会话的活动回合、等候消息、暂停状态及修订号 |
| DELETE | `/api/chat/queue/{turn_id}` | 移除尚未开始的排队消息 |
| GET | `/api/chat/turns/{turn_id}?after=<seq>` | 订阅回合更新，从指定序号后重放已发出的事件 |
| POST | `/api/chat/attachments` | 发送带附件的消息；同样支持 `?receipt=1`。使用 multipart 表单，可重复提交 `files`，可选字段为 `message` 和 `session_id` |
| GET | `/api/sessions/{id}/messages/{message_id}/attachments/{attachment_id}` | 下载该消息对应的本地原件；文件缺失或校验失败时返回 404 |
| POST | `/api/chat/insert` | 提交 `turn_id`、`session_id` 和 `message`；回合加入补充消息后返回 204，时机不可用返回 409 |
| POST | `/api/chat/cancel` | 按 `turn_id` 取消当前请求 |

Web、TUI 与 CLI 共用 `SessionStore.list()`：在应用列表条数上限前，按最后一条有效消息的时间倒序。撤销后的消息不计入排序；改名、摘要和沉淀记录不会把旧会话顶到前面。没有有效消息时使用创建时间。

侧栏每条会话在悬停或键盘聚焦时显示删除按钮，移动端常显。删除确认框列出引用该会话的活动待确认内容。确认时服务端复核会话、队列、正式记忆来源及清单版本。仍有正式记忆引用时拒绝删除，已弃用记忆的来源也受保护。候选有变化时，显示新清单并重新确认。检查通过后一起移除相关候选、会话记录、该会话上传到 Vault 的副本、思考附件和撤销日志。删除当前会话时 Web 先切换到空白对话。会话记录删除失败会恢复相关候选与附件。

最近一条已保存用户消息提供撤销和编辑重发按钮。编辑中的文本和附件选择在点击重发后才生效。两个按钮均调用 `POST /api/commands`，提交 `/undo`、`session_id` 和 `expected_message_id`。如果另一页面已发送新消息，后端拒绝撤销旧目标。成功重发时，修改后的消息优先于该会话中已有的排队消息。

FastAPI 自动提供 `/openapi.json` 与 `/docs`。

`POST /api/chat` 请求体：`{"message":"...","session_id":null}`。`session_id` 必须匹配服务端当前会话，只有尚未发送首条消息的新会话使用 `null`。过期标签页收到 `409 session_mismatch`，消息不会写入。每个 SSE 帧带单调递增的 `id`/`seq` 和 `event` 类型，`data` 为 JSON：

| event | 关键字段 | 含义 |
| --- | --- | --- |
| `start` | `turn_id`, `session_id` | 请求开始 |
| `reasoning_delta` | `text`, `phase` | 思考增量；`phase` 按模型请求递增，不同工具轮次不混淆 |
| `reasoning_end` | `text`, `seconds`, `phase` | 该阶段思考定稿，使用完整文本校准 |
| `content_delta` | `text` | 正文增量；前端收到后折叠 Thinking |
| `tool_start` | `name` | 工具调用开始 |
| `tool_end` | `name`, `text`, `ok` | 工具结果展示，与 `tool_start` 按调用顺序配对；`ok=false` 表示调用失败，前端当场显示 `Failed` |
| `event` | `text` | 其他事件（召回、沉淀提示等） |
| `progress` | `text` | 阶段文案 |
| `done` | `text`, `steps`, `estimated_tokens` | 正常完成，正文以完整文本定稿 |
| `cancelled` | — | 已停止，保留半截输出 |
| `failed` | `message` | 失败，不泄露堆栈 |

只有模型提供思考时才产生 Reasoning 事件。客户端只消费统一事件；Provider 协议在后端适配。浏览器断流后，后端继续执行并保存该回合。客户端可查询队列、重新订阅事件，并从会话历史加载定稿。主动停止则发送匹配的 `turn_id`。

## Slash Command 与快捷键

### 单一命令目录

命令名称、用法、说明和子命令定义在 `src/syvrio/application/commands.py`，不依赖 Textual / Rich / HTTP。`src/syvrio/app/command_catalog.py` 变成薄转发，TUI 现有调用点与测试无需改动。Web 通过 `GET /api/commands` 取得同一份目录，用于输入框补全、命令面板和 `/help`；**React 端不维护第二套命令定义**。新增命令只需在共享目录加一行、在 `CommandUseCases` 复用 Application 用例、由两端各自决定展示方式。

`WEB_CAPABILITIES` 用能力标记说明每个命令在 Web 的语义：

| `support` | 含义 | 例子 |
| --- | --- | --- |
| `server` | Web 后端通过 `CommandUseCases` 执行，复用 `ConversationUseCases` / `SettingsUseCases` / `MemoryUseCases` 等 | `/new`、`/model`、`/status`、`/undo` |
| `client` | 纯浏览器交互 | `/sessions` 展开会话栏；`/setup` 打开设置面板；`/exit`、`/quit` 返回入口视图，**不会关闭后台 Web Server** |

- `/import` 与输入框附件按钮打开同一个本地多文件选择器。可选择文档和图片，取消不会改变草稿。选好的附件在按发送后才交给当前会话。命令不接受服务器路径。
- `/git` 显示同步状态，`/git sync` 二次确认后执行同步。`/pending approve|reject|edit <id>` 和 `/inferences confirm|reject|correct <id>` 可从 Web 审查记录。
- `/pending` 每条候选只列一次。选中后查看完整正文、主题、待澄清问题和相关记忆 ID，可编辑多行内容、批准或拒绝；提交前需确认。写入成功原位刷新列表，失败时可重试且保留编辑内容。编辑表单通过命令请求的 `pending_text` 字段发送正文，独立于斜杠命令的长度上限。
- `/profile` 展示最近生成的画像。`/dream <n>` 提交带有请求预算的后台回顾，`/dream status|cancel|resume` 查询、取消或继续。关闭客户端后，定时任务继续由常驻后端调度。
- 输入 `/undo` 或点击最近一条用户消息上的撤销按钮，会立即发出取消信号。已经开始的写工具先安全收尾，再撤销该回合的对话和有日志的本地文件写入。撤销成功后回填原文，暂停队列直到下一次主动发送。已有输入草稿时，等清空后再回填。受影响的文件后来被改动时，整次撤销会被拒绝。
- 未识别的 `/` 命令返回 `unknown` 并提示 `/help`；前端把所有 `/` 行发往 `/api/commands`，后端 `/api/chat` 也会拒绝斜杠开头的消息（`422 command_required`），双重保证不会误发给 LLM。
- 多标签页：`POST /api/commands` 携带的 `session_id` 与 Runtime 当前会话不一致时返回 `409 session_mismatch`，提示刷新，避免旧标签页操作到别人的会话。

### 生成期间的命令

Web 单 Turn 模型下，`CommandUseCases.BUSY_READ_ONLY` 与 `BUSY_NO_ARGUMENT` 明确哪些命令可在生成中执行：只读命令（`/help`、`/status`、`/summary`、`/topics`、`/pending`、`/inferences`、`/profile`、`/dream`、`/git`）以及不带参数的 `/model`、`/think`、`/context`。`/undo` 在生成中额外承担取消当前 Turn 的语义。该集合除 `/undo` 外保持为 TUI `SAFE_COMMANDS` 的子集，并有测试断言，避免两个客户端语义分歧。其余命令返回明确的 `busy`，输入框不会因此被永久禁用。

### 输入与补全

- 输入 `/` 显示候选，前缀实时过滤；`↑ / ↓` 选择、`Tab` 补全、`Enter` 执行选中的命令或子命令、`Escape` 关闭候选；鼠标点击与移动端触摸同样可选。
- 候选列表与输入卡片共用左右边界并独立滚动。可用高度较小时，候选进入输入区的正常布局，留在顶栏下方。
- 中文输入法组字状态（`isComposing`、`keyCode 229`、`Process` / `Unidentified`）不会触发 `Enter`、`Tab` 或任何全局快捷键。
- 斜杠命令的结果与错误在站内弹窗显示。状态、摘要和主题等使用可滚动的结果窗口；`/sessions` 用弹窗浏览和切换会话，`/help` 打开命令面板，`/rename` 可在弹窗直接编辑标题。
- `/model`、`/think`、`/context` 打开选项弹窗。选择后在弹窗显示设置结果，模型变更后刷新顶栏。
- `/pending` 与 `/inferences` 共用列表和详情布局。审核按钮与键盘操作触发同样的确认流程，提交失败保留草稿。`/git` 可通过弹窗请求同步。
- 模型目录是**进程内缓存**（`llm/client.py` 的 `_MODEL_CACHE`），Web 进程刚启动、或供应商未暴露 `/models` 时可能为空。此时 `/model` 仍列出当前配置模型并明确提示，不会输出空白列表；`/model refresh` 会真正向已配置供应商发起一次有界拉取（单供应商 5 秒超时），拉不到时说明原因而不是谎称「需要本地终端」。缓存为空时仍可用 `/model <当前provider>/<模型ID>` 按 ID 切换，未知供应商仍拒绝。
- 命令面板（`Ctrl+Shift+P` / `Alt+P`，或顶栏 `Commands` 按钮）复用同一目录与分发逻辑：搜索、方向键、Enter 执行、Escape 关闭并恢复输入框焦点，同时列出快捷键和浏览器保留键说明。

### 设置面板

顶栏 `Settings` 按钮、`Alt+S` 或 `/setup` 打开同一个设置面板（`web/src/components/SettingsPanel.tsx`）。模型相关读写仍走 `POST /api/commands`，供应商管理走 `GET/POST /api/settings/providers`，复用 TUI 相同的 Application 用例，React 端不保存设置副本：

| 分区 | 数据来源 | 说明 |
| --- | --- | --- |
| Model | `GET /api/settings/models?q=…`、`/model <provider>/<model>` | 刷新按钮右侧可按名称搜索所有已配置厂商的可用目录。首次自动获取，手动刷新可重新拉取。点选结果才切换 |
| Appearance | 浏览器 `localStorage` | 默认跟随系统 / 浏览器明暗主题，也可固定亮色或暗色；只保存在当前浏览器 |
| Thinking | `/think` | 优先读取模型目录为当前模型公布的档位，缺少有效档位或查询失败时采用预设。`auto` 置顶，`default` 表示使用供应商默认设置 |
| Context | `/context` | 默认 100K，提供 100K、200K、400K、600K 预设和自定义 token 数；受模型上限约束 |
| Providers | `GET/POST /api/settings/providers/{provider}` | 添加/更新 Key、修改 Base URL、删除供应商、自定义模型；**保存的 Key 永不回传浏览器** |
| About | `/api/health` | 版本、当前 provider/model、是否生成中、单客户端锁、静态资源状态与快捷键 |

打开面板只读取当前设置；删除供应商仍需二次确认，服务端复用 `SettingsUseCases` 的「当前供应商不可删」等守卫，失败原因以 `provider-error` 原样展示。

### 上下文占用

发送键左侧的水球显示当前会话的 token 占比估算。悬停或点按可查看估算用量和模型的配置上限，达到压缩阈值时水球高亮。水波在占用 ≥10% 且 ≤85% 时流动（低于 10% 时保持静止，避免刚有内容就出现动效）；系统开启减少动态效果时也保持静止。 自动压缩开始时显示压缩状态，摘要保存成功后按重新估算的用量降低水位。手动执行 `/compact` 也会显示状态；没有可压缩的历史时不会显示成功动画。Web 复用 TUI 的历史上下文估算，估算不触发模型请求，也不计入尚未发送的输入或本轮临时召回内容。

### 主题、侧栏与消息滚动

- 服务端保存用户消息后，Web 立即刷新会话顺序，模型回复期间无需刷新网页。排序使用最后一条有效用户消息的时间。

- 界面使用同一套 CSS 变量生成亮色与暗色主题。默认 `Follow system` 响应 `prefers-color-scheme` 的实时变化，也可在 Appearance 中固定 Light / Dark；选择只写入当前浏览器的 `syvrio.theme`，移动端地址栏 `theme-color` 同步切换。显式主题在样式加载前恢复，避免刷新时黑白闪烁。
- 桌面端点击侧栏头部的面板图标会收起整列（`display: none`，宽度让给对话区），顶栏左侧同一图标或 `Ctrl+B` / `Alt+B` 重新展开；桌面选择保存在 `syvrio.sidebar`。手机端仍使用默认收起的抽屉与蒙层，跨断点时自动折叠。
- 恢复或切换会话时，消息区在绘制前以 `instant` 直接定位到最新一条，不播放从顶部滑到底部的长动画；同一会话的流式更新保留平滑跟随，用户向上翻阅后暂停跟随，回到底部附近后恢复。
- 展开的思考面板同样是固定高度的独立滚动区：流式期间跟随最新一行，用户向上翻阅后停止跟随、回到底部附近恢复；已结束的思考从顶部开始阅读。

### 快捷键与浏览器冲突

以 TUI 实际绑定为准（`chat_app.py`：`Ctrl+P` 在模型面板管理供应商、`Ctrl+B` 会话栏、`Ctrl+Q` 退出、`Escape` 关闭/取消）。映射集中在 `web/src/state/shortcuts.ts`：

| TUI | Web | 说明 |
| --- | --- | --- |
| 命令面板 | `Ctrl+Shift+P` / `Alt+P` | `Ctrl+P` 是浏览器打印，不拦截；Firefox 的 `Ctrl+Shift+P` 是隐私窗口，`Alt+P` 是跨浏览器可靠替代 |
| `Ctrl+P`（模型面板内管理供应商） | 在模型选项弹窗按 `Alt+S` | 打开设置的 Providers 分区，保留浏览器打印键 |
| TUI `/setup` | `Alt+S`、顶栏 `Settings` 或 `/setup` | 设置面板：模型、思考强度、上下文、供应商 Key 与端点 |
| `Ctrl+B`（会话栏） | `Ctrl+B` / `Alt+B` | 可稳定拦截，另有顶栏侧栏图标按钮 |
| `Ctrl+Q`（退出） | 不拦截 | 浏览器保留；`/exit` 只返回入口视图 |
| `Escape` | `Escape` | 关闭弹层 / 关闭补全 / 停止生成 |
| `↑ / ↓`、`Tab`、`Enter`、`Shift+Enter` | 同 TUI 语义 | 输入区支持补全与换行；弹窗内可移动焦点和选择 |
| `C / R / E`、`Page Up / Page Down` | 相同 | 审核弹窗内确认、拒绝、编辑及翻阅详情 |
| `← / →` | 相同 | 推断弹窗切换状态筛选 |
| `Ctrl+S` | `Ctrl+S` / `⌘S` | 审核或重命名弹窗保存，Git 弹窗请求同步 |

`KEY_BINDINGS` 是唯一映射表；`Ctrl+Q`、`Ctrl+P` 明确列在 `RESERVED_KEYS` 中不做拦截，避免破坏浏览器正常功能，并在命令面板里显示替代组合键。

## 跨客户端会话与并发

- Web、TUI、CLI、MCP 可以同时连接。每个网页标签页和终端独立选择会话，打开同一会话时共享正在生成的回复、等待队列及保存的历史。不同会话能同时生成。
- 普通发送进入服务端队列，按接收顺序逐条执行。关闭页面、终端或 SSE 连接后，已接收的消息继续执行。网页刷新后从后端读取队列及当前回复。
- Ctrl+Enter 或 **Insert** 在模型请求期间申请补充。工具运行时拒绝插入。已接收的补充仅在模型响应完整结束、相关工具结果写入后加入上下文；超过预算或回合状态变化时保留草稿。
- 通过 `/undo` 打断当前回合，按会话及目标消息 ID 核对后撤销本地写入并回填原文。其他会话已修改相关文件或添加引用时，后端拒绝整次撤销。排队消息暂停，用户下一次主动发送后继续。
- 同一 Vault 在本机只能由一个常驻后端写入，即使使用不同的 Runtime 目录也一样。关闭后端会等待后台工作停止，再释放所有权。

## 移动端适配

页面按移动浏览器设计，不依赖 App 或 WebView：

- **布局断点**：`≤767px` 侧栏变成 `82vw` 抽屉（蒙层点击 / `«` 关闭，默认收起），顶栏显示 `Sessions` 入口；`≤380px` 进一步压缩顶栏按钮与设置标签；横屏矮屏（`max-height: 520px`）单独压缩欢迎区间距与弹层内边距。
- **软键盘**：`index.html` 使用 `viewport-fit=cover` 与 `interactive-widget=resizes-content`；`App.tsx` 监听 `visualViewport` 把可用高度写入 `--app-height`，`.app` 优先使用该值，软键盘压缩可见区域时启用紧凑布局，隐藏欢迎区和快捷按钮，给输入框及命令列表留出空间。
- **输入缩放**：移动端 `textarea` / 命令面板输入 / 设置与供应商输入统一 `font-size: 16px`，避免 iOS 聚焦时整页放大；`touch-action: manipulation` 去掉双击缩放的点击延迟。
- **弹层高度**：命令面板、确认框、选项面板与设置面板优先使用 `dvh`（不支持时退回 `vh`），手机键盘弹出或地址栏收起后仍可见；设置面板在手机上为顶部横向标签 + 纵向内容，窄屏自动换行。
- **安全区**：顶栏、侧栏与输入框使用 `env(safe-area-inset-*)`，适配刘海屏横屏与底部 Home 指示条；`index.html` 提供 `theme-color`，由主题控制逻辑同步为当前亮色或暗色背景。
- **触摸可用性**：`hover: none` 设备上删除按钮常显（宽度断点之外再按能力判断），会话行、关闭按钮、设置选项与供应商按钮最小高度 36–44px；消息区 `overscroll-behavior: contain`，滚动不会带动整页回弹。

## 安全边界

默认只监听 `127.0.0.1`；不启用 CORS，不信任 `X-Forwarded-*`；校验 `Host` 与写请求的 `Origin` 白名单；会话 ID 使用与 `SessionStore` 相同的正则校验，拒绝路径穿越；静态资源只从构建目录提供；Markdown 经 `react-markdown` + `rehype-sanitize`；未处理异常返回通用 500，不向浏览器暴露堆栈；uvicorn access log 关闭，普通日志不记录聊天正文；API Key 不出现在任何响应中。第一版按单用户私有部署设计，网络访问由 Tailscale 设备/用户策略控制。

## WSL + systemd 常驻

本节适用于从源码运行的系统级服务；如需由 `syvrio update` 管理安装版本，请使用下文的[用户级服务](#pypi-自动更新)。源码模板见 `docs/syvrio-web.service`，把 `REPLACE_USER`、`REPLACE_REPO`、`REPLACE_UV`、`REPLACE_TAILSCALE_HOST` 替换成实际值后安装。

### 启用 systemd

在 WSL 发行版内写入 `/etc/wsl.conf`：

```ini
[boot]
systemd=true
```

在 Windows PowerShell 执行 `wsl --shutdown`，重新打开发行版，再检查：

```bash
ps -p 1 -o comm=                 # 应输出 systemd
systemctl is-system-running
```

### 安装与日常操作

```bash
sudo cp docs/syvrio-web.service /etc/systemd/system/syvrio-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now syvrio-web
systemctl status syvrio-web
journalctl -u syvrio-web -f
sudo systemctl restart syvrio-web
sudo systemctl stop syvrio-web
```

`Restart=on-failure` + `RestartSec=5` 覆盖进程异常退出；`journalctl -u syvrio-web -b -1` 可查看上次启动日志。异常重启后 `client.lock` 由内核释放，无需手工清理。

### Windows 侧生命周期

- **systemd enabled 不等于 Windows 开机后一定可访问**：WSL 实例没启动时，unit 不会运行。
- 登录后启动：`wsl.exe -d <发行版> --exec systemctl is-system-running` 会拉起实例；Windows 任务计划程序可在登录时触发。
- 无人值守（用户未登录）：需用任务计划程序以「不管用户是否登录都要运行」和最高权限启动 `wsl.exe -d <发行版> --exec /bin/true` 保持实例存活。该方式仍受 WSL VM 生命周期影响，不能等同于 Windows 服务。
- `wsl --shutdown`、`wsl --terminate <发行版>`、Windows 重启都会停止服务；Windows 睡眠/休眠期间 WSL 虚拟机会暂停或被回收，唤醒后需重新检查 `systemctl status syvrio-web`。
- localhost 转发验证：WSL 内 `curl -fsS http://127.0.0.1:8765/api/health`；Windows PowerShell `curl.exe http://127.0.0.1:8765/api/health` 应返回同一结果。WSL2 默认开启 `localhostForwarding`，若不通再检查 `C:\Users\<用户>\.wslconfig`。

## Tailscale Serve（仅 tailnet，不启用 Funnel）

### 拓扑：浏览器访问远程部署的实例

常见部署是 Syvrio Web 跑在**远程主机**（另一台 Windows / WSL Debian / Linux 服务器）上，仍只监听 `127.0.0.1:8765`；本机浏览器通过 Tailscale 访问。因为服务没监听 Tailscale 网卡，**直接打开 `http://<Tailscale-IP>:8765/` 是连不上的**，必须由远程主机上的 Tailscale 把 loopback 端口代理出去：

```text
本机浏览器 ── WireGuard ──> 远程主机 tailscaled ── 127.0.0.1:8765 ──> Syvrio Web
          https://<远程机器名>.<tailnet>.ts.net/
```

两条路径：

- **推荐：Tailscale Serve**（远程主机执行 `serve --bg 8765`）。自动 HTTPS + MagicDNS 域名，服务保持只监听 loopback，本机不需要写 hosts。
- **可选：直连 Tailscale IP**。把远程服务监听改为 Tailscale 接口地址（`syvrio web --host <Tailscale-IP>`），并在白名单放行该 IP / 自定义短名；流量仍在 WireGuard 隧道内，但没有 TLS 证书且监听面变大。此时可在本机执行 `syvrio web --domain syvrio.local --domain-ip <Tailscale-IP> --map-only` 写一条短名映射，浏览器打开 `http://syvrio.local:8765/`；**远程服务的 `SYVRIO_WEB_ALLOWED_HOSTS` 必须含 `syvrio.local`**，否则 Host 校验返回 403。走 Serve HTTPS 时不要用短名，证书只对 MagicDNS 名有效。

CLI 语法按 Tailscale 1.104.5 的 `tailscale serve --help` 核对：`serve <target>` 的 target 是**本地端口**，默认以 HTTPS 443 暴露；`--bg` 后台持久，`--yes` 非交互。Windows 客户端的 `tailscale.exe` 通常不在 PATH，完整路径为 `C:\Program Files\Tailscale\tailscale.exe`。用 `tailscale status --json` 里的 `Self.DNSName` 取得远程主机的 MagicDNS 名（形如 `<机器名>.<tailnet>.ts.net`）。

### 步骤（命令都在远程主机上执行）

1. 启动 Web 服务时把 MagicDNS 名加入白名单，否则写入类请求会返回 `403 untrusted_origin`：

   ```powershell
   $env:SYVRIO_WEB_ALLOWED_HOSTS = "<机器名>.<tailnet>.ts.net"
   uv run syvrio web --host 127.0.0.1 --port 8765
   ```

   同时用了自定义短名时写成 `"syvrio.local,<机器名>.<tailnet>.ts.net"`。WSL / systemd 常驻部署把该变量写进 `docs/syvrio-web.service` 的 `Environment=`。

2. 管理员 PowerShell 开启 Serve（target 填服务实际监听的端口）：

   ```powershell
   & "C:\Program Files\Tailscale\tailscale.exe" serve --bg 8765
   & "C:\Program Files\Tailscale\tailscale.exe" serve status
   ```

   旧写法 `serve --bg --https=443 127.0.0.1:8765` 在 1.104 仍可用，但推荐上面的简写。`--bg` 在设备重启或 `tailscale down/up` 后自动恢复；不带 `--bg` 时需手动重开。

3. **本机浏览器**访问 `https://<机器名>.<tailnet>.ts.net/`，健康检查为 `/api/health`。两台设备需登录同一 tailnet 且 ACL 允许。

4. 关闭远程入口：`tailscale serve reset`（清空全部 Serve 配置）。

### 边界与排查

- **只用 Serve，不用 `tailscale funnel`**；不开放公网端口、不需要公网 IP，HTTPS 证书由 Tailscale 自动签发。
- 在管理控制台用 ACL `grants` 限制可访问 443 的设备与用户，不要用宽松的 `*` 源。
- 服务必须处于运行状态：Windows 睡眠 / 关机 / 重启、WSL 实例未启动都会导致远程不可达；`systemd enabled` 不等于开机即可访问。
- 排查：`tailscale status`、`tailscale ping <机器名>`、`curl.exe -I https://<机器名>.<tailnet>.ts.net/api/health`；403 查白名单，502/503 查本地 `127.0.0.1:<port>` 是否可达。

## 使用边界

Web 面向单用户私有部署。附件在用户设备上选择，手工发送后由常驻后端保存到 Vault 的 `attachments/` 目录；该目录由 Vault 的 Git 忽略。通过复制整个 Vault 可以一并迁移附件。

## 测试

```bash
uv run pytest -m web -q              # 后端 API / SSE / 并发 / 安全 / 命令目录与分发
(cd web && corepack pnpm test)       # 前端单元测试、组件交互、补全与快捷键
(cd web && corepack pnpm build)      # 类型检查 + 生产构建
uv run python scripts/build_web.py --check  # 校验已暂存静态资源
```

命令相关自动化覆盖：目录与 TUI 一致性、能力标记、未知命令不转发 LLM、`/import` 打开本地多选附件，不读取服务器路径、`/undo` 优先取消与回填、忙期策略、会话错配 409、跨站写拒绝、选项面板不重开；前端覆盖前缀/子命令补全、`Tab`/`Enter`/`Escape`、方向键、IME 组字、快捷键冲突、命令面板、设置面板（模型切换、思考强度、上下文、供应商分区、About 不泄露 Key）、危险确认、命令失败后输入框恢复、生成中只读命令不打断流式回复。


## PyPI 自动更新

`syvrio update` 切换由 Linux/WSL 用户级 systemd 管理的 Web 服务。需要本机已装有 Python ≥ 3.12（任意补丁版本即可，不要求 3.12）、`uv`、可访问的用户 systemd 实例以及带 Web 资源的 PyPI wheel。

新版环境基于「当前版本 venv 的基础解释器」创建：更新器先在本机验证解释器存在、可执行且满足目标 wheel 的 `requires-python`，再以显式路径调用 `uv venv --no-python-downloads`，uv 绝不联网下载解释器。找不到兼容解释器时立即报错（错误中会列出尝试过的候选），可通过更新配置指定其他本地 Python。

首次接入时，在 `~/.syvrio/update/` 下创建 `current` 符号链接，指向已安装且包含 Web 前端的版本的 `bin/syvrio`。建立用户级服务（如 `syvrio-web.service`），让其 `ExecStart` 使用该链接，例如：

```ini
[Service]
ExecStart=%h/.syvrio/update/current web --host 127.0.0.1 --port 8765 --no-hosts
TimeoutStopSec=5min

[Install]
WantedBy=default.target
```

将现有 Web 服务的配置、凭据环境、域名白名单和启动参数迁入该服务。切换前停掉旧的源码服务，再启用此 unit，核对 `/api/health` 中的版本和前端状态。跨 Windows 重启仍需确保 WSL 和用户 systemd 实例会启动。

创建 `~/.syvrio/update/config.toml`：

```toml
channel = "evo"
service = "syvrio-web.service"
port = 8765
# 可选：显式指定本地解释器；默认用当前版本 venv 的基础解释器
# python = "/usr/bin/python3.13"
# 可选：显式指定 uv；默认取调用者 PATH 上的 uv，找不到就直接报错
# uv = "/home/<user>/.local/bin/uv"
```

`stable` 只选正式版，`evo` 只选形如 `0.5.2rc1` 的候选版。更新不会降级。首次切换到 Evo 时，初始安装版本必须低于目标预发布版本。服务正常启动以后，停用旧的源码拉取任务，定时任务只运行 `syvrio update`。定时任务的 PATH 须包含 `syvrio`，并可访问对应的用户 systemd 实例；`uv` 须位于 PATH，或在 `config.toml` 中配置绝对路径。未配置且 PATH 里没有 `uv` 时，更新器在下载前就报错，不会走到一半才失败。更新命令应在 Web unit 外运行。

如果 `uv` 只通过开发环境的 `env.sh` 注入 PATH，注意 `wsl.exe -d <发行版> -- bash -s` 这类非登录、非交互 shell 不读 `.bashrc` / `.profile`，此时要么 `bash -lc 'syvrio update'`、要么先 `source <env.sh>`，要么在 `config.toml` 里写死 `uv` 的绝对路径。

新版先安装在独立环境，校验通过后才停止旧版。新版启动后健康检查最多等待 120 秒（容忍慢启动的初始化与记忆补记），切换链接、启动或健康检查失败时，更新器检查当前链接指向；如果已指向新版，先停止新版并恢复旧链接，再启动并验证旧版。若链接已经指向旧版，直接尝试启动旧服务。遇到意外链接目标或恢复失败时会报出具体阶段，留待人工处理。停止单元最多等待 6 分钟并确认单元确实停下，避免「更新器以为停止失败、旧服务却被继续停掉」的半停止状态。更新保留旧安装目录用于回滚，配置和 Vault 继续使用服务定义的原有路径。

### 旧版更新器的一次性手工升级

早于解释器修复的版本（如 0.5.3rc1）在 `_install` 中硬编码 Python 3.12，本机只有 3.13 时 `syvrio update` 会超时；这类旧版本需手工升级一次，之后的 `syvrio update` 恢复自动。`current` 始终指向可执行文件 `<版本目录>/bin/syvrio`（不是版本目录本身），启动脚本经 `readlink -f current` 取可执行路径、再用其同目录的 `bin/python` 启动，因此链接目标必须一致。步骤（服务名、端口、路径以实际部署为准；旧版本目录全程保留）：

```bash
# 0. 记录现状（回滚凭据）
readlink -f ~/.syvrio/update/current    # → ~/.syvrio/update/versions/<旧版本>/bin/syvrio
systemctl --user is-active syvrio-web.service

# 1. 显式用本地解释器建新环境并安装；禁止 uv 下载解释器
UV_PYTHON_DOWNLOADS=never uv venv --no-python-downloads \
  --python /usr/bin/python3.13 ~/.syvrio/update/versions/<新版本>
uv pip install --python ~/.syvrio/update/versions/<新版本>/bin/python "syvrio==<新版本>"

# 2. 切换前校验（旧服务仍在运行）
~/.syvrio/update/versions/<新版本>/bin/syvrio --version
~/.syvrio/update/versions/<新版本>/bin/python -c \
  "from importlib.resources import files; assert files('syvrio.app.web').joinpath('static', 'index.html').is_file()"

# 3. 停旧服务 → 原子切换（临时链接 + mv -T，等价 os.replace）→ 启动 → 健康检查
systemctl --user stop syvrio-web.service
ln -s ~/.syvrio/update/versions/<新版本>/bin/syvrio ~/.syvrio/update/.current.next
mv -T ~/.syvrio/update/.current.next ~/.syvrio/update/current
readlink -f ~/.syvrio/update/current     # 确认指向 <新版本>/bin/syvrio
systemctl --user start syvrio-web.service
curl -fsS http://127.0.0.1:8765/api/health   # 启动慢可等几秒重试；核对 version 与 static_ready

# 4. 失败回滚：停 → 指回旧版本可执行文件 → 启动 → 健康检查
systemctl --user stop syvrio-web.service
ln -s ~/.syvrio/update/versions/<旧版本>/bin/syvrio ~/.syvrio/update/.current.next
mv -T ~/.syvrio/update/.current.next ~/.syvrio/update/current
systemctl --user start syvrio-web.service
```

应用配置、Vault 与 Runtime 应放在版本目录之外。如果部署了自定义启动脚本，也保留在版本目录之外，让它始终通过 `current` 找到所选版本。
