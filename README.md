# A股镰刀手 · 项目索引

> A股 AI 模拟交易系统 v1.2 ｜ paper 模拟盘（不接实盘）｜ 规则引擎硬裁决 + LLM 决策 + 人工闸门
> 状态：**P6 模拟盘试运行就绪**（代码与功能完整性审查已通过，见 docs/代码审查报告.md）
> 本文件是全项目导航索引；总体设计见 [技术方案.md](技术方案.md)

---

## 30 秒上手

| 动作 | 方式 |
|---|---|
| 打开看板 | 双击 **`启动控制台.command`**（或 `python3 webapp/server.py` → http://127.0.0.1:8317） |
| 看门狗安装/卸载 | 双击 **`看门狗开关.command`**（按当前状态自动切换；装好后需一次性授权，步骤脚本会打印） |
| 跑测试 | `python3 tests/test_*.py`（8 个文件 119 用例，全部离线，逐个直跑） |
| 重建前端 | `cd webapp/frontend && npm run build`（产物 → webapp/dist，Storybook: `npm run storybook`） |
| 补跑漏掉的任务 | `python3 pipeline/catchup.py`（幂等，随时可跑；4 个 cron 首步也会自动跑它） |

## 每日节奏（交易日，4 个 ZCode cron 已建）

| 时间 | 任务 | LLM | 人工动作 |
|---|---|---|---|
| 09:00 | 盘前：行情/资讯/信号/动态池 → 输入包 → **LLM 决策** → 入库 | ✅ | — |
| 09:31 | 同会话跑风控（propose-db），订单挂人工闸门 | — | — |
| 09:30-15:00 任意时点 | — | — | **看板「成交与风控」页点确认**（或 confirm 命令） |
| 11:35 | 午评：实时价+新闻刷新 → LLM 增量复核（默认 `[]` 不动作） | ✅ | — |
| 14:50 | 尾盘扫描：实时回撤 kill 检查 + pending 漂移预警（纯脚本） | ❌ | — |
| 15:30 | 盘后：日线入库 → 盯市 → 日报/周报 | ❌ | — |
| 漏开机兜底 | catchup.py 三层兜底（cron 互备 / launchd 看门狗 / 安全侧失效），见 docs/决策策略与工作流.md | ❌ | — |

---

## 目录索引

```
A股镰刀手/
├── README.md                 ← 本索引
├── 技术方案.md                # 总体设计、分阶段计划、验收标准（§5）与当前状态（§7）
├── config.json               # 全局配置：watchlist(30只·概念标签)/blacklist_rules/risk/pools/execution
├── 启动控制台.command          # 双击启动看板（后台常驻+自动开浏览器）
├── 看门狗开关.command          # 双击安装/卸载漏开机兜底看门狗（launchd，自动切换状态）
├── deploy/
│   └── com.agsickle.catchup.plist  # launchd 看门狗定义（看门狗开关脚本安装它）
│
├── data/                     # ── 数据层 ──
│   ├── fetcher.py            # 行情增量入库（东财主源+腾讯兜底；盘中自动防部分bar污染）
│   ├── news.py               # 资讯采集（个股 stock_news_em + 市场级全球快讯，去重幂等）
│   ├── macro.py              # 指数日线 + PE/PB 历史分位（乐咕乐股，多源降级）
│   └── quotes.py             # 盘中实时行情（腾讯批量主源+东财兜底，30s TTL，快照审计 logs/quotes/）
│
├── signals/                  # ── 信号层 ──
│   ├── factors.py            # 因子库：MA/RSI/ATR/动量/换手分位/涨跌停幅度（纯函数）
│   ├── signals.py            # 信号计算 → signal 表（score 加权合成，黑名单过滤）
│   ├── backtest.py           # 动量周调仓回测（前视偏差已修正）→ logs/backtest_result.json
│   ├── dynpool.py            # 动态池读写（按日留痕，当前成员=最新刷新行）
│   ├── movers.py             # 异动池：五规则筛异动（涨幅/放量/振幅/加速/新高新低，全市场快照优先+降级链）
│   └── hot.py                # 热门池：题材关键词计数+个股新闻突增+概念板块榜（TTL缓存）
│
├── ai/                       # ── AI 决策层 ──
│   ├── bundle.py             # 决策输入包组装（信号+新闻+宏观+异动热门+账户）→ logs/session/<日>/bundle.md
│   └── decide.py             # 决策 JSON 强制校验+落库（≥2条理由/置信度门槛建议/watch 放宽；失败=整包放弃）
│
├── risk/                     # ── 风控层 ──
│   ├── blacklist.py          # 黑名单（上市<60日/ST/N次新）+ 数据健康检查
│   └── engine.py             # 15条硬规则裁决（仓位/价格/T+1/涨跌停/时段/次数/kill熔断…），所有下单必经
│
├── execution/                # ── 执行层 ──
│   ├── paper.py              # PaperBroker 模拟成交（佣金/印花税/T+1/账本回读校验）
│   ├── runner.py             # 编排：propose(风控)→人工闸门→confirm(重跑风控)→成交→回读→状态机
│   └── runbook_ths.md        # 同花顺模拟炒股 UI 自动化手册（切 mode=ui 前必演练3次）
│
├── pipeline/                 # ── 流水线（cron 入口）──
│   ├── premarket.py          # 盘前 9:00：①行情→②资讯→③估值→④体检→⑤T+1解锁→⑥信号→⑥.5动态池→⑦bundle
│   ├── midday.py             # 午评 11:35：实时价+新闻刷新 → 增量决策输入包
│   ├── intraday_check.py     # 尾盘 14:50：实时回撤 kill 安全网 + pending 漂移预警（触发kill退出码2）
│   ├── postclose.py          # 盘后 15:30：日线入库→盯市→日报（周五+周报）→动态池收盘口径刷新
│   └── catchup.py            # 兜底补跑器（幂等）：漏开机场景自动补齐，launchd 每30分钟触发
│
├── review/                   # ── 复盘层 ──
│   ├── daily.py              # 盯市(portfolio_state) + 每日复盘报告 logs/reports/YYYY-MM-DD.md
│   └── weekly.py             # 周报：vs 沪深300 + 简化归因（选股/择时）
│
├── webapp/                   # ── Web 控制台 ──
│   ├── server.py             # stdlib 后端（22 GET + 2 POST 端点；127.0.0.1:8317；写接口有三重来源防护）
│   ├── frontend/             # React+Vite+TS+Tailwind+shadcn/ui 源码（npm run build → ../dist）
│   │   ├── src/pages/        # 8 页签：决策工作流/总览/信号/自选分组/决策/成交与风控/新闻与宏观/报告与日志/策略库
│   │   ├── src/stories/      # Storybook 组件工作台（npm run storybook）
│   │   └── dist/             # 构建产物（server 优先服务）
│   └── static/               # 旧版前端（已被 dist 替代，保留备用）
│
├── tests/                    # 8 个测试文件 119 用例（python3 tests/test_xxx.py 直跑，全离线）
│   ├── test_risk_engine.py(28) test_execution.py(21) test_ai_pipeline.py(17) test_signals.py(18)
│   └── test_review.py(15) test_news_macro.py(7) test_quotes.py(7) test_movers_hot.py(6)
│
├── logs/                     # 运行产物（全部可删，自动重建）
│   ├── reports/              # 日报 YYYY-MM-DD.md / 周报 YYYY-Www.md / 盘中扫描 intraday-*.md
│   ├── session/<日期>/       # bundle.md|json（决策输入包）、decision.json、midday_bundle.md
│   ├── orders/<日期>/        # 人工闸门待确认单 pending_<id>.json
│   ├── *.log                 # fetch/news/macro/signal/ai/pipeline/exec/quotes/webapp/catchup 审计日志
│   ├── quotes/               # 盘中快照审计（jsonl）
│   └── backtest_result.json  # 最新回测结果
│
├── docs/                     # ── 文档 ──
│   ├── 决策策略与工作流.md      # 决策包构成/LLM规则/15条裁决/全链路工作流/兜底机制（看板内嵌渲染）
│   ├── 策略库.md              # 19 个策略（GitHub开源+大赛复盘+因子，带来源URL与兼容度分级）
│   └── 代码审查报告.md         # 2026-09-13 全面审查：P0=0，P1×3已修，回测前视修正说明
│
└── data/market.db            # SQLite 数据库（13张表，见下）
```

## 数据库表索引（data/market.db，DDL 全在 data/fetcher.py）

| 表 | 用途 |
|---|---|
| daily_bar / stock_info / fetch_log | 日线行情 / 票档案（上市日等）/ 采集日志 |
| news / index_daily / index_valuation | 资讯（code=''为市场级）/ 指数日线 / PE·PB历史分位 |
| signal | 技术信号（signals JSON + score，每票每交易日） |
| decision | LLM 决策流水（input_snapshot 存完整输入包快照，可归因） |
| position / trade / portfolio_state | 持仓（T+1 用 avail_shares）/ 成交流水 / 每日盯市与回撤 |
| risk_event | 风控事件留痕（违规/kill/人工否决/回读异常） |
| dynamic_pool | 异动池/热门池成员（按刷新日期留痕，可回看进出池历史） |

## 看板 API 索引（webapp/server.py，全部 JSON）

核心：`/api/overview`（总览+持仓）· `/api/workflow`（十段流水线+决策追踪链）· `/api/concepts`（概念分组）· `/api/dynamic_pools`（异动/热门池）· `/api/equity_curve` · `/api/candles?code=` · `/api/signals` · `/api/decisions` · `/api/trades` · `/api/risk_events` · `/api/pending` · `/api/news` · `/api/macro(-_history)` · `/api/health` · `/api/reports` + `/api/report?file=` · `/api/logs?name=` · `/api/sessions` + `/api/session` · `/api/backtest` · `/api/doc?name=`
写操作（仅此两个，有三重来源防护）：`POST /api/confirm`、`POST /api/reject`

## 关键文档

| 文档 | 内容 |
|---|---|
| [技术方案.md](技术方案.md) | 总体设计、P1-P6 分阶段计划、验收标准与当前状态 |
| [docs/决策策略与工作流.md](docs/决策策略与工作流.md) | 决策包依据、LLM 规则、15 条裁决、盘中节奏、兜底机制（看板「决策工作流」页内嵌） |
| [docs/代码审查报告.md](docs/代码审查报告.md) | 2026-09-13 全面审查：P0=0、P1×3 已修、回测前视修正（年化 54.95%→31.81%，仍达标） |
| [docs/策略库.md](docs/策略库.md) | 19 个候选策略与 Top3 落地建议（看板「策略库」页内嵌） |
| [execution/runbook_ths.md](execution/runbook_ths.md) | 同花顺模拟炒股 UI 自动化操作手册（checklist） |

## 当前状态与 P6 前待办

- ✅ P1~P5 全部落地；审查通过（P0=0）；119 测试全绿；账本干净基线（¥1,000,000 空仓）
- ⏳ 待办 1：看门狗一次性授权（双击看门狗开关后按打印步骤操作，或电源设置禁用自动睡眠）
- ⏳ 待办 2：同花顺实机演练 3 次（按 execution/runbook_ths.md），之后才可切 `execution.mode: "ui"`
- ▶ 就绪后进入 **P6：连续 20 交易日模拟盘试运行**（验收口径见技术方案 §5.5）
