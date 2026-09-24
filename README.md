# A股镰刀手 / AGSICKLE

> A 股 AI 模拟交易系统 · v1.3 · paper-only · 单机本地运行
> 规则引擎硬裁决 + LLM 决策 + 人工闸门 + 市场环境总闸

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/Tests-10%20files%20%C2%B7%20158%20cases-green.svg)](./tests/run_all.py)
[![Paper-only](https://img.shields.io/badge/Trading-Paper%20Only-orange.svg)](./SECURITY.md)
[![Platform: macOS](https://img.shields.io/badge/Platform-macOS-lightgrey.svg)](#平台支持)

---

## ⚠️ 重要声明

**本项目仅用于本地模拟交易，不连接任何真实券商账户、不执行任何真实交易。**

- 本项目输出**不构成投资建议**
- 数据源（东方财富 / 雪球 / 新浪 / 腾讯财经 / 乐咕乐股等）均为公开 API，遵守各自 ToS，**不可商用**
- 所有资金变动均为纸面记账，作者不对任何真实交易损失负责
- 严禁将本项目用于实盘对接

详见 [SECURITY.md](./SECURITY.md)。

---

## 这是什么

一个端到端的 A 股模拟交易自动化系统。从数据采集、信号计算、LLM 决策、风控裁决，到人工闸门、模拟成交、复盘统计，全部在一个本地仓库里跑通。

**和同类开源项目的差异**：

| 维度 | 本项目 | backtrader / zipline | akshare / qstock |
|---|---|---|---|
| 完整交易链路 | 数据 → 信号 → AI 决策 → 风控 → 闸门 → 成交 → 复盘 | 仅回测框架 | 仅数据/工具 |
| A 股硬约束 | T+1 / 涨跌停 / ST 黑名单 / 集合竞价原生建模 | 需自配 | — |
| 决策方式 | LLM 输出 + 规则引擎硬裁决 | 策略代码 | — |
| 风控 | 19 条硬规则（永不自动成交） | 需自配 | — |
| 默认安全姿态 | paper-only + 人工闸门 | 直连券商示例 | — |

简而言之：这是一个**"LLM 当交易员 + 硬规则当合规"**的实验场，适合想研究 AI 决策在真实市场约束下表现的开发者。

---

## 30 秒上手

```bash
# 1. 克隆 & 装依赖
git clone https://github.com/Wynnhao/a-gu-lian-dao-shou.git
cd a-gu-lian-dao-shou
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 跑测试（必须全绿）
.venv/bin/python3 tests/run_all.py

# 3. 启动 Web 控制台
.venv/bin/python3 webapp/server.py
# 浏览器打开 http://127.0.0.1:8317
```

macOS 用户可双击 `启动控制台.command` 一键启动（含后台守护与自动开浏览器）。

---

## 每日节奏

| 时点 | 任务 | 输出 |
|---|---|---|
| 09:00 | 盘前：行情 / 资讯 / 信号 / 动态池 → 输入包 → **LLM 决策** → 入库 | `decision` 表 |
| 09:31 | 同会话跑风控（propose-db），订单挂人工闸门 | `orders/<日期>/pending_*.json` |
| 09:30~15:00 | — | **人工在 Web 控制台「成交与风控」页 confirm 或 reject** |
| 11:35 | 午评：实时价 + 新闻刷新 → LLM 增量复核（默认 `[]` 不动作） | `decision` 表 |
| 13:30 | 下午复核：12:30 起增量新闻 + 实时价 → LLM 增量复核 | `decision` 表 |
| 14:50 | 尾盘扫描：实时回撤 kill 检查 + pending 漂移预警 | 通知 / 写盘 |
| 15:30 | 盘后：日线入库 → 盯市 → 日报 / 周报 | `logs/reports/` |
| 漏开机 | catchup.py 三层兜底（cron 互备 / launchd 看门狗 / 安全侧失效） | 自动补跑 |

---

## 目录索引

```
A股镰刀手/
├── README.md                     ← 本文件
├── 技术方案.md                    # 总体设计、P1~P6 分阶段计划与验收标准
├── CONSTRAINTS.md                # 项目硬约束章程（L1~L4 红线 + 流程约束）
├── CHANGELOG.md                  # 更新日志
├── LICENSE                       # MIT 许可证
├── CONTRIBUTING.md               # 贡献指南
├── SECURITY.md                   # 安全策略 + paper-only 声明
├── CODE_OF_CONDUCT.md            # 贡献者公约
├── 启动控制台.command              # macOS 双击启动看板
├── config.json                   # 全局配置（watchlist / 风控 / 流水线 / 通知）
│
├── data/                         # ── 数据层 ──
│   ├── fetcher.py                # 行情增量入库（东财主源 + 腾讯兜底；退避熔断）
│   ├── news.py                   # 资讯采集（个股新闻 + 公告 + 市场快讯）
│   ├── macro.py                  # 指数日线 + PE/PB 历史分位
│   ├── quotes.py                 # 盘中实时行情（30s TTL）
│   ├── calendar.py               # 交易日历（新浪表缓存 + 节假日判断）
│   └── audit.py                  # 数据体检 / 修复 / 备份
│
├── signals/                      # ── 信号层 ──
│   ├── factors.py                # 因子库（MA/RSI/ATR/动量/换手分位）
│   ├── signals.py                # 信号计算 → signal 表（多 profile）
│   ├── backtest.py               # 回测（生产同源 score + 双 profile 对比）
│   ├── dynpool.py                # 动态池读写
│   ├── movers.py                 # 异动池（五规则筛选）
│   └── hot.py                    # 热门池（题材 + 个股新闻突增）
│
├── ai/                           # ── AI 决策层 ──
│   ├── bundle.py                 # 决策输入包组装
│   └── decide.py                 # 决策 JSON 强制校验 + 落库
│
├── risk/                         # ── 风控层 ──
│   ├── blacklist.py              # 黑名单（上市<60 日 / ST / N 次新）
│   ├── regime.py                 # 市场环境总闸（RSRS 三档 + 二八轮动）
│   ├── engine.py                 # 19 条硬规则裁决
│   └── notify.py                 # 通知（macOS 通知中心 + webhook）
│
├── execution/                    # ── 执行层 ──
│   ├── paper.py                  # PaperBroker 模拟成交
│   └── runner.py                 # 编排：propose → 闸门 → confirm → 成交 → 回读
│
├── pipeline/                     # ── 流水线（cron 入口）──
│   ├── premarket.py              # 盘前 09:00
│   ├── midday.py                 # 午评 11:35
│   ├── afternoon.py              # 下午复核 13:30
│   ├── intraday_check.py         # 尾盘 14:50
│   ├── postclose.py              # 盘后 15:30
│   └── catchup.py                # 漏开机兜底（幂等补跑）
│
├── review/                       # ── 复盘层 ──
│   ├── daily.py                  # 盯市 + 每日复盘报告
│   ├── weekly.py                 # 周报（vs 沪深300 + 归因）
│   └── signal_eval.py            # 信号有效性评估（RankIC / 五分位）
│
├── webapp/                       # ── Web 控制台 ──
│   ├── server.py                 # stdlib 后端（22 GET + 2 POST 端点）
│   └── frontend/                 # React + Vite + shadcn/ui 前端
│
├── tests/                        # 10 个测试文件 158 用例
│
├── docs/                         # ── 文档 ──
│   ├── README.md                 # 文档导航
│   ├── 决策策略与工作流.md          # LLM 规则 / 19 条裁决 / 流程
│   ├── 策略库.md                  # 19 个策略 + 落地状态
│   ├── 代码审查报告.md             # 全面审查结论
│   ├── 优化修复纪要.md             # 40+ 项修复清单
│   └── internal/                 # 研发过程留档（不供外部阅读）
│
├── data/market.db                # SQLite 数据库（WAL 模式，14 张表）
├── logs/                         # 运行产物（不入仓）
└── requirements.txt              # Python 依赖锁定
```

---

## 关键配置

`config.json` 的核心段（详见文件内嵌注释）：

| 段 | 内容 |
|---|---|
| `watchlist` | 自选池（默认 86 只，按 8 个题材分组） |
| `watchlist_core` / `watchlist_extended` | 可交易池 / 仅观察池 |
| `risk` | 19 条硬规则阈值（止损 8% / ATR 自适应 / 概念集中度 / 流动性上限） |
| `regime` | 市场环境总闸（RSRS 三档 + 二八轮动 → 动态总仓位 cap） |
| `execution` | `mode: paper` 为默认；滑点 / 成交量参与 / 停板模拟 |
| `notify` | macOS 通知 / webhook（kill / 回读失败 / 成交失败） |
| `signals` | profile：`momentum` / `reversal_lowvol` / `reversal_lowvol_v2` |

**改配置前先备份**：复制 `config.json` 为 `config.local.json`，本地改动在后者。

---

## Web 控制台

启动 `webapp/server.py` 后访问 `http://127.0.0.1:8317`：

| 页签 | 用途 |
|---|---|
| 决策工作流 | 十段流水线追踪 + 决策链 |
| 总览 | 持仓 + 账户概况 |
| 信号 | 自选池信号分数 |
| 自选分组 | 概念股组合视图 |
| 决策 | 历史决策列表与归因 |
| 成交与风控 | 人工闸门 confirm / reject |
| 新闻与宏观 | 实时资讯 |
| 报告与日志 | 日报 / 周报 / 运行日志 |
| 策略库 | 19 个候选策略 |

写操作仅两个端点：`POST /api/confirm` 与 `POST /api/reject`，均有 Host / Origin / Content-Type 三重校验。

---

## 平台支持

| 平台 | 数据采集 | Web 控制台 | 测试 | 调度（cron） |
|---|---|---|---|---|
| macOS | ✅ | ✅ | ✅ | ✅ launchd 看门狗 |
| Linux | ✅ | ✅ | ✅ | ⚠️ 需自配 systemd-timer / cron |
| Windows | ✅ | ✅ | ✅ | ⚠️ 需自配 schtasks |

**macOS 是最丝滑的平台**：`启动控制台.command` 与 `看门狗开关.command` 是 macOS 专用脚本。Linux/Windows 用户可用 cron / systemd-timer 调度 `pipeline/*.py`。

---

## 文档

| 文档 | 用途 |
|---|---|
| [技术方案.md](./技术方案.md) | 总体设计、P1~P6 分阶段计划与验收标准 |
| [CONSTRAINTS.md](./CONSTRAINTS.md) | 项目硬约束章程（L1~L4 红线 + 流程约束） |
| [CHANGELOG.md](./CHANGELOG.md) | 更新日志 |
| [docs/决策策略与工作流.md](./docs/决策策略与工作流.md) | LLM 规则、19 条裁决、流程细节 |
| [docs/策略库.md](./docs/策略库.md) | 19 个候选策略与落地状态 |
| [docs/代码审查报告.md](./docs/代码审查报告.md) | 全面审查结论 |
| [docs/README.md](./docs/README.md) | 完整文档导航 |

---

## 数据源合规

本项目使用以下公开行情 / 资讯 API：

- **东方财富**（行情主源）— 公开网页接口
- **腾讯财经**（行情兜底 + 实时）— 公开网页接口
- **新浪财经**（交易日历）— 公开网页接口
- **雪球**（资讯）— 公开网页接口
- **乐咕乐股**（指数估值）— 公开网页接口

各家均**不允许大批量爬取用于商业用途**。本项目配置里的 `data.source_cooldown_min: 30` 与 `source_max_fail: 5` 已做节流与熔断。请勿降低节流参数、不要并发爬取、不要用作商业服务。

---

## 许可证

MIT — 详见 [LICENSE](./LICENSE)。

## 致谢

- akshare / pandas / requests / tqdm / pillow / mini-racer 等开源依赖
- Wind MCP、Claude Code 等工具生态
- 所有提 Issue / PR 的贡献者
