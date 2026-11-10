# Syvrio Web

私有单用户 Web 客户端。Web 只是传输层，不重复实现 Agent、Memory、Session 或模型适配。

```text
React Web ──┐
            ├── ApplicationService ── AgentRuntime ── Memory / LLM ── Vault / Session
Textual TUI ┘
```

- 后端 `src/syvrio/app/web/`：FastAPI + Pydantic，单 Worker，REST + SSE。
- 前端 `web/`：React + TypeScript + Vite + 原生 CSS，pnpm 管理。
- `src/syvrio/app/server.py` 仍是 MCP Server，未被 Web 覆盖。
- 首版不替代 TUI，也不把 TUI 改成 HTTP 客户端。

## 开发与运行

```bash
# 后端（同时提供 API 和已构建的静态资源）
uv sync
uv run syvrio web                      # 默认 127.0.0.1:8765
uv run syvrio web --host 127.0.0.1 --port 9000

# 前端开发服务器（/api 由 Vite 代理到 8765）
cd web
corepack pnpm install
corepack pnpm dev
```

开发模式下前端由 Vite 提供，直接访问后端根路径会返回明确的 `assets_missing` 提示（503），这是正常的；API 仍可用。

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
python scripts/build_web.py                     # pnpm install + build + 复制 + 校验
python scripts/build_web.py --check             # 只校验已暂存的静态资源
uv build --clear
python scripts/build_web.py --wheel dist/*.whl  # 校验实际 wheel 内含静态资源
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
| GET | `/api/health` | 状态、模型、busy、活动 turn、静态资源是否就绪 |
| GET | `/api/sessions?limit=100` | 会话列表 |
| POST | `/api/sessions` | 新建会话（立即创建空会话并返回 ID） |
| POST | `/api/sessions/{id}/switch` | 切换/恢复会话 |
| DELETE | `/api/sessions/{id}?confirmed=true` | 删除会话（需二次确认；当前/生成中会话受守卫保护） |
| GET | `/api/sessions/{id}` | 结构化历史（含已落盘的 Reasoning，默认折叠） |
| GET | `/api/commands` | 共享命令目录（名称、用法、能力标记、子命令、快捷键） |
| POST | `/api/commands` | 执行斜杠命令，返回结构化结果，不经过 LLM |
| POST | `/api/chat` | 流式聊天，`text/event-stream` |
| POST | `/api/chat/cancel` | 按 `turn_id` 取消当前请求 |

会话栏排序口径：`GET /api/sessions` 按**会话最后一条有效消息的发送时间**倒序（`SessionInfo.last_message_at`），没有消息的空会话回退到创建时间 `started_at`。共享的 `SessionStore.list()` 仍按 `updated_at` 排序供 TUI / CLI 使用，但 `updated_at` 会被重命名（`title`）、摘要、沉淀、`undo` 等控制记录刷新，因此 Web 侧栏在 `WebBackend.list_sessions()` 里单独重排；这些写入不会再把旧会话顶到最前。被 `undo` 撤销的消息不计入有效消息。

**删除会话**：侧栏每条会话悬停（或键盘聚焦）时出现 `×`，移动端常显。确认弹窗在浏览器侧提示「不可撤销」，服务端仍独立校验 `confirmed=true`、会话 ID 合法性、会话存在、`_require_idle()` 以及 `ConversationUseCases.delete_guard()`（与 TUI 同一守卫：当前会话、生成中、队列引用不可删），通过后由 `SessionStore.delete()` 删除 JSONL 与 Reasoning 附件。删除当前会话时 Web 先新建空会话再删旧文件，等价于 TUI 的「先 `/new` 再 `Ctrl+D`」，服务端语义不变。删除失败返回稳定的 `delete_failed` 错误并保留文件。

FastAPI 自动提供 `/openapi.json` 与 `/docs`。

`POST /api/chat` 请求体：`{"message":"...","session_id":null}`。每个 SSE 帧带单调递增的 `id`/`seq` 和 `event` 类型，`data` 为 JSON：

| event | 关键字段 | 含义 |
| --- | --- | --- |
| `start` | `turn_id`, `session_id` | 请求开始 |
| `reasoning_delta` | `text`, `phase` | 思考增量；`phase` 按模型请求递增，不同工具轮次不混淆 |
| `reasoning_end` | `text`, `seconds`, `phase` | 该阶段思考定稿，使用完整文本校准 |
| `content_delta` | `text` | 正文增量；前端收到后折叠 Thinking |
| `tool_start` | `name` | 工具调用开始 |
| `tool_end` | `name`, `text` | 工具结果展示，与 `tool_start` 按调用顺序配对 |
| `event` | `text` | 其他事件（召回、沉淀提示等） |
| `progress` | `text` | 阶段文案 |
| `done` | `text`, `steps`, `estimated_tokens` | 正常完成，正文以完整文本定稿 |
| `cancelled` | — | 已停止，保留半截输出 |
| `failed` | `message` | 失败，不泄露堆栈 |

事件只在模型确实返回 reasoning 时产生；前端不解析任何 Provider 私有协议，也不虚构思考文本。断流时前端保留半截回复并显式标注，不会留下永久 busy 状态。

## Slash Command 与快捷键

### 单一命令目录

命令名称、用法、说明和子命令定义在 `src/syvrio/application/commands.py`，不依赖 Textual / Rich / HTTP。`src/syvrio/app/command_catalog.py` 变成薄转发，TUI 现有调用点与测试无需改动。Web 通过 `GET /api/commands` 取得同一份目录，用于输入框补全、命令面板和 `/help`；**React 端不维护第二套命令定义**。新增命令只需在共享目录加一行、在 `CommandUseCases` 复用 Application 用例、由两端各自决定展示方式。

`WEB_CAPABILITIES` 用能力标记说明每个命令在 Web 的语义：

| `support` | 含义 | 例子 |
| --- | --- | --- |
| `server` | Web 后端通过 `CommandUseCases` 执行，复用 `ConversationUseCases` / `SettingsUseCases` / `MemoryUseCases` 等 | `/new`、`/model`、`/status`、`/undo` |
| `client` | 纯浏览器交互 | `/sessions` 展开会话栏；`/setup` 打开设置面板；`/exit`、`/quit` 返回入口视图，**不会关闭后台 Web Server** |
| `unsupported` | 明确提示不可用及原因，绝不静默忽略 | `/import` |

- `/import` 不允许浏览器把任意路径交给服务器读取，返回 `unsupported` 并说明改用 TUI。
- `/git`、`/pending`、`/inferences`、`/profile`、`/dream` 在 Web 只提供只读状态；同步 / commit / push、批准候选、删除或确认记忆等副作用操作仍留在 TUI，授权约束不被绕过。
- `/undo` 前端弹二次确认，服务端仍独立复核 `confirmed` 才执行。
- 未识别的 `/` 命令返回 `unknown` 并提示 `/help`；前端把所有 `/` 行发往 `/api/commands`，后端 `/api/chat` 也会拒绝斜杠开头的消息（`422 command_required`），双重保证不会误发给 LLM。
- 多标签页：`POST /api/commands` 携带的 `session_id` 与 Runtime 当前会话不一致时返回 `409 session_mismatch`，提示刷新，避免旧标签页操作到别人的会话。

### 生成期间的命令

Web 单 Turn 模型下，`CommandUseCases.BUSY_READ_ONLY` 与 `BUSY_NO_ARGUMENT` 明确哪些命令可在生成中执行：只读命令（`/help`、`/status`、`/summary`、`/topics`、`/pending`、`/inferences`、`/profile`、`/dream`、`/git`）以及不带参数的 `/model`、`/think`、`/context`。`/undo` 在生成中额外承担取消当前 Turn 的语义。该集合除 `/undo` 外保持为 TUI `SAFE_COMMANDS` 的子集，并有测试断言，避免两个客户端语义分歧。其余命令返回明确的 `busy`，输入框不会因此被永久禁用。

### 输入与补全

- 输入 `/` 显示候选，前缀实时过滤；`↑ / ↓` 选择、`Tab` 补全、`Enter` 执行选中的命令或子命令、`Escape` 关闭候选；鼠标点击与移动端触摸同样可选。
- 候选列表贴近输入框、固定最大高度、内部滚动，不遮挡主要聊天内容。
- 中文输入法组字状态（`isComposing`、`keyCode 229`、`Process` / `Unidentified`）不会触发 `Enter`、`Tab` 或任何全局快捷键。
- `/model`、`/think`、`/context` 的结果会转成轻量可点击选项面板；选择后服务端应用设置，不会重新弹出面板。模型变更后刷新顶栏模型信息。
- 模型目录是**进程内缓存**（`llm/client.py` 的 `_MODEL_CACHE`），Web 进程刚启动、或供应商未暴露 `/models` 时可能为空。此时 `/model` 仍列出当前配置模型并明确提示，不会输出空白列表；`/model refresh` 会真正向已配置供应商发起一次有界拉取（单供应商 5 秒超时），拉不到时说明原因而不是谎称「需要本地终端」。缓存为空时仍可用 `/model <当前provider>/<模型ID>` 按 ID 切换，未知供应商仍拒绝。
- 命令面板（`Ctrl+Shift+P` / `Alt+P`，或顶栏 `Commands` 按钮）复用同一目录与分发逻辑：搜索、方向键、Enter 执行、Escape 关闭并恢复输入框焦点，同时列出快捷键和浏览器保留键说明。

### 设置面板

顶栏 `Settings` 按钮、`Alt+S` 或 `/setup` 打开同一个设置面板（`web/src/components/SettingsPanel.tsx`）。所有读写仍走 `POST /api/commands` 与 `GET /api/providers`，复用 TUI 相同的 Application 用例，React 端不保存设置副本：

| 分区 | 数据来源 | 说明 |
| --- | --- | --- |
| Model | `/model`、`/model refresh`、`/model <provider>/<model>` | 列出目录中的模型并标记当前项；目录为空时明确提示，可按模型 ID 切换 |
| Thinking | `/think` | 仅展示模型真实支持的 reasoning 取值，`default` 表示不覆盖供应商默认 |
| Context | `/context` | 上下文窗口候选与当前值 |
| Providers | `GET/POST /api/settings/providers/{provider}` | 添加/更新 Key、修改 Base URL、删除供应商、自定义模型；**保存的 Key 永不回传浏览器** |
| About | `/api/health` | 版本、当前 provider/model、是否生成中、单客户端锁、静态资源状态与快捷键 |

面板本身不执行任何副作用：删除供应商仍需二次确认，服务端复用 `SettingsUseCases` 的「当前供应商不可删」等守卫，失败原因以 `provider-error` 原样展示。

### 快捷键与浏览器冲突

以 TUI 实际绑定为准（`chat_app.py`：`Ctrl+P` 命令面板、`Ctrl+B` 会话栏、`Ctrl+Q` 退出、`Escape` 关闭/取消）。映射集中在 `web/src/state/shortcuts.ts`：

| TUI | Web | 说明 |
| --- | --- | --- |
| `Ctrl+P`（命令面板） | `Ctrl+Shift+P` / `Alt+P` | `Ctrl+P` 是浏览器打印，不拦截；Firefox 的 `Ctrl+Shift+P` 是隐私窗口，`Alt+P` 是跨浏览器可靠替代 |
| `Ctrl+P`（`/model` 内供应商入口） | `Alt+M`，或点击顶栏模型名，或 `/model` | 不占用浏览器打印键 |
| TUI `/setup` | `Alt+S`、顶栏 `Settings` 或 `/setup` | 设置面板：模型、思考强度、上下文、供应商 Key 与端点 |
| `Ctrl+B`（会话栏） | `Ctrl+B` / `Alt+B` | 可稳定拦截，另有可点击 `Sessions` 按钮 |
| `Ctrl+Q`（退出） | 不拦截 | 浏览器保留；`/exit` 只返回入口视图 |
| `Escape` | `Escape` | 关闭弹层 / 关闭补全 / 停止生成 |
| `↑ / ↓`、`Tab`、`Enter`、`Shift+Enter` | 同 TUI 语义 | 在输入框内处理，`Shift+Enter` 换行 |

`KEY_BINDINGS` 是唯一映射表；`Ctrl+Q`、`Ctrl+P` 明确列在 `RESERVED_KEYS` 中不做拦截，避免破坏浏览器正常功能，并在命令面板里显示替代组合键。

## 并发与 TUI 边界

- 单进程单 Worker，进程内共享一个 `ApplicationService` / `AgentRuntime`。
- 同一时刻只允许一个生成中的 Turn；第二个 `POST /api/chat` 返回 `409 busy`（`code="busy"`），不排队、不串线。
- 取消请求必须带匹配的 `turn_id`；过期或伪造 ID 不会取消当前请求。
- 浏览器断开 SSE 时只取消并释放该 Turn，不误伤其他请求。
- `runtime/locks/client.lock` 是整进程持有的独占标记：**TUI 与 Web 不能同时使用同一 Vault**。后启动的一方会收到明确错误（Web 拒绝启动，TUI 提示先关闭另一客户端）。进程退出或崩溃时由操作系统释放锁，不会残留。
- `vault.lock` / `git.lock` 仍只保护单次写入，不是跨进程 Agent 协调机制。

## 移动端适配

页面按移动浏览器设计，不依赖 App 或 WebView：

- **布局断点**：`≤767px` 侧栏变成 `82vw` 抽屉（蒙层点击 / `«` 关闭，默认收起），顶栏显示 `Sessions` 入口；`≤380px` 进一步压缩顶栏按钮与设置标签；横屏矮屏（`max-height: 520px`）单独压缩欢迎区间距与弹层内边距。
- **软键盘**：`index.html` 使用 `viewport-fit=cover` 与 `interactive-widget=resizes-content`；`App.tsx` 监听 `visualViewport` 把可用高度写入 `--app-height`，`.app` 优先使用该值，iOS Safari 弹出键盘时输入框不会被遮住。
- **输入缩放**：移动端 `textarea` / 命令面板输入 / 设置与供应商输入统一 `font-size: 16px`，避免 iOS 聚焦时整页放大；`touch-action: manipulation` 去掉双击缩放的点击延迟。
- **弹层高度**：命令面板、确认框、选项面板与设置面板优先使用 `dvh`（不支持时退回 `vh`），手机键盘弹出或地址栏收起后仍可见；设置面板在手机上为顶部横向标签 + 纵向内容，窄屏自动换行。
- **安全区**：顶栏、侧栏与输入框使用 `env(safe-area-inset-*)`，适配刘海屏横屏与底部 Home 指示条；`index.html` 提供 `theme-color` 让地址栏与暗色界面一致。
- **触摸可用性**：`hover: none` 设备上删除按钮常显（宽度断点之外再按能力判断），会话行、关闭按钮、设置选项与供应商按钮最小高度 36–44px；消息区 `overscroll-behavior: contain`，滚动不会带动整页回弹。

## 安全边界

默认只监听 `127.0.0.1`；不启用 CORS，不信任 `X-Forwarded-*`；校验 `Host` 与写请求的 `Origin` 白名单；会话 ID 使用与 `SessionStore` 相同的正则校验，拒绝路径穿越；静态资源只从构建目录提供；Markdown 经 `react-markdown` + `rehype-sanitize`；未处理异常返回通用 500，不向浏览器暴露堆栈；uvicorn access log 关闭，普通日志不记录聊天正文；API Key 不出现在任何响应中。第一版按单用户私有部署设计，网络访问由 Tailscale 设备/用户策略控制。

## WSL + systemd 常驻

模板见 `docs/syvrio-web.service`，把 `REPLACE_USER`、`REPLACE_REPO`、`REPLACE_UV`、`REPLACE_TAILSCALE_HOST` 替换成实际值后安装。

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

> 当前开发环境未执行 WSL systemd 常驻与 Serve 的远程联机验收，也未改动本机 Tailscale 配置；命令语法按本机 CLI 实测帮助核对，实际远程连通性仍需在获准设备上验证。

## 暂未实现

完整模型管理后台、Git 可视化、Memory 管理界面、多用户、文件上传、插件市场、工作流编辑器、多 Agent 协作、数据分析看板、PWA、Web 与 TUI 共用统一常驻 Backend。

命令侧明确未支持或只提供只读视图：`/import`、`/setup` 未支持；`/git` 的同步与 push、`/pending` 与 `/inferences` 的批准/拒绝、`/profile` 与 `/dream` 的刷新和任务控制仍留在 TUI。这些命令在 Web 会给出明确提示，不会静默忽略。首版也不引入 WebSocket、Redux、路由库或任何数据库。

## 测试

```bash
uv run pytest -m web -q              # 后端 API / SSE / 并发 / 安全 / 命令目录与分发
cd web && corepack pnpm test         # 前端单元测试、组件交互、补全与快捷键
cd web && corepack pnpm build        # 类型检查 + 生产构建
python scripts/build_web.py --check  # 校验已暂存静态资源
```

命令相关自动化覆盖：目录与 TUI 一致性、能力标记、未知命令不转发 LLM、`/import` 不读取浏览器路径、`/undo` 服务端确认、忙期策略、会话错配 409、跨站写拒绝、选项面板不重开；前端覆盖前缀/子命令补全、`Tab`/`Enter`/`Escape`、方向键、IME 组字、快捷键冲突、命令面板、设置面板（模型切换、思考强度、上下文、供应商分区、About 不泄露 Key）、危险确认、命令失败后输入框恢复、生成中只读命令不打断流式回复。
