# A股镰刀手 · 借鉴 CloddsBot 策略库 · 三方讨论档案

> - **版本**：v1.0（2026-09-17）
> - **来源**：CloddsBot 策略汇总 `<外部项目>/STRATEGIES.md`
> - **触发**：用户提交 CloddsBot 策略库 → 是否借鉴 / 可行性 / 有机结合 三方 agent 调研结论固化
> - **状态**：三项共识（采纳 4 项、拒绝/暂缓 3 项、组合化学反应 3 项）已通过用户拍板，进入实施排期（见 §6）

---

## 0. 市场异构前提

CloddsBot 是加密/二元预测市场（Polymarket/Solana/Kalshi）的 HFT+做市+套利库，与本项目（A 股 T+1 限价撮合、涨跌停约束、散户主导）几乎完全异构。**直接照搬的策略基本没有**，但底层范式有可借鉴之处。本文档只记录"经三方讨论后达成共识"的部分，**不替代** `docs/策略库.md` 的策略研究归档。

---

## 1. 决策记录（ADR）

| # | 决策 | 结论 | 理由摘要 |
|---|---|---|---|
| 1 | 出场优先级显式化（追踪止盈 + 时间退出） | **采纳** | 与现有规则 5/16/21 无冲突；补 v1.4 reversal_lowvol MDD -41% 的"反转买完没止盈"根因 |
| 2 | 拉盘防御（pump defense） | **暂缓** | A 股涨跌停约束下结构性失效（涨停下买不进、跌停下卖不出），信号发出日 ≠ 可执行日 |
| 3 | ladder buy/sell 阶梯买卖 | **拒绝** | 与 T+1 制度冲突 + UI 复杂度 3x + 5 票 core 池分档无意义 |
| 4 | 业绩预告 vs 一致预期 edge | **降级采纳** | 业绩预告侧已就位（Sprint 2 P1-4）；一致预期侧 ak 无独立接口，覆盖率低，先用"预告净利润 vs 上期净利润"代替 |
| 5 | 涨停封单量 / 开板次数量化 | **采纳** | 规则 21 已有封单比，缺"开板次数"——与封单比互补，零数据成本（OHLC 重算） |
| 6 | 北向/龙虎榜/ETF 聪明钱检测 | **降级采纳** | ETF 旁路已落地（Sprint 2 P1-3）；北向降级为季度 cache（2024-08 披露降频已断日频）；龙虎榜彻底暂缓（监管整治 + 项目原决策） |
| 7 | 波动率自适应入场阈值 | **采纳**（受控） | 0.8 天/易；约束条件：仅在 regime=normal 启用，shock/off 档禁用（避免与拥挤降权 + vol_target 三层耦合） |
| 8 | 动态凯利 + 15% 减仓（强版） | **拒绝（强版）** | 样本 20 个交易日不够 → 任何凯利都是伪精确；与现有规则 5 8% kill 永远先到 |
| 8b | 软减仓（预警层，不 kill） | **采纳（弱化版）** | 给"8% kill"加 15% 预警层（不停机，仓位×0.7），落 risk_event；区别于规则 5 紧急熔断 |
| 9 | 入场时机三态语义 execute_now / wait / monitor | **采纳** | 0.5 天只改 schema，让"看到机会没下手"的样本留痕（归因/IC 重训练价值高） |

---

## 2. 三方讨论结论速览

| # | 借鉴项 | 审视者（反方） | 工程可行性 | 整合方案 | **综合** |
|---|--------|----------------|-----------|----------|---------|
| 1 | 出场优先级（追踪+时间退出） | ✅必要 | ✅1-2 天/易 | 阶段 1 (1.2d) | ✅ **采纳** |
| 2 | 拉盘防御 | ❌反对 | ⚠️必须 pending | 阶段 2 (1.2d) | ⚠️ **暂缓** |
| 3 | ladder 阶梯 | ❌反对 | ⚠️大改造 3-5 天 | 阶段 1 (1.5d) | ❌ **拒绝** |
| 4 | 业绩预告 edge | ⚠️部分就位 | 缺 consensus 接口 | 阶段 2 (1.5d) | ⚠️ **降级** |
| 5 | 涨停封单/开板次数 | ✅必要 | 半天/部分就绪 | 阶段 2 (1.0d) | ✅ **采纳** |
| 6 | 聪明钱（北向/龙虎/ETF） | ❌不推荐 | ETF 已就绪+北向可补 | 阶段 2 (1.8d) | ⚠️ **降级** |
| 7 | 波动率自适应 | ⚠️可选 | 半天/易 | 阶段 1 (0.8d) | ✅ **采纳**（受控） |
| 8 | 动态凯利 | ❌反对 | ⚠️与 8% kill 冲突 | 阶段 1 (1.0d) | ⚠️ **软减仓** |
| 9 | 三态语义 | ✅必要 | 2-3 天/中 | 阶段 1 (1.0d) | ✅ **采纳** |

---

## 3. 采纳清单（实施详情）

### 3.1 阶段 1 · 借鉴验证小 sprint（合计 ~4 人日）

> 启动条件：v1.4 momentum profile 已跑出 ≥2 周数据，避免改 profile 又改因子混淆归因。

| # | 借鉴项 | 关键文件 | 工时 | 验收标准 |
|---|--------|----------|------|----------|
| 7 | 波动率自适应入场阈值 | `signals/signals.py` `_score_cross_section` 加 `vol_regime_weight`；`config.json` `score_vol_regime.{low,mid,high}` | 0.8d | regime=high 时 vol 因子权重 +30%，RankIC 回测不降；**仅 regime=normal 启用** |
| 5a | 开板次数量化 | `signals/factors.py` 加 `board_open_count()`（OHLC 重算）；`signals/limit_halt.py` 同步输出 | 0.5d | `tests/signals/test_board_open_count.py` 全绿；规则 21 看到原值 |
| 1 | 追踪止盈基础版 | `signals/factors.py` 加 `trailing_atr(close, atr, mult)`；`risk/engine.py` 加 `rule_22_take_profit` | 1.0d | 浮盈 >2N 自动压回成本+1N；现有 21 条规则 demo 跑通 |
| 8b | 软减仓（15% 预警层） | `risk/regime.py` 加 `soft_drawdown_tier()`；`risk/engine.py` 加 `rule_25_soft_reduce` | 1.0d | 规则 5 不触发时软减仓先动；触发 15% 仓位×0.7 写 risk_event 不停机；**规则 6 max_single_weight 同步 0.20→0.15**（联动防集中度） |
| 9 | 三态语义 schema | `ai/decide.py` schema 加 `timing: execute_now/wait/monitor`；`execution/runner.py` 三态分发（wait→watchlist，monitor→15:00 重提） | 0.5d | bundle 落 `timing` 字段；DecisionTable.tsx 加列+徽章 |

**依赖**：⑦⑤① 共享 `atr_pct/live_quotes` hook，可并行；⑧b 需在 ① 完成后做（共用 risk_event 落库路径）；⑨ 独立。

### 3.2 阶段 2 · 因子扩 sprint（合计 ~5 人日）

| # | 借鉴项 | 关键文件 | 工时 | 验收标准 |
|---|--------|----------|------|----------|
| 4 | 业绩预告 edge（降级版） | `signals/earnings.py` 加 `earnings_compare()`（用预告净利润 vs 上期净利润，不引 consensus）；`data/fetcher.py` 扩 news_earnings 表 | 1.5d | net_score 升级 vs_last_period 列；bundle 注入 edge 段 |
| 5b | 封单量化补完 | `data/freadth.py` 加 `limit_seal_daily(code, date, seal_amount, open_board_count)` 表；`signals/factors.py` 加 `seal_strength`；规则 21 加 `ask1_vol/float_mv` 加权 | 1.0d | seal_ratio≥5% 升级 emergency_skip；与 Fix-4 stuck 共用 |
| 6 | ETF+北向降级 | `data/macro.py` 加 `fetch_north_bound_q()`（akshare `stock_hsgt_fund_flow_summary_em`，季度 cache 30d）；regime 第 7 路 ETF 板块综合；**不做龙虎榜** | 1.0d | regime compute 8 路并联；不引入日频幻觉 |
| 1b | 时间退出补完 | `risk/engine.py` 加 `rule_23_time_stop`（持有 N 日未达预期 → 减仓）；与 ① 联调 | 0.5d | 持有 15 日且浮盈 <2N 自动 review |
| 9b | monitor 重提 | `execution/runner.py` `monitor_requeue()`；`pipeline/intraday_check.py` 步骤 3.7 | 1.0d | 15:00 扫描 monitor 单，未达阈值重提 pending；runner 不双提交 |

---

## 4. 拒绝 / 暂缓清单（含复活条件）

| # | 项 | 状态 | 理由 | 复活条件 |
|---|----|------|------|----------|
| 2 | 拉盘防御 | **暂缓** | A 股涨跌停约束下"急涨 X% 卖 Y%"信号发出日 ≠ 可执行日（涨停下买不进、跌停下卖不出），与"急涨"语义差一天 | 等"开板后 N 分钟内急涨 X%"这种更精准的条件再立项；或实现为确认人闸门外的纯预警（不进 pending） |
| 3 | ladder buy/sell | **拒绝** | (1) 与 T+1 制度结构性冲突（当日买入的不可卖会卡住阶梯第二档）；(2) UI 复杂度 3x（同花顺客户端每笔都要截图）；(3) 5 票 core 池单笔仓位本就限定，阶梯无意义 | 项目扩到 ≥10 只 core + 引入分钟级 paper 撮合；且有人愿意承担 ladder UI 重构 |
| 8 | 动态凯利（强版） | **拒绝** | (1) 样本 20 个交易日，任何凯利输出都是伪精确；(2) 与现有规则 5 8% kill 永远先到（15% 减仓来不及触发）；(3) 审查报告 §8.3 已明示"不要绕过硬规则让 LLM 更灵活"，凯利参数化是同类陷阱 | 样本 ≥6 个月独立观测；规则 5 阈值上调到 12%（决策 ADR 复核） |
| 4b | consensus_compare | **降级** | (1) ak 无独立 consensus 接口；(2) A 股一致预期覆盖率低（≈30%）、披露口径不一（盈利上下限 vs 点估计混用）；(3) 回测必前视、生产必缺数据 | akshare/聚宽出现独立 consensus 接口且覆盖率 ≥70% |
| 6b | 龙虎榜席位评分 | **暂缓** | (1) 2026 监管整治"游资战法"，披露规则酝酿优化；(2) 项目原决策已 [暂缓] | 监管政策明朗 + 席位评分层离线跑通并验证 |

---

## 5. 三个"组合化学反应"（1+1>2 杠杆点）

### A. ①追踪止盈 + ⑦vol_regime + ⑨三态
**互补点**：低波环境追踪止盈收窄到 1.5N（vs 高波 3N），`monitor` 态的票重新评估是否解锁下一档加仓。**直接补 v1.4 reversal_lowvol MDD -41% 的根因**——反转买完没止盈。把"止盈"从硬规则升级为"环境自适应的渐进确认"。

**落地形式**：阶段 1 完成后做联动测试；阶段 2 在 `risk/engine.py` 规则 22 里加 `if regime=='low_vol': trail_mult=1.5 elif regime=='high_vol': trail_mult=3.0`。

### B. ⑤封单量化 + ⑥ETF 旁路 + ⑧软减仓
**互补点**：宏观 ETF 顶部信号（份额突减 ≥2%）→ 触发软减仓（cap×0.7）→ 持仓票若同时封单比 <3% → 规则 21 走应急通道。**形成"宏观 ETF → 中观 cap → 微观单票"三级联动**，单点失效不会全局失控。反过来把审视者担忧的"耦合振荡"转化为"分层熔断"。

**落地形式**：阶段 1 + 阶段 2 完成后做端到端验证；写入 `tests/integration/test_three_tier_guard.py`。

### C. ④业绩预告 edge + ⑨三态 + ⑨ ladder（已拒绝，见 ADR#3）

> **注**：组合 C 原方案是 ladder 联动，但 ADR#3 已拒绝 ladder，故组合 C 降级为——"业绩超预期 + `timing=execute_now` → LLM 直接跳过观望（不再走 monitor 路径）"。这在阶段 2 ⑨b monitor 重提里实现。

---

## 6. 实施节奏与依赖

```
阶段 1（本周可做 · ~4 人日）         阶段 2（下个 sprint · ~5 人日）
─────────────                      ─────────────
⑦vol_regime ─┐                    ④ earnings_compare ─→ bundle 注入
⑤board_open ─┤                    ⑤b seal_strength ──→ 规则 21 强化
①trailing_atr ┴─→ 共享 atr_pct     ⑥ ETF+北向降级 ───→ regime 第 7/8 路
⑧b soft_dd ───→ 联动规则 6          ①b time_stop ──────→ 规则 23
⑨timing (独立)                      ⑨b monitor_requeue → ⑨原版
```

**关键路径**：⑦⑤① → ②（共用指标，本文档已暂缓）。**最大可并行**：⑨ 与 ⑦⑤① 完全解耦。

**启动条件**：v1.4 momentum profile 已跑出 ≥2 周数据；Sprint 3 闭环合拢修复计划已合拢。

**强约束（不变）**：
- 新功能必须复用现有 `RiskContext` / `quote` / `atr_pct` / `confidence` 字段
- 不引新表（除阶段 2 ⑥ETF+北向扩表）
- 不改 confirm 闸门契约
- 不破 PENDING 兜底
- 不破"永不自动成交"红线（runner 总原则）

---

## 7. 与现有规则/策略库的关联

| 借鉴项 | 关联现有规则/策略库条目 | 协同/冲突 |
|--------|------------------------|----------|
| ①追踪止盈 | 规则 16 ATR 自适应止损、§6.2 海龟 ATR | 协同（共用 atr_pct） |
| ⑤封单/开板次数 | 规则 21 跌停封单应急、§4.1 涨停/首板策略 | 协同（升规则 21 颗粒度） |
| ⑦vol_regime | `regime.compute_vol_target`（§6.1）、拥挤降权 `FC_DEGRADED_WEIGHTS`（Sprint 1） | 约束：仅 regime=normal 启用，避免三层耦合 |
| ⑧b 软减仓 | 规则 5 8% kill、规则 6 max_single_weight | 协同：触发软减仓时同步下调规则 6 上限 0.20→0.15 |
| ⑨三态语义 | `decision.action ∈ {buy/sell/hold/watch}`、Sprint 2 业绩预告事件通道 | 协同：bundle 落 `timing` 字段 |
| ⑥ETF+北向降级 | regime 第 6 路 ETF 旁路（Sprint 2 P1-3）、§4.3 北向资金（已 [降级为 LLM 背景]） | 一致：尊重原 [暂缓]/[降级] 决策 |

---

## 8. 验收与监控

### 8.1 阶段 1 硬验收
1. `pytest tests/` 全绿，新增规则在 `risk/engine.py:897` demo 区至少 3 case
2. `signals/backtest.py` 跑 reversal_lowvol 不劣于 v1.4 现状
3. `webapp/api/workflow.py` ⑨ 阶段状态展示新增 `timing` 字段不报错
4. **规则 22/25 必须落 `risk_event` 表**，落库率 100%
5. **confirm 闸门 PENDING 闭环 24h 无泄漏**

### 8.2 阶段 2 硬验收
1. `signals/signal_eval.py` RankIC 月度改善 ≥0.01（v2 profile 测）
2. `risk_event` 表新增 `rule_22/24/25` 三类
3. confirm 闸门 PENDING 闭环持续 0 泄漏

### 8.3 长期观测（无硬截止）
- 预期 MDD 从 -41% 改善至 -25%（§6.1 vol_target 已证）
- 软减仓触发频次预期 < 5%/季度（过频说明规则 5 阈值需上调，单独 ADR）

---

## 9. 文档维护

- 本档案每次 sprint 借鉴相关 ADR 变更需更新 §1 决策记录
- 阶段 1 启动时把排期同步到 `技术方案.md` §8
- 阶段 1 完成后回写实际工时/偏差到 §3
- 任何"暂缓"项复活必须更新 §4 复活条件判定