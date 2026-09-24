---
name: Bug 报告
about: 报告 A股镰刀手（AGSICKLE）的功能异常或崩溃
title: "[Bug] "
labels: bug
---

## 现象

简明描述你遇到的 bug。预期行为 vs 实际行为。

## 复现步骤

最小复现步骤（命令序列 + 配置 / 输入数据）：

1. …
2. …
3. …

## 实际结果

报错堆栈、异常退出码、相关日志片段（请用 ` ``` ` 代码块包裹，注意脱敏）：

```
…(paste here)
```

## 预期结果

按你的理解应该发生什么。

## 环境

- 操作系统与版本（macOS 13.5 / Ubuntu 22.04 / Windows 11 ...）
- Python 版本（`.venv/bin/python3 --version`）
- 仓库 commit hash（`git rev-parse HEAD`）
- 是否为 macOS launchd / Linux cron / 手动运行
- 是否运行了完整 cron（盘前 → 午评 → 下午 → 尾盘 → 盘后）

## 配置文件相关

- `config.json` 关键段是否改过？（如有改动请说明）
- 是否启用了 webhook / macOS 通知？

## 影响面

- 是否影响你的 paper 模拟盘决策流水？
- 是否影响数据入库？
- 是否影响其他模块？

## 截图 / 日志

如有关键日志文件（`logs/<module>.log`、`logs/catchup.log`、`logs/webapp.log`），请指明文件路径与时间窗口。
