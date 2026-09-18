"""市场口径唯一权威实现（结构性重构 Phase 2，docs/结构性重构实施方案.md）。

stdlib-only（datetime/decimal/typing）、Python 3.9 语法、零项目依赖——任何潜在
导入方（risk.engine / signals / data / execution）与本模块均无循环导入。

语义红线（收敛自五个分散实现，改动任何一条需先推翻方案 ADR 并给出新事实）：
1. data/audit._limit_pct 的 +0.5pp 是「百分数舍入容差」，不是 ST 容差——audit
   明确不区分 ST（超 ±5% 会被 10% 上限误报，靠报告人工确认，不静默修正）；
2. execution.paper「超板才拒(>)」 vs risk.engine「到板即拒(≥)」——调用点策略
   差异，不进本模块的函数；
3. in_trading_session（秒级，风控规则4，11:30:01 ∉）与 is_trading_time（分钟级，
   行情门控，11:30:30 ∈）两套时段窗并存，当前调用图下不造成成交分歧，合并即行为变更；
4. 量纲归一的失败回退两侧不同：data/fetcher._norm_volume 返回原值（交 audit 兜底）、
   data/audit._norm_volume 返回 None（体检报问题）；audit 比值带判据
   （0.5<shares/implied<2.0）是审计专用策略，留 audit；
5. （已废止，2026-09-19 Sprint4 W-D6④）原红线5「backtest._limit_up_price 保留
   裸乘法（换 Decimal 会微变 tradable 边界）」：新事实（P0-3 证据链3）= 裸乘积
   使约 38% 真涨停收盘漏判为"可买"（prev_close=10.07 主板票：裸乘积 11.077 <
   交易所涨停价 11.08，收盘 11.08 的真涨停被当成可买），单独贡献约 +14pp 年化
   虚高——回测取整边界差异远小于漏判危害，_limit_up_price 已改用本模块
   limit_price（Decimal HALF_UP 交易所口径）。
"""
from datetime import datetime, time as dtime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

SESSION_AM = (dtime(9, 30), dtime(11, 30))
SESSION_PM = (dtime(13, 0), dtime(15, 0))


def limit_pct(code: str) -> float:
    """涨跌停幅度：创业板(30，含302新段)/科创板(68，含689 CDR) 0.20，
    北交所(43/83/87/88/92，含920新段) 0.30，其余主板 0.10。"""
    code = str(code)
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("43", "83", "87", "88", "92")):
        return 0.30
    return 0.10


def limit_price(pc: float, pct: float, up: bool) -> float:
    """停板价：Decimal 四舍五入到分（交易所口径）。

    不用 round()——银行家舍入在 .005 边界会与交易所差 1 分钱。
    """
    q = Decimal(str(pc)) * (Decimal("1") + Decimal(str(pct)) if up
                            else Decimal("1") - Decimal(str(pct)))
    return float(q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def in_trading_session(now: datetime) -> bool:
    """是否处于 A 股连续竞价时段（周一~五 09:30-11:30 / 13:00-15:00，秒级边界含）。

    风控规则4 语义：11:30:01 ∉。与 is_trading_time 的分钟级口径并存（红线3）。
    """
    if now.weekday() >= 5:
        return False
    t = now.time()
    return SESSION_AM[0] <= t <= SESSION_AM[1] or SESSION_PM[0] <= t <= SESSION_PM[1]


def is_trading_time(now: Optional[datetime] = None) -> bool:
    """分钟级行情门控时段（11:30:30 ∈，与秒级 in_trading_session 的差集见红线3）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def volume_unit_is_lots(volume, amount, close) -> bool:
    """量纲判别核心：True=原值已是「手」，False=原值是「股」（应除以100）。

    依据 amount/close 隐含股数与 v / v*100 的距离逐行判定（比按源判定可靠）。
    输入非法或非正时返回 True——调用方薄壳各自定义回退语义（红线4）。
    """
    try:
        v, amt, c = float(volume), float(amount), float(close)
    except (TypeError, ValueError):
        return True
    if v <= 0 or amt <= 0 or c <= 0:
        return True
    implied = amt / c  # ≈ 成交股数
    return abs(v - implied) > abs(v * 100 - implied)
