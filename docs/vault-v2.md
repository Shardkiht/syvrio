# Vault 格式、备份与时间规范

常驻后端按本文格式读写 Vault 与 Runtime。

## 记录格式

正式记忆、待审核候选和推断使用 UTF-8 Markdown。路径决定记录类型。每条记录是一个以 `**Core**` 开始的顶层列表项，其后按顺序可有 `**Context**` 和 `**Extensions**`。正式记忆的逻辑主题取自 `memories/` 下的相对路径，例如 `memories/events/exhibits.md` 对应 `events/exhibits`。

```markdown
# 示例展览

* **Core**
  * `id`: `mem-example`
  * `status`: `active`
  * `text`:
    ~~~
    我去看了一场纸艺展。
    最喜欢的是入口处的纸灯。
    ~~~

  **Context**
  * `event_time`: `2024-06`
  * `location`: `示例展厅`
  * `emotion.event`: `有点紧张`
  * `emotion.recollection`: `现在觉得很开心`

  **Extensions**
  * `recorded_at`: `2026-01-20T08:30:00+00:00`
  * `session_id`: `session-example`
  * `message_id`: `message-example`
  * `custom.marker`: `合成样例`
```

Core 恰好包含 `id`、`status`、`text`。单行正文可写成行内代码，多行正文用缩进围栏保存，保留前后空行、缩进和原换行符。元数据标量用行内代码，内部含反引号可以使用更长的定界符。程序写入带换行的元数据时采用 `~syvrio-escaped~` 前缀和百分号编码。扩展名支持小写字母、数字、下划线和点分命名空间。未知扩展可以重复、嵌套，程序更新已知字段时保留这些扩展的原文字节。

`recorded_at` 是必填写入时刻。有会话来源时同时保存 `session_id` 和 `message_id`，可用 `read_memory_source` 查到用户原话。`supersedes` 可指向被更正或发生变化的旧事实。记忆 ID 跨主题唯一。`active` 和 `deprecated` 分别表示可用和已弃用，弃用的事实继续保留原文与来源。

Context 支持 `event_time`、`valid_from`、`valid_to`、`location` 和情绪字段。`emotion.event` 保存事件当时的感受，`emotion.recollection` 保存回想时的感受，`emotion.unspecified` 保存时点不明确的情绪原话。同一事实可以同时保留两种感受。更新可以补齐空字段及更改状态。改写事实正文时建立新 ID，根据更正或状态演化的语义处理旧事实。

## 数据布局

| 相对 Vault 的路径 | 用途 |
| --- | --- |
| `memories/<category>/<topic>.md` | 正式记忆，类别可嵌套 |
| `memories/pending.md` | 活动候选及其 `question`、`related_ids`、来源信息 |
| `archived/pending/rejected.md` | 用户拒绝的候选原文，保留其扩展 |
| `inferences/<kind>/<id>.md` | 一个动态分类下每条推断的独立记录 |
| `profile.md` | 后台 Dream 生成的完整只读画像 |
| `records/memory-changes.jsonl` | 正式记忆变化事件 |
| `records/memory/audit.jsonl` | 操作审计 |
| `sessions/YYYY/MM/<id>.jsonl` | 会话事件、标题、稳定消息 ID、附件清单、压缩与沉淀进度。生成失败记录 `turn_failed` 保存对应用户消息 ID、时间和脱敏原因，随回合撤销。 |
| `archived/sessions/YYYY/MM/<id>.jsonl` | 每个归档会话一份原文，用原 ID 查看、还原或永久删除。整个 `archived/` 排除在检索、来源回查、后台任务输入和自动 Git 同步之外。 |
| `attachments/<session-id>/<message-id>/<attachment-id>.<ext>` | 已绑定到消息的上传原件 |
| `SOUL.md` | 可选的个性化提示词 |

会话标题沿用 `type: title` 事件。模型生成的标题带有 `source: llm` 和生成依据的 `answer_id`，撤回对应回答后失效。用户手动设置的标题保存在同类事件中。Runtime 中尚未完成的标题任务只保存会话和消息引用。


确认候选时写入正式记忆，并从活动文件移除。改写后确认会保存最终正文、清空与旧表述关联的 Context，并同时登记后台补充任务。拒绝会把原文移到归档。`related_ids` 提供审核上下文，仅审核候选本身不会改变这些已有事实。批准重试以候选的稳定 ID 找回已写入的结果。

推断的 Core 包含 `id`、`status`、`text`、`kind`、`source_ids`，Extensions 保存 `created_at`、`updated_at`。`source_ids` 是包含一个或多个 `id` 子项的列表，指向真实的正式记忆。用户可将 `hypothesis` 确认为 `confirmed`，或弃用为 `deprecated`。人工改写会在同一事务中弃用旧记录并用新 ID 创建已确认记录，旧文继续可查。事实发生变化后，已有推断保留其当前状态。事实检索仅投影记忆 ID、状态、主题、正文和 Context。

正式记忆首次写入产生 `{"id":"mem-example","change":"created"}`。已有事实的实际修改仅记录发生变化的新值，例如 `{"id":"mem-example","change":{"context":{"valid_to":"2026-01"},"status":"deprecated"}}`。同值重试保留原有变化日志。候选和推断单独保存各自的生命周期。

## Runtime 与恢复

每套部署使用一个 Vault 和一个 Runtime，分别存放在独立目录。首次使用时 Runtime 记录 Vault 的物理路径，启动发现路径不符就停止并保留现有文件。配置中的 `vault`、`runtime` 与环境覆盖规则见[配置示例](../config.example.toml)。Runtime 应位于 Git 工作树之外。Web、TUI、CLI 和 MCP 的业务请求连接同一个常驻后端。后端按实际 Vault 身份获取独占权，Runtime 里另外保存本地端点和运行锁。

| 相对 Runtime 的路径 | 用途 |
| --- | --- |
| `state/vault-binding.json` | 此 Runtime 配对的 Vault 绝对路径 |
| `state/backend.json` | 本地端口、访问令牌和绑定的 Vault |
| `locks/`、`state/git-sync.json` | 写入、单会话日志协调与 Git 同步状态 |
| `indexes/`、`cache/` | 可重建索引与缓存 |
| `jobs/background-memory/{distillation,context,dream,title}/` | 后台任务、版本和续跑进度 |
| `jobs/vault-transaction.json` | 跨文件写入与永久删除会话的恢复记录 |
| `session-reasoning/<session-id>/<message-id>.json` | 本机保存的助手推理附件 |
| `turn-undo/<session-id>/<first-user-message-id>.json` | 最新回合文件撤回需要的原始状态与校验值 |
| `logs/` | 本地诊断日志 |

升级前请先用原版本恢复未完成的事务并停掉后端。如果旧布局的 `jobs/vault-transactions/` 中仍有事务日志，需用原版本和原来的 Vault、Runtime 完成恢复。如果 `jobs/background-memory/<路径摘要>/` 中仍有任务或进度文件，核实属于当前 Vault 后，停机将各任务类型目录下的文件移到对应的新目录并保留文件名。遇到同名文件应先核对内容。新版检测到这些遗留工作时会停止启动并保留文件。

多文件修改先在 Runtime 保存恢复记录，再在 Vault 锁内逐个替换目标文件。操作全部成功才清除恢复记录。进程异常退出后，下次访问会先恢复整个未完成操作。若文件被其他写入方改成了无法验证的版本，恢复会停下并报告冲突。Git 快照与 Vault 写入共用同一把写锁。

`/undo` 只处理最新回合。安全插入的用户消息通过 `turn_root` 关联到首条消息。撤回时校验受影响文件和仍会存续的来源引用，再统一恢复文件并追加会话撤回事件。编辑重发时，撤回事件与替代用户消息一起追加，并与工具文件恢复一起提交。写入失败时，恢复记录会还原原会话与这些文件。新增记忆在无冲突时可以随这次撤回消失。这是正式记忆允许物理移除的例外。别的写者修改了受影响文件或新增了依赖时保留整个回合。已发出的联网请求无法收回。需要撤回工具写入的回合必须有对应 Runtime 日志。

## 停机后在原目录更换数据

先让常驻后端正常退出，再在原 Vault 路径切换 Git 版本、修改文件或用备份替换目录。保留部署配置和 Vault 的绝对路径。重启会从当前目录读取正式记忆和会话。整库恢复时用备份替换整个目录，避免新版独有的文件残留。Git 历史只覆盖已跟踪的文件。

恢复整套历史时，优先同时还原同一时点的 Runtime。单独回退记忆文件或会话正文不会回退 Runtime 里的思考附件、工具撤回凭据和排队任务。只持有 Vault 备份，又希望从该版本重新开始时，可先将停机后的 Runtime 完整另存，再在原 Runtime 路径启动。届时会重新建立索引，已另存的旧任务和旧撤回记录不会参与新一次运行。

## 附件和备份

Web 与 TUI 可以一起上传多份文档或图片。每份原件在会话中有稳定 ID、显示名、字节数、媒体类型和 SHA-256。恢复原件时按这些标识核对。手动核验包含活动与归档会话，以及已撤回消息的上传原件。Vault 根的 `.gitignore` 排除 `/attachments/` 与 `/archived/`，仅克隆 Git 仓库无法还原上传原件。

迁机或完整备份时先停掉常驻后端及其他写入者，复制整个 Vault，包括 `attachments/`。如果上次运行异常退出，先用原先配对的 Vault 和 Runtime 重开并完成恢复，再制作快照。需要保留历史思考面板、撤回能力或未完成任务时同时复制匹配的 Runtime，并妥善保护其中的本地凭据和原文件快照。若迁机后 Vault 的绝对路径发生变化，确认目标是同一套停机备份后，在目标副本的 `state/vault-binding.json` 中将 `vault` 更新为新目录的物理绝对路径，保留其余字段。旧的工具撤回凭据记录了原绝对路径，跨路径搬迁后仅新回合可生成可用的工具撤回凭据。助手消息的 `reasoning_local` 标记关联 Runtime 附件。推理附件缺失时，仍可读取会话回复。

归档只移动会话正文，保留稳定 ID、原始附件、思考和撤销记录。

永久删除已归档会话前会检查正式记忆的来源，包括已弃用的记忆。仍有引用时需保留会话。只有活动候选引用时，确认框会列出这些候选，确认删除后一起移除。会话拥有的上传原件和本地辅助文件随会话删除。

## 时间规范

绝对时刻使用带 UTC 偏移的 ISO 8601，程序写入统一为 `+00:00`。形式包括记忆 `recorded_at`、候选 `created_at`、推断 `created_at/updated_at`、审计 `at` 和会话事件 `at`。程序输入要求明确偏移，展示按查看者时区转换。会话保存源消息已知的 `original_local_date`，供跨午夜、跨时区时解释“昨天”“去年”等表述。无法确定来源本地日期时保留不确定性。

`event_time`、`valid_from`、`valid_to` 为日历值，可分别精确到 `YYYY`、`YYYY-MM` 或 `YYYY-MM-DD`。日期区间、Git 每日计划、搜索配额的自然日与耗时各保留自身语义。历史事件的日期和写入数据库的时刻可分别追溯。

## 时间字段清单

| 数据 | 字段 | 含义 |
| --- | --- | --- |
| 会话事件 | `started_at`、`at` | 会话开始和事件发生的 UTC 时刻 |
| 用户来源消息 | `original_local_date` | 由已知原始偏移确定的本地日历日 |
| 正式记忆 Extensions | `recorded_at` | 事实落盘的 UTC 时刻 |
| 正式记忆和候选 Context | `event_time`、`valid_from`、`valid_to` | 事件或有效期的年、月或日 |
| 活动及拒绝归档的候选 | `created_at` | 候选首次写入的 UTC 时刻 |
| 推断 Extensions | `created_at`、`updated_at` | 本条推断创建及最近修订的 UTC 时刻 |
| 审计记录 | `at` | 业务操作的 UTC 时刻 |
| Git 同步状态 | `last_commit_at`、`last_push_at`、`last_sync_at`、`retry_after` | 提交、推送、同步与重试的 UTC 时刻 |
