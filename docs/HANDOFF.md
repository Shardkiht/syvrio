# 开发交接

## 运行开发分支

在仓库根目录运行 `uv sync --frozen`。Web、TUI、CLI 和 MCP 使用同一套应用服务。各客户端使用同一份开发配置时，可以看到相同的会话、正式记忆和审核结果。首次连接会按配置发现或启动本机常驻后端。Web 网关另行监听浏览器使用的地址。

```sh
uv run syvrio web --help
corepack pnpm --dir web install --frozen-lockfile
corepack pnpm --dir web dev --host 127.0.0.1 --port 5173 --strictPort
```

启动 Web 后，Vite 开发服务器将 API 请求转发至默认的本机 8765 端口。开发与生产分别使用独立的 Vault、Runtime、配置和端口。上传的原件由对应会话拥有，保存于 Vault 的 `attachments/`，该目录由 Vault 自己的 Git 设置忽略。迁移上传原件需单独复制，部署和历史格式迁移分开进行。

## 客户端行为

- 普通发送在后台的会话队列中等待，当前回合结束后发送下一条。Ctrl+Enter 请求在当前回合的安全边界插入，执行中的工具先完成。重新连接时按回合事件的位置续读。
- 最新用户消息的撤销与编辑复用 `/undo`。Web 支持鼠标悬停、键盘聚焦和触屏。撤销先校验持久化的最后一回合、被修改文件的版本，以及其他会话增加的引用。发生冲突时保留整回合。
- Web 和 TUI 的 `/import` 打开本机的多文件选择器。Web 的圆形加号共用同一个选择器，文档和图片先加入草稿，发送后再由模型结合用户要求阅读或提炼记忆。聊天命令由共享命令目录提供。独立的 `syvrio import` 可以在终端批量整理已有资料。模型设置可按名称搜索厂商目录，搜索框位于刷新按钮右侧。窄屏使用两行设置导航，桌面内容区与侧栏分开滚动。
- 思考档位优先采用厂商提供的能力，缺失时使用模型预设。auto 优先列出。会话记录的思考附件可在 Runtime 中保存，单个附件损坏不会阻止会话正文加载。

## 记忆写入与审核

聊天写入正式事实或创建候选前，Runtime 调用统一的 `recall`，把结构化事实交给模型复核。创建候选时额外读取活动候选清单。操作、对象、内容、事实版本或对话上下文发生变化后，会重新提供依据。一次模型请求中的两个工具调用不能替代模型重新决定。

正式记忆的来源通过真实会话和用户消息关联。按 ID 查原话使用 `read_memory_source`。普通检索和主题读取只返回事实、主题、状态和可选上下文。事实的有效期可使用年、月、日精度；事件时与回想时的情绪独立保存。

`correct` 保存正确事实并弃用原记录，`transition` 保留过去真实成立的事实，`deprecate` 软弃用。候选上的关联 ID 供审核参考。直接确认保留其上下文，改写后确认先保存最终正文，随后由后台任务补充有依据的空字段。普通记忆变更与审计、变化记录同组落盘。删除会话时会检查正式来源引用，并在确认框展示将一并清理的活动候选。

常驻后台负责编排会话沉淀、Dream 以及待审改写后的上下文补充。沉淀游标与相应写入一同提交。Dream 生成画像和有正式来源的新推断；人工编辑推断保留旧正文。原始会话、正式事实、推断与待审候选的格式和路径见 [Vault 规范](vault-v2.md)。

## 回归入口

```sh
uv run pytest tests/test_resident_clients.py tests/test_agent.py
uv run pytest tests/test_background_memory.py tests/test_reflection.py
uv run pytest tests/test_store.py tests/test_queue.py tests/test_service.py
uv run python -m evals.memory_eval
uv run python -m evals.retrieval_eval --keyword
corepack pnpm --dir web test
corepack pnpm --dir web build
```

跨客户端回归覆盖本机 TCP 通信、队列顺序、断线重放、TUI 输入恢复和 Vault 后端所有权。涉及并发或落盘的改动，还需核对失败后文件、游标、审计与跨会话来源引用。自动化测试用临时目录与合成数据。真实厂商联调应明确记录厂商、平台和实测范围。
