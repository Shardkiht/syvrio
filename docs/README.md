# Syvrio Evo 文档索引

本目录用于 `feature-memory-evolution`。先按目的阅读现行文档，再查交接或 Git 历史定位早期决策。快速启动、Web 演示和当前客户端能力见[项目 README](../README.md)。

## 现行产品与工程规范

| 文档 | 内容 |
| --- | --- |
| [PRD.md](PRD.md) | 产品定位、场景、Foundation/Evo 边界、功能与验收 |
| [ROADMAP.md](ROADMAP.md) | Now、Next、Later 的优先级、依赖和完成条件 |
| [architecture.md](architecture.md) | 已实现的分层、数据流、存储、资源生命周期和分支同步 |
| [web.md](web.md) | Web 操作、API、源码与受管理部署、更新和恢复 |
| [vault-v2.md](vault-v2.md) | 数据格式、备份、迁移和回退 |
| [time.md](time.md) | UTC 存储、界面 24 小时制与历史字段 |
| [开发约定](../AGENTS.md) | 代码、数据安全、测试、脱敏和提交规则 |
| [配置示例](../config.example.toml) | 实际配置键与默认值 |
| [用户手册](../README.md) · [English](../README.en.md) | 安装、命令和使用方式 |

## 阅读顺序

- **产品或规划**：PRD → ROADMAP。
- **开发或审查**：架构 → AGENTS → 对应技术规范 → 源码与测试。
- **升级和部署**：Vault V2 → 时间规范 → Web 指南。
- **查旧决策或历史验证**：交接 → 原提交、发布记录或当时的审查报告。

## 交接与历史

- [HANDOFF.md](HANDOFF.md) 顶部维护当前接续信息，其余为实现记录与以往验收。对当前行为的判断以现行规范、代码和可复现实验为准。
- [早期代码审查](review-2026-10-27.md) 与 [稳定版冻结审查](review-2026-11-01-freeze.md) 用于查询历史背景。
