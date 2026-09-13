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
import time
from typing import Optional

import pandas as pd

from data.fetcher import get_conn  # noqa: E402
from risk.blacklist import check_blacklist  # noqa: E402
from signals.factors import atr, ma, mom, rsi, turnover_pct  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signals")
log.addHandler(logging.handlers.RotatingFileHandler(BASE / "logs" / "signal.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3))
log.propagate = False  # 只写 signal.log，不串到 fetcher 的 root handler

PROFILES = ("reversal_lowvol", "momentum")
# momentum（旧，时序）：趋势 / 20日动量 / RSI 健康
W_TREND, W_MOM, W_RSI = 0.35, 0.40, 0.25
MOM_SPAN = 0.30          # mom 映射斜率：score = clip(0.5 + mom/MOM_SPAN, 0, 1)
RSI_HEALTH = (30.0, 70.0)
# reversal_lowvol（截面）：反转 / 低波动 / 低换手
W_REV, W_LOWVOL, W_LOTURNOVER = 0.40, 0.35, 0.25
REV_SPAN = 0.15          # 单票绝对参考 clip 斜率：clip(0.5 - mom5/REV_SPAN)
VOL_SPAN = 0.05          # 单票绝对参考 clip 斜率：clip(1 - atr_pct/VOL_SPAN)
TURNOVER_WINDOW = 250
TURN20_WINDOW = 20


def profile() -> str:
    """当前 score profile（config.signals.profile，缺省 reversal_lowvol）。"""
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        p = cfg.get("signals", {}).get("profile", "reversal_lowvol")
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
    if profile() == "momentum":
        score = _score_momentum(trend if ma5 is not None else None, m, r)
    else:
        score = None  # 截面打分由调用方统一合成
    return {"code": str(code), "as_of": str(last["trade_date"]),
            "signals": signals, "factors": f, "score": score}


def _score_cross_section(rows: list) -> None:
    """对 _factor_from_bars 产出的行列表做当日截面打分（原地写 score/score_parts）。"""
    m5s = pd.Series({r["code"]: r["factors"]["mom_5d"] for r in rows}, dtype=float)
    vs = pd.Series({r["code"]: r["factors"]["atr_pct"] for r in rows}, dtype=float)
    ts = pd.Series({r["code"]: r["factors"]["turn20"] for r in rows}, dtype=float)
    scores, parts = score_reversal_lowvol_xs(m5s, vs, ts)
    for r in rows:
        r["score"] = round(float(scores[r["code"]]), 4)
        r["signals"]["score_parts"] = {
            "rev_rank": _f(parts.loc[r["code"], "rev"]),
            "lowvol_rank": _f(parts.loc[r["code"], "vol"]),
            "lowturn_rank": _f(parts.loc[r["code"], "turn"]),
        }


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


def compute_all(as_of: Optional[str] = None, conn=None) -> list:
    """全池信号：读一次表，黑名单过滤后逐票计算、当日截面打分并 INSERT OR REPLACE 入 signal 表。"""
    own = conn is None
    c = conn or get_conn()
    t0 = time.time()
    pool = pd.read_sql("SELECT * FROM daily_bar", c)
    codes = [str(r[0]) for r in c.execute(
        "SELECT code FROM stock_info ORDER BY code").fetchall()]
    bl = check_blacklist(c)
    by_code = _pool_by_code(pool)

    rows = _rows_as_of(by_code, codes, as_of, bl)
    if profile() != "momentum":
        _score_cross_section(rows)

    results = []
    for r in rows:
        c.execute("INSERT OR REPLACE INTO signal VALUES (?,?,?,?)",
                  (r["code"], r["as_of"],
                   json.dumps(r["signals"], ensure_ascii=False),
                   r["score"] if r["score"] is not None else 0.5))
        results.append({"code": r["code"], "signals": r["signals"],
                        "score": r["score"], "as_of": r["as_of"]})
    c.commit()
    if own:
        c.close()
    log.info("compute_all %s 票（profile=%s），耗时 %.2fs",
             len(results), profile(), time.time() - t0)
    return results


def backfill_history(conn=None, start: Optional[str] = None,
                     end: Optional[str] = None, batch_commit: int = 30) -> int:
    """signal 全史回填：对 daily_bar 全部交易日逐日按当前代码口径重算并入库。

    - 与 compute_all 完全同口径（当日截面打分、黑名单、复权价优先）——
      signal 表由此成为"若当天跑当前代码会算出什么"的可评估时序，
      signal_eval 的 RankIC/分层/切换判据都建立在它上面；
    - 幂等：(code, as_of) 主键 INSERT OR REPLACE；score 口径变更后必须全量重跑；
    - 回填样本仍限于 daily_bar 内票池（自选池 30 只，事后人工挑选），IC 结论
      带池偏差，signal_eval 输出中显式标注。
    """
    own = conn is None
    c = conn or get_conn()
    t0 = time.time()
    pool = pd.read_sql("SELECT * FROM daily_bar", c)
    codes = [str(r[0]) for r in c.execute(
        "SELECT code FROM stock_info ORDER BY code").fetchall()]
    bl = check_blacklist(c)
    by_code = _pool_by_code(pool)

    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM daily_bar ORDER BY trade_date").fetchall()]
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    is_momentum = profile() == "momentum"
    n_rows = 0
    buf: list = []
    for i, d in enumerate(dates, 1):
        rows = _rows_as_of(by_code, codes, d, bl)
        if not rows:
            continue
        if not is_momentum:
            _score_cross_section(rows)
        for r in rows:
            buf.append((r["code"], r["as_of"],
                        json.dumps(r["signals"], ensure_ascii=False),
                        r["score"] if r["score"] is not None else 0.5))
        if len(buf) >= batch_commit * max(len(codes), 1):
            c.executemany("INSERT OR REPLACE INTO signal VALUES (?,?,?,?)", buf)
            c.commit()
            n_rows += len(buf)
            buf = []
        if i % 60 == 0 or i == len(dates):
            log.info("backfill %d/%d 日（%s），已写 %d 行，%.1fs",
                     i, len(dates), d, n_rows + len(buf), time.time() - t0)
    if buf:
        c.executemany("INSERT OR REPLACE INTO signal VALUES (?,?,?,?)", buf)
        c.commit()
        n_rows += len(buf)
    if own:
        c.close()
    log.info("backfill 完成：%d 交易日，%d 行 signal，耗时 %.1fs",
             len(dates), n_rows, time.time() - t0)
    return n_rows


def main():
    ap = argparse.ArgumentParser(description="计算全池技术信号并入库（支持全史回填）")
    ap.add_argument("--as_of", default=None, help="截至日期 YYYY-MM-DD（默认最新交易日）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON 数组（与表内容一致）")
    ap.add_argument("--backfill", action="store_true",
                    help="全史回填模式：对 daily_bar 全部交易日逐日重算 signal（幂等覆盖）")
    ap.add_argument("--start", default=None, help="回填起始日 YYYY-MM-DD（含）")
    ap.add_argument("--end", default=None, help="回填结束日 YYYY-MM-DD（含）")
    args = ap.parse_args()

    if args.backfill:
        n = backfill_history(start=args.start, end=args.end)
        print(json.dumps({"backfilled_rows": n}, ensure_ascii=False))
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
