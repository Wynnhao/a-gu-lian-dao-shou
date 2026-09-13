"""信号有效性评估：signal 表留了全史却从没人读过——本模块把闭环接上。

产出（周报引用，样本不足时显式标注）：
- 因子 RankIC：score 及各因子值 vs 后 1/5/10 日收益的秩相关（-1~1，>0 正向预测）；
- score 五分位分层：高分组是否真的跑赢低分组；
- 池子评估：异动池/热门池入池后 N 日收益与胜率（dynamic_pool 按日留痕已支持）。

数据量不足（signal 行数 < MIN_SAMPLES）时输出照样生成但标注"样本不足"，
不进任何自动决策——先积累证据，再回头校准 score 权重。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

MIN_SAMPLES = 100
FACTORS = ("mom_5d", "mom_20d", "atr_pct", "turnover_pct", "turn20", "rsi_14")
HORIZONS = (1, 5, 10)
# profile 切换判据（成文防事后挑赢家）：score h5 RankIC ≥ +0.02 保留、
# ≤ −0.02 切换、其间维持积累；样本不足一律 insufficient
IC_KEEP = 0.02
IC_SWITCH = -0.02


def _signal_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """signal 表 → DataFrame（因子值拍平成列）。"""
    rows = conn.execute("SELECT code, as_of, signals, score FROM signal").fetchall()
    if not rows:
        return pd.DataFrame()
    rec = []
    for code, as_of, sig_json, score in rows:
        try:
            s = json.loads(sig_json) if sig_json else {}
        except (TypeError, ValueError):
            s = {}
        r = {"code": code, "as_of": as_of, "score": score}
        for f in FACTORS:
            r[f] = s.get(f)
        rec.append(r)
    return pd.DataFrame(rec)


def _forward_returns(conn: sqlite3.Connection, horizon: int) -> pd.DataFrame:
    """每票每交易日的后 horizon 日收益（优先前复权 close_qfq）。"""
    df = pd.read_sql(
        "SELECT code, trade_date, close, close_qfq FROM daily_bar", conn
    ).sort_values(["code", "trade_date"])
    if df.empty:
        return pd.DataFrame()
    close = df["close_qfq"].fillna(df["close"]).astype(float)
    df["fwd_ret"] = close.groupby(df["code"]).pct_change(horizon).shift(-horizon)
    return df[["code", "trade_date", "fwd_ret"]].rename(columns={"trade_date": "as_of"})


def factor_ic(conn: sqlite3.Connection, horizons=HORIZONS) -> dict:
    """各因子 vs 后 N 日收益的 RankIC（均值；|IC|<0.02 视为无预测力）。"""
    sig = _signal_frame(conn)
    if sig.empty:
        return {"samples": 0, "sufficient": False, "ic": {}}
    out = {"samples": int(len(sig)), "sufficient": len(sig) >= MIN_SAMPLES, "ic": {}}
    for h in horizons:
        fwd = _forward_returns(conn, h)
        if fwd.empty:
            continue
        m = sig.merge(fwd, on=["code", "as_of"], how="inner").dropna(subset=["fwd_ret"])
        for col in ("score",) + FACTORS:
            sub = m[["fwd_ret", col]].dropna()
            if len(sub) < 20:
                continue
            ic = sub[col].rank(pct=True).corr(sub["fwd_ret"].rank(pct=True))
            out["ic"].setdefault("h%d" % h, {})[col] = round(float(ic), 4)
    return out


def score_quintiles(conn: sqlite3.Connection, horizon: int = 5) -> dict:
    """score 五分位分层后 N 日平均收益（高分组-低分组差 = 多空价差）。"""
    sig = _signal_frame(conn)
    if sig.empty:
        return {"samples": 0, "sufficient": False}
    fwd = _forward_returns(conn, horizon)
    if fwd.empty:
        return {"samples": int(len(sig)), "sufficient": False}
    m = sig.merge(fwd, on=["code", "as_of"], how="inner").dropna(
        subset=["fwd_ret", "score"])
    if m.empty:
        return {"samples": int(len(sig)), "sufficient": len(m) >= MIN_SAMPLES}
    try:
        m["q"] = pd.qcut(m["score"], 5, labels=False, duplicates="drop")
    except ValueError:
        return {"samples": int(len(m)), "sufficient": False}
    layer = m.groupby("q")["fwd_ret"].mean().round(4).to_dict()
    spread = round(float(layer.get(4, 0) - layer.get(0, 0)), 4)
    return {"samples": int(len(m)), "sufficient": len(m) >= MIN_SAMPLES,
            "horizon": horizon, "quintile_mean_ret": {str(k): v for k, v in layer.items()},
            "top_minus_bottom": spread}


def pool_eval(conn: sqlite3.Connection, pool: str = "movers", horizon: int = 5) -> dict:
    """入池后 N 日收益与胜率（vs 0；历史进池行全部统计）。"""
    rows = conn.execute(
        "SELECT code, added_date FROM dynamic_pool WHERE pool=? AND code NOT LIKE 'THEME:%'",
        (pool,)).fetchall()
    if not rows:
        return {"pool": pool, "samples": 0}
    df = pd.DataFrame(rows, columns=["code", "as_of"])
    fwd = _forward_returns(conn, horizon)
    if fwd.empty:
        return {"pool": pool, "samples": 0}
    m = df.merge(fwd, on=["code", "as_of"], how="inner").dropna(subset=["fwd_ret"])
    if m.empty:
        return {"pool": pool, "samples": 0, "sufficient": False}
    win = float((m["fwd_ret"] > 0).mean())
    return {"pool": pool, "samples": int(len(m)),
            "sufficient": len(m) >= MIN_SAMPLES,
            "mean_ret": round(float(m["fwd_ret"].mean()), 4),
            "win_rate": round(win, 4)}


def rolling_ic(conn: sqlite3.Connection, col: str = "score", horizon: int = 5,
               min_bucket: int = 20) -> dict:
    """按月分桶的滚动 RankIC（观察因子衰减/拥挤：连续走负即预警，策略库 §2.1
    对换手率因子拥挤迹象的警示即靠它监控）。样本不足的月份跳过。"""
    sig = _signal_frame(conn)
    if sig.empty:
        return {"col": col, "horizon": horizon, "buckets": []}
    fwd = _forward_returns(conn, horizon)
    if fwd.empty:
        return {"col": col, "horizon": horizon, "buckets": []}
    m = sig.merge(fwd, on=["code", "as_of"], how="inner").dropna(
        subset=["fwd_ret", col])
    if m.empty:
        return {"col": col, "horizon": horizon, "buckets": []}
    m = m.copy()
    m["bucket"] = m["as_of"].str[:7]
    buckets = []
    for b, g in m.groupby("bucket"):
        if len(g) < min_bucket:
            continue
        ic = g[col].rank(pct=True).corr(g["fwd_ret"].rank(pct=True))
        buckets.append({"bucket": str(b), "ic": round(float(ic), 4),
                        "n": int(len(g))})
    return {"col": col, "horizon": horizon, "buckets": buckets}


def profile_verdict(conn: sqlite3.Connection) -> dict:
    """profile 切换判据（成文执行，杜绝事后人为挑赢家）。

    - 样本 < MIN_SAMPLES 或 IC 不可算 → insufficient（维持现状，继续积累）；
    - score h5 RankIC ≥ IC_KEEP → keep（当前 profile 正向预测力成立）；
    - ≤ IC_SWITCH → switch（当前 profile 反向，建议切另一 profile）；
    - 其间 → hold（证据不足，维持并继续积累）。
    只输出建议——切换需人工确认后改 config.signals.profile 并留痕
    （决策文档 §7 判据），不做自动切换。
    """
    from signals.signals import profile as _profile
    prof = _profile()
    other = "momentum" if prof == "reversal_lowvol" else "reversal_lowvol"
    ic = factor_ic(conn)
    n = int(ic.get("samples", 0))
    h5 = (ic.get("ic") or {}).get("h5", {}).get("score")
    if not ic.get("sufficient") or h5 is None:
        verdict = "insufficient"
    elif h5 >= IC_KEEP:
        verdict = "keep"
    elif h5 <= IC_SWITCH:
        verdict = "switch"
    else:
        verdict = "hold"
    return {"profile": prof, "samples": n, "score_ic_h5": h5,
            "thresholds": {"keep_ge": IC_KEEP, "switch_le": IC_SWITCH},
            "verdict": verdict,
            "suggest_profile": prof if verdict != "switch" else other,
            "note": "建议需人工确认后改 config.signals.profile（不自动切换）"}


def evaluate(conn: sqlite3.Connection) -> dict:
    """完整评估包（供周报与看板引用）。"""
    return {
        "factor_ic": factor_ic(conn),
        "score_quintiles_h5": score_quintiles(conn, 5),
        "pool_movers_h5": pool_eval(conn, "movers", 5),
        "pool_hot_stock_h5": pool_eval(conn, "hot_stock", 5),
        "rolling_ic_score_h5": rolling_ic(conn, "score", 5),
        "rolling_ic_mom5d_h5": rolling_ic(conn, "mom_5d", 5),
        "profile_verdict": profile_verdict(conn),
    }


def main():
    from data.fetcher import get_conn
    conn = get_conn()
    try:
        print(json.dumps(evaluate(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
