# 更新日志

本项目的变更记录按版本号管理，遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

## [v1.3.0] - 2026-09-24 · 开源首发版

首个对外公开版本。

### 主要内容

- 五层架构（数据 / 信号 / AI 决策 / 风控 / 执行）+ 流水线（盘前 / 午评 / 下午 / 尾盘 / 盘后 / catchup 兜底）
- 19 条硬规则风控裁决引擎，含市场环境总闸（RSRS 三档 + 二八轮动）+ ATR 自适应止损
- LLM 决策 JSON 强制校验与可追溯快照（`decision.input_snapshot`）
- 模拟成交（`PaperBroker`）+ 人工闸门（confirm / reject 双写接口）
- Web 控制台（stdlib 后端 + React/Vite + shadcn/ui 前端，9 个功能页签）
- 复盘层（日报 / 周报 / 信号有效性评估）
- 测试套件：10 个文件 158 用例（截至 v1.3 末）

### 已知边界

- **仅模拟盘**：默认 `execution.mode: paper`，不接实盘资金
- **macOS 优先**：调度依赖 `launchd`，Linux/Windows 用户可跑回测/测试/Web 控制台，但需自配 cron/systemd
- **A 股市场**：T+1、涨跌停、ST 黑名单等 A 股硬约束原生建模；不适用于其它市场
- **公开行情数据源**：东方财富 / 雪球 / 新浪 / 腾讯财经 / 乐咕乐股，遵守各自 ToS，不可商用
- **LLM 后端**：当前依赖第三方 API（详见 `ai/decide.py`），运行前需自行配置密钥

### 与此前内部版本的差异

- 移除个人身份信息（commit author 重写为 GitHub noreply 邮箱）
- 工作区清理：移除损坏数据库副本、营销素材、空文件
- 文档重组：核心 5 篇保留在 `docs/`，内部研发过程文档归档到 `docs/internal/archives/`
- 新增标准开源文件：`LICENSE` / `CONTRIBUTING.md` / `SECURITY.md` / `CODE_OF_CONDUCT.md`

### 安全声明

本项目不构成投资建议。所有决策均由 LLM 在人工闸门下产出，作者不对任何真实交易损失负责。详见 [SECURITY.md](./SECURITY.md)。
