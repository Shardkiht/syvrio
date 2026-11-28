# Syvrio Web

Web、Textual TUI、CLI 和 MCP 连接到同一常驻后端。每个会话有独立的运行时和发送队列。后端持有 Vault 的写入所有权，统一处理工具调用、撤销、自动沉淀及定时回顾。

```text
浏览器 → Web 网关 ─┐
TUI / CLI / MCP ──┴─ 常驻后端 → 会话 / 记忆 / 模型
```

Web 网关直接提供页面和静态资源，通过 REST 和 SSE 转发 API 请求与事件。网关检查 Host 和 Origin，并阻止访问仅供本机程序使用的管理接口。常驻后端独立装配共享 API，只监听本机地址。首次连接会自动启动，退出客户端后继续运行。使用 `syvrio backend status` 查看状态，`syvrio backend stop` 安全停止。

## 开发与运行

PyPI wheel 已包含 Web 静态资源，安装并运行 `syvrio setup` 后，用 `syvrio web` 启动，浏览器打开 `http://127.0.0.1:8765/`。

从源码运行需要 Python 3.12+、uv、Node.js 和 Corepack。首次使用先运行 `uv run syvrio setup`。

```bash
uv sync
uv run python scripts/build_web.py
uv run syvrio web
```

前后端联调时，可跳过静态构建。先在仓库根目录运行 `uv run syvrio web --port 8746`，再用另一个终端启动 Vite：

```bash
cd web
corepack pnpm install --frozen-lockfile
corepack pnpm dev
```

拉取包含新 API 的代码后，已运行的常驻后端需要重启。若会话搜索返回 `404 session_not_found`，先等待进行中的回复结束，再使用同一配置运行 `uv run syvrio backend stop` 和 `uv run syvrio backend start`；Web 网关会在下一次读取时重新连接。

开发和静态预览共用 `http://127.0.0.1:7987/`，`/api` 转发到 `127.0.0.1:8746`。这两个端口分别对应电话键盘上的 SYVR 和 VRIO。端口占用时启动会报错退出。环境变量 `SYVRIO_WEB_PORT` 可设置 Web 网关的默认端口，命令行 `--port` 优先。未暂存静态资源时，访问网关根路径会返回 `503 assets_missing`，网关的 `/api/health` 返回 `static_ready: false`。API 和终端仍可使用。

### 本地域名映射（浏览器只输域名）

`syvrio web --domain <域名>` 把 `127.0.0.1 <域名>` 追加到系统 hosts（先备份为 `hosts.syvrio-backup`，幂等、不覆盖已有映射），同时把该域名加入可信 Host / Origin 白名单。服务仍只监听 loopback，域名只是本机解析别名，不改变监听范围。

```bash
uv run syvrio web --domain syvrio.local --port 80
# 浏览器打开 http://syvrio.local/
```

- 只有 `--port 80` 才能省略端口号；Windows 绑定 80 通常无需提权，Linux / WSL 需要 root 或 `CAP_NET_BIND_SERVICE`。
- 写 hosts 需要管理员 / root 权限。权限不足时命令不会失败，而是打印需要手动添加的那一行；`--no-hosts` 只打印不写文件。
- 额外域名（Tailscale / 局域网主机名等）用 `SYVRIO_WEB_ALLOWED_HOSTS=host1,host2`；也可只靠环境变量、不写 hosts。
- 撤销映射可调用 `src/syvrio/backend/hosts.py` 的 `remove_hosts_mapping()`，或手动删除 hosts 中对应行；备份保留在 hosts 同目录。
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
| GET | `/api/health` | 版本、供应商与模型、生成状态、活动轮次、静态资源状态，以及 Web/TUI 选文件时使用的附件格式、数量和大小限制 |
| GET | `/api/context-usage?session_id=<id>` | 会话上下文估算、配置上限与压缩阈值；省略 ID 表示空白草稿 |
| GET | `/api/sessions?limit=100` | 会话列表 |
| POST | `/api/sessions` | 回到新对话首页（返回 `draft=true`、`session_id=null`，不写文件；首条消息才创建会话） |
| POST | `/api/sessions/{id}/switch` | 切换或恢复会话。传 `include_history=true` 时同时返回 `history_version`。首次请求或 `known_version` 与当前版本不同时，`history` 含目标会话的正文；版本一致时为 `null` |
| PUT | `/api/sessions/{id}/title` | 用 `title` 修改指定会话的名称，成功返回 204 |
| GET | `/api/sessions/{id}/deletion-preview` | 读取随会话移除的待确认内容及本次清单版本 |
| POST | `/api/sessions/{id}/archive`、`/api/sessions/{id}/restore` | 归档或还原。普通 `DELETE /api/sessions/{id}` 也执行归档 |
| GET | `/api/sessions?archived=true`、`/api/sessions/{id}?archived=true` | 人工浏览归档列表和原始会话。列表可用 `limit`、`offset` 翻页，下载归档附件同样需要 `archived=true` |
| DELETE | `/api/sessions/{id}?permanent=true&confirmed=true` | 仅可永久删除已归档会话，存在相关候选时提交已查看的 `pending_revision` |
| GET | `/api/sessions/search?q=关键词` | 按连续文本搜索活动会话的标题和对话正文 |
| GET | `/api/sessions/{id}/tools/{record_id}` | 按历史记录 ID 取完整工具结果。历史列表只携带长结果的预览、总字符数和预览标志。归档结果须传 `archived=true`，撤回的结果无法读取 |
| GET | `/api/sessions/{id}/messages/{message_id}/context` | 读取命中消息及前后各一条可见对话 |
| GET | `/api/pending/{item_id}/evidence` | 按候选的已存引用读取原话与关联记忆，包括手动追溯的归档原话 |
| GET | `/api/sessions/{id}?limit=300` | 按时间顺序返回最近的历史窗口，思考正文默认折叠。响应的 `earlier_cursor` 用于下一次请求的 `before_seq`，为空时已到开头。归档历史加 `archived=true`。读取活动会话的默认 300 条窗口时可传 `include_version=true`，从响应头 `X-Syvrio-History-Version` 取得下次切换可用的版本 |
| GET | `/api/sessions/{id}/messages/{message_id}/draft` | 读取最近回合的原文及附件清单供编辑；消息已变更时返回 409 |
| GET | `/api/settings/providers` | 供应商、端点和配置状态；不返回已保存的 Key |
| POST | `/api/settings/providers/{provider}` | 添加、更新或删除凭据，以及修改端点 |
| GET | `/api/commands` | 共享命令目录（名称、用法、能力标记、子命令、快捷键） |
| POST | `/api/commands` | 执行斜杠命令，返回结构化结果，不经过 LLM |
| POST | `/api/chat?receipt=1` | 将消息放入当前会话队列，返回 202 和回合 ID。提交 ID 随回执和队列返回；202 表示已接收请求或入队，不等同于已持久化。正式送达回执与保存用户消息同事务提交。Web 提交时立即显示用户气泡，左侧三点表示正在发送。投递失败时可点红色圆形按钮，以原提交 ID 重试。省略回执参数时直接返回该回合的 SSE 流 |
| GET | `/api/chat/queue` | 查询本会话的活动回合、队列、暂停状态和修订号，以及正在回复的会话 ID 列表 `replying_session_ids` |
| DELETE | `/api/chat/queue/{turn_id}` | 移除尚未开始的排队消息 |
| GET | `/api/chat/turns/{turn_id}?after=<seq>` | 订阅回合更新，从指定序号后重放已发出的事件 |
| POST | `/api/chat/attachments` | 发送带附件的消息；同样支持 `?receipt=1`。使用 multipart 表单，可重复提交 `files`，可选字段为 `message` 和 `session_id` |
| GET | `/api/sessions/{id}/messages/{message_id}/attachments/{attachment_id}` | 下载该消息对应的本地原件。`preview=true` 可内联显示 PNG、JPEG、WebP 和 GIF，可与 `archived=true` 同用。文件缺失或校验失败时返回 404 |
| POST | `/api/chat/insert` | 提交 `turn_id`、`session_id` 和 `message`；回合加入补充消息后返回 204，时机不可用返回 409 |
| POST | `/api/chat/cancel` | 按 `turn_id` 取消当前请求 |

Web、TUI 与 CLI 共用 `SessionStore.list()`：在应用列表条数上限前，按最后一条有效消息的时间倒序。撤销后的消息不计入排序；改名、摘要和沉淀记录不会把旧会话顶到前面。没有有效消息时使用创建时间。

首轮完整问答结束后，后台模型会概括话题，生成与对话语言一致的短标题。点击顶栏的会话名可就地改名，输入时侧栏同步预览。按 Enter 或离开输入框保存，按 Esc 取消，留空恢复原标题。手动名称最多 80 个字符，会优先于后台生成的标题。

点击侧栏的归档按钮会显示目标会话标题，默认选中取消。确认后会话移入 Archive。用户可在侧栏底部打开 Archive，查看原始消息、下载附件或还原。目标会话正在写入、消息排队或运行标题、沉淀、上下文补全任务时，服务会说明原因。只读标签页可以同时打开；归档后这些标签页回到空白会话并保留尚未发送的输入。

永久删除需在 Archive 中打开目标会话，查看将被移除的活动候选，再手动确认。候选有变化会要求再次确认。正式记忆引用来源时会阻止永久删除，包括已弃用的记忆。删除成功后移除会话、其候选、上传副本、思考附件和撤销日志。

最近一条已保存用户消息提供撤销和编辑重发按钮。撤销通过 `POST /api/commands` 提交 `/undo`、`session_id` 和 `expected_message_id`。编辑重发通过 `POST /api/chat?receipt=1&replace=<原消息 ID>` 提交修改稿，有附件时使用同参数的 `/api/chat/attachments`。后端校验正文与上传，若原回复还在生成，会先停止并保存已收到的部分。替代消息与原回合的撤回在同一次写入中生效，成功后才返回回执并继续生成。替代回合优先于已有排队消息。后台启动、附件保存或会话写入失败会返回 `503 edit_not_sent`。原问答、原附件和工具写入的记忆仍可读取，可以在原会话修改草稿并重试。若只丢失了回执，重试同一修改稿可以取回近期回执，回执过期后仍会核对原消息 ID。

编辑时的首次修改即保存在本标签页的会话存储，有附件时先将文件副本保存到 IndexedDB。编辑期间如果另一客户端发送新消息，可取回原草稿和附件。未修改的编辑器在刷新后自动关闭，取消编辑会清理副本。提交的修改稿携带独立的回执标识。刷新后可在原会话取回草稿，只有点击重试才会提交。成功、丢弃草稿或删除会话会清除本地副本，浏览器在后续附件写入或清理时回收超过七天的文件副本。若浏览器禁止存储，页面会提示保留当前标签页。

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

`src/syvrio/application/command_catalog.py` 定义命令名称、用法、说明、子命令和文本拆分规则。TUI 使用这一协议提供补全。Web 通过 `GET /api/commands` 取得同一份目录及能力信息，供输入框、命令面板和 `/help` 使用。`src/syvrio/application/commands.py` 负责校验命令、检查当前状态并调用相应的应用用例。

`WEB_CAPABILITIES` 用能力标记说明每个命令在 Web 的语义：

| `support` | 含义 | 例子 |
| --- | --- | --- |
| `server` | Web 后端通过 `CommandUseCases` 执行，复用 `ConversationUseCases` / `SettingsUseCases` / `MemoryUseCases` 等 | `/new`、`/model`、`/status`、`/undo` |
| `client` | 纯浏览器交互 | `/sessions` 展开会话栏；`/setup` 打开设置面板；`/exit`、`/quit` 返回入口视图，**不会关闭后台 Web Server** |

- **会话图片布局**。Markdown 正文图片与已发送图片附件共用 4:3 缩略图框，桌面上限 192 × 144 CSS 像素，视口宽度不超过 767 像素时为 144 × 108。容器更窄时继续等比缩小。缩略图完整显示原图并保留长宽比，小图保持原始尺寸，多张附件自动换行。点图先按视口缩放，选择 **Actual size** 可按原始尺寸滚动查看细节，**Fit to screen** 恢复全图。Esc、关闭按钮或点背景均可退出。带跳转链接的 Markdown 图片保留原链接目标。草稿图片采用 44 × 44 的方形缩略图，便于同时核对多张附件。
- `/import` 与输入框附件按钮打开同一个本地多文件选择器。可选择文档和图片，取消不会改变草稿。图片选中后在草稿中显示缩略图，按发送后才交给当前会话。消息保存后即可在对话中点图放大。归档也可预览，点文件名可下载原件。命令不接受服务器路径。
- `/git` 显示同步状态，`/git sync` 二次确认后执行同步。`/pending approve|reject|edit <id>` 和 `/inferences confirm|reject|correct <id>` 可从 Web 审查记录。
- `/topics` 与 Settings 使用相同大小的弹窗。目录可按标题或路径筛选，选定主题或进入 **All memories** 后可搜索记忆正文。匹配的原文带底色和实线下划线。
- 编辑中的记忆若已被其他操作更改，面板会保留草稿并读取最新正文和修订号。对照后可修改草稿，再次确认保存。目标已失效时仍可复制草稿。
- 顶栏右侧的 Pending 与 `/pending` 打开同一待审侧栏，可滚动并按正文、主题或 ID 搜索。选中后可查看问题和关联记忆、编辑多行正文、批准或拒绝。`/pending edit <id>` 直接打开完整原文，保留原文时需单独确认。表单通过 `pending_text` 提交，写入失败保留编辑内容。
- `/profile` 展示最近生成的画像。`/dream` 和 `/dream run` 确认后提交后台回顾，`/dream status` 查看进度。关闭客户端后，定时任务继续由常驻后端调度。
- 输入 `/undo` 或点击最近一条用户消息上的撤销按钮，会立即发出取消信号。已经开始的写工具先安全收尾，再撤销该回合的对话和有日志的本地文件写入。撤销成功后回填原文，暂停队列直到下一次主动发送。已有输入草稿时，等清空后再回填。受影响的文件后来被改动时，整次撤销会被拒绝。
- 未识别的命令或子命令显示 `Invalid command.`。斜杠输入走命令路径，聊天接口会拒绝斜杠开头的消息（`422 command_required`）。
- 多标签页：`POST /api/commands` 携带的 `session_id` 与 Runtime 当前会话不一致时返回 `409 session_mismatch`，提示刷新，避免旧标签页操作到别人的会话。

### 生成期间的命令

生成中的会话仍可使用只读命令，例如 `/help`、`/status`、`/topics` 和 `/pending`。用 `/model <厂商>/<模型>`、`/think <档位>` 和 `/context <token 数>` 可以保存随后回合使用的设置，同一回合的模型请求和工具续请求共用开始时的设置。各会话都在新回合实际开始时读取最新值。`/undo` 可打断当前回合，`/model refresh` 和 `/compact` 等操作仍须等待生成结束。

### 输入与补全

- 输入 `/` 显示候选，前缀实时过滤；`↑ / ↓` 选择、`Tab` 补全、`Enter` 执行选中的命令或子命令、`Escape` 关闭候选；鼠标点击与移动端触摸同样可选。
- 候选列表与输入卡片共用左右边界并独立滚动。可用高度较小时，候选进入输入区的正常布局，留在顶栏下方。
- 中文输入法组字状态（`isComposing`、`keyCode 229`、`Process` / `Unidentified`）不会触发 `Enter`、`Tab` 或任何全局快捷键。
- 斜杠命令的结果与错误在站内弹窗显示。状态、摘要和主题等使用可滚动的结果窗口；`/sessions` 用弹窗浏览和切换会话，`/help` 打开命令面板，`/rename` 可在弹窗直接编辑标题。
- `/model`、`/think`、`/context` 打开选项弹窗。选择后在弹窗显示设置结果，模型变更后刷新顶栏。
- `/inferences` 默认打开 Pending，可切换至 Confirmed 或 Deprecated。搜索在当前分类内生效。审核按钮与键盘操作触发相同的确认流程。`/git` 可通过弹窗请求同步。
- 推断与 Pending 面板会立即打开，首次读取显示加载状态，再次打开先展示本标签页最近一次取得的列表并后台刷新。读取失败可在面板内重试，加载期间可关闭面板。
- 模型目录缓存在常驻后端的 `infra/llm/client.py` 中。后端刚启动或供应商未提供 `/models` 时，目录可能为空。此时 `/model` 仍列出当前配置模型并明确提示，不会输出空白列表；`/model refresh` 会真正向已配置供应商发起一次有界拉取（单供应商 5 秒超时），拉不到时说明原因而不是谎称「需要本地终端」。缓存为空时仍可用 `/model <当前provider>/<模型ID>` 按 ID 切换，未知供应商仍拒绝。
- 用顶栏 `Commands`、`Ctrl+Shift+P` 或 `Alt+P` 打开命令面板。输入关键词搜索，用方向键选择，按 Enter 执行。Escape 关闭面板并恢复输入框焦点。快捷键列表位于右上角的 **Settings → Shortcuts**。

### 设置面板

顶栏 `Settings` 按钮、`Alt+S` 或 `/setup` 打开同一个设置面板（`web/src/features/settings/SettingsPanel.tsx`）。模型目录通过设置接口读取，模型切换通过 `/model` 执行。Web 与 TUI 复用同一套 Application 用例。

| 分区 | 数据来源 | 说明 |
| --- | --- | --- |
| Model | `GET /api/settings/models?q=…`、`/model <provider>/<model>` | 可按名称搜索所有已配置厂商的目录。Model 和 Providers 的刷新按钮共用目录请求，在按钮旁显示进度、完成或不可用的厂商。点选模型后切换 |
| Appearance | 浏览器 `localStorage` | 默认跟随系统 / 浏览器明暗主题，也可固定亮色或暗色；只保存在当前浏览器 |
| Thinking | `/think` | 优先读取模型目录为当前模型公布的档位，缺少有效档位或查询失败时采用预设。`auto` 置顶，`default` 表示使用供应商默认设置 |
| Context | `/context` | 最小和默认值为 50,000 tokens。可选 50K、100K、200K、400K、600K 或输入自定义值。旧配置低于下限时按 50K 读取。更大的值按原值保存，实际请求受当前模型容量限制。 |
| Providers | `GET /api/settings/providers`、`POST /api/settings/providers/{provider}` | 在供应商卡片中管理 API Key 和 Base URL。配置凭据后，到 Model 搜索并选择模型 |
| Shortcuts | `web/src/shared/shortcuts.ts` | 网页快捷键、浏览器保留键及替代组合键 |
| About | `/api/health` | 版本、当前 provider/model、是否生成中、单客户端锁与静态资源状态 |

确认 **Remove API key** 后，后端从凭据库删除该 Key，供应商卡片可重新添加凭据。当前模型使用的 Key 会提示先切换模型。

### 上下文占用

发送键左侧的水球用水位表示上下文占用。鼠标悬停在水球上时显示估算用量、上限与百分比，移开后收起。点击水球会有轻微缩放反馈。达到压缩阈值时，详情中会显示提示。已有用量且占用不超过 85% 时水波流动，浅水的起伏较小，系统开启减少动态效果时保持静止。自动压缩开始时显示压缩状态，摘要保存后按重新估算的用量降低水位。手动执行 `/compact` 也会显示状态。Web 与 TUI 共用历史上下文估算，计算时无需调用模型，统计范围为已保存的会话内容。

### 主题、侧栏与消息滚动

- 侧栏顶行的放大镜可展开搜索框并自动聚焦，支持会话标题和消息。点击结果直接在主聊天区打开对应会话。点击搜索结果会保留搜索词。点击搜索区外、再次点击放大镜或按 Esc 可收起搜索栏，清空按钮则保留输入框。

- 服务端保存用户消息后，Web 立即刷新会话顺序，模型回复期间无需刷新网页。排序使用最后一条有效用户消息的时间。
- 正在回复的会话标题左侧显示呼吸小点。切到其他会话后仍可看到哪些回复正在生成，回合结束时小点自动消失。系统开启减少动态效果时，小点保持静止。

- 界面使用同一套 CSS 变量生成亮色与暗色主题。默认 `Follow system` 响应 `prefers-color-scheme` 的实时变化，也可在 Appearance 中固定 Light / Dark；选择只写入当前浏览器的 `syvrio.theme`，移动端地址栏 `theme-color` 同步切换。显式主题在样式加载前恢复，避免刷新时黑白闪烁。
- 桌面会话栏默认宽 300 像素，可拖动右边缘在 240 至 480 像素之间调整，同时为聊天区保留至少 440 像素。双击边缘恢复默认宽度。键盘聚焦分隔线后，左右方向键每次调整 10 像素，Home / End 跳到当前最小或最大宽度。浏览器在 `syvrio.sidebar.width` 中保存所选宽度。侧栏头部和顶栏的面板图标或 `Ctrl+B` / `Alt+B` 可收起和展开，开合状态保存在 `syvrio.sidebar`。手机端沿用默认收起的抽屉。
- 侧栏搜索在会话标题和消息摘要的命中词上显示底色与实线下划线。长标题优先留出首个命中词的位置，悬停可查看完整标题。
- 点击侧栏会话后，选中项和标题立即更新。网页先展示本标签页缓存的历史，首次打开时显示加载状态。长会话先显示最近一页，活动和归档会话都可点击 **Load earlier messages** 向前翻页。一次切换请求同时确认目标和历史版本，正文发生变化时随响应更新。切换到已有会话时可以编辑草稿并提前点击发送；后台确认目标后才提交，改选或切换失败时草稿留在原目标会话。快速连续切换时，页面只显示最后选中的会话。
- 恢复或切换会话时，消息区在绘制前以 `instant` 直接定位到最新一条，不播放从顶部滑到底部的长动画；同一会话的流式更新保留平滑跟随，用户向上翻阅后暂停跟随，回到底部附近后恢复。
- 展开的思考面板同样是固定高度的独立滚动区：流式期间跟随最新一行，用户向上翻阅后停止跟随、回到底部附近恢复；已结束的思考从顶部开始阅读。

### 快捷键与浏览器冲突

TUI 快捷键见 `src/syvrio/app/tui/resident.py`（`Ctrl+P` 在模型面板管理供应商、`Ctrl+B` 会话栏、`Ctrl+Q` 退出、`Escape` 关闭/取消）。映射集中在 `web/src/shared/shortcuts.ts`：

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

`KEY_BINDINGS` 定义全局快捷键。`RESERVED_KEYS` 记录交由浏览器处理的 `Ctrl+Q` 和 `Ctrl+P`。可在 **Settings → Shortcuts** 查看替代组合键。

## 跨客户端会话与并发

- Web、TUI、CLI、MCP 可以同时连接。每个网页标签页和终端独立选择会话，打开同一会话时共享正在生成的回复、等待队列及保存的历史。不同会话能同时生成。
- 回复过程中可以点击其他会话、选择 New chat 或输入 `/new`。原会话继续生成并处理自己的队列。切回时先载入历史，再接上活动回合。网页输入框里尚未发送的文字和附件在当前标签页内按会话保留。
- 普通发送进入服务端队列，按接收顺序逐条执行。关闭页面、终端或 SSE 连接后，已接收的消息继续执行。网页刷新后从后端读取队列及当前回复。
- 发送和附件上传等候回执时，客户端用同一个提交编号补发。服务端将编号与原会话、正文和附件绑定。已经收到的请求返回原回合；网页掉线后可根据回合编号和事件序号继续读。刷新时若回复恰好结束，网页直接加载已保存的会话。
- 模型在生成中失败时，会话历史保留已收到的正文、思考和失败原因。失败原因仅供客户端展示。撤销该回合时，它随对应消息一起撤销。
- 归档以目标会话当前的实际写入和后台任务为准。空闲的只读视图可以立即释放，也可以在归档页面恢复会话。
- Ctrl+Enter 或 **Insert** 在模型请求期间申请补充。工具运行时拒绝插入。已接收的补充仅在模型响应完整结束、相关工具结果写入后加入上下文；超过预算或回合状态变化时保留草稿。
- 通过 `/undo` 打断当前回合，按会话及目标消息 ID 核对后撤销本地写入并回填原文。其他会话已修改相关文件或添加引用时，后端拒绝整次撤销。排队消息暂停，用户下一次主动发送后继续。
- 同一 Vault 在本机只能由一个常驻后端写入，即使使用不同的 Runtime 目录也一样。关闭后端会等待后台工作停止，再释放所有权。

## 移动端适配

页面按移动浏览器设计，不依赖 App 或 WebView：

- **布局断点**：`≤767px` 侧栏变成宽度为 `min(62vw, 232px)` 的抽屉（蒙层点击 / `«` 关闭，默认收起），顶栏显示 `Sessions` 入口；`≤380px` 进一步压缩顶栏按钮与设置标签；横屏矮屏（`max-height: 520px`）单独压缩欢迎区间距与弹层内边距。
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
- 无人值守（用户未登录）：使用可访问该 WSL 发行版的 Windows 账号配置开机任务，并从另一台机器验证启动后能持续访问。像 `wsl.exe -d <发行版> --exec /bin/true` 这样立即结束的命令只能触发启动，不能作为常驻保活。WSL 仍受 Windows 的电源与实例生命周期影响。
- `wsl --shutdown`、`wsl --terminate <发行版>`、Windows 重启都会停止服务；Windows 睡眠/休眠期间 WSL 虚拟机会暂停或被回收，唤醒后需重新检查 `systemctl status syvrio-web`。
- localhost 转发验证：WSL 内 `curl -fsS http://127.0.0.1:8765/api/health`；Windows PowerShell `curl.exe http://127.0.0.1:8765/api/health` 应返回同一结果。WSL2 默认开启 `localhostForwarding`，若不通再检查 `C:\Users\<用户>\.wslconfig`。

## ngrok

PyPI wheel 包含分享命令及网页资源。先在运行 Syvrio 的同一系统、同一用户下安装 ngrok v3 并配置其 authtoken；已有可用的 ngrok 配置可直接复用。ngrok 安装和账号注册见[官方指引](https://ngrok.com/download)。使用 ngrok 分配的主机名，以及 GitHub 公开接口 `https://api.github.com/users/<用户名>` 返回的数字 `id`。

```sh
syvrio web share --domain demo.ngrok-free.dev --github-id 12345
```

确认后在本地 `config.toml` 的 `[web_share]` 下保存 `domain` 和字符串 `github_user_id`。以后只需 `syvrio web share`。改绑账号时再次指定参数并确认。CLI 只向本次分享的 Web 网关加入该可信域名，不修改系统 hosts。端口沿用 `--port` 或 `SYVRIO_WEB_PORT`，默认 8765。启动前先停掉占用同一端口的旧 Web 服务及占用该网址的旧隧道。

ngrok 校验 GitHub 账号的数字 ID，邮箱可以隐藏。手机和电脑使用同一个 HTTPS 地址。验收时用无痕窗口分别打开首页和 `/api/sessions`，两者都应先跳转至 GitHub。授权账号登录后应可打开会话，其他账号应收到 403。Ctrl+C 或停止 Web 服务会同时结束随它启动的 ngrok 子进程。

使用 PyPI 自更新部署时，可在已配置以上设置后，将现有用户服务的 Web 启动命令设为 `%h/.syvrio/update/current web share --yes`。服务应使用保存 API Key 时的系统账号运行。`--yes` 代表同意使用已指定的公开入口。服务的 PATH 需要包含 ngrok 可执行文件。安装包更新后仍读取同一份本地设置。外部网址、账号和令牌均由部署方提供。

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

Web 面向单用户私有部署。附件可用文件选择器添加，也可在输入框粘贴剪贴板中的图片或文件。相同内容在草稿中仅列出一次；选择或粘贴只更新本机草稿。手工发送后由常驻后端保存到 Vault 的 `attachments/` 目录；该目录由 Vault 的 Git 忽略。通过复制整个 Vault 可以一并迁移附件。

## 测试

```bash
uv run pytest -m web -q              # 后端 API / SSE / 并发 / 安全 / 命令目录与分发
(cd web && corepack pnpm test)       # 前端单元测试、组件交互、补全与快捷键
(cd web && corepack pnpm build)      # 类型检查 + 生产构建
uv run python scripts/build_web.py --check  # 校验已暂存静态资源
```

命令相关自动化覆盖：目录与 TUI 一致性、能力标记、未知命令不转发 LLM、`/import` 打开本地多选附件，不读取服务器路径、`/undo` 优先取消与回填、忙期策略、会话错配 409、跨站写拒绝、选项面板不重开；前端覆盖前缀/子命令补全、`Tab`/`Enter`/`Escape`、方向键、IME 组字、快捷键冲突、命令面板、设置面板（模型切换、思考强度、上下文、供应商分区、About 不泄露 Key）、危险确认、命令失败后输入框恢复、生成中只读命令不打断流式回复。


## PyPI 自动更新

在 Web 或 TUI 输入 `/update` 可立即检查 PyPI 正式版。手动打开 Web 的 **Settings → About** 时，点击 **Check for updates** 开始检查。界面显示当前版本和可用版本；受管理的安装可以确认目标版本，启动独立的 systemd 用户任务执行更新。升级会保留旧环境已安装的本地向量检索和文档解析组件。服务准备重启时会检查所有会话的活动请求，忙时可稍后重试。重启期间连接可能暂时中断，可在服务恢复后重新打开更新界面查看结果。直接监听局域网地址的 Web 网关只提供版本检查；确认安装可使用本机 TUI、本机环回网关或带 GitHub 登录的 ngrok 共享入口。

`syvrio update` 切换由 Linux/WSL 用户级 systemd 管理的 Web 服务。需要本机已装有 Python ≥ 3.12（任意补丁版本即可，不要求 3.12）、`uv`、可访问的用户 systemd 实例以及带 Web 资源的 PyPI wheel。

命令运行期间会显示当前步骤。下载 wheel 时按实际收到的字节更新，已知总大小时同时显示百分比。创建环境、安装依赖、等待新版通过 Web 健康检查和失败后的回滚都有独立提示。交互式终端显示动态指示和已用时间；重定向时进度逐行写入标准错误，最终版本结果写入标准输出。Web 或 TUI 发起安装后可在更新面板查看同一任务，重新打开面板可读取最近状态。

新版环境基于「当前版本 venv 的基础解释器」创建：更新器先在本机验证解释器存在、可执行且满足目标 wheel 的 `requires-python`，再以显式路径调用 `uv venv --no-python-downloads`，uv 绝不联网下载解释器。找不到兼容解释器时立即报错（错误中会列出尝试过的候选），可通过更新配置指定其他本地 Python。

首次接入时，在 `~/.syvrio/update/` 下创建 `current` 符号链接，指向已安装且包含 Web 前端的版本的 `bin/syvrio`。建立用户级服务（如 `syvrio-web.service`），让其 `ExecStart` 使用该链接，例如：

```ini
[Service]
ExecStart=%h/.syvrio/update/current web --host 127.0.0.1 --port 8765 --no-hosts
TimeoutStopSec=5min

[Install]
WantedBy=default.target
```

将现有 Web 服务的配置、凭据库访问权限、域名白名单和启动参数迁入该服务。切换前停掉旧的源码服务，再启用此 unit，核对 `/api/health` 中的版本和前端状态。跨 Windows 重启仍需确保 WSL 和用户 systemd 实例会启动。

创建 `~/.syvrio/update/config.toml`：

```toml
service = "syvrio-web.service"
port = 8765
# 可选：显式指定本地解释器；默认用当前版本 venv 的基础解释器
# python = "/usr/bin/python3.13"
# 可选：显式指定 uv；默认取调用者 PATH 上的 uv，找不到就直接报错
# uv = "/home/<user>/.local/bin/uv"
```

更新器只选择 PyPI 上更新的正式版，忽略已撤回的包和历史候选版。更新配置可以省略 `channel`，显式配置时只接受 `channel = "stable"`。升级前请核对配置、确认 Vault 使用当前格式，并备份 Vault 和配套 Runtime。服务正常启动以后，停用旧的源码拉取任务，定时任务只运行 `syvrio update`。定时任务的 PATH 须包含 `syvrio`，并可访问对应的用户 systemd 实例；`uv` 须位于 PATH，或在 `config.toml` 中配置绝对路径。未配置且 PATH 里没有 `uv` 时，更新器在下载前就报错，不会走到一半才失败。更新命令应在 Web unit 外运行。

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
