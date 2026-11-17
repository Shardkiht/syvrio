# Syvrio Evo 文档索引

本目录用于 `feature-memory-evolution`。先按目的阅读现行文档，再查交接或 Git 历史定位早期决策。快速启动、Web 演示和当前客户端能力见[项目 README](../README.md)。

## 现行产品与工程规范

| 文档 | 内容 |
| --- | --- |
| [product.md](product.md) | 产品定位、场景、Foundation/Evo 边界、功能与验收、路线图 |
| [architecture.md](architecture.md) | 已实现的分层、数据流、存储、资源生命周期和分支同步 |
| [web.md](web.md) | Web 操作、API、源码与受管理部署、更新和恢复 |
| [vault-v2.md](vault-v2.md) | 数据格式、备份、迁移、回退，以及 UTC 存储与时间字段清单 |
| [开发约定](../AGENTS.md) | 代码、数据安全、测试、脱敏和提交规则 |
| [配置示例](../config.example.toml) | 实际配置键与默认值 |
| [用户手册](../README.md) · [English](../README.en.md) | 安装、命令和使用方式 |

## 阅读顺序

- **产品或规划**：product（产品需求与路线图）。
- **开发或审查**：架构 → AGENTS → 对应技术规范 → 源码与测试。
- **升级和部署**：Vault V2 与时间规范 → Web 指南。
- **查旧决策或历史验证**：交接 → 原提交、发布记录或当时的审查报告。

## 交接与历史

- [HANDOFF.md](HANDOFF.md) 顶部维护当前接续信息，其余为实现记录与以往验收。对当前行为的判断以现行规范、代码和可复现实验为准。
- [历史代码评审](reviews.md) 汇总早期代码审查与稳定版冻结审查，用于查询历史背景。
