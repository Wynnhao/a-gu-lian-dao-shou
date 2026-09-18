# Sprint 3 · 多 Agent 重新审查报告（2026-09-17）

> **审查方式**：4 个并行 agent 分工审查——①信号与数据层、②风控与决策层、③管线与控制台 API、④前端 UI；另由主 agent 独立跑全量测试核验基线。
> **审查对象**：main 工作树全部未提交改动（39 文件，+3900/−269），即 Sprint 3 快修三件套（K1-K3）+ Fix-1~6 全部落地内容。
> **测试基线**：`267/267 通过`（3.31s，离线）——**但下列 P0 问题全部位于测试盲区，绿灯不构成放行依据**。
> **前端构建**：`npm run build`（tsc --noEmit + vite build）通过，无类型错误。

## 总判定：不通过（Sprint 3 未达"闭环合拢"验收标准）

四个 agent 结论：①②③均"不通过"，④"有条件通过"。问题高度收敛于 **D1 应急单闭环（Fix-4）**——三条腿（扫描→confirm→兜底执行）**每一条都有断裂点**，叠加扫描器核心条件缺失，实际效果与设计相反：本意"连续跌停死锁解锁"退化成了"任意浮亏破线持仓的自动清算器"。

---

## P0（必须修复后才能合入）

### P0-1 应急扫描缺"当日跌停"条件①：任何浮亏破线持仓都会生成跌停价卖单并被自动执行
- **位置**：`signals/limit_halt.py:104-123` + `risk/engine.py:410-444`（两个 agent 独立复现）
- **证据**：`scan_positions` 拿到 `_limit_halt_condition` 命中后直接生成 `order.price=down` 应急单，全程未比对 `latest_prices[code]` 与跌停价 `down`。共用函数自己注释"由调用方比对 order.price == down"，但扫描器自己生成 order（price=down），比对恒真，条件①形同虚设。三条件实际只剩"浮亏≥止损线"一条。
- **实测**：昨收 100 / 今收 103（+3%，远离跌停）、成本 130（浮亏 20.8%）→ 照样生成 `sell @90.0` 应急单。
- **放大链**：postclose 步骤 3.5 对每只破线持仓生成次日单并通知 → 09:14 未确认由 failsafe 自动 confirm（`emergency_timeout_failsafe`）→ **绕过 LLM 与人工闸门强平所有亏损仓**；且非跌停票被计入 stuck（`signals/limit_halt.py:185-210`），3 只普通亏损票即触发全账户 kill_switch。
- **修法**：`scan_positions` 命中后补条件①（如 `lp = latest_prices.get(code); if lp is None or lp > down * (1 + 0.005): continue`）；补"未跌停但破线不命中"测试。

### P0-2 "睡前 confirm"腿断裂：当晚 confirm 反而把应急单作废，周五单永久搁浅
- **位置**：`execution/runner.py:689-698` × `signals/limit_halt.py:155`（三个 agent 一致确认）
- **证据**：盘后生成单 `run_date=自然日次日`；通知文案引导"sleep 前 confirm 可次日 09:15 执行"（`limit_halt.py:176-177`）。但用户当晚 confirm 时 `run_date != 今天` → status=**expired**、pending 文件删除（实测复现）。次日 failsafe 只查 `status='approved'` → 该单彻底消失，无任何通知。
- **周末问题**：周五盘后 run_date=周六 → 周末无 notify 无兜底 → 周一 failsafe 查 run_date=周一查不到 → 单子以 approved 状态永久滞留（注释宣称"自然顺延"与实现相反）。
- **修法**：`confirm()` 对 `emergency_scan=1` 豁免跨日闸门（run_date 语义=预期执行日）；run_date 改用交易日历取下一交易日；补"当晚 confirm → 次日执行"集成测试。

### P0-3 "09:14 兜底自动执行"无任何调度触发者：正常交易日是死代码，通知却在虚假承诺
- **位置**：`pipeline/premarket.py:93`（全仓库唯一调用点）+ `pipeline/catchup.py:192-199`
- **证据**：`premarket_failsafe` 仅在 premarket 步骤 0.5（09:00 cron）被调，此时 `do_exec = now>=09:14` 恒 False → 只通知。launchd 每 30 分钟只跑 catchup，而 catchup 重跑 premarket 的条件是 bundle 不新鲜或 signal 未对齐——09:00 premarket 成功后两者都不满足 → **09:14 后无人再调 failsafe**。通知文案"将于 09:14 自动执行"（`limit_halt.py:315-317`）无法兑现。
- **修法**：catchup 增加独立分支：交易日 ≥09:14 且存在 run_date=今日 status=approved 的 emergency 单 → 直接调 `premarket_failsafe()`（与 bundle 新鲜度解耦）；或加 09:14 专用 launchd 任务。

### P0-4 `engine.check()` 内部 `get_conn()` 直写生产库：测试运行实测向生产 risk_event 写入 71 条假事件，K3 在 DB 侧被击穿
- **位置**：`risk/engine.py:750-757`（emergency_pending 落库）、`risk/engine.py:652-668`（factor_crowding 同日去重）
- **证据**：`check()` 无 conn 参数，测试跑 `:memory:` propose/confirm 时这两处落到生产 `data/market.db`。实测今日已写入 `limit_halt_emergency` ×70（detail 即测试 seed 文本）+ `factor_crowding_active` ×1。连带风险：`factor_crowding_active` 同日去重会被测试残留压住当日真实事件。
- **附带发现**：每次 propose+confirm 对同一单写两条 `limit_halt_emergency`（无去重）。
- **修法**：落库挪到持有 conn 的调用方，或 RiskContext 加 conn 字段；短期统一让测试套件设 `AGSICKLE_DB` 指向临时库；**清理生产库今日 71 条测试残留**（见文末清理建议）。

### P0-5 业绩预告黑名单连 SELL 一起拦：负面预告股卖不掉，规则 21 在最典型场景被打断
- **位置**：`risk/blacklist.py:64-67` + `risk/engine.py:134-139` + `ai/decide.py:78-82`
- **证据**：`check_blacklist`/`rule_blacklist`/`validate` 三层都不分买卖方向。持仓票出现"预减/首亏"标题（net≤-2）后 3 日内：LLM 止损卖单在 validate 与规则 1 两层被拒；`run_postclose_scan → propose → check()` 中 `rule_blacklist` 先于规则 21 → 应急单 rejected。连续跌停+负面预告恰是规则 21 的典型场景。与自身原则矛盾：`rule_factor_crowding` 注释明确"sell 单不挡（拥挤熔断不该堵止损）"。
- **与 P1-1 叠加**：earnings 同极性嵌套双计使单条负面标题即可 net=-2 触发拦截（见 P1-1）。
- **修法**：对 `action=="sell"`（含 emergency 单）豁免业绩预告类黑名单项（保留 ST 等原有项现状）。

### P0-6 腾讯行情 ask1 字段错位 + float_mv 单位错 1e8 倍：规则 21 条件②（封单比 3%）结构性死代码
- **位置**：`data/quotes.py:109-111`
- **证据**：标准 qt.gtimg 布局下卖一价/量是 f[19]/f[20]，f[21]/f[22] 是**卖二**档（由同函数 price=f[3]/time=f[30] 等既有字段锚定布局）；f[42] 流通市值单位是**亿元**却被按"元"用 → 封单比天文数字永远 ≥3%。涨停封板时卖二为空 → 缺数据视为 True。**连带**：`data/breadth.py:141-144` tx 兜底源用 `ask1_price==up` 数涨停 → 系统性为 0。
- **测试盲区原因**：`tests/test_quotes.py` fixture 用与实现相同索引构造报文（自证循环）。
- **修法**：ask1 改 f[19]/f[20]；float_mv ×1e8；fixture 改用真实报文样本。

### P0-7 涨跌家数接口用错：`stock_zh_a_gdhs` 是"股东户数"不是涨跌家数 → advance_decline_ratio 恒 None
- **位置**：`data/breadth.py:83-91`
- **证据**：akshare 1.18.94 实测该函数返回列 SECURITY_CODE/HOLDER_NUM…，列名匹配"上涨"恒 None，白爬多页股东户数。生产 `breadth_daily` 2026-09-17 行 `advance_decline_ratio=None` 已实证。composite 四因子永远缺一，权重永久重归一到 0.8。正确接口应为 `ak.stock_market_activity_legu`（返回长表需按行取数）。
- **修法**：换接口 + mock 改成真实列结构。

### P0-8 市场宽度挂在盘前 09:00 采集当日：涨停/跌停池开盘前为空 → 写当日假 0 行，盘后无刷新
- **位置**：`pipeline/premarket.py:205-214` + `data/breadth.py:58-62,198`（两个 agent 一致确认）
- **证据**：premarket 6.7 盘前调 `fetch_breadth_daily()`（默认今天）→ em 涨停池当日盘前返回空 → `limit_up_count=0`，且成功判定把 0 当成功 → 当日假 0 行落库。全项目仅 premarket 一处调 breadth，postclose 无补采。regime 读 `ORDER BY date DESC LIMIT 1` 全天读假 0 行；0 行污染 z 历史后 std→0 → composite 恒 None——复审要修的"极端避险档死代码"换个形式复现，且极端档可能被假数据误触。
- **修法**：breadth 采集移到 postclose（收盘后）；`_fetch_breadth` 区分"池为空"与"失败"。

### P0-9 Sprint3 三项新能力在控制台/UI 零展示（验收缺口）
- **位置**：`webapp/server.py:155-178` 路由表 + `webapp/frontend/src/` 全局
- **证据**：后端无任何 breadth/earnings/limit_halt endpoint；前端 grep 仅命中 stories 假数据。市场宽度 z、业绩预告黑名单、stuck 计数、09:14 倒计时在 UI 全部不可见；应急单只能混入既有 pending 列表"隐姓埋名"。若前端确属本 Sprint 划线范围，需在文档显式补记；否则这是验收缺口。
- **修法**：后端补 `/api/breadth`、`/api/limit_halt/stuck`；前端加宽度卡片与应急单专区（emergency_scan 徽章、stuck_days、委托价、超时状态）。

---

## P1（合入前应修）

| # | 问题 | 位置 | 要点 |
|---|---|---|---|
| P1-1 | earnings 同极性关键词嵌套双计 | `signals/earnings.py:31-35` | `"净利润同比下滑"`⊃`"同比下滑"` 等 3 对嵌套，单条标题 neg=2 即击穿 net=±2 硬阈值（实测）。修法：命中长词后屏蔽子串，或每标题每极性计 1 |
| P1-2 | stuck 计数双缺陷 | `signals/limit_halt.py:185-210` | ①无同日去重：catchup 每 30 分钟跑一次，单日膨胀约 14 倍，一天击穿"连续 5 日"阈值；②"未命中即删行"：扫描漏跑一天即清零。修法：`WHERE last_attempt < ?` + 只在确认未跌停时清零 |
| P1-3 | signal 表 profile 未过滤（三处漏改） | `signals/signals.py:566-570`、`webapp/api/market.py:160-164`、`workflow.py:61`、`data_status.py:67` | 生产已回填 v1+v2 双份（53,151+55,254 行），拥挤度 IC 混口径污染规则 20 输入；/api/signals 一票两行混排；workflow/data_status COUNT 翻倍。计划 §6.2 只改了 signal_eval，漏了这些 |
| P1-4 | IVOL 基准尾部对齐未按日期 | `signals/signals.py:264-271` | 个股截 as_of、基准截最新日，错位=前视；停牌票进一步错位。v2 55,254 行历史已带污染，D3 人工切换依据部分失真。修法：基准先截 ≤as_of 再对齐，重跑 v2 backfill |
| P1-5 | backfill 复用"当前"拥挤 state 写全部历史 | `signals/signals.py:334-341` | state=active 期间重跑 backfill → 整条历史用降级权重，IC→拥挤判定自反馈。修法：backfill 强制 `degraded=False` |
| P1-6 | skip_gate 直写路径被实际接通 | `execution/runner.py:586-618` + `risk/engine.py:500-503` | 规则 21 对非 emergency 的 LLM 跌停价卖单置 `skip_gate=True`，runner 新分支直写成交——HEAD 中该 flag 无消费方（闸门必拦），本次使其生效且无配置开关。违背"绝不自动成交"总原则。修法：加 `execution.emergency_direct_exec` 开关（默认 false）或并入 confirm-first 链 |
| P1-7 | webapp health/blacklist 默认 scope 收窄为 watchlist | `webapp/api/common.py:58,96` | 看板与管线闸门口径背离：可出现"看板体检 OK、流水线却只出报告不下单"。修法：并排给全量口径计数或响应标 `scope` |
| P1-8 | GateCard 应急单不显示委托价/股数，risk_notes 不渲染 | `webapp/frontend/src/components/GateCard.tsx:76-144`、`WorkflowPage.tsx:49` | 确认"跌停价卖出"时卡面看不到价格/股数/"09:14 自动执行"警示（在 risk_notes 里但没渲染）；traces 映射写死 `risk_notes: []` |
| P1-9 | "确认执行"无二次确认，单击即真实落单 | `GateCard.tsx:156-162` | 否决反而有 Dialog+理由必填。确认是系统唯一写操作，应急单上线后敞口变大。至少对 emergency_scan 单强制二次确认 |
| P1-10 | stuck 盘中扫描用 daily_bar 昨收，观测不到当日跌停 | `signals/limit_halt.py:256-265`、`pipeline/intraday_check.py:113` | 14:50 扫描实况滞后一天；且 `live_quotes` 恒空使封单比条件在管线里永远"缺数据放行"（召回偏高，配合超时自动执行放大误卖面） |
| P1-11 | 文案过期："15 条硬规则"（现为 21 条） | `TradesGatePage.tsx:74,104`、`webapp/api/workflow.py:98` | 在应急单确认页谎报风控复跑范围 |
| P1-12 | `decision_id` null 时 `Number(null)=0` 可发无效请求 | `GateCard.tsx:159` | null 时应禁用操作按钮 |

## P2（择要）

- `_persist_latest` 注释称 Path.replace 原子覆盖，实现是裸 write_text（`review/signal_eval.py:395-421`，两个 agent 都点名）
- `enforce_stuck_rules(conn)` 缺省 now=None 时 TypeError（当前调用方都传了，latent）
- `signals/breadth.read_breadth` 不校验行新鲜度，数月前 composite 仍可触发极端避险档
- `_count_new_high_low` 回填历史日期时用"今天"口径（生产当日路径无影响）
- `FACTOR_CROWDING_PATH` 死常量零引用，建议删除防绕过隔离
- `/api/decisions` 与 workflow traces 未输出 `emergency_scan` 字段（前端无法区分两类单）
- premarket 日志"净分 ≥ 2"与写库过滤口径不符；`workflow.py:81` 未导入 `Dict`；groups.py `_AUDIT_CACHE` 死代码
- 确认操作人为空默认 `by || "human"`，削弱 confirmed_by 审计归因
- 黑名单全 PASS 折叠时空表体；overview 加载中渲染假 OK；"展开全部 (8)" 括号全半角混用

## 已验证正确项（四 agent 交叉核过）

- **Fix-1 z-score**：权重/方向/重归一化/冷启动（<60→None，60-249 用实际窗口标 `z_window=N`）全部与计划一致；历史严格 `date < 当日` 无前视；手算比对 <1e-9
- **seal_rate 真算**（涨停/(涨停+炸板)，失败→None 不再硬编码 1.0）、**new_high_minus_new_low** 全表扫（elif 互斥、qfq 优先、生产实算 -82 性能可接受）
- **K1 跨极性碰撞**：全表自查为零，回归 case 通过（遗留同极性双计见 P1-1）
- **D2**：权重验算 0.20/0.55 等正确；滞回状态机实测无抖动；5% target_weight 压制保留；且顺手修了 compute_all 落盘时序老 bug（原来从未成功落盘）
- **D3 v2 profile**：三 profile 并存、主键迁移、backfill 双跑互不覆盖、config 未切生产（符合划线）
- **D4**：投票 ≥1→switch、红线 -30% 无条件 hold 且先于 switch、IVOL intercept（polyfit 带截距）公式正确
- **Fix-2**：min-after override 顺序正确（唯一允许上提路径），国债/breadth 仍走 min(caps) 最保守
- **K2 步骤编号** 6.5→6.8 连续无误；**K3 文件侧隔离**实测有效（跑测试后生产 `logs/signal_eval/` 未被动）
- **confirm 安全链未回退**：确认人清洗、Host/Origin 白名单、15:05 TTL、LLM 输出键白名单（无法注入 skip_gate/emergency_scan）、23 条路由无漏挂、fcntl 锁保留、兜底 confirm 保留执行价二次校验，人工 reject 不会被兜底选中
- **前端**：构建零错误、契约逐字段一致、useQuery 竞态守卫、无 XSS 面、暗色 token 合规

## 修复优先级建议

1. **先堵资金风险**：P0-1（扫描器补条件①）→ P0-5（黑名单豁免 sell）→ P1-6（skip_gate 加开关）——三件共同决定"会不会出现非授权自动卖出"
2. **再合 D1 闭环**：P0-2（confirm 豁免跨日）→ P0-3（catchup 加 09:14 分支）→ run_date 改交易日
3. **数据正确性**：P0-6/P0-7/P0-8 + P1-3/P1-4（重跑 v2 backfill 前先修对齐）
4. **卫生**：P0-4（DB 隔离）+ 清理生产库 71 条测试残留 + P1-1
5. **控制台/UI**：P0-9 + P1-8/9/11（若本 Sprint 划线则至少补记文档）

## 附：生产库测试残留清理建议（未执行，需确认）

`data/market.db` 的 `risk_event` 表中今日（2026-09-17 02:45-02:48）由测试写入的假事件，删除前可用以下语句核对：

```sql
SELECT COUNT(*) FROM risk_event
WHERE date(ts)='2026-09-17'
  AND (rule='limit_halt_emergency' AND detail LIKE '%600519%')
   OR (rule='factor_crowding_active' AND detail LIKE '%target_weight 20.0% → 5%');
```

核对无误后按同条件 DELETE。另建议尽快落实 P0-4 修法，否则每次跑测试都会继续污染。
