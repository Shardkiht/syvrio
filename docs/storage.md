# SQLite 存储与恢复

首次启动使用空 Vault。业务主库位于 Vault 根目录。初始化在一个事务内建立主库的表、索引和触发器，并写入应用标识。已有数据库直接按当前结构打开。打开这个分支前请为它配置专用的 Vault 和 Runtime。

## 数据布局

| 内容 | 位置 |
| --- | --- |
| 事实及来源 | `memories` 保存当前正文，`memory_versions` 以 `(memory_id, commit_id)` 保存各次提交的完整版本、来源和时间。`correct` 沿用 ID，`transition` 用 `supersedes` 关联新旧事实。Context 保存已知精度、地点和感受。 |
| 待审与推断 | `candidates`、`candidate_related`、`inferences`、`inference_sources`。拒绝保留记录，审核推断独立于事实状态。 |
| 会话 | `sessions` 保存会话信息。`session_events` 一行保存一条消息的正文、可选思考、工具预览和原文长度。历史分页按需读列。稳定消息 ID 用于定位引用；事件顺序、会话历史版本和归档标志入库。 |
| 业务记录 | `audit` 为每次事实变更保存一条操作记录及变更详情。撤销成功后追加一条指向原操作的审计。事实写入时将变更内容存入 `audit.memory_change`。普通操作的同名详情保留在 `details`。Git 从同一个已提交快照读取公开审计和仍然生效的变更，新增操作名称无需调整导出。`undo_effects` 保存回合撤销依据，`receipts` 防止重复发送。 |
| 后台与检索 | `jobs` 保存沉淀、Dream 和 Context 任务的检查点；`profile` 保存已发布结果。Dream 在排队时将 `revisions` 中 `evidence` 的提交号保存为 `evidence_commit`，两阶段均按该提交号读取事实、主题归档状态和推断版本。每批在短事务内一起提交结果与游标，重启后按同一版本恢复。模型请求发送前扣减预算，已发出但未提交结果的批次恢复时会重试。`revisions` 跟踪索引依赖的数据版本。 |
| 搜索供应商暂停 | 主库的 `provider_pauses` 按 `provider` 保存供应商返回的暂停原因（`reason`）和可选恢复时间（`resume_at`）。搜索和网页抓取都只读检查当前暂停状态，手动恢复时删除该供应商的记录。 |
| 本机 Git 同步 | 主库的 `git_sync` 记录本机同步状态。恢复到新部署时清空此表。 |
| 可重建缓存 | `Runtime/cache/vectors.sqlite3` 保存按模型、记忆 ID 和输入摘要核验的向量。`Runtime/cache/web-search.sqlite3` 保存有有效期的网页搜索结果。停服后可单独清理其中一个缓存库及其同名 WAL 附属文件，下次使用时会重新生成。 |
| 附件 | `attachments` 每条引用保存会话、消息、文件名、大小、类型和哈希。相同原件共用 `attachments/objects/<前两位>/<sha256>`。发送中的引用计数留在常驻后端内存；发布前将哈希登记到 `file_cleanup`，用于失败和重启后的清理。 |

需要 SQLite 3.38 或更新版本。可运行 `python -c "import sqlite3; print(sqlite3.sqlite_version)"` 核对当前 Python 实际链接的版本。数据库启用外键、WAL 与 FULL 同步。常驻后端保留一个空闲连接，各次读写仍使用独立事务。同一线程和任务内的嵌套写入使用保存点，外层成功后才发布。批准候选、按消息沉淀、发布画像和撤回编辑均需要在提交前核对对象状态。模型推理和向量计算可以在读快照之后进行，迟到结果须复核。归档保留原始数据，正式事实仍引用会话时拦截永久删除。绝对时刻用 UTC，来源消息的本地日期和事实的年、月、日精度独立保存。

`jobs` 按任务类型和目标定位一行，调度状态、重试时间和 Dream 固定的证据提交号各自成列。游标、阶段、中间稿和请求预算等任务专用字段存入同一行的 `checkpoint`。每批完成时，结果、证据版本与进度在同一事务内提交。凡是任务保留了固定证据提交且尚未完成，撤销都要保护该版本可见的记忆。暂停和可重试的失败继续持有保护；完成或放弃并清除任务检查点后释放。

查询 Dream 状态、会话删除预览和写入前的证据审阅使用只读快照。确认写入时重新核对来源与版本。会话写入负责计算所写消息的搜索签名；外部 SQL 修改的正文在签名尚未生成时仍可直接搜索。Git 导出始终读取已提交快照，调用方事务中的临时更改不会出现在导出目录。


## Git 阅读视图

`Vault/export/` 是独立 Git 工作树。在此目录配置远端和 upstream。同步读取一致快照，在临时目录生成整批 `content/` 后替换，随后执行 Git 暂存、提交与推送。`records/memory_versions.jsonl` 保存可导出的历史版本。推断携带来源提交号，旧数据中无法考证的来源版本留空。导出包含活动及弃用的事实、活动候选、带来源的推断、画像和未归档会话。归档会话、拒绝项、附件原件、私有推理、任务、撤回凭据和数据库文件留在本机。导出失败可以重试。编辑导出文件不会写回数据库。

## 完整备份与还原

结束使用此 Vault 的常驻后端，选择位于 Vault 之外且尚不存在的目标目录。示例在已配置相应部署环境的终端运行。

```sh
syvrio backup /backup/syvrio-01 --config /srv/syvrio/config/config.toml
syvrio restore /backup/syvrio-01 /srv/syvrio/restored-vault
syvrio verify-attachments --vault /srv/syvrio/restored-vault
```

备份持有独占租约，使用 SQLite backup API 复制业务主库，与当前及归档会话引用的原件、可选 SOUL.md 和哈希清单一起发布。恢复检查清单、附件长度、SHA-256、SQLite 完整性和外键，全部通过后才发布目标目录。任务、游标、幂等记录、供应商暂停记录随主库恢复。恢复后的部署重新记录 Git 同步状态。使用新的 Runtime 时，向量和网页搜索结果按需重新缓存。部署 TOML、凭据、Git 远端和服务启动设置需要分别管理。用独立 Runtime 启动恢复出的 Vault，后端会重新绑定并协调任务。

## 验证存储与客户端

在已安装项目及测试依赖的源码环境运行以下用例。四端联调用例启动自己的后端、Web 网关和回环假模型，在临时空库中从 CLI 与 MCP 写入，从 Web 与 TUI 发送消息，触发 Dream 并核对重启后的结果。Git 用例用本地裸仓库接收导出。

```sh
python -m pytest -q tests/infra/test_sqlite_transactions.py tests/infra/test_attachment_objects.py
python -m pytest -q tests/backend/test_sqlite_clients.py tests/application/test_application_architecture.py
python evals/recall_gold_eval.py --keyword
```

## 从历史文件 Vault 导入

使用与目标程序同版本的 `scripts/import_legacy_vault.py` 迁移历史文件库。通过 PyPI 部署时，从该版本的 PyPI 源码包解压 `scripts/`，用已安装该版本 wheel 的 Python 执行脚本。使用 Git 工作树时，在仓库根目录运行 `uv sync --frozen`。先配置并初始化使用当前表结构的目标 SQLite Vault，再停服导入。下例在副本上预演，参数顺序从最新来源排到最旧来源。

```sh
uv run python scripts/import_legacy_vault.py --source /data/vault-v2 /data/runtime-v2 --source /data/old-vault /data/old-runtime --target /data/sqlite-vault --timezone Asia/Shanghai
```

预演通过后，停止目标常驻后端，在同一命令末尾加 `--apply --backup-dir /backup/syvrio-imports`。完成后用原配置重启。Windows 使用相同参数，路径替换为本机目录。没有配套 Runtime 时填 `-`，指定时区用于解释缺少偏移量的历史时间。用 `--archive-only VAULT RUNTIME` 保留旧备份的文件，避免把快照中的旧状态恢复为当前业务状态。

- 会话按事件顺序写入，保留 ID、撤销、归档、摘要、工具结果和可找到的本地思考记录。相同会话只合并前缀一致的日志，分叉会中止。记忆、推断和候选按 ID 归并，以优先来源为准。多个来源的原文在档案里都可查阅。
- 正式记忆及推断的来源经过外键核验。附件逐个核对 SHA-256，在目标 Vault 的标准对象目录保存一份原件，消息中的文件名和引用分别入库。目标里已有的画像与 SOUL 优先保留。
- 每次正式运行生成一个私有备份目录。`before/` 是可用 `syvrio restore` 还原的导入前备份。`legacy-source.sqlite3` 保存原始文件，可关联 `sources`、`files`、`objects` 按来源、路径和 SHA-256 查阅。`report.json` 记录实际写入数量和缺失的思考附件。旧运行锁、Git 对象库和上传暂存目录不属于业务数据。Runtime 仅采集思考附件和已发布画像。
- 正式导入在一个业务事务内提交。重复运行同一输入时通过审计回执跳过已导入对象。后续同一 ID 的源记录发生变化时会报告冲突，避免覆盖 SQLite 中继续使用的数据。已结束的旧调度任务和文件版凭据不在新后端重放。
