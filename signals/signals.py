"""信号层：输出个股技术信号 JSON 并入库；score 为 0~1 加权合成，支持双 profile。

score profiles（config.signals.profile 切换）：

- `reversal_lowvol`（默认，落地 docs/策略库.md Top1 结论）：
  短期反转 0.40（5日动量反向：clip(0.5 - mom5/REV_SPAN)）+ 低波动 0.35
  （ATR占价比反向）+ 低换手 0.25（换手分位反向）。
  依据：策略库 §2——A 股 2015-2025 中期价格动量 IC 为负、短期反转/低波动/低换手
  显著有效。旧 profile 的 20 日正向动量与自有研究结论相反，降为可选项。

- `momentum`（旧版保留）：趋势方向 0.35 + 20日动量 0.40（正向）+ RSI 健康 0.25
  （RSI 连续衰减 1-|RSI-50|/50，替代原 30~70 二值跳变）。

权重未经实证校准（signal 表历史尚短），review/signal_eval.py 会随数据积累输出
IC/分层验证，届时用证据重校。任一因子缺失按剩余权重重归一化；全缺取中性 0.5。

因子价格口径：优先用前复权 close_qfq（除权除息不污染动量/均线/新高），缺失回退不复权。
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import math
import time
from typing import Optional

import pandas as pd

from data.fetcher import get_conn  # noqa: E402
from risk.blacklist import check_blacklist  # noqa: E402
from signals.factors import atr, ma, mom, rsi, turnover_pct  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signals")
log.addHandler(logging.FileHandler(BASE / "logs" / "signal.log", encoding="utf-8"))
log.propagate = False  # 只写 signal.log，不串到 fetcher 的 root handler

PROFILES = ("reversal_lowvol", "momentum")
# momentum（旧）：趋势 / 20日动量 / RSI 健康
W_TREND, W_MOM, W_RSI = 0.35, 0.40, 0.25
MOM_SPAN = 0.30          # mom 映射斜率：score = clip(0.5 + mom/MOM_SPAN, 0, 1)
RSI_HEALTH = (30.0, 70.0)
# reversal_lowvol（新默认）：短期反转 / 低波动 / 低换手
W_REV, W_LOWVOL, W_LOTURNOVER = 0.40, 0.35, 0.25
REV_SPAN = 0.15          # 5日动量映射斜率：score = clip(0.5 - mom5/REV_SPAN, 0, 1)
VOL_SPAN = 0.05          # ATR/收盘 占价比映射：score = clip(1 - atr_pct/VOL_SPAN, 0, 1)
TURNOVER_WINDOW = 250


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


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def _score_reversal_lowvol(m5, atr_pct, tp) -> tuple:
    """反转+低波+低换手三因子加权，缺谁去掉谁的权重（重归一化）。返回 (score, den)。"""
    num, den = 0.0, 0.0
    if m5 is not None:
        num += W_REV * _clip01(0.5 - m5 / REV_SPAN)
        den += W_REV
    if atr_pct is not None:
        num += W_LOWVOL * _clip01(1.0 - atr_pct / VOL_SPAN)
        den += W_LOWVOL
    if tp is not None:
        num += W_LOTURNOVER * (1.0 - tp)  # 换手分位越低分越高
        den += W_LOTURNOVER
    return (num / den if den > 0 else 0.5), den


def _score_momentum(trend, m, r) -> float:
    """旧三因子：趋势 / 20日动量正向 / RSI 连续健康度。"""
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


def _signal_from_bars(bars: pd.DataFrame, code: str, as_of: Optional[str]) -> dict:
    """对单只票的截至 as_of 日线序列计算信号 dict（bars 需按 trade_date 升序）。"""
    if as_of is not None:
        bars = bars[bars["trade_date"] <= as_of]
    bars = bars.sort_values("trade_date")
    if bars.empty:
        raise ValueError(f"{code} 在 as_of={as_of} 前无行情数据")

    close = _factor_close(bars).reset_index(drop=True)
    raw_close = pd.to_numeric(bars["close"], errors="coerce").reset_index(drop=True)
    high = pd.to_numeric(bars["high"], errors="coerce").reset_index(drop=True)
    low = pd.to_numeric(bars["low"], errors="coerce").reset_index(drop=True)
    to = pd.to_numeric(bars["turnover"], errors="coerce").reset_index(drop=True)

    ma5, ma20, ma60 = ma(close, 5), ma(close, 20), ma(close, 60)
    if ma5 is not None and ma20 is not None and ma60 is not None:
        trend = "up" if (ma5 > ma20 > ma60) else "down" if (ma5 < ma20 < ma60) else "flat"
    else:
        trend = "flat"  # MA 不全时无法判向，输出 schema 要求恒为字符串
    r = rsi(close, 14)
    a = atr(high, low, close, 14)
    m = mom(close, 20)
    m5 = mom(close, 5)
    tp = turnover_pct(to, TURNOVER_WINDOW)
    last = bars.iloc[-1]
    last_close = _f(raw_close.iloc[-1]) or 0.0
    atr_pct = (a / last_close) if (a is not None and last_close > 0) else None

    signals = {
        "ma_trend": trend,
        "ma5": _f(ma5), "ma20": _f(ma20), "ma60": _f(ma60),
        "rsi_14": _f(r), "atr_14": _f(a), "atr_pct": _f(atr_pct),
        "mom_20d": _f(m), "mom_5d": _f(m5), "turnover_pct": _f(tp),
        "above_ma60": bool(_f(ma60) is not None and last_close > _f(ma60)),
        "close": _f(last_close), "pct_chg": _f(last["pct_chg"]),
        "adj_close": _f(close.iloc[-1]),
    }
    if "close_qfq" in bars.columns and pd.to_numeric(
            bars["close_qfq"], errors="coerce").notna().any():
        signals["price_basis"] = "qfq"
    else:
        signals["price_basis"] = "raw"

    if profile() == "momentum":
        score = _score_momentum(trend if ma5 is not None else None, m, r)
    else:
        score, _den = _score_reversal_lowvol(m5, atr_pct, tp)
        signals["score_profile"] = "reversal_lowvol"

    return {"code": str(code), "signals": signals,
            "score": round(float(score), 4),
            "as_of": str(last["trade_date"])}


def compute_signal(code: str, as_of: Optional[str] = None,
                   conn=None, pool: Optional[pd.DataFrame] = None) -> dict:
    """计算单票信号。pool 为已读入的 daily_bar DataFrame（compute_all 复用，避免重复读表）。"""
    own = conn is None and pool is None
    if pool is None:
        c = conn or get_conn()
        pool = pd.read_sql("SELECT * FROM daily_bar WHERE code=?", c, params=(code,))
        if own:
            c.close()
    bars = pool[pool["code"] == str(code)]
    return _signal_from_bars(bars, code, as_of)


def compute_all(as_of: Optional[str] = None, conn=None) -> list:
    """全池信号：读一次表，黑名单过滤后逐票计算并 INSERT OR REPLACE 入 signal 表。"""
    own = conn is None
    c = conn or get_conn()
    t0 = time.time()
    pool = pd.read_sql("SELECT * FROM daily_bar", c)
    codes = [r[0] for r in c.execute("SELECT code FROM stock_info ORDER BY code").fetchall()]
    bl = check_blacklist(c)

    results = []
    for code in codes:
        ok, reason = bl.get(code, (True, "-"))
        if not ok:
            log.info("skip %s 黑名单: %s", code, reason)
            continue
        try:
            res = compute_signal(code, as_of=as_of, conn=c, pool=pool)
        except ValueError as e:
            log.warning("skip %s: %s", code, e)
            continue
        c.execute("INSERT OR REPLACE INTO signal VALUES (?,?,?,?)",
                  (res["code"], res["as_of"],
                   json.dumps(res["signals"], ensure_ascii=False), res["score"]))
        results.append(res)
    c.commit()
    if own:
        c.close()
    log.info("compute_all %s 票（profile=%s），耗时 %.2fs",
             len(results), profile(), time.time() - t0)
    return results


def main():
    ap = argparse.ArgumentParser(description="计算全池技术信号并入库")
    ap.add_argument("--as_of", default=None, help="截至日期 YYYY-MM-DD（默认最新交易日）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON 数组（与表内容一致）")
    args = ap.parse_args()

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
