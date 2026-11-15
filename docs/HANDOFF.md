# 开发交接

## 分支

- `main` 是稳定源码分支。通过受保护的 PR 和 Debian CI 交付稳定变更。
- `feature-memory-evolution` 用于实验性记忆功能。PyPI 稳定包与 Evo 预发布独立安排。
- 稳定发布从 main 手动触发。PR 合并只更新源码。

## Foundation

- Foundation 使用 Vault V2 和 UTC 存储，界面显示本地 24 小时制。既有 Vault 的备份、V2 与 UTC 迁移顺序见 [vault-v2.md](vault-v2.md)。
- Foundation 保留原有记忆检索、抽取和裁决。会话推理原文写 Runtime 附件，附件缺失时仍能读取正文。
- Web、TUI 共用 Vault 客户端锁。Web 切换会话时后台补沉淀，断线后继续生成，重新进入可读取已保存的回复。流式阅读时自动跟随底部，手动上滑暂停。
- 本地测试使用临时配置、Vault、Runtime 和合成数据。安装与运行见 [README](../README.md)，完整文档见 [索引](README.md)。
