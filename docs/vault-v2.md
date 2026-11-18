# 记忆 Schema v2、Vault 布局 v2 与时间规范

- 适用分支：`main`
- 文档状态：现行数据格式与时间基准
- 更新日期：2026-11-17

## 记录格式

记忆文件中的每个顶层无序列表项就是一条记录。正文先用自由 Markdown 书写，同一列表项内再放一个独立的 `**Core**` 段落；可选的 `**Context**` 和 `**Extensions**` 段落按此顺序跟随。字段列表要缩进到该条目内。正文里的代码围栏和普通嵌套列表仍算内容。

```markdown
# 展览

* 我去看了一个**纸质雕塑**展。

  **Core**
  * `id`: `mem-example`
  * `status`: `active`
  * `source`: `note`
  * `confidence`: `0.95`

  **Context**
  * `temporal.event_time`: `2024-06`
  * `spatial.location`: `示例港`
  * `affective.emotion`: `惊喜`

  **Extensions**
  * `custom.marker`: `cobalt-fox`
  * `custom.marker`: `second-marker`
  * `entities.person`:
    * `name`: `示例助手`
```

Core 恰好四个字段。文件必须是 UTF-8；解码失败时报错会指出受影响的 Vault 相对路径，便于重新以 UTF-8 保存。ID 在整个 Vault 内唯一，且不应再改动。状态取 `active`、`superseded` 或 `deleted`。来源是非空字符串，置信度是 0 到 1 之间的有限数值。字段名用 snake_case，可带点分隔的命名空间。可选段落允许有序、重复的字段和嵌套分组。标量值使用行内代码；更长的反引号定界符可以包含较短的反引号；字面换行用保留前缀 `~syvrio-escaped~` 加百分号编码写入。这里没有 frontmatter，也没有内嵌 JSON 元数据。

`syvrio.recorded_at`、`syvrio.supersedes`、`syvrio.session`、`syvrio.message` 和 `syvrio.change_kind` 保存写入时间、关联和来源链，不能替代事件时间。程序用时间上下文生成当前视图和历史视图，并把正文、Context 与 Extensions 一起索引供召回使用。选中的记录以完整原文作为非可信 Markdown 引用交给模型，同时附上 Vault 相对路径与行号；超长记录直接跳过而不是截断。修改状态只替换该值所在位置，其余记录字节与 LF/CRLF 保持不变。

```text
~/.syvrio/
  config.toml
  vault/
    README.md
    SOUL.md
    memories/<category>/<topic>.md
    sessions/YYYY/MM/<id>.jsonl
    sessions/legacy/undated/<id>.jsonl
    records/memory/{pending,resolved,audit,inferences}.jsonl
  runtime/
    indexes/vectors.npz
    cache/search/
    cache/profile/profile-summary.json
    state/{reflection,fingerprints,git-sync,usage,vault-binding}.json
    jobs/dreaming/jobs.jsonl
    session-reasoning/<session-id>/<message-id>.json
    turn-undo/<session-id>/<first-user-message-id>.json
    locks/{client,vault,git}.lock
    logs/
```

类别可以嵌套。主题名重复时用 Vault 相对 Markdown 路径区分。Git 只跟踪 Vault，看到的是记忆、会话和记录变更。切换活动 Vault 前先停掉正在运行的 Syvrio 客户端。Runtime 绑定到别的 Vault 后，其已知派生文件全部失效，Vault 数据本身不受影响。在 Syvrio 停止时删除 Runtime 会保留事实与会话文本，但会丢掉本机推理附件、计数和任务进度；需要这些记录就单独备份 Runtime。

助手记录可能带有 `reasoning_local` 标记，指向 Runtime 中的私有推理附件；附件含推理正文和可选的协议片段。恢复数据后还想重开这些推理，就必须保留 Runtime。旧会话无需重写 JSONL：没有该标记的助手记录照样渲染回复；附件或附件 ID 缺失时，回复仍可加载，只是没有推理内容。旧的正文内嵌推理字段不用于展示，也不参与模型重放。

从支持整回合撤销的版本开始，Ctrl+Enter 新增的用户消息可以带可选 `turn_root`，指向同一回合首条用户消息。旧记录无需更新。工具写入前，Runtime 下的 `turn-undo/` 记录受影响的本地文件原始状态和预期写入结果。此日志包含私有备份，不进入 Vault Git 历史。撤销时先核对所有受影响的文件和推断。发生后续冲突时保留整个回合，成功时追加会话和审计事件；原有审计和推断历史保留。工具记录上的可选 `turn_undo_recorded` 标记表明撤销依赖这份 Runtime 日志；如果日志丢失，请先恢复匹配的 Runtime。没有此标记的旧回合仍可撤回对话。删除会话也会清理对应的本机撤销日志。

配置查找顺序：`SYVRIO_CONFIG` → 当前目录 `syvrio.toml` → `~/.syvrio/config.toml`；最后一个文件不存在时仍接受旧的平台配置路径。`vault` 与 `runtime` 是各自独立的顶层设置，`SYVRIO_RUNTIME` 覆盖 Runtime 路径。Runtime 必须位于任何 Git 工作树之外，并与 Vault 分开。见 [config.example.toml](../config.example.toml)。

## 迁移既有 Vault

1. 停掉所有 Syvrio 写入方。把整个 Vault（含隐藏文件和 `.git`）以及既有 Runtime 归档，校验 SHA-256，并把归档恢复到测试目录。
2. 在恢复出的副本上执行下面的命令，目标目录对必须为空且共享同一父目录。旧日志或状态也在 Vault 外时用 `--source-runtime`。
3. 核对计数和新 Runtime 中的私有报告，比对 ID、会话字节和记录日志，验证测试副本后再切换配置。对未改动、完全一致的目标对重复执行 `--apply` 是安全的。校验会检测源数据被编辑、冲突或变更；新的迁移请用全新目标目录。发布 Runtime 之后被中断的运行，可依据已验证的报告继续。
4. 把本地配置切到验证通过的目标对，保留原配置和归档用于回滚。v1 读端无法打开 v2 记录；回滚必须把旧程序和旧 Vault 一起恢复。

```sh
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --dry-run
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --apply
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --verify
```

Runtime 目标默认是 `vault-v2-runtime`，可用 `--runtime-destination` 指定同级目录。脚本不写输入、不改配置、不联网。原始记忆文件保存在 Runtime 的私有旧版归档里，会话与记录 JSONL 按字节复制。没有显式 ID 的旧文件沿用一个读端的确定性 ID；ID 重复会中止迁移。来源未知写为 `migration:unknown`；置信度缺失或非法时使用保守的存储值 `0` 并加 `migration.review_required` 标记。这些只是审查标记，不是推断出的事实。未知键和无法直接表示的取值以编码后的 extension 值保留，同时留在原始归档文件中。

## 时间规范

创建绝对时间戳使用 `syvrio.time.utc_now()` 或 `utc_timestamp()`，用 `serialize_timestamp()` 持久化为带 `+00:00` 的 ISO 8601。输入与比较用 `parse_timestamp()`：它要求显式偏移、接受 `Z`，返回 UTC datetime。可选字段缺失时保持原有含义；绝对时间戳字段里出现非法值或不带偏移的值，需要显式做数据修正。

展示时按查看者本地时区输出 `YYYY-MM-DD HH:mm`，只在必要时加秒。TUI 与 CLI 用 `format_local_timestamp()`，Web 接收 UTC 时间戳后在浏览器侧用 `web/src/time.ts` 转换；两边都使用 00–23 小时制。TUI 会话标签可以只显示 `HH:mm`。本地展示不影响排序。

日历日期（含按天精度导入的记录）、日期区间、Git 的每日墙钟计划、搜索配额的自然日边界、Unix 秒、耗时与超时都保持原有类型和语义。相对日期表达式的输入若自带偏移，按来源日历解释；UTC 存储的消息在解释新的相对日期时，用当前客户端的本地日历确定日期锚点。

## 时间字段清单

| 存储 | 绝对时间戳字段 | 旧表示 |
| --- | --- | --- |
| `sessions/**/*.jsonl` | `started_at`、`at`；列表的 `updated_at`、`last_message_at` 由它们派生 | 创建时间为本地裸时间，消息与控制记录带本地偏移 |
| `memories/**/*.md` | Extensions 中的 `syvrio.recorded_at` | 本地裸时间；导入记录带偏移或按天精度 |
| `records/memory/pending.jsonl`、`resolved.jsonl` | `created_at`，适用时含 `resolved_at` | 本地裸时间 |
| `records/memory/audit.jsonl` | `at` | 本地裸时间 |
| `records/memory/inferences.jsonl` | `created_at`；快照上的 `updated_at`，含 `revision.records[]` | 本地偏移 |
| Runtime `state/reflection.json` | `last_checked_at`；`profile` 与 `dream` 的 `last_attempt_at`、`last_success_at`、`next_retry_at` | 本地偏移 |
| Runtime `jobs/dreaming/jobs.jsonl`、`cache/profile/profile-summary.json` | 分别为 `at`、`generated_at` | 本地偏移 |
| Runtime `state/git-sync.json` | `last_commit_at`、`last_push_at`、`last_sync_at`、`retry_after` | Git 提交偏移或本地偏移 |
| Runtime 诊断与延迟日志 | `time` | UTC；既有日志本来就是 UTC |
| 搜索缓存与额度 | 缓存年龄用 Unix 秒；配额键是本地自然日/月 | 不变 |
| Web API | 会话时间字段与命令载荷时间戳 | 现在按 UTC 发送，在浏览器侧本地化展示 |

记忆的事件时间与有效期保持日历字段。修改记忆状态会更新状态，并在适用时产生一条 UTC 审计事件，但不会改写原始记录时间。新增持久化时间字段时，同时更新这份清单与迁移脚本的字段映射。

## 迁移时间戳（UTC）

用 v1 Vault 时，先完成并验证[迁移既有 Vault](#迁移既有-vault)。整个迁移期间停掉 TUI、Web、MCP、定时 Git 同步以及所选 Vault/Runtime 的其他写入方。使用实际存储根路径，并在 apply 前确认来源时区。脚本从不读取活动配置，也不从本机推断来源时区。

```bash
uv run python scripts/migrate_timestamps_to_utc.py \
  --vault /path/to/vault --runtime /path/to/runtime \
  --source-timezone Asia/Shanghai --dry-run

uv run python scripts/migrate_timestamps_to_utc.py \
  --vault /path/to/vault --runtime /path/to/runtime \
  --source-timezone Asia/Shanghai --apply --backup-dir /path/to/new-backup
```

- `--vault` 含标准会话树；独立的会话树用 `--sessions` 指定。加 `--runtime` 会把活动 Runtime 一并迁移；同时作用于两者时，apply 会获取它的 client、Git 和 Vault 锁。
- 不同范围可用 `--vault-source-timezone`、`--sessions-source-timezone`、`--runtime-source-timezone`；`--timezone-for` 可重复使用范围相对前缀，例如 `sessions/legacy=America/New_York`。匹配最具体的路径优先。
- 默认是只读预览。非法值、裸时间缺少来源时区、记录格式错误、夏令时歧义或不存在的时间点都会在写入前中止整批。已被有效 recovery 标记覆盖的会话行按字节保留；其余问题用原始时区或偏移的独立证据解决后重新跑预览。
- apply 要求在新的、位于数据根之外的备份目录中进行：先备份受影响文件再替换，在 `vault/`、`sessions/`、`runtime/` 下保留其相对路径，并使用原子替换。未知字段、缺失值与日历日期都保留；匹配的推断证据指纹继续有效，过期指纹仍为过期；Runtime 画像缓存可以重建。
- apply 中断或想撤销时，先停掉写入方，再把每个备份文件复制回原根下相同的相对路径。`sessions/` 备份对应显式的会话根或 Vault 的 `sessions/` 目录。验证恢复结果前不要删备份。apply 是幂等的；迁移成功后最后一次 dry-run 应报告零变更。
