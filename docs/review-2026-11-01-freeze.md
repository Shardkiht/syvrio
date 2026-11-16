# Quiet-Memo main 冻结前代码评审（2026-11-01）

> 历史评审快照。保留当时的项目名称、发现和验证结论；当前设计及命令请看[文档索引](README.md)。

> **品牌迁移提示（2026-11-04）**：本报告沿用当时的 `Quiet-Memo` 名称与 `src/quiet_memo` 路径，仅作历史证据；当前项目为 `Syvrio`，源码路径为 `src/syvrio`，Vault 元数据目录为 `.syvrio/`。

> **历史评审／已结案提示（2026-11-02）**：本报告的 `b016ccbf`、`0.3.2` 和“F1/F2 待修”等均是冻结前的时点证据，不代表当前状态。后续 PR #15 已合并稳定性修复并形成 `portfolio-2026-11-01`（`316a030`）；PR #16 仅做发布准备，`main` 已到 `22217c2`，`v0.3.3` GitHub Release 已发布。两次 PR 的 Windows/Ubuntu CI 均通过；原生 Windows TUI 人工验收仍未确认。现行分支规则见 [AGENTS.md](../AGENTS.md) 和 [HANDOFF.md](HANDOFF.md)。以下历史评审正文保留原貌，禁止把其待办当成当前指令。

> 基线：`main@b016ccbf4e220db8e432da115f0ab4a0c677b89c`（本报告和分支规范的文档提交不改变该源码基线）。
> 方法：远端源码静态走查，核查相关单测、已有评审和 GitHub Actions 日志。**本轮未在本地重新运行 pytest、打包、真实 TUI 或真实模型 / API 调用**。
> 决策：后续默认在 `feature-memory-evolution` 开发；`main` 只做最终稳定性验收后冻结的简历展示基线，未经用户针对 main 明确授权不得自动同步或合并。

## 结论

- **有条件可冻结**：现有自动化测试与模块化程度可以支撑简历展示；静态走查未发现已证实的 P0 / P1 阻断项。
- 冻结前建议处理 F1、F2，或明确记录故障注入验收结果；F3 应在文档中注明接受或采取有限修复。
- 应在代码修复停止后完成一次实际安装/打包与终端人工检查；以固定 SHA/专用 portfolio tag 关联简历，不能把较早的 `v0.3.2` Release 直接视为本轮 `main` 快照。

## 验证与覆盖

- `main` 源码基线：`b016ccbf`；分支保护已开启。
- GitHub Actions：https://github.com/Shardkiht/quiet-memo/actions/runs/41281207052 。Windows 和 Ubuntu 两个 job 均 success；Ubuntu 日志：**846 passed, 5 skipped, 4 warnings**。警告来自依赖（jieba/Pydantic），不能等同于项目逻辑失败。
- `.github/workflows/test.yml` 执行 `uv sync --frozen` 与 `pytest -q`，没有在普通 push/PR CI 阶段单独运行 `uv build`；发布工作流 `workflow.yml` 有打包步骤。
- 已重点审阅：`agent/session.py`、`agent/runtime.py`、`agent/context.py`、`agent/tools.py`、`app/chat_flow.py`、`app/chat_app.py`、`app/chat_commands.py`、`memory/{service,store,queue,detect,models,extractor}.py`、`retrieval/{keyword,vector,hybrid}.py`、`infra/search/` 搜索/抓取/计数模块、`llm/client.py`、配置、测试及文档。属于重点静态审查，**不宣称逐行审完仓库所有文件**。

## 发现清单（按优先级）

### F1 · P2 · 损坏会话隔离范围不足（建议冻结前修）

- `src/quiet_memo/agent/session.py:204-234`：`SessionStore.load()` 对中间行不可解码的 UTF-8 会抛 `UnicodeDecodeError`，符合“未标记中间损坏不能静默忽略”的数据不变量。
- 但 `session.py:278-307` 的 `_listing_row()`、`session.py:337-360` 的 `unconsolidated()` 只捕获部分异常（`JSONDecodeError`、`KeyError`），可能让**单个损坏文件导致整个会话列表或补沉淀扫描中断**。另外，合法 JSON 但非对象的记录在 `load():221` 访问 `record.get` 时可能抛 `AttributeError`。
- 当前 `tests/test_session.py` 已覆盖损坏尾行恢复和中间行加载失败；未看到“损坏会话与正常会话并存时列表/扫描仍可用”的针对性用例。
- 建议：保持单文件直接读取时报错和原始字节不变，在列表/扫描批量入口隔离单文件错误；用临时 vault 注入坏 UTF-8 中间行与非对象 JSON，并验证正常会话不受影响、错误不丢失。

### F2 · P2 · 待确认状态跨文件更新的失败恢复（建议冻结前做故障注入）

- `src/quiet_memo/memory/queue.py:56-66`：`resolve()` 先从 `pending.jsonl` 移除项，再写 `resolved.jsonl`。如果后一步磁盘写失败或进程中断，待确认项已消失，但解决日志未写，影响审计追溯。
- `src/quiet_memo/memory/service.py:496-514`：`approve()` 依次写新记忆、修改旧状态、删除待确认项、写审计；`service.py:394-419` 的 `correct()` 同样先写新条目再停用旧条目。跨多个原子文件操作的中途故障可能留下重复有效记忆/部分完成状态。
- 已有 `test_approve_append_failure_keeps_previous_memory_active` 与 pending 保存失败测试，但与以上**中间步骤**对应的故障注入/崩溃恢复验证仍需补充。
- 建议：优先保证失败可检测、重试幂等及审计可补偿；如需引入大规模事务基础设施，本次宜记录残余风险、交由 evolution 处理。避免临时复杂重构。

### F3 · P2 / 已知限制 · 厂商调用计数不保证跨进程原子性

- `src/quiet_memo/infra/search/usage.py:22-31,106-124` 使用进程内 `RLock`，Search / Fetch 同进程可共享，**多个 QM/MCP 进程并发时不能保证绝对不超限**。`docs/HANDOFF.md` 已明确这一边界。
- `tests/test_search_usage.py` 的并发验证是线程内实例，不等于跨进程事务测试。
- 冻结建议：保留功能并在说明中明确“本地保守限额，非供应商账单上限”；多实例严格计费保护属于 evolution 可选工作，不作为本轮冻结的强制重构。

### F4 · P3 · 可选联网依赖未完全固定

- `src/quiet_memo/infra/search/open_websearch.py:259-264` 与 `infra/search/daemon.py:59-66` 均存在 `open-websearch@latest` 兜底启动路径。
- 使用该路径时，后续安装可能拉取不同上游实现，影响冻结版本的环境复现与兼容性。
- 建议固定已经验证的依赖版本，或者明确记录稳定运行环境并保留已安装命令优先逻辑。是否改动以兼容性回归为前提。

### F5 · P3 · 极端并发下的会话创建占用

- `src/quiet_memo/agent/session.py:183-198` 为同秒随机 ID 加 `path.exists()` 循环，但最终使用 `write_text()`，不是独占创建。在**同时有多个进程创建完全相同 ID**的极端竞态下理论上可能覆盖文件。
- 常规 TUI 入口序列化且已有随机碰撞重试测试。无复现证据，冻结期可以接受；以后若支持多实例并发，可用独占打开补强。

### F6 · 发布与简历展示版本一致性（发布前必核）

- `pyproject.toml` 为 `0.3.2`，公开 `v0.3.2` Release 指向较早发布过程；当前 `main` 已有后续变更。
- README 对当前特性描述、合成/真实检索结果口径、搜索评测范围应与固定源码一致；优先使用**专属 commit SHA / portfolio 标签**标识简历展示版本。
- `evals/README.md` 载明 456 合成记忆/27 用例的 BM25/Vector/Hybrid Hit@3 分别为 74.1% / 92.6% / 92.6%，882 真实有效事实/24 模型生成题分别为 95.8% / 91.7% / 91.7%。这不是同一检索方法优化前后的提升，也不能把小样本模型生成题写成人工标注的真实准确率。

## 已确认的正面设计

- 记忆主题 Markdown 使用同目录临时文件 + `os.replace()`；会话保留原始 JSONL，损坏尾行采用追加恢复标记，不覆写原始字节。
- 检索接口解耦，BM25 + 可选向量 + Hybrid，向量异常有冷却和关键词降级路径；部分关键算法有增量缓存与回归用例。
- TUI 生成期间输入、跨会话排队、Esc 取消、消息所属会话隔离、Markdown 复制等已有广泛 UI 测试。
- 搜索结果有质量门控、多个 provider 回退、超时/调用次数预算与外部资料的提示注入边界；Search 与 Fetch 使用独立顺序和共享本地厂商计数。
- 配置和凭据存储优先环境变量/系统 Keyring，仓库 .gitignore 排除本地 vault、会话、配置与密钥等敏感产物。

## 最终冻结验收建议

1. 修复/验证 F1 的批量异常隔离；为 F2 做故障注入，决定做小修还是明确接受剩余风险。
2. 按 `AGENTS.md` 要求运行受影响测试；对最终冻结版本跑一次 `uv sync --frozen`、`uv run pytest -q`、`uv build` 和 CLI 基础启动检查。真实 Windows TUI 输入/复制/切换应人工验收；未运行的步骤不得标已通过。
3. 不沿用历史真实 API 调用授权；本轮评审不调用付费模型、外部 Search API，也不读取个人 vault。
4. 确认最终稳定 SHA 和 CI 后再由用户决定标签/发布；**不要因本报告而自动修改业务代码、创建 Release 或合并 evolution 到 main**。
5. **后续所有开发从 `feature-memory-evolution` 开始**。本轮收尾完成后 `main` 不接受常规功能、架构探索或自动修复同步；特殊稳定修复需用户明确指定。

## 评审结论

`main@b016ccbf` 可作为稳定展示基线的候选，质量基础已较充分。**不等同于已经通过最终打包/真实终端验收，也不意味着风险为零。** 后续 Agent 应优先处理 F1 和 F2 这两个直接涉及数据可靠性的收尾项，完成验收后冻结。


## Local implementation and acceptance update

The final stability task independently reproduced F1/F2. Record validation and per-file diagnostics now isolate invalid sessions while direct load still fails and raw bytes stay unchanged. F2 uses scoped exception compensation for manual operations, with fault injection after state/log writes and retry checks, including cross-topic approval. It is deliberately not a durable transaction: forced termination, power loss, concurrent writers and rollback failure remain explicit limitations. F3 (cross-process quota atomicity) and F4 (optional latest upstream fallback) are accepted documented limits, without a new subsystem or unverified dependency pin. F5 now uses exclusive file creation, tested against an injected create-time race. F6 retains package version 0.3.2 and separates the portfolio source tag from the older release and historical evaluation cohorts; no evaluation inputs or metric values changed.

A build inspection also found default sdist inclusion of a pre-existing untracked local script. The archive now has a project-content allowlist; user-directed relocation preserved the original script outside the repository. Archive listings exclude local scripts/data/caches, and wheel CLI installation was checked in a disposable environment with locked dependencies. No real credentials, personal Vault, paid API or model calls were used. A flaky Markdown copy assertion was corrected to compare against the rendered width at copy time after scrollbar layout, rather than a stale snapshot.

Local validation: affected session/queue/service/Agent suite 155 passed; queue suite including explicit rollback-error reporting 8 passed; Markdown suite 25 passed; final default full suite 871 passed, 4 optional document tests skipped, one existing dependency warning. `uv sync --frozen` and `uv build` succeeded. Real Windows terminal/IME/OS clipboard interaction remains unaccepted manually; headless tests are not presented as manual verification. Remote PR, dual-platform CI, stable source SHA/tag and branch deletion are separate gates and must be recorded from actual results.

The pre-existing main Windows CI failure (run 41284275480) was independently inspected: native cursor refocus was asserted before its after-refresh event. The regression now awaits the captured cursor-show event and still checks active focus and cleanup. Product cursor behavior is unchanged; related input cases pass locally.
