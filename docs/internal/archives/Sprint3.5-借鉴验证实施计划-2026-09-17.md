# Sprint 3.5 · CloddsBot 借鉴验证实施计划（2026-09-17）

> **目标**：把 `docs/策略库借鉴-cloddsbot.md` 三方调研后采纳的 5 项（含软减仓）从档案落到代码，补 v1.4 reversal_lowvol MDD -41% 的"反转买完没止盈"根因；同时为 Sprint 4（阶段 2 因子扩）留出干净的接入点。
> **基线**：main 工作树（结构性重构全案 + Sprint 1/2/3 全部落地）；v1.4 momentum profile 已切生产。
> **预计总工时**：~1.5 人日（§-1 前置快速回测，先做）+ ~4 人日（§1-§5 实施，不含前置回测）。阶段 2 在 Sprint 4 单独排期。
> **启动条件**：① §-1 前置快速回测通过（用历史数据校准 §1-§5 默认值 + 筛掉无效项） + ② v1.4 momentum profile 跑出 ≥2 周数据（实盘归因清晰，避免与 profile 切换混淆）。
> **执行环境**：项目根 `<REPO_ROOT>`，一律用 `.venv/bin/python3`。
> **关联文档**：`docs/策略库借鉴-cloddsbot.md`（三方调研档案 + ADR）/ `CONSTRAINTS.md`（项目约束章程）/ `技术方案.md` §8（顶层排期）。

---

## §-1 前置快速回测（约 1.5 人日，先做）

> **目标**：借鉴清单里可回测的 4 项（#1 / #5a / #7 / #8b）用历史数据快速验证有效性，校准 §1-§5 任务的默认值；筛掉无效项，避免"先写代码再验证"。
> **脚本**：`signals/backtest_cloddsbot_validate.py`（新建独立脚本，不污染 `signals/backtest.py`）。
> **产物**：`logs/backtest/cloddsbot_validate_YYYY-MM-DD.md`（4 项对比 + v1.4 基线）。
> **依赖**：仅 daily_bar + config.signals.profile（已有），不出网、不写生产 DB。

### §-1.1 测试范围与对比基线

| 子测试 | 借鉴项 | 模拟方式 | 对比 |
|--------|--------|----------|------|
| T1 trailing_atr | #1 | backtest 规则链加 trailing_atr 模拟（cost+1N 触发卖出） | v1.4 reversal_lowvol 基线（无 trailing） |
| T2 board_open_count | #5a | 因子加入 reversal_lowvol 池子（权重 0.15 等权起步） | 仅 reversal_lowvol 基线 |
| T3 vol_regime_weight | #7 | 权重按 vol_regime={low/mid/high} 切换 0.7/1.0/1.3 | 固定权重 1.0 基线 |
| T4 soft_dd_tier | #8b | 模拟 drawdown≤-0.15 时仓位×0.7（不 kill） | v1.4（纯 8% kill，无软减仓） |

### §-1.2 实施步骤

1. **新建 `signals/backtest_cloddsbot_validate.py`**：
   - 函数 `run_baseline()`：复用 `signals/backtest.py::run_backtest` 跑 v1.4 reversal_lowvol，输出基线年化/MDD/Calmar
   - 函数 `run_t1_trailing_atr()`：在规则链加 trailing_atr 钩子（复用 `signals/factors.py::trailing_atr` 草案实现，**仅在 backtest 上下文里走分支，不动 risk/engine.py 生产路径**）
   - 函数 `run_t2_board_open_count()`：把 `board_open_count()` 因子加入 `score_reversal_lowvol_xs`（权重 0.15）
   - 函数 `run_t3_vol_regime_weight()`：在 `_score_cross_section` 加 vol_regime 分支（仅 backtest 上下文）
   - 函数 `run_t4_soft_dd_tier()`：在 portfolio_state 路径加 soft_dd_tier 钩子（drawdown≤-0.15 时把 target_weight 整体 ×0.7，**不调 apply_kill_switch**）
2. **执行顺序**：baseline → T1 → T2 → T3 → T4（每个子测试产物落 `logs/backtest/cloddsbot_validate_<date>_T<n>.json`）
3. **汇总**：脚本 main 汇总 4 项 → 输出 `logs/backtest/cloddsbot_validate_YYYY-MM-DD.md`
4. **v1.4 数字一致性校验**：baseline 必须与 Sprint 3 修复后的 v1.4 一致（年化 +31.81% / MDD -18.21% / Calmar 1.74 量级），否则 backtest 路径污染

### §-1.3 验收口径

| 验收项 | 通过判据 | 不通过动作 |
|--------|----------|-----------|
| T1 trailing_atr | v1.4 reversal_lowvol MDD 改善 ≥3 个百分点（-18.21% → ≤-15%）或 Calmar 提升 ≥0.1 | 若无显著改善：§3 任务默认值 `trailing_atr_mult` 从 2.0 上调到 2.5；若仍无效，**§3 任务优先级降为 P2** |
| T2 board_open_count | 加入池子后 RankIC ≥ 0.02 或年化提升 ≥ 1 个百分点 | RankIC ≈ 0 → **§2 任务从 Sprint 3.5 砍掉**，移到 Sprint 4 阶段 2 重审 |
| T3 vol_regime_weight | 高波动环境下 MDD 改善 ≥ 1 个百分点或 Calmar 提升 ≥ 0.05 | 无改善 → §1 任务 `score_vol_regime.high` 默认值从 1.3 下调到 1.15 |
| T4 soft_dd_tier | 回撤曲线峰值不劣于纯 kill（最大回撤幅度不增 + 回撤后净值恢复更快） | 软减仓反而放大回撤 → §4 任务默认值 `factor` 从 0.7 下调到 0.85，**或暂缓实现** |

### §-1.4 输出物

`logs/backtest/cloddsbot_validate_YYYY-MM-DD.md` 模板：
```markdown
# CloddsBot 借鉴验证快速回测 · YYYY-MM-DD

## 基线（v1.4 reversal_lowvol）
- 年化 / MDD / Calmar / Sharpe

## T1 trailing_atr
- 测试条件 / 结果 / 与基线对比 / 结论（采纳 P1 / 降级 P2 / 砍掉）

## T2 board_open_count
- 测试条件 / 结果 / 与基线对比 / 结论

## T3 vol_regime_weight
- 测试条件 / 结果 / 与基线对比 / 结论

## T4 soft_dd_tier
- 测试条件 / 结果 / 与基线对比 / 结论

## Sprint 3.5 任务调整建议
- §1-§5 默认值更新
- 优先级重排
- 是否砍项
```

### §-1.5 与 §1-§5 的联动

- **通过**：§1-§5 任务的 `cfg` 默认值用回测校准值（如 `trailing_atr_mult=2.5` 而非 2.0）
- **不通过**：相应任务从 Sprint 3.5 移到 Sprint 4 阶段 2 重审
- **§-1 报告作为 Sprint 3.5 启动条件**：未跑过 → 不进入 §1-§5 实施

### §-1.6 沿用约束

- 测试全离线：复用 `signals/backtest.py` 已有 AGSICKLE_DB 隔离模式
- 回测 universe = `watchlist_core` 51 只可交易池（与 Sprint 1/2/3 一致）
- 因子层函数纯计算：仅 daily_bar 入参，不引实时数据
- 报告写库风险：日志目录 `logs/backtest/` 已有 cron 清理策略，新增文件名带日期避免堆积
- 不动 `risk/engine.py` / `signals/signals.py` / `signals/factors.py` 生产路径（T1/T3 在 backtest 上下文里走钩子，**所有分支仅 backtest 模式触发**）

---

## §0 已固化的用户决策（不要再询问，直接执行）

| # | 决策点 | 用户拍板 | 实现要求 |
|---|---|---|---|
| D1 | 借鉴清单采纳范围 | **采纳 5 项**：① 追踪止盈 + 时间退出 / ⑤ 涨停封单+开板次数 / ⑦ 波动率自适应 / ⑧b 软减仓 / ⑨ 三态语义 | 阶段 1 全部落地；阶段 2 在 Sprint 4 单独排期 |
| D2 | 软减仓 vs 规则 5 边界 | **并存分层**：8% 紧急 kill（停机 72h）+ 15% 软减仓（不停机，仓位×0.7） | 软减仓触发时不调用 `apply_kill_switch`，仅调 `record_event` + 仓位调整 |
| D3 | vol_regime 受控条件 | **仅 regime=normal 启用**，shock/off 档禁用 | `compute_vol_target` 输出 `regime ∈ {normal, shock, off}`；vol_regime 权重只在 normal 时生效 |
| D4 | confirm 闸门契约 | **永不变**：新功能全部走 pending → 人工 confirm；不允许 `skip_gate=True` 直写 | 任务 5 timing=monitor 也不允许直写，仅落 `watchlist_requeue` 等下次触发 |
| D5 | 阶段 1 不做项 | ladder 4b 共识_compare 4b 龙虎榜 4b 北向日频 4b 拉盘防御 4b 动态凯利强版 | 见 `CONSTRAINTS.md` §4 C-STR-1~6；任何反悔必须先改 CONSTRAINTS.md |

**沿用约束**（结构性重构 ADR + Sprint 1/2/3 既定，不要破坏）：
- profile 切换永远人工确认（改 `config.json → signals.profile`，mtime 热读）
- 所有新数据源走 `call_ak` 熔断（`data/fetcher.py`）
- regime 合成 `min(caps)` + ETF 升档 min-after override（`risk/regime.py:386+`，Fix-2 落地后）
- 测试全离线：不出网、不写生产 DB、新接口必须支持 `conn` 注入
- risk_event 写库用 `risk.engine.record_event(conn, rule, detail)`，rule 字符串命名与现存风格一致（如 `rule_22_take_profit`）
- Python 3.9 语法：`from __future__ import annotations` + `Optional[X]`，禁 `X | Y`
- AGSICKLE_* env 语义不变（`run_in_background` 测试隔离）

---

## §1 任务 7：波动率自适应入场阈值（0.8d）

**目标**：让 vol_regime=normal 时 vol 因子权重 +30%，自适应收缩低波池；shock/off 档禁用避免与拥挤降权 + vol_target 三层耦合。

### 改动
1. **`signals/signals.py::_score_cross_section`**（行号以实际为准）：
   - 入口加读 `cfg.get("score_vol_regime")`（新增配置段，缺省 `{"low": 0.7, "mid": 1.0, "high": 1.3}`）
   - 从 `RiskContext.regime`（或 `risk/regime.py::compute_regime` 返回值）读当前 regime
   - regime=normal → `weights = tuple(w * scale for w, scale in zip(weights, [scale_low, scale_mid, scale_high]))` 后重归一化
   - regime=shock/off → `weights` 保持不变（**显式禁用**，落 `signals.score_parts.vol_regime_disabled: True`）
   - `signals` JSON 加 `"vol_regime": regime + scale` 字段供审计
2. **`config.json` 新增配置段**：
   ```json
   "score_vol_regime": {"low": 0.7, "mid": 1.0, "high": 1.3}
   ```
   数值含义：低/中/高波动环境下 vol 因子权重的乘子
3. **risk/regime.py 输出加 regime 字符串**（若尚未暴露）：
   - `compute_regime` 返回 dict 增加 `"regime_label": "normal"|"shock"|"off"`（已有 `caps` 计算逻辑可派生）
   - `RiskContext` 增加 `regime_label` 字段（参见 risk/engine.py 现有 RiskContext 定义）

### 测试（`tests/test_signals.py` 新增 ≥3 case）
- regime=normal + vol_regime=high → scale=1.3 生效；score 排序变化
- regime=shock → scale 不生效，weights 不变；`vol_regime_disabled: true` 字段落库
- regime=off → 同 shock
- 缺 `score_vol_regime` 配置段 → 缺省 `low=0.7/mid=1.0/high=1.3`

---

## §2 任务 5a：开板次数量化（0.5d）

**目标**：把涨停"触板→开板→回封"次数量化成因子，与规则 21 封单比互补。

### 改动
1. **`signals/factors.py` 新增 `board_open_count(code, conn, lookback=20) -> int`**：
   - 从 `daily_bar` 取最近 lookback 日的 OHLC
   - 涨停判定：`high == limit_price(code, prev_close)`（复用 `common/market.py::limit_price` 或 `risk/engine.py:112` 的等价实现）
   - 涨停日内开板判定：`(high == limit_price) AND (close < limit_price)`（或 `low < limit_price` + close 仍为涨停）
   - 返回该 lookback 窗内"涨停后未封死"的天数
2. **`signals/limit_halt.py`**（若已存在 / 新建）：
   - 输出 `sealed_count` / `open_count` / `total_zt_days` 三个字段
   - `open_count` 即任务核心输出
3. **`data/breadth.py`**（最小改动版，本任务只做"已有炸板池拉明细 + 写库"）：
   - `breadth_daily` 表新增列 `board_open_count INTEGER DEFAULT 0`（`_MIGRATIONS` ALTER）
   - em 源 `ak.stock_zt_pool_zbgc_em` 拉明细（每天每只票的开板次数）→ 写 `breadth_daily` 按 code 聚合

### 测试（`tests/test_signals.py` + `tests/test_breadth.py` 新增 ≥2 case）
- `board_open_count`：构造已知涨停/炸板日线 → 计数正确（如 5 日内 2 日涨停未封死 → 返回 2）
- breadth 写库：mock 炸板池返回 → `breadth_daily.board_open_count` 写入正确

### 后续 Sprint 4 联动（不在本 Sprint）
- 规则 21 强化：seal_ratio（=ask1_vol / float_mv）≥ 5% + open_count ≥ 2 → 升级 emergency_skip（参见借鉴清单 §3.2 阶段 2 任务 5b）

---

## §3 任务 1：追踪止盈基础版（1.0d）

**目标**：补 v1.4 reversal_lowvol MDD -41% 根因——反转买完没止盈。浮盈 >2N 自动压回成本+1N。

### 改动
1. **`signals/factors.py` 新增 `trailing_atr(close_series, atr_series, mult=2.0)`**：
   - 维护 trailing peak（截至当日最高 close）
   - trailing stop = peak × (1 - mult × atr_pct)
   - 返回 `(trailing_stop: float, current_peak: float, activated: bool)`
   - `activated = True` 当 `peak ≥ cost + 2 × atr`
2. **`risk/engine.py` 新增 `rule_22_take_profit(ctx, cfg) -> (ok, detail)`**：
   - 输入：持仓每只票的 cost / close / trailing_stop / `cfg["risk"]["trailing_atr_mult"]`（缺省 2.0）
   - 触发：`close < trailing_stop AND activated`
   - 动作：写 `record_event(rule="rule_22_take_profit", ...)` + 标 `pending_sell = True`
   - **不直写 trade**，必须经 `runner.propose` 走 confirm 闸门
3. **`config.json` 新增配置段**：
   ```json
   "risk": {
     "trailing_atr_mult": 2.0,
     "trailing_atr_activation_n": 2  // 浮盈超过 N 个 ATR 才激活
   }
   ```
4. **风控优先级链写入 `docs/策略库借鉴-cloddsbot.md` §1 ADR#1 已声明**，本任务代码注释同步：
   ```
   强平 > 止损(规则16) > 追踪止盈(规则22) > 时间退出(规则23) > 止盈 > 时间退出
   ```
   在 `risk/engine.py::evaluate_all_rules` 函数注释中明确该顺序

### 测试（`tests/test_risk_engine.py` 新增 ≥4 case）
- 未激活（浮盈 <2N）→ 不触发
- 已激活 + close 跌破 trailing_stop → 触发 + record_event 落库 + pending_sell=True
- 已激活 + close 未跌破 → 不触发
- 优先级链：止损先于追踪止盈（构造同时满足两个规则的场景 → 规则 16 先返）

---

## §4 任务 8b：软减仓 15% 预警层（1.0d）

**目标**：给规则 5（8% kill）加 15% 预警层（不停机，仓位×0.7），落 risk_event；规则 6 max_single_weight 同步下调 0.20→0.15 防集中度。

### 改动
1. **`risk/regime.py` 新增 `soft_drawdown_tier(portfolio_state, cfg) -> dict`**：
   - 输入：`portfolio_state.drawdown` + `cfg["risk"]["soft_dd"]`
   - 配置缺省 `{"trigger": -0.15, "factor": 0.7}`（`trigger` 为负数）
   - 返回：`{"triggered": bool, "factor": float, "drawdown": float}`
   - 触发条件：`portfolio_state.drawdown <= soft_dd.trigger`
2. **`risk/engine.py` 新增 `rule_25_soft_reduce(ctx, cfg, portfolio_state) -> (ok, detail)`**：
   - 调用 `soft_drawdown_tier`
   - 触发 → `cap_factor = soft_dd.factor`（写 detail）→ 落 `record_event(rule="rule_25_soft_reduce", ...)`
   - **不调用** `apply_kill_switch`（与规则 5 边界）
   - 与规则 6 联动：触发时同步设 `ctx.max_single_weight_override = 0.15`，规则 6 读这个 override
3. **`risk/engine.py::rule_max_single_weight`（规则 6）改造**：
   - 当前读 `cfg["risk"]["max_single_weight"]`
   - 改为先读 `ctx.max_single_weight_override`，有则用 override，否则用 cfg 默认
4. **`config.json` 新增配置段**：
   ```json
   "risk": {
     "soft_dd": {"trigger": -0.15, "factor": 0.7},
     "max_single_weight": 0.20
   }
   ```
5. **`portfolio_state` 表增加 `max_single_weight_override REAL` 列**（`_MIGRATIONS` ALTER，缺省 NULL）

### 测试（`tests/test_risk_engine.py` 新增 ≥4 case）
- drawdown=-0.10（未到 -0.15）→ 不触发
- drawdown=-0.16 → 触发 + factor=0.7 + record_event 落库
- drawdown=-0.20 → 仍触发，但 factor 仍是 0.7（不是越深越压）；**规则 5 8% kill 仍未触**（kill 优先级更高，单独 case）
- 软减仓触发后规则 6：单票上限 0.20 → 0.15（`max_single_weight_override` 生效）
- 软减仓后 drawdown 回升到 -0.10：soft_dd 不自动撤销（**人工确认**才恢复，或下个 sprint 加冷却机制）

### 监控指标（长期观测，写入 `docs/策略库借鉴-cloddsbot.md` §8.3）
- 软减仓触发频次预期 < 5%/季度；过频 → 规则 5 阈值需上调（单独 ADR）

---

## §5 任务 9：入场时机三态语义（0.5d）

**目标**：让 LLM 能输出 `execute_now / wait / monitor` 三态，把"看到机会没下手"的样本留痕（归因/IC 重训练价值高）。

### 改动
1. **`ai/decide.py` schema 加 `timing` 字段**：
   - 校验白名单：`{"execute_now", "wait", "monitor"}`
   - 缺省 `execute_now`（向后兼容已有 decision）
   - validate 失败条件：值不在白名单（与现有 schema 一致：任一字段失败整包放弃）
2. **`execution/runner.py` 三态分发**：
   - `execute_now` → 现有路径（pending → confirm）
   - `wait` → 不进 pending，写 `decision_watchlist` 表（或现有 watchlist 标记），等下一次 premarket/午评触发重新评估
   - `monitor` → 进 pending 但 status=`monitor_pending`，15:00 由 `intraday_check` 重提（**留待 Sprint 4 任务 9b，本 Sprint 只做 schema + 分发骨架**）
3. **`decision` 表增加 `timing TEXT DEFAULT 'execute_now'`** 列（`_MIGRATIONS` ALTER）
4. **`webapp/frontend/src/components/DecisionTable.tsx`** 加列 + 徽章：
   - 三种徽章颜色：`execute_now`=绿、`wait`=黄、`monitor`=蓝
   - 显示在决策表第一列
5. **`ai/bundle.py` 注入**：bundle 不变，decide 输出落库时自动带 `timing` 字段

### 测试（`tests/test_ai_pipeline.py` + `tests/test_execution.py` 新增 ≥3 case）
- decide validate：`timing=execute_now` ✓ / `timing=invalid` ✗（整包放弃）
- runner 分发：timing=wait → decision.status=`wait` + decision_watchlist 落表 + 不进 confirm 列表
- DecisionTable.tsx：3种 timing 渲染对应徽章（前端测试可在 Sprint 4 一并补，本 Sprint 仅校验 TSX 类型）

---

## §6 收尾与验收（0.5d）

1. **全套回归**：`for f in tests/test_*.py; do .venv/bin/python3 $f; done` —— 阶段 1 完成后约 300+ 用例（基线 281+19=300，加 ~25 个新 case）
2. **新规则 demo 场景**（`risk/engine.py:897` demo 区）：
   - 规则 22（追踪止盈）：≥ 2 case（激活/未激活）
   - 规则 25（软减仓）：≥ 3 case（未触发/触发/与规则 6 联动）
3. **risk_event 落库审计**：
   - `SELECT rule, COUNT(*) FROM risk_event WHERE date>=? AND rule IN ('rule_22_take_profit', 'rule_25_soft_reduce') GROUP BY rule`
   - 必须落库率 100%
4. **confirm 闸门 PENDING 闭环**：
   - 阶段 1 引入的 timing=wait 决策必须经下一次 premarket 重评估（不静默堆积）
   - 24h PENDING 泄漏数 = 0（与 Sprint 3 K3 测试隔离一致）
5. **端到端冒烟**：
   - 盘前干跑：premarket 步骤无 FAIL；新增 task 7 vol_regime + task 9 timing 输出在 bundle.md / decision.json 可见
   - backtest：`.venv/bin/python3 signals/backtest.py` 跑 reversal_lowvol 不劣于 v1.4 现状（**注意：阶段 1 改动不破坏 v1.4 数字**）
6. **文档更新**：
   - `docs/优化修复纪要.md` 追加"Sprint 3.5 验收条目"（同 Sprint 1/2/3 格式：改动清单 + 测试计数 + 与 ADR 编号对照）
   - `docs/决策策略与工作流.md` §7 v1.6（新增规则 22/25 + timing 三态说明）
   - `docs/策略库借鉴-cloddsbot.md` §3.1 阶段 1 实际工时回写（vs 计划 ~4 人日）
   - `CONSTRAINTS.md` 不动（L4 策略约束已 v1.0 落地）
7. **汇报格式**：任务 7/5a/1/8b/9 逐条"ADR 引用 → 修复位置 → 测试证据 → 实际工时偏差"

---

## §7 明确划线（本 Sprint 不做）

- ❌ Sprint 4 阶段 2 全量（任务 4 业绩预告 / 5b 封单量化补完 / 6 ETF+北向 / 1b 时间退出 / 9b monitor 重提）—— Sprint 4 单独排期
- ❌ 阶段 1 任一项的"完整版"或"扩展版"（如 7 vol_regime 完整跨 regime 联动 / ⑨ monitor 重提）
- ❌ ladder buy/sell（CONSTRAINTS.md C-STR-1 明确拒绝）
- ❌ 拉盘防御 pump defense（C-STR-2 暂缓，待"开板后急涨"立项）
- ❌ 动态凯利强版（C-STR-3 拒绝）
- ❌ consensus_compare（C-STR-4 降级，Sprint 4 用"预告 vs 上期"代替）
- ❌ 龙虎榜 / 北向日频（C-STR-5/6 暂缓）
- ❌ 切换生产 profile（v1.4 momentum profile 已就位，阶段 1 不动 config.signals.profile）
- ❌ git commit / PR（如用户需要会另行指示）

---

## §8 执行顺序与提交粒度

```
任务 5a 开板次数 (0.5d) ─┐ 独立
任务 7 vol_regime (0.8d)  ─┤ 共享 atr_pct hook，顺序无关
任务 1 trailing_atr (1.0d) ─┘
任务 8b 软减仓 (1.0d) ──── 依赖任务 1 完成（共用 risk_event 落库）
任务 9 三态语义 (0.5d) ──── 独立（schema + UI + runner 分发）
收尾 §6 (0.5d) ──────────── 最后
```

**推荐执行路径**：5a → 7 → 1 → 8b → 9 → 收尾

**提交粒度**（每个任务完成即跑相关测试文件 + 受影响套件回归，全绿再进下一个）：
- 每个任务单独 squash commit（或用户拍板的合并策略）
- commit message 格式：`task(N): <借鉴清单编号> <一句话>`，如 `task(1): 借鉴#1 trailing_atr 追踪止盈基础版`
- 测试失败 → 阻塞下个任务，单独修复 + 重跑

---

## §9 验收对照（与 `docs/策略库借鉴-cloddsbot.md` §8.1 一致）

| 验收项 | 实施位置 | 阶段 1 完成判据 |
|--------|----------|-----------------|
| pytest 全绿 | `tests/` 全套 | 300+ 用例全绿，新增 ~25 case |
| reversal_lowvol backtest 不劣化 | `signals/backtest.py` | 年化/MDD 与 v1.4 一致或更好 |
| ⑨ timing 字段展示 | `webapp/api/workflow.py` | `/api/workflow` 返回含 timing 字段不报错 |
| 规则 22/25 落 risk_event | `risk/engine.py:record_event` | 落库率 100% |
| confirm 闸门 PENDING 闭环 | 24h 观察 | 0 泄漏 |

---

## §10 阶段 2（Sprint 4）前置摘要

> 阶段 1 完成后，**Sprint 4 启动条件** = 阶段 1 全绿 + v1.4 momentum profile 跑满 ≥4 周。
> 阶段 2 任务清单（详见 `docs/策略库借鉴-cloddsbot.md` §3.2）：

| # | 任务 | 工时 | 关联阶段 1 |
|---|------|------|------------|
| 4 | 业绩预告 edge（降级版） | 1.5d | 独立 |
| 5b | 封单量化补完 | 1.0d | 续 §2 任务 5a（breadth_daily 已扩列） |
| 6 | ETF+北向降级 | 1.0d | 独立（regime 第 7/8 路） |
| 1b | 时间退出补完 | 0.5d | 续 §3 任务 1（规则 23） |
| 9b | monitor 重提 | 1.0d | 续 §5 任务 9（runner.wait 分发完整化） |

**总工时 ~5 人日**，单独立项 Sprint 4 实施计划文档时引用本文档 §10。