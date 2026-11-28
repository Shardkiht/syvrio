# Syvrio 文档索引

本目录用于 `main`。先按目的阅读现行文档，再查交接或 Git 历史定位早期决策。快速启动、Web 演示和当前客户端能力见[项目 README](../README.md)。

## 现行产品与工程规范

| 文档 | 内容 |
| --- | --- |
| [product.md](product.md) | 产品定位、场景、当前能力及验收路线图 |
| [architecture.md](architecture.md) | 统一后端、会话调度、SQLite、联网搜索与部署边界 |
| [web.md](web.md) | Web 操作、API、源码与受管理部署、更新和恢复 |
| [storage.md](storage.md) | 数据格式、备份与恢复，以及 UTC 存储与时间字段清单 |
| [开发约定](../AGENTS.md) | 代码、数据安全、测试、脱敏和提交规则 |
| [配置示例](../config.example.toml) | 本地覆盖示例，完整默认值见 [内置配置](../src/syvrio/infra/config/defaults/) |
| [用户手册](../README.md) · [English](../README.en.md) | 安装、命令和使用方式 |

## 阅读顺序

- **产品或规划**：product（产品需求与路线图）。
- **开发或审查**：架构 → AGENTS → 对应技术规范 → 源码与测试。
- **升级和部署**：SQLite 存储与恢复 → Web 指南。旧文件 Vault 的导入见存储文档的专节。
- **复现评测**：[评测指南](../evals/README.md)（本地记忆检索与联网搜索离线质量验证）。
- **查旧决策或历史验证**：交接 → 原提交、发布记录或当时的审查报告。

## 交接与历史

- [HANDOFF.md](HANDOFF.md) 顶部维护当前接续信息，其余为实现记录与以往验收。对当前行为的判断以现行规范、代码和可复现实验为准。
- [历史代码评审](reviews.md) 汇总早期代码审查与稳定版冻结审查，用于查询历史背景。
