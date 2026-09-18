"""信号层：输出个股技术信号 JSON 并入库；score 为 0~1 加权合成，支持双 profile。

score profiles（config.signals.profile 切换）：

- `reversal_lowvol`（默认，落地 docs/策略库.md Top1/2.1 原口径）：
  **池内截面分位合成**——①反转 0.40（5 日收益 rank 取反）+ 低波动 0.35
  （ATR 占价比 rank 取反）+ 低换手 0.25（近 20 日平均换手 rank 取反），
  三因子各自在当日池内做 min-max rank 归一后加权，缺因子重归一化，
  全缺/单票截面取中性 0.5。
  依据：策略库 §2——A 股短期反转/低波动/低换手显著有效。2026-09-14 前的
  clip 时序映射版本会饱和（创业板 5 日 ±7.5% 即 clip 到边界），30 只票的
  截面信息完全没用上，已按文档 2.1 改回截面口径；旧 clip 值保留在
  signals JSON（rev_clip/lowvol_clip）作单票绝对水平参考。

- `momentum`（旧版保留，时序口径）：趋势方向 0.35 + 20 日动量 0.40（正向）
  + RSI 健康 0.25（连续衰减）。该 profile 是单票时序打分，不走截面。

权重未经实证校准，review/signal_eval.py 输出 RankIC/五分位/滚动 IC 与
profile 切换判据，样本充足后按证据重校。

因子价格口径：动量/均线用前复权 close_qfq；ATR 用全复权 OHLC
（high_qfq/low_qfq/close_qfq 齐全时；此前混用 raw H/L 与复权 C 会在除权日
产生假 TR 跳变），任一缺失整体回退不复权。
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import logging.handlers
import math
import sqlite3
from datetime import datetime
import time
from typing import Optional

import pandas as pd

from common.config import active_profile, load  # noqa: E402
from data import repo  # noqa: E402
from data.fetcher import get_conn  # noqa: E402
from risk.blacklist import check_blacklist  # noqa: E402
from signals.factors import atr, ivol, ma, max_ret_bali, mom, rsi, turnover_pct  # noqa: E402

log = logging.getLogger("signals")
log.setLevel(logging.INFO)
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("signal.log"))
log.propagate = False  # 只写 signal.log


def _watchlist_codes() -> set:
    """config.watchlist_core 的 6 位码集合（策略会下单的池子；含 N 前缀次新但黑名单会再过滤）。

    优先 watchlist_core（精确的"我会下什么单"集合，2026-09-13 错配修复后默认 51 只核心）；
    兼容旧配置（无 watchlist_core 字段时退回 watchlist）。
    """
    try:
        from common.config import core_codes
        return set(core_codes())
    except Exception:
        return set()


PROFILES = ("reversal_lowvol", "reversal_lowvol_v2", "momentum")
# momentum（旧，时序）：趋势 / 20日动量 / RSI 健康
W_TREND, W_MOM, W_RSI = 0.35, 0.40, 0.25
MOM_SPAN = 0.30          # mom 映射斜率：score = clip(0.5 + mom/MOM_SPAN, 0, 1)
RSI_HEALTH = (30.0, 70.0)
# reversal_lowvol（截面）：反转 / 低波动 / 低换手
W_REV, W_LOWVOL, W_LOTURNOVER = 0.40, 0.35, 0.25
# reversal_lowvol_v2（截面，D3）：五因子 = v1 三因子 + 低 IVOL + 低 MAX(5)
W_REV_V2, W_LOWVOL_V2, W_LOTURNOVER_V2, W_IVOL_V2, W_MAX_V2 = \
    0.30, 0.20, 0.15, 0.20, 0.15
REV_SPAN = 0.15          # 单票绝对参考 clip 斜率：clip(0.5 - mom5/REV_SPAN)
VOL_SPAN = 0.05          # 单票绝对参考 clip 斜率：clip(1 - atr_pct/VOL_SPAN)
TURNOVER_WINDOW = 250
TURN20_WINDOW = 20


def profile() -> str:
    """当前 score profile（config.signals.profile，缺省 reversal_lowvol）。

    测试夹具：环境变量 AGSICKLE_SIGNALS_PROFILE 可临时强制返回指定 profile，
    跨进程结束后不影响 config 真实配置——仅用于单测中"调用指定 profile 评分"。
    """
    import os
    force = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    if force:
        return force
    try:
        p = load().get("signals", {}).get("profile", "reversal_lowvol")
        return p if p in PROFILES else "reversal_lowvol"
    except Exception:  # noqa: BLE001
        return "reversal_lowvol"


def _f(x) -> Optional[float]:
    """转 JSON 安全 float：None/NaN → None，否则 float。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _factor_close(bars: pd.DataFrame) -> pd.Series:
    """因子用收盘价：前复权 close_qfq 优先（除权除息日不产生假暴跌），缺失回退不复权。"""
    if "close_qfq" in bars.columns:
        qfq = pd.to_numeric(bars["close_qfq"], errors="coerce")
        if qfq.notna().any():
            return qfq.fillna(pd.to_numeric(bars["close"], errors="coerce"))
    return pd.to_numeric(bars["close"], errors="coerce")


def _factor_ohlc(bars: pd.DataFrame) -> tuple:
    """ATR 用 OHLC：high/low/close 前复权**完整覆盖**时用全复权口径（除权日无假 TR），
    任一行缺失则整体回退不复权——qfq 部分缺失时丢行续算会产生不连续序列的假 TR，
    比整体 raw 更糟。返回 (h, l, c, basis)，三列已 dropna 对齐。"""
    if all(col in bars.columns for col in ("high_qfq", "low_qfq", "close_qfq")):
        h = pd.to_numeric(bars["high_qfq"], errors="coerce")
        l = pd.to_numeric(bars["low_qfq"], errors="coerce")
        c = _factor_close(bars)
        if h.notna().all() and l.notna().all() and c.notna().all():
            df3 = pd.concat([h, l, c], axis=1).dropna()
            if len(df3) > 15:
                return (df3.iloc[:, 0].reset_index(drop=True),
                        df3.iloc[:, 1].reset_index(drop=True),
                        df3.iloc[:, 2].reset_index(drop=True), "qfq")
    h = pd.to_numeric(bars["high"], errors="coerce")
    l = pd.to_numeric(bars["low"], errors="coerce")
    c = _factor_close(bars)
    df3 = pd.concat([h, l, c], axis=1).dropna()
    return (df3.iloc[:, 0].reset_index(drop=True),
            df3.iloc[:, 1].reset_index(drop=True),
            df3.iloc[:, 2].reset_index(drop=True), "raw")


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def _rank01(s: pd.Series) -> pd.Series:
    """min-max rank 归一 0~1（截面打分基元）：最小→0，最大→1，并列取平均秩；
    截面仅 1 个有效值时按中性 0.5 处理（单元素截面无排序信息）。"""
    s = s.dropna()
    n = len(s)
    if n == 0:
        return pd.Series(dtype=float)
    if n == 1:
        return pd.Series([0.5], index=s.index)
    return (s.rank(method="average") - 1.0) / (n - 1.0)


def score_reversal_lowvol_xs(m5: pd.Series, atrp: pd.Series, turn20: pd.Series,
                             weights=(W_REV, W_LOWVOL, W_LOTURNOVER)) -> tuple:
    """reversal_lowvol 截面打分（生产 compute_all 与 backtest 共用的唯一口径）。

    - 反转：5 日收益 rank 取反（跌得深分高）；低波：ATR 占价比 rank 取反；
      低换手：近 20 日均换手 rank 取反——三者各自做当日池内 min-max rank 后加权；
    - 某票缺某因子 → 该因子权重剔除重归一化；全缺 → 0.5；
    - 返回 (score Series, parts DataFrame)；parts 列 rev/vol/turn 为各因子 rank。
    """
    w_rev, w_vol, w_turn = weights
    parts = pd.DataFrame({
        "rev": _rank01(-m5.astype(float)),
        "vol": _rank01(-atrp.astype(float)),
        "turn": _rank01(-turn20.astype(float)),
    })
    w = pd.DataFrame({"rev": w_rev, "vol": w_vol, "turn": w_turn},
                     index=parts.index)
    valid = parts.notna()
    num = (parts.fillna(0.0) * w.where(valid, 0.0)).sum(axis=1)
    den = w.where(valid, 0.0).sum(axis=1)
    score = (num / den.where(den > 0.0, 1.0)).where(den > 0.0, 0.5)
    return score.clip(0.0, 1.0), parts


def score_reversal_lowvol_v2_xs(m5: pd.Series, atrp: pd.Series, turn20: pd.Series,
                                ivol20: pd.Series, max5: pd.Series) -> tuple:
    """reversal_lowvol_v2 五因子截面打分（D3；生产 compute_all 与 backtest 共用）。

    - v1 三因子 + 低 IVOL（残差波动 rank 取反）+ 低 MAX（Bali 最大日收益 rank 取反，
      彩票偏好溢价：MAX 与未来收益负相关）——五因子各 _rank01 后加权
      (0.30, 0.20, 0.15, 0.20, 0.15)；
    - 某票缺某因子 → 该因子权重剔除重归一化；全缺 → 0.5（完全复刻 v1 模式）；
    - 返回 (score Series, parts DataFrame)；parts 列 rev/vol/turn/ivol/maxr。
    """
    parts = pd.DataFrame({
        "rev": _rank01(-m5.astype(float)),
        "vol": _rank01(-atrp.astype(float)),
        "turn": _rank01(-turn20.astype(float)),
        "ivol": _rank01(-ivol20.astype(float)),
        "maxr": _rank01(-max5.astype(float)),
    })
    w = pd.DataFrame({"rev": W_REV_V2, "vol": W_LOWVOL_V2, "turn": W_LOTURNOVER_V2,
                      "ivol": W_IVOL_V2, "maxr": W_MAX_V2}, index=parts.index)
    valid = parts.notna()
    num = (parts.fillna(0.0) * w.where(valid, 0.0)).sum(axis=1)
    den = w.where(valid, 0.0).sum(axis=1)
    score = (num / den.where(den > 0.0, 1.0)).where(den > 0.0, 0.5)
    return score.clip(0.0, 1.0), parts


def _score_momentum(trend, m, r) -> float:
    """旧三因子（时序）：趋势 / 20日动量正向 / RSI 连续健康度。"""
    num, den = 0.0, 0.0
    if trend is not None:
        num += W_TREND * {"up": 1.0, "flat": 0.5, "down": 0.0}[trend]
        den += W_TREND
    if m is not None:
        num += W_MOM * _clip01(0.5 + m / MOM_SPAN)
        den += W_MOM
    if r is not None:
        num += W_RSI * (1.0 - abs(r - 50.0) / 50.0)  # 连续衰减，替代 30~70 二值
        den += W_RSI
    return num / den if den > 0 else 0.5


def _factor_from_bars(bars: pd.DataFrame, code: str,
                      as_of: Optional[str]) -> Optional[dict]:
    """单票因子计算（不含截面 score）。bars 为该票全部日线（升序无所谓，内部排序）。
    截至 as_of 无任何数据返回 None。"""
    if as_of is not None:
        bars = bars[bars["trade_date"] <= as_of]
    bars = bars.sort_values("trade_date")
    if bars.empty:
        return None

    close = _factor_close(bars).reset_index(drop=True)
    raw_close = pd.to_numeric(bars["close"], errors="coerce").reset_index(drop=True)
    to = pd.to_numeric(bars["turnover"], errors="coerce").reset_index(drop=True)
    h, l, c, atr_basis = _factor_ohlc(bars)

    ma5, ma20, ma60 = ma(close, 5), ma(close, 20), ma(close, 60)
    if ma5 is not None and ma20 is not None and ma60 is not None:
        trend = "up" if (ma5 > ma20 > ma60) else "down" if (ma5 < ma20 < ma60) else "flat"
    else:
        trend = "flat"  # MA 不全时无法判向，输出 schema 要求恒为字符串
    r = rsi(close, 14)
    a = atr(h, l, c, 14)
    m = mom(close, 20)
    m5 = mom(close, 5)
    tp = turnover_pct(to, TURNOVER_WINDOW)
    last = bars.iloc[-1]
    last_close = _f(raw_close.iloc[-1]) or 0.0
    last_factor_close = _f(c.iloc[-1]) if len(c) else None
    # atr_pct 与 atr 同口径（复权 atr 除以复权收盘；混口径会在除权票上失真）
    denom = last_factor_close if (atr_basis == "qfq" and last_factor_close) else last_close
    atr_pct = (a / denom) if (a is not None and denom and denom > 0) else None
    turn20 = _f(to.iloc[-TURN20_WINDOW:].mean()) if to.notna().any() else None

    signals = {
        "ma_trend": trend,
        "ma5": _f(ma5), "ma20": _f(ma20), "ma60": _f(ma60),
        "rsi_14": _f(r), "atr_14": _f(a), "atr_pct": _f(atr_pct),
        "mom_20d": _f(m), "mom_5d": _f(m5), "turnover_pct": _f(tp),
        "turn20": turn20,
        "above_ma60": bool(_f(ma60) is not None and last_close > _f(ma60)),
        "close": _f(last_close), "pct_chg": _f(last["pct_chg"]),
        "adj_close": _f(close.iloc[-1]),
    }
    qfq_active = "close_qfq" in bars.columns and pd.to_numeric(
        bars["close_qfq"], errors="coerce").notna().any()
    signals["price_basis"] = "qfq" if qfq_active else "raw"
    signals["atr_basis"] = atr_basis

    # 单票绝对水平参考（旧 clip 口径，仅注解不再参与截面打分）
    if m5 is not None:
        signals["rev_clip"] = round(_clip01(0.5 - m5 / REV_SPAN), 4)
    if atr_pct is not None:
        signals["lowvol_clip"] = round(_clip01(1.0 - atr_pct / VOL_SPAN), 4)

    f = {"mom_5d": _f(m5), "atr_pct": _f(atr_pct), "turn20": turn20}

    # ---- Sprint 2 附任务（P1-2）：IVOL + MAX(5) 残差类因子 ----
    # MAX(5)：个股自身 top-5 日收益均值，**不依赖基准**，独立计算（此前误 gated 在
    # bench_close 分支内，HS300 缺失时被连带置 None；max_ret_bali 对短序列安全返回 None）。
    signals["max_ret_5_20d"] = _f(max_ret_bali(close.values, 20, 5))
    # IVOL：需 HS300 基准（index_daily '000300'）做 CAPM 残差；基准缺失 → None（不阻断）
    try:
        bench_close = _hs300_close_series(conn_hint=None)
    except Exception:
        bench_close = None
    if bench_close is not None and len(close) >= 2:
        # 尾部对齐：按交易日交集对齐（简单取长度对齐——compute 侧全表同源 daily_bar，
        # 日期基本同步；精确对齐留给 v2 profile 接入时做）
        n = min(len(close), len(bench_close))
        signals["ivol_20d"] = _f(ivol(close.iloc[-n:].values,
                                      bench_close.iloc[-n:].values, 20))
    else:
        signals["ivol_20d"] = None

    if profile() not in ("reversal_lowvol", "reversal_lowvol_v2"):
        score = _score_momentum(trend if ma5 is not None else None, m, r)
    else:
        score = None  # 截面打分由调用方统一合成（v1/v2）
    return {"code": str(code), "as_of": str(last["trade_date"]),
            "signals": signals, "factors": f, "score": score}


def _hs300_close_series(conn_hint=None) -> Optional[pd.Series]:
    """读 index_daily('000300') 的 close 升序 Series（P1-2 回归基准）。

    conn_hint 未用（保留签名）；每次调用独立取 get_conn()——compute_all
    场景下 daily_bar 已全表入内存，指数一行只有 2113 行，开销可忽略。
    缺数据/缺表返回 None。
    """
    try:
        from data.fetcher import get_conn
        c = get_conn()
        try:
            rows = c.execute(
                "SELECT trade_date, close FROM index_daily"
                " WHERE index_code='000300' ORDER BY trade_date").fetchall()
        finally:
            c.close()
        if not rows:
            return None
        dates, closes = zip(*rows)
        return pd.Series([_f(x) for x in closes],
                         index=pd.Index(dates, name="trade_date"), dtype=float)
    except Exception:
        return None


def _score_cross_section(rows: list, prof: Optional[str] = None) -> None:
    """对 _factor_from_bars 产出的行列表做当日截面打分（原地写 score/score_parts）。

    prof 缺省取 config.signals.profile（backfill 回填 v2 时显式传入，勿动 config）。
    Fix-3（D2）：因子拥挤 state == "active" 时 v1 打分权重降级为
    FC_DEGRADED_WEIGHTS=(0.20,0.20,0.15)（重归一化后改选股排序），
    score_parts.crowding_downgraded=true 供审计；cooling/off 用正常权重。
    Fix-5（D3）：profile=reversal_lowvol_v2 走五因子打分（ivols/maxs 取自
    signals JSON 的 ivol_20d / max_ret_5_20d；v2 拥挤降权表未定义，恒 False）。
    """
    prof = prof or profile()
    m5s = pd.Series({r["code"]: r["factors"]["mom_5d"] for r in rows}, dtype=float)
    vs = pd.Series({r["code"]: r["factors"]["atr_pct"] for r in rows}, dtype=float)
    ts = pd.Series({r["code"]: r["factors"]["turn20"] for r in rows}, dtype=float)
    is_v2 = prof == "reversal_lowvol_v2"
    if is_v2:
        ivols = pd.Series({r["code"]: (r["signals"] or {}).get("ivol_20d")
                           for r in rows}, dtype=float)
        maxs = pd.Series({r["code"]: (r["signals"] or {}).get("max_ret_5_20d")
                          for r in rows}, dtype=float)
        scores, parts = score_reversal_lowvol_v2_xs(m5s, vs, ts, ivols, maxs)
        degraded = False  # v2 拥挤降权表未定义（Sprint 4）
        part_names = {"rev": "rev_rank", "vol": "lowvol_rank",
                      "turn": "lowturn_rank", "ivol": "ivol_rank",
                      "maxr": "lowmax_rank"}
    else:
        try:
            degraded = read_factor_crowding().get("state") == "active"
        except Exception:  # noqa: BLE001
            degraded = False
        weights = FC_DEGRADED_WEIGHTS if degraded else (W_REV, W_LOWVOL,
                                                        W_LOTURNOVER)
        scores, parts = score_reversal_lowvol_xs(m5s, vs, ts, weights=weights)
        part_names = {"rev": "rev_rank", "vol": "lowvol_rank",
                      "turn": "lowturn_rank"}
    for r in rows:
        r["score"] = round(float(scores[r["code"]]), 4)
        sp = {name: _f(parts.loc[r["code"], key])
              for key, name in part_names.items()}
        sp["crowding_downgraded"] = degraded
        r["signals"]["score_parts"] = sp


def _pool_by_code(pool: pd.DataFrame) -> dict:
    """daily_bar 全表按 code 预切分（回填 653 日 × 30 票时避免重复布尔扫描）。"""
    return {str(c): g for c, g in pool.groupby("code")}


def compute_signal(code: str, as_of: Optional[str] = None,
                   conn=None, pool: Optional[pd.DataFrame] = None) -> dict:
    """计算单票信号。截面打分需要全池上下文：pool 为 daily_bar 全表 DataFrame
    （缺省现读），对池内全部票算完截面后返回 code 那一份。"""
    own = conn is None and pool is None
    if pool is None:
        c = conn or get_conn()
        pool = pd.read_sql("SELECT * FROM daily_bar", c)
        if own:
            c.close()
    by_code = _pool_by_code(pool)
    if str(code) not in by_code:
        raise ValueError(f"{code} 无行情数据")
    rows = []
    for cd, g in by_code.items():
        r = _factor_from_bars(g, cd, as_of)
        if r is not None:
            rows.append(r)
    if profile() != "momentum":
        _score_cross_section(rows)
    for r in rows:
        if r["code"] == str(code):
            return {"code": r["code"], "signals": r["signals"],
                    "score": r["score"] if r["score"] is not None else 0.5,
                    "as_of": r["as_of"]}
    raise ValueError(f"{code} 在 as_of={as_of} 前无行情数据")


def _rows_as_of(by_code: dict, codes: list, as_of: Optional[str],
                 bl: dict) -> list:
    """对给定 codes 计算截至 as_of 的因子行（黑名单过滤，与 compute_all 同口径）。"""
    rows = []
    for code in codes:
        ok, reason = bl.get(code, (True, "-"))
        if not ok:
            log.info("skip %s 黑名单: %s", code, reason)
            continue
        g = by_code.get(code)
        if g is None:
            continue
        try:
            r = _factor_from_bars(g, code, as_of)
        except ValueError as e:
            log.warning("skip %s: %s", code, e)
            continue
        if r is not None:
            rows.append(r)
    return rows


def compute_all(as_of: Optional[str] = None, conn=None,
                  watchlist_only: bool = True, profile: Optional[str] = None) -> list:
    """全池信号：读一次表，黑名单过滤后逐票计算、当日截面打分并 INSERT OR REPLACE 入 signal 表。

    watchlist_only=True（默认）：仅对 config.watchlist 内的票出 score（这是 AI 决策会下
    单的真实池子，与 backtest/回测 universe 保持一致）。设 False 退化为"对 stock_info
    全表出 score"，保留老行为。

    profile（None 默认）：用 config.signals.profile 当前所选。显式传"reversal_lowvol"
    等可针对特定 profile 入库不丢——2026-09-18 v1.4 跟踪期需要每日刷两个 profile 的当日
    score 供观察期评测可比。
    """
    own = conn is None
    c = conn or get_conn()
    t0 = time.time()
    prof_name = profile or active_profile()
    pool = pd.read_sql("SELECT * FROM daily_bar", c)
    all_c = sorted(repo.all_codes(c))
    bl = check_blacklist(c)
    if watchlist_only:
        wl = _watchlist_codes()
        codes = [c for c in all_c if c in wl]
    else:
        codes = all_c
    by_code = _pool_by_code(pool)

    rows = _rows_as_of(by_code, codes, as_of, bl)
    if prof_name != "momentum":
        _score_cross_section(rows)

    results = []
    for r in rows:
        c.execute("INSERT OR REPLACE INTO signal (code, as_of, signals, score,"
                  " profile) VALUES (?,?,?,?,?)",
                  (r["code"], r["as_of"],
                   json.dumps(r["signals"], ensure_ascii=False),
                   r["score"] if r["score"] is not None else 0.5,
                   prof_name))
        results.append({"code": r["code"], "signals": r["signals"],
                        "score": r["score"], "as_of": r["as_of"]})
    c.commit()
    # Sprint 1 任务 5：因子拥挤度熔断（闭熔断已有 rolling_ic 信号，闭环）
    # Fix-3 顺手修：必须在 close 之前跑——此前 conn=None（生产）路径先 close 再
    # 调用，crowding 落盘在 compute_all 里从未成功过。
    try:
        _write_factor_crowding(c)
    except Exception as e:
        log.warning("因子拥挤熔断落盘失败（不阻断主流程）: %s", repr(e))
    if own:
        c.close()
    log.info("compute_all %d 票（profile=%s），耗时 %.2fs",
             len(results), prof_name, time.time() - t0)
    return results


# ============================================================
# Sprint 1 任务 5：因子拥挤度熔断
# ============================================================

def _factor_crowding_path():
    """factor_crowding.json 落盘路径：优先环境变量 AGSICKLE_SIGNAL_EVAL_DIR
    （测试隔离，K3），缺省生产路径。"""
    import os
    env = os.environ.get("AGSICKLE_SIGNAL_EVAL_DIR")
    base_dir = Path(env) if env else BASE / "logs" / "signal_eval"
    return base_dir / "factor_crowding.json"


FACTOR_CROWDING_PATH = BASE / "logs" / "signal_eval" / "factor_crowding.json"  # 兼容旧引用（生产缺省）
# 阈值：审查报告 P0-2 默认值（近 12 个月 bucket IC 均值与标准差）
FC_MU_LOW = 0.005       # μ60 < 0.005 → 信号弱
FC_SIGMA_HIGH = 0.02    # σ60 > 0.02 → 信号不稳
FC_BUCKET_WINDOW = 12   # 近 12 个月 bucket
FC_MIN_BUCKETS = 4      # 至少 4 个 bucket 才算可信
# Fix-3 滞回状态机：退出需 μ 连续多次恢复（月度 bucket 更新慢，用调用计数而非自然日）
FC_MU_RECOVER = 0.015   # μ60 ≥ 0.015 视为恢复（与触发阈值 0.005 之间构成滞回带）
FC_COOLING_EXIT_CALLS = 5   # cooling 计满 5 次 compute_all → off
# Fix-3 权重降级：拥挤期打分权重（按 score_reversal_lowvol_xs 现有逻辑重归一化）
FC_DEGRADED_WEIGHTS = (0.20, 0.20, 0.15)


def _crowding_next_state(prev: dict, mu: Optional[float], sigma: Optional[float],
                         today: str) -> dict:
    """Fix-3 滞回状态机（纯函数）。

    - off → active：μ<FC_MU_LOW 且 σ>FC_SIGMA_HIGH（写 active_since）；
    - active → cooling：μ≥FC_MU_RECOVER（cooling_count=1）；
    - cooling → active：μ 再跌破 FC_MU_LOW（cooling_count 清零，保留 active_since）；
    - cooling 计满 FC_COOLING_EXIT_CALLS 次 → off；
    - 滞回带 [FC_MU_LOW, FC_MU_RECOVER) 内：active 不翻转，cooling 计数不动。
    返回 {"state": ..., "active_since": ..., "cooling_count": ...}。
    """
    state = prev.get("state") or "off"
    cooling = int(prev.get("cooling_count") or 0)
    active_since = prev.get("active_since")
    hit = (mu is not None and sigma is not None
           and mu < FC_MU_LOW and sigma > FC_SIGMA_HIGH)
    recover = mu is not None and mu >= FC_MU_RECOVER
    if state == "active":
        if recover:
            return {"state": "cooling", "active_since": active_since,
                    "cooling_count": 1}
        return {"state": "active",
                "active_since": active_since or today, "cooling_count": 0}
    if state == "cooling":
        if hit:
            return {"state": "active", "active_since": active_since,
                    "cooling_count": 0}
        if recover:
            cooling += 1
            if cooling >= FC_COOLING_EXIT_CALLS:
                return {"state": "off", "active_since": None, "cooling_count": 0}
            return {"state": "cooling", "active_since": active_since,
                    "cooling_count": cooling}
        return {"state": "cooling", "active_since": active_since,
                "cooling_count": cooling}
    # off
    if hit:
        return {"state": "active", "active_since": today, "cooling_count": 0}
    return {"state": "off", "active_since": None, "cooling_count": 0}


def _record_crowding_state_event(conn: sqlite3.Connection, from_state: str,
                                 to_state: str, mu: Optional[float]) -> None:
    """state 迁移写 risk_event（rule='factor_crowding_state'，同日同迁移去重）。
    失败仅 warning（不阻断信号计算）。"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        n = conn.execute(
            "SELECT COUNT(*) FROM risk_event WHERE rule='factor_crowding_state'"
            " AND ts LIKE ? AND detail LIKE ?",
            (today + "%", f"%{from_state}→{to_state}%")).fetchone()[0]
        if n > 0:
            return
        from risk.engine import record_event
        record_event(conn, "factor_crowding_state",
                     f"因子拥挤 state 迁移 {from_state}→{to_state}"
                     f"（mu60={mu}）")
        log.info("因子拥挤 state 迁移留痕: %s→%s", from_state, to_state)
    except Exception as e:  # noqa: BLE001
        log.warning("factor_crowding_state 写 risk_event 失败（不阻断）: %s",
                    repr(e))


def _write_factor_crowding(conn: sqlite3.Connection) -> dict:
    """从 signal 表 + daily_bar 算 score 近 12 个月滚动 IC，维护 factor_crowding.json。

    Fix-3 滞回状态机：json 维护 state（active/cooling/off）+ active_since +
    cooling_count；crowded = state in ("active","cooling")（规则 20 兼容字段）。
    计算不可用（样本不足等）时保留旧 state——熔断不因一次计算失败而意外解除。

    风控规则 20 (rule_factor_crowding) 在 buy 端读 crowded 字段，目标权重 > 5%
    自动压回；权重降级（_score_cross_section）只在 state == "active" 时启用。

    失败：抛异常由 compute_all 捕获并 warning，不阻断信号计算。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    prev = read_factor_crowding()
    prev_state = prev.get("state") or "off"
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "mu60": prev.get("mu60"), "sigma60": prev.get("sigma60"),
           "n_buckets": 0,
           "state": prev_state,
           "active_since": prev.get("active_since"),
           "cooling_count": int(prev.get("cooling_count") or 0),
           "crowded": prev_state in ("active", "cooling"),
           "reason": ""}

    def _finish(state_change: bool = False, from_state: str = None,
                mu: Optional[float] = None) -> None:
        out["crowded"] = out["state"] in ("active", "cooling")
        if state_change and from_state is not None and conn is not None:
            _record_crowding_state_event(conn, from_state, out["state"], mu)
        _persist_factor_crowding(out)

    try:
        rows = conn.execute(
            "SELECT code, as_of, score FROM signal WHERE score IS NOT NULL"
        ).fetchall()
        if not rows:
            out["reason"] = "signal 表为空（保留旧 state）"
            _finish()
            return out
        sig = pd.DataFrame(rows, columns=["code", "as_of", "score"])
        # 后 5 日收益（前复权 close_qfq 优先，回退 close）
        # 必须用全 daily_bar 算 fwd5（group 内需要至少 6 行），再 merge 到 signal 行
        bars = pd.read_sql(
            "SELECT code, trade_date, close, close_qfq FROM daily_bar", conn)
        if bars.empty:
            out["reason"] = "daily_bar 为空（保留旧 state）"
            _finish()
            return out
        c = bars["close_qfq"].fillna(bars["close"]).astype(float)
        bars["fwd5"] = c.groupby(bars["code"]).pct_change(5).shift(-5)
        # 合并：signal.as_of = daily_bar.trade_date，按 (code, date) inner join
        m = sig.merge(bars[["code", "trade_date", "fwd5"]],
                      left_on=["code", "as_of"], right_on=["code", "trade_date"],
                      how="inner").dropna(subset=["score", "fwd5"])
        if m.empty:
            out["reason"] = "signal 与 daily_bar 无交集（保留旧 state）"
            _finish()
            return out
        m["bucket"] = m["as_of"].astype(str).str[:7]
        ics = []
        for b, g in m.groupby("bucket"):
            if len(g) < 5:  # 桶内样本太少跳过（自选池 ≥30 票/桶，生产环境远高于此）
                continue
            ic = g["score"].rank(pct=True).corr(g["fwd5"].rank(pct=True))
            if pd.notna(ic):
                ics.append({"bucket": str(b), "ic": float(ic), "n": int(len(g))})
        if len(ics) < FC_MIN_BUCKETS:
            out["reason"] = f"bucket 不足（{len(ics)} < {FC_MIN_BUCKETS}，保留旧 state）"
            _finish()
            return out
        # 取最近 FC_BUCKET_WINDOW 个 bucket
        recent = ics[-FC_BUCKET_WINDOW:]
        vals = pd.Series([b["ic"] for b in recent])
        mu = float(vals.mean())
        sigma = float(vals.std(ddof=1)) if len(vals) > 1 else 0.0
        out["mu60"] = round(mu, 4)
        out["sigma60"] = round(sigma, 4)
        out["n_buckets"] = len(recent)
        nxt = _crowding_next_state(prev, mu, sigma, today)
        from_state = out["state"]
        out.update(nxt)
        out["reason"] = (f"state={out['state']}：μ={mu:.4f}, σ={sigma:.4f}"
                         + ("（拥挤降权启用）" if out["state"] == "active" else ""))
        _finish(state_change=(from_state != out["state"]),
                from_state=from_state, mu=out["mu60"])
        return out
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"计算失败：{type(e).__name__}: {e}（保留旧 state）"
        _finish()
        return out


def _persist_factor_crowding(payload: dict) -> None:
    """写 factor_crowding.json（覆盖式）。失败仅 warning。"""
    try:
        path = _factor_crowding_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("factor_crowding 落盘失败（不阻断）: %s", repr(e))


def read_factor_crowding() -> dict:
    """风控规则 20 与 bundle 读取入口：永远返回 dict，缺文件/解析失败 → crowded=False。"""
    try:
        path = _factor_crowding_path()
        if not path.exists():
            return {"crowded": False, "reason": "factor_crowding.json 不存在（首次运行）"}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"crowded": False, "reason": f"读取失败：{type(e).__name__}: {e}"}


def backfill_history(conn=None, start: Optional[str] = None,
                     end: Optional[str] = None, batch_commit: int = 30,
                     prof: Optional[str] = None) -> int:
    """signal 全史回填：对 daily_bar 全部交易日逐日按指定 profile 口径重算并入库。

    - 与 compute_all 完全同口径（当日截面打分、黑名单、复权价优先）——
      signal 表由此成为"若当天跑当前代码会算出什么"的可评估时序，
      signal_eval 的 RankIC/分层/切换判据都建立在它上面；
    - 幂等：(code, as_of, profile) 主键 INSERT OR REPLACE；score 口径变更后
      必须全量重跑；
    - prof：回填目标 profile（Fix-5，缺省取 config.signals.profile）——v1/v2
      双 profile 各回填一次即可并存（主键含 profile，互不覆盖）；
    - 回填样本仍限于 daily_bar 内票池（事后人工挑选），IC 结论带池偏差，
      signal_eval 输出中显式标注。
    """
    own = conn is None
    c = conn or get_conn()
    prof = prof or profile()
    if prof not in PROFILES:
        raise ValueError(f"未知 profile: {prof}")
    t0 = time.time()
    pool = pd.read_sql("SELECT * FROM daily_bar", c)
    codes = sorted(repo.all_codes(c))
    bl = check_blacklist(c)
    by_code = _pool_by_code(pool)

    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM daily_bar ORDER BY trade_date").fetchall()]
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    is_momentum = prof == "momentum"
    n_rows = 0
    buf: list = []
    for i, d in enumerate(dates, 1):
        rows = _rows_as_of(by_code, codes, d, bl)
        if not rows:
            continue
        if not is_momentum:
            _score_cross_section(rows, prof=prof)
        for r in rows:
            buf.append((r["code"], r["as_of"],
                        json.dumps(r["signals"], ensure_ascii=False),
                        r["score"] if r["score"] is not None else 0.5,
                        prof))
        if len(buf) >= batch_commit * max(len(codes), 1):
            c.executemany("INSERT OR REPLACE INTO signal (code, as_of, signals,"
                          " score, profile) VALUES (?,?,?,?,?)", buf)
            c.commit()
            n_rows += len(buf)
            buf = []
        if i % 60 == 0 or i == len(dates):
            log.info("backfill[%s] %d/%d 日（%s），已写 %d 行，%.1fs",
                     prof, i, len(dates), d, n_rows + len(buf), time.time() - t0)
    if buf:
        c.executemany("INSERT OR REPLACE INTO signal (code, as_of, signals,"
                      " score, profile) VALUES (?,?,?,?,?)", buf)
        c.commit()
        n_rows += len(buf)
    if own:
        c.close()
    log.info("backfill[%s] 完成：%d 交易日，%d 行 signal，耗时 %.1fs",
             prof, len(dates), n_rows, time.time() - t0)
    return n_rows


def main():
    ap = argparse.ArgumentParser(description="计算全池技术信号并入库（支持全史回填）")
    ap.add_argument("--as_of", default=None, help="截至日期 YYYY-MM-DD（默认最新交易日）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON 数组（与表内容一致）")
    ap.add_argument("--backfill", action="store_true",
                    help="全史回填模式：对 daily_bar 全部交易日逐日重算 signal（幂等覆盖）")
    ap.add_argument("--start", default=None, help="回填起始日 YYYY-MM-DD（含）")
    ap.add_argument("--end", default=None, help="回填结束日 YYYY-MM-DD（含）")
    ap.add_argument("--profile", default=None, choices=PROFILES,
                    help="回填目标 profile（Fix-5：v1/v2 双 profile 可并存回填）")
    args = ap.parse_args()

    if args.backfill:
        n = backfill_history(start=args.start, end=args.end, prof=args.profile)
        print(json.dumps({"backfilled_rows": n, "profile": args.profile
                          or profile()}, ensure_ascii=False))
        return
    results = compute_all(as_of=args.as_of)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            s = r["signals"]
            log.info("%s as_of=%s trend=%s score=%.3f mom5=%.3f rsi=%.1f close=%.2f",
                     r["code"], r["as_of"], s["ma_trend"], r["score"],
                     s["mom_5d"] if s["mom_5d"] is not None else float("nan"),
                     s["rsi_14"] if s["rsi_14"] is not None else float("nan"),
                     s["close"] if s["close"] is not None else float("nan"))


if __name__ == "__main__":
    main()