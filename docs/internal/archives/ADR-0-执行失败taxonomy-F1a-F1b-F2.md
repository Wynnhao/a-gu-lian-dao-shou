# ADR-0 · 执行失败 taxonomy（F1a / F1b / F2）与事件规范

> - **日期**：2026-09-19（施工方案 §0.5 的独立落点，供 grep 与 D6 修订锚定）
> - **上游**：`docs/C-ARC架构加固施工方案-2026-09-19.md`（审核通过版）
> - **约束力**：T3（执行级有限重挂）与 T4（执行失败熔断）实现期若发现本 taxonomy 与代码事实再冲突，**停手先改本文件再继续**（施工方案 §5 D6——审核已抓过一次定义错位，不允许二次）
> - **性质**：纯文档 + 事件命名规范，无代码行为变化

## 1. 三类「执行失败」

代码中「执行失败」实际是三种，处置各不相同：

| 类别 | 发生位置 | decision 终态 | pending | 既有事件 | 现有恢复路径 | 本方案处置 |
|------|---------|--------------|---------|----------|-------------|-----------|
| **F1a** confirm 重跑风控被拒 | runner.py confirm()（violations 落 `risk_check_reconfirm`） | rejected | 已删除 | risk_check_reconfirm | 人工插新决策重提（09-16 #28→#31） | **T3 自动重挂**的触发对象；计入熔断（T4） |
| **F1b** 执行价二次校验被拒 | runner.py confirm() 尾段（`price_recheck`） | approved（保留） | **保留** | price_recheck | 人工 `confirm --price` 重试 | 不自动重挂（pending 仍活，路径已顺）；**不计入熔断**（与 F1a 同根的漂移问题，避免双计） |
| **F2** broker 执行返回 None | runner.py `_execute()`（`execution_failed`） | approved（保留） | **保留** | execution_failed | 人工再 confirm（自动取最新价） | 不自动重挂（pending 仍活）；计入熔断（T4） |

**判定规则**：熔断输入 = F1a ∪ F2，按「根决策链」归并（T4）；重挂输入 = 仅 F1a 中「价格漂移型」（T3 判定式）。F1b 因 pending 未死、`--price` 人工路径存在，两类机制均不覆盖——**这是有意的范围收窄**，防止把仍可确认的单子换成一个新单（制造双 pending 风险）。

## 2. 新增 risk_event 事件规范（T2/T3/T4 落地后生效）

| rule | 触发点 | 落点任务 | 去重 |
|------|--------|---------|------|
| `warn` | propose/confirm 复跑风控产生的 v.warnings（detail=`<code> <原文>`） | T2 | once_today 按 code 前缀（≤1 行/票/日） |
| `failopen_factor_crowding` | 规则20 拥挤度读取失败 fail-open | T2 | once_today 同名前缀 |
| `failopen_regime_cap` | build_context regime/vol cap 计算失败 fail-open | T2 | once_today 同名前缀 |
| `kill_executed` | _do_kill 末尾汇总（卖出笔数/trade_ids/递延笔数），**不做逐笔**（trade 行天然带 decision_id+confirmed_by='kill_switch'，100% 冗余） | T2 | 不去重（每次 kill 一条） |
| `exec_retry` | T3 每次重挂（含根 id/attempt/旧价/新价） | T3 | 不去重（每 attempt 一条） |
| `stop_loss_unfilled` | T3 重挂链耗尽且仍为价格漂移型拒单（仅 sell） | T3 | once_today 按 code 前缀 |
| `exec_retry_skip` | T3 累计漂移超护栏（exec_retry_drift_max）放弃追价 | T3 | once_today 按 code 前缀 |
| `exec_circuit_breaker` | T4 熔断触发（当日首次） | T4 | once_today 同名前缀 |

## 3. 事件 detail 与去重前缀的书写约定（强制）

`flush_events` 的 once_today 去重按 `rule + 当日 + detail LIKE '<prefix>%'` 匹配——
**detail 必须以 once_today_prefix 开头**，否则前缀永远匹配不上、结构性 warning 会涓流刷表
（审核对 T2 的核心修正）。既有一致先例：`factor_crowding_active`（detail=“因子拥挤熔断生效：…”）、
规则21（prefix=code+空格，detail 以 code 开头）。

## 4. 时间口径

去重窗口与事件 ts 一律用**真实系统自然日**（`datetime.now()`，照 signals/limit_halt.py
enforce_stuck_rules 先例）——`--now` 注入的回放时间不得参与去重判定，防回放击穿去重。

## 5. 优先级链声明（T4 落地后回填 CONSTRAINTS §3.3）

```
kill（强平）> kill_liquidation 补清算 > 执行熔断（挡新 propose）> … > 确认人闸门
```
