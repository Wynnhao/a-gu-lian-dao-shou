# A股镰刀手 · 借鉴 CloddsBot 架构层 · 调研档案

> - **日期**：2026-09-19
> - **来源**：飞书文档《CloddsBot 架构与交易决策机制》（v1.9.0 源码梳理），doc id `OXdldCDeBo3AaCxYvCfcD2NrnRd`
> - **性质**：架构层借鉴的三方调研档案（对照方=现状代码证据，见 §0.2）
> - **状态**：已审核通过（2026-09-19 对抗式审查：4 项 pass-with-fix + 1 项 pass，无红线冲突）；施工方案见 `docs/C-ARC架构加固施工方案-2026-09-19.md`（含 ADR-0 执行失败 taxonomy、ADR-A1 触发器改 F1a-only、ADR-A3 验收口径修正）
> - **与上轮的关系**：2026-09-17 的 `docs/策略库借鉴-cloddsbot.md` 借鉴的是**策略层**（STRATEGIES.md，9 项已拍板）；本档案借鉴的是**架构层**（五层架构/三层防线/事件总线/执行保护/录制回放），编号 `C-ARC-*` 与 `C-STR-*` 区分，无重叠

---

## 0. 异构前提与现状证据

### 0.1 为什么这轮只看架构

CloddsBot 是加密/预测市场 HFT 连续流系统（21 渠道、7 永续所、链上 DEX、200x 杠杆），A股镰刀手是 T+1 日频 4 节点 cron 模拟盘（paper + LLM 决策 + 人工闸门）。**市场与节奏结构性异构，策略层已按 C-STR-* 拍板完毕；架构层中"执行安全 / 数据基础设施 / 留痕完整性"三类机制与市场异构无关，才有借鉴价值。**

### 0.2 现状代码证据（Explore 全库调研，2026-09-19）

| CloddsBot 概念 | 镰刀手现状 | 结论 |
|---|---|---|
| TradingOrchestrator 统一订单门卫（所有下单路径必经） | `risk.engine.check()` 挂在 propose（runner.py:579）与 confirm（runner.py:732）两处；webapp confirm 走子进程同样过检；`PaperBroker.buy/sell` 是 trade 表唯一写入口（全库调用点仅 runner 3 处） | ✅ **已等价甚至更严**（多一层人工 confirm） |
| confirm 时风控重跑 | confirm 完整重跑 check + 成交价二次校验（runner.py:766-780） | ✅ 已覆盖 |
| L2 SafetyManager（kill switch/回撤停机/集中度/日亏） | 规则 5 8% kill + 72h 停机、规则 6 集中度、max_positions=5 / max_daily_trades=3 / min_confidence=0.6 | ✅ 已覆盖 |
| L1 信号过滤（强度≥0.5/冷却120s/仓位≤5/日亏） | 等价物在 L2：min_confidence、同日同参查重（runner.py:532-550）、max_positions、规则 5 | ✅ 等价（日频下"冷却"即查重） |
| L3 价格保护 ±2% / 滑点 | `rule_price_guard` 2% + confirm 二次校验 + 滑点模拟 10bps | ✅ 已覆盖 |
| 默认 dry-run | 整个系统就是 paper 模拟盘 | ✅ 天然满足 |
| 信号串行处理防并发放大 | `_exec_lock` + catchup/postclose flock + `_RUNNER_LOCK`，决策生成单进程串行 | ✅ 已覆盖 |
| 模拟盘三件套 sim-engine/feed/wallet | `execution/paper.py`（撮合+虚拟资金+readback）+ live quotes | ✅ 已覆盖 |
| **L3 执行级重试/错误率熔断** | 无。exec 失败只留 `execution_failed` 等人工；熔断仅数据源层（source_max_fail=5） | ❌ **缺口 → C-ARC-1/2** |
| **run-tick-recorder 长期录制供回放** | 无。仅机会性 `logs/quotes/*.jsonl`（TTL 30s，密度取决于调用方），无固定录制、无回放 | ❌ **缺口 → C-ARC-3** |
| SignalRouter "丢弃并计数" | risk_event 覆盖主链路，但 warnings/fail-open/窗外触线不落库 | ❌ **部分缺口 → C-ARC-4** |
| 门卫不变量 | 事实成立但无守护测试，靠约定 | ⚠️ 可固化 → C-ARC-5 |
| SignalBus 事件总线 | 直接函数调用 + SQLite/文件交接 | 🔍 评估 → C-ARC-6 |
| 动态 Kelly 反馈回路 | 已拒（CONSTRAINTS C-STR-3） | ❌ 不复议 |
| 21 渠道 / TWAP / DCA / MEV 防护 | — | ❌ 加密特化，不适用 |

---

## 1. 决策记录（ADR，待拍板）

### ADR-A1 · C-ARC-1 执行级有限重挂（保守版）——建议采纳

**痛点实证**：09-16 两次价格保护拒单（10:02 premarket #28 偏离 4.29%、12:17 午休 #31）均需人工「status 重置 + 按实时价重挂 + 再 confirm」，13:01 重挂 13:16 才成交。这是当前最高频的人工介入点。

**方案（保守版，不碰红线）**：`execution_failed`（price_guard/涨跌停模拟拒）后，当日在剩余交易时段内自动**重挂**——按实时价生成新委托参数、完整重跑 `check()`、写回 pending 等人工 confirm。上限 3 次/决策，每次落 `risk_event(exec_retry)`。
- **明确不做激进版**（自动换价重执行）：用户 confirm 的是特定价位附近的委托，自动换价成交超出确认语义，触 L3 红线精神。
- 与 Sprint 4「9b monitor_requeue」的关系：9b 是 `monitor` 态决策重提，C-ARC-1 是 `execution_failed` 态重挂，同机制不同状态，实现时应共用重提工具函数。
- 工时 ~1.0d（runner.py + intraday_check 触发 + tests 3 case）。

**反方论点**：止损单被拒后重挂可能追价更深。**对策**：重挂价仍受 rule_price_guard ±2% 与止损优先级链约束；追价连续 3 次失败即放弃并落 `stop_loss_unfilled` 告警（盘中已有人工盯 PENDING 的习惯，09-16 即如此）。

### ADR-A2 · C-ARC-2 执行失败熔断——建议采纳

连续 3 笔 `execution_failed`（当日累计）→ 当日停止新 propose、落 `risk_event(exec_circuit_breaker)`、webapp HealthPanel 红警。与 fetcher 数据源熔断（source_max_fail=5 → 冷却 30min）同构，搬到执行层。防止 C-ARC-1 的重挂机制在异常行情/数据坏价下无限空转。工时 ~0.5d。

### ADR-A3 · C-ARC-3 盘中分钟快照录制器——建议采纳（本档案最大增量）

**借鉴对象**：run-tick-recorder 长期后台录制历史 Tick 供策略离线回放。

**方案**：launchd 新增 `com.agsickle.recorder`（StartInterval=300，交易时段 gate 9:30-11:30/13:00-15:00，复用 `is_trading_time`），每 5 分钟调 `quotes.get_live_prices`（含 spot_tx 兜底）落新表 `minute_snapshot(code, ts, price, volume, amount)`，86 票自选池 + 3 指数。存储量 ~100 万行/年，SQLite + (code,ts) 索引可承受；postclose 周度清理 >2 年数据。
- **需要豁免**：技术方案 §8.3「不引新表」约束需为此新增一条显式例外（写 ADR 即本条）。
- **解锁能力**：尾盘 14:50 时点决策回测、止损触线时点回测（当前只有日线，"10:30 触线会怎样"不可验证）、limit_halt 应急策略回放、Sprint 3.5 追踪止盈 N 参数的盘中校准——是策略层借鉴项的**验证基础设施**。
- 工时 ~1.5d（录制器 + 清理 + 回放读取接口 + tests）。
- **先决条件**：先跑 2 周评估数据质量与缺测率，再接入回测。

**反方论点**：5 分钟粒度对"触线精确时点"仍粗。**对策**：够用于"当日何时触线/触线后走势"级别的归因；1 分钟粒度存储×5 且 akshare 免费接口撑不住，不追。

### ADR-A4 · C-ARC-4 拒绝/告警计数补全——建议采纳

SignalRouter 哲学"丢弃并计数"落到留痕层：补齐 risk_event 缺口——(a) 规则 13 手数规整等 warnings；(b) regime 动态闸/规则 20 拥挤读失败的 fail-open；(c) kill 清仓每笔卖出明细事件。统一 `warn_*/failopen_*` 前缀，复用 flush_events 同日去重防爆量。价值：09-17"600519 跌停应急疑似数据异常待复核"这类悬案正是缺留痕所致。工时 ~1.0d。

### ADR-A5 · C-ARC-5 门卫不变量守护测试——建议采纳（最便宜）

把"PaperBroker.buy/sell 唯一成交入口、调用点仅 runner 3 处"从约定固化为静态测试：扫描源码 `broker.buy(`/`broker.sell(`/`PaperBroker(` 调用点对照白名单，新增调用点必须显式改白名单（触发 review）。工时 ~0.5d，直接服务 CONSTRAINTS §3.1。

### ADR-A6 · C-ARC-6 SignalBus 事件总线——拒绝

(1) 日频 4 节点 + 短命进程 + SQLite/文件交接是**有意的简单架构**，直接调用无解耦需求；(2) 总线=常驻进程+新故障面，与"PENDING 兜底 + kill 链"的可靠性模型冲突；(3) CloddsBot 需要总线是因为连续 Tick 流 + 21 渠道并发。**复活条件**：盘中连续决策（分钟级策略）上线且出现 ≥3 个生产者-消费者对。

### ADR-A7 · C-ARC-7 动态 Kelly 反馈回路——不复议

CONSTRAINTS C-STR-3 已拒（样本不足 + 规则 5 永远先到 + §8.3"不要绕过硬规则"）。架构文档复述不改结论。

### ADR-A8 · C-ARC-8 多渠道接入/TWAP/DCA/MEV——拒绝

加密特化。TWAP 对 paper 撮合反而是错误建模（A 股散户无算法拆单通道）；渠道接入现有 webapp+终端+看门狗足够。复活条件实质违反 L1 边界（接实盘），即永不。

### ADR-A9 · C-ARC-9 盘中连续盯市——暂缓

把止损/涨跌停监控从 11:35/14:50 双窗加密到分钟级。**暂缓理由**：依赖 C-ARC-3 先录制 2 周，量化"双窗之间漏了多少触线事件"再决定加密到什么频率（避免拍脑袋加密度）。复活条件：录制数据显示窗外触线 ≥1 次/周且造成过实际损失。

---

## 2. 采纳清单速览（待拍板后进排期）

| # | 项 | 层 | 工时 | 依赖 |
|---|----|----|------|------|
| C-ARC-1 | 执行级有限重挂（保守版） | 执行 | 1.0d | 无 |
| C-ARC-2 | 执行失败熔断 | 执行 | 0.5d | 与 C-ARC-1 配对 |
| C-ARC-5 | 门卫不变量守护测试 | 测试 | 0.5d | 无 |
| C-ARC-3 | 分钟快照录制器 | 数据 | 1.5d | 豁免"不引新表"；跑 2 周再接回测 |
| C-ARC-4 | 拒绝/告警计数补全 | 留痕 | 1.0d | 无 |

建议批次：**批次 A（执行安全 ~2d）**= 1+2+5，消当下人工重提痛点；**批次 B（数据基础设施 ~2.5d）**= 3+4，解锁盘中回测。全部与 P6 并行可行，不碰策略语义、不改 confirm 契约、不引第三方依赖。

## 3. 组合化学反应

- **A**：C-ARC-3 × Sprint 3.5 追踪止盈/时间退出 → 止盈参数（1.5N/3N）首次获得盘中时点数据校准，§-1 前置回测从"只有日线"升级。
- **B**：C-ARC-1 × C-ARC-2 → 重挂与熔断是一对：重提失败累计触熔断，防异常行情下隐性追价空转。
- **C**：C-ARC-4 × C-ARC-3 → 快照反推窗外触线 + warn 留痕补全 → 复盘归因链闭合（决策表、risk_event、盘中价格三源可对账）。

## 4. 验收（拍板后随批次细化）

1. 批次 A：pytest 全绿；守护测试在故意注入第 4 处 broker.buy 调用点时变红；`exec_retry`/`exec_circuit_breaker` 落库 100%；confirm 闸门 PENDING 闭环 24h 仍 0 泄漏。
2. 批次 B：录制器连续 5 交易日缺测率 <5%；`warn_*` 事件可按日聚合；回放接口能重放 09-16 300750 止损日分钟价并与 trade 时点对账。

## 5. 拍板后回填清单

- `CONSTRAINTS.md`：附录 A 增 C-ARC ADR 索引；§4.1 增 C-ARC-6/7/8 拒绝条目；§3.1 可引用 C-ARC-5 守护测试。
- `技术方案.md`：§8 后新增 §9 排期（或在 §8.5 并批）；§8.3 "不引新表"增 minute_snapshot 例外。
- 本档案状态行改"已拍板"。

## 6. 文档维护

- 维护规则同 `docs/策略库借鉴-cloddsbot.md` §9：推翻任何 C-ARC-* 需用户拍板 + 微型 ADR，被推翻条目保留不删。
