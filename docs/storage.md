# SQLite 存储与恢复

首次启动使用空 Vault。数据库文件位于 Vault 根目录，初始化在一个事务内写入应用标识和 schema 版本。版本不匹配或发现其他应用的数据表时启动失败。打开这个分支前请为它配置专用的 Vault 和 Runtime。

## 数据布局

| 内容 | 位置 |
| --- | --- |
| 事实及来源 | `topics`、`memories`。来源对指向同一会话里的原消息，更正用 `supersedes` 保留链条。Context 保存已知精度、地点和感受。 |
| 待审与推断 | `candidates`、`candidate_related`、`inferences`、`inference_sources`。拒绝保留记录，审核推断独立于事实状态。 |
| 会话 | `sessions`、`session_events`、`session_reasoning`。稳定消息 ID 用于定位引用；事件顺序、会话历史版本和归档标志入库。 |
| 业务记录 | `audit` 是操作审计，`memory_changes` 记录事实字段变化；`undo_effects` 保存本回合影响的实体前像，`receipts` 保存幂等结果。 |
| 后台与检索 | `jobs` 保存沉淀、Dream 和 Context 任务的检查点；`profile` 保存已发布结果。索引使用 `revisions`；向量按事实、模型与输入摘要核对。配额、暂停、搜索缓存和同步结果使用各自的表。 |
| 附件 | `attachments` 引用 `attachment_objects`，原件在 `attachments/objects/<前两位>/<sha256>`。预留及清理队列记录跨介质处理进度。 |

需要 SQLite 3.38 或更新版本。可运行 `python -c "import sqlite3; print(sqlite3.sqlite_version)"` 核对当前 Python 实际链接的版本。数据库启用外键、WAL 与 FULL 同步。同一线程和任务内的嵌套写入使用保存点，外层成功后才发布。批准候选、按消息沉淀、发布画像和撤回编辑均需要在提交前核对对象状态。模型推理和向量计算可以在读快照之后进行，迟到结果须复核。归档保留原始数据，正式事实仍引用会话时拦截永久删除。绝对时刻用 UTC，来源消息的本地日期和事实的年、月、日精度独立保存。

## Git 阅读视图

`Vault/export/` 是独立 Git 工作树。在此目录配置远端和 upstream。同步读取一致快照，在临时目录生成整批 `content/` 后替换，随后执行 Git 暂存、提交与推送。导出包含活动及弃用的事实、活动候选、带来源的推断、画像和未归档会话。归档会话、拒绝项、附件原件、私有推理、任务、撤回凭据和数据库文件留在本机。导出失败可以重试。编辑导出文件不会写回数据库。

## 完整备份与还原

结束使用此 Vault 的常驻后端，选择位于 Vault 之外且尚不存在的目标目录。示例在已配置相应部署环境的终端运行。

```sh
syvrio backup /backup/syvrio-01 --config /srv/syvrio/config/config.toml
syvrio restore /backup/syvrio-01 /srv/syvrio/restored-vault
syvrio verify-attachments --vault /srv/syvrio/restored-vault
```

备份持有独占租约，使用 SQLite backup API 复制一致数据库，与当前及归档会话引用的原件、可选 SOUL.md 和哈希清单一起发布。恢复检查清单、附件长度、SHA-256、SQLite 完整性和外键，全部通过后才发布目标目录。任务、游标和幂等记录随库恢复，可重建的向量、搜索缓存及旧同步状态会清除。部署 TOML、凭据、Git 远端和服务启动设置需要分别管理。用独立 Runtime 启动恢复出的 Vault，后端会重新绑定并协调任务。

## 验证存储与客户端

在已安装项目及测试依赖的源码环境运行以下用例。四端联调用例启动自己的后端、Web 网关和回环假模型，在临时空库中从 CLI 与 MCP 写入，从 Web 与 TUI 发送消息，触发 Dream 并核对重启后的结果。Git 用例用本地裸仓库接收导出。

```sh
python -m pytest -q tests/infra/test_sqlite_transactions.py tests/infra/test_attachment_objects.py
python -m pytest -q tests/backend/test_sqlite_clients.py tests/application/test_application_architecture.py
python evals/recall_gold_eval.py --keyword
```
