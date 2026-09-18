"""信号有效性评估：signal 表留了全史却从没人读过——本模块把闭环接上。

产出（周报引用，样本不足时显式标注）：
- 因子 RankIC：score 及各因子值 vs 后 1/5/10 日收益的秩相关（-1~1，>0 正向预测）；
- score 五分位分层：高分组是否真的跑赢低分组；
- 池子评估：异动池/热门池入池后 N 日收益与胜率（dynamic_pool 按日留痕已支持）。

数据量不足（signal 行数 < MIN_SAMPLES）时输出照样生成但标注"样本不足"，
不进任何自动决策——先积累证据，再回头校准 score 权重。

落盘：每次运行 evaluate() 同步写 logs/signal_eval/YYYY-MM-DD.json + latest.json
（任务 1），bundle.py 与下游消费方读 latest.json 即可，免去选最新文件的逻辑。
"""
import json
import logging
import logging.handlers
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

MIN_SAMPLES = 100
FACTORS = ("mom_5d", "mom_20d", "atr_pct", "turnover_pct", "turn20", "rsi_14")
HORIZONS = (1, 5, 10)
# profile 切换判据（v1.4 起：B+C 两维投票 + MDD 红线 override；A/D 弃权）
# 见 docs/决策策略与工作流.md §7 与 docs/优化修复纪要.md Sprint 1 验收条目
IC_KEEP = 0.02          # 旧 IC 单维判据，保留常量供历史 verdict 对照，不参与 v1.4 投票
IC_SWITCH = -0.02
MDD_RED_LINE = -0.30    # C 维红线：当前 profile MDD 破 −30% → 无条件 hold
B_GAP_SWITCH = -0.10    # B 维切换阈值：B_cur − B_alt < −10pp → 投切换
C_GAP_SWITCH = -0.05    # C 维切换阈值：C_cur < C_alt − 5pp → 投切换
# 落盘目录：优先环境变量 AGSICKLE_SIGNAL_EVAL_DIR（测试隔离用），缺省生产路径。
# K3：测试与生产共用 logs/signal_eval 曾致 latest.json / factor_crowding.json 被覆盖污染。
def _signal_eval_dir() -> Path:
    import os
    env = os.environ.get("AGSICKLE_SIGNAL_EVAL_DIR")
    if env:
        return Path(env)
    return BASE / "logs" / "signal_eval"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(BASE / "logs" / "signal_eval.log",
                                                  encoding="utf-8",
                                                  maxBytes=5_000_000, backupCount=3),
              logging.StreamHandler()],
)
log = logging.getLogger("signal_eval")


def _signal_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """signal 表 → DataFrame（因子值拍平成列）。

    Fix-5：按 config.signals.profile 过滤——v1/v2 双 profile 行并存后，IC 评估
    必须只看当前口径的 score（混口径会稀释甚至反转 IC 结论）。
    老行（迁移前）profile='reversal_lowvol'。
    """
    try:
        from signals.signals import profile as _profile
        prof = _profile()
    except Exception:  # noqa: BLE001
        prof = "reversal_lowvol"
    rows = conn.execute(
        "SELECT code, as_of, signals, score FROM signal"
        " WHERE profile = ? OR profile IS NULL", (prof,)).fetchall()
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


# Fix-5（D3）：三 profile 备选显式枚举——二元 if/else 在三 profile 下会推导错
OTHER_PROFILES = {
    "reversal_lowvol": ("momentum", "reversal_lowvol_v2"),
    "reversal_lowvol_v2": ("reversal_lowvol",),
    "momentum": ("reversal_lowvol", "reversal_lowvol_v2"),
}


def profile_verdict(conn: sqlite3.Connection) -> dict:
    """profile 切换判据（v1.5：B+C 两维投票 ≥1 票即建议 + MDD 红线 override；不自动切）。

    维度（用户决策，A/D 因 momentum 无 signal 行弃权，详见 Sprint 1 任务 2）：
      A = score h5 RankIC        —— 仅 current 可算，momentum 弃权
      B = 年化超额 vs HS300       —— 各 profile 从 logs/backtest_result.json 读
      C = MDD                    —— 同 B
      D = 五分位多空价差 Q4-Q0    —— 同 A，弃权

    备选（Fix-5）：OTHER_PROFILES[prof] 枚举；备选多于一票时 B/C 各取备选中
    较优者作为对比基准（B 取年化超额最高者，C 取 MDD 最接近 0 者）。

    投票（B+C 两维有效票；v1.5 起 ≥1 票即建议 switch，D4 决策）：
      票1：B_cur − B_alt < −10pp → 投切换
      票2：C_cur < C_alt − 5pp （更负） → 投切换
      ≥1 票 → switch（confidence: 2/2=high、1/2=low）；0 票 → keep；任一弃权 → hold
      红线 override 优先于 switch；B/C 全弃权 → hold（弃权语义不变）

    红线 override：C_cur < −30% → verdict=hold（无条件），并写 risk_event 一条
    （rule='profile_verdict_red_line'，同日去重）。

    只输出建议——切换需人工确认后改 config.signals.profile 并留痕
    （docs/决策策略与工作流.md §7 v1.5），不做自动切换。
    """
    from signals.signals import profile as _profile
    prof = _profile()
    others = OTHER_PROFILES.get(prof, ("reversal_lowvol",))

    # ---- 读 backtest_result.json 拿 B/C ----
    bt_path = BASE / "logs" / "backtest_result.json"
    bt_cur, bt_alts, bt_missing = None, {}, None
    if not bt_path.exists():
        bt_missing = f"backtest_result.json 不存在 ({bt_path})"
        log.warning("profile_verdict: %s，B/C 维弃权", bt_missing)
    else:
        try:
            bt = json.loads(bt_path.read_text(encoding="utf-8"))
            profiles = bt.get("profiles") or {}
            bt_cur = profiles.get(prof)
            for o in others:
                if profiles.get(o):
                    bt_alts[o] = profiles.get(o)
            if not bt_cur or not bt_alts:
                bt_missing = (f"backtest_result.json 缺 {prof} 或"
                              f" {'/'.join(others)} 字段")
                bt_cur = None
                bt_alts = {}
        except (OSError, ValueError) as e:
            bt_missing = f"backtest_result.json 解析失败：{type(e).__name__}: {e}"
            bt_cur = bt_alts = None
            bt_alts = {}

    def _bc(profile_block):
        if not profile_block:
            return None, None
        sp = profile_block.get("strategy_perf") or {}
        bm = profile_block.get("benchmark_hs300") or {}
        ann = sp.get("annual_return")
        bma = bm.get("annual_return")
        if ann is None or bma is None:
            return None, sp.get("max_drawdown")
        return float(ann) - float(bma), sp.get("max_drawdown")

    B_cur, C_cur = _bc(bt_cur)
    # 备选取较优（Fix-5）：B 取年化超额最高者，C 取 MDD 最接近 0 者
    alt_bcs = {o: _bc(b) for o, b in bt_alts.items()}
    if alt_bcs:
        best_b = max(alt_bcs, key=lambda o: (alt_bcs[o][0]
                                             if alt_bcs[o][0] is not None else -9e9))
        best_c = max(alt_bcs, key=lambda o: (alt_bcs[o][1]
                                             if alt_bcs[o][1] is not None else -9e9))
        B_alt = alt_bcs[best_b][0]
        C_alt = alt_bcs[best_c][1]
        alt_best = {"b_from": best_b, "c_from": best_c}
    else:
        B_alt = C_alt = None
        alt_best = {"b_from": None, "c_from": None}

    # ---- 投票（B+C 两维多数决；弃权维度算"未投切换"，固定 2 票总数）----
    abstains = []
    if B_cur is None or B_alt is None:
        abstains.append("B (年化超额 vs HS300)")
    if C_cur is None or C_alt is None:
        abstains.append("C (MDD)")

    if B_cur is not None and B_alt is not None:
        b_gap = B_cur - B_alt
        b_vote_switch = b_gap < B_GAP_SWITCH
    else:
        b_vote_switch = False
    if C_cur is not None and C_alt is not None:
        c_gap_better = (C_cur - C_alt) < -C_GAP_SWITCH  # 当前比备选更负 → 投切换
    else:
        c_gap_better = False

    votes_switch = int(b_vote_switch) + int(c_gap_better)
    votes_total = 2  # B+C 两维；v1.5 起 ≥1 票即建议 switch，弃权维度算"未投切换"

    # ---- 红线 override（优先于 switch，语义不变）----
    red_line_triggered = C_cur is not None and C_cur < MDD_RED_LINE
    if red_line_triggered:
        verdict = "hold"
        # 写 risk_event + 同日去重
        _record_red_line_event(conn, prof, C_cur, bt_path)
    elif abstains:
        verdict = "hold"
    elif votes_switch >= 1:  # v1.5（D4）：≥1 票即建议 switch（原 2/2 全票逻辑废弃）
        verdict = "switch"
    else:
        verdict = "keep"
    # 置信度标注（v1.5）：2/2 → high；1/2 → low（提示人工复核权重）
    confidence = {2: "high", 1: "low"}.get(votes_switch, None)

    out = {
        "profile": prof,
        "other_profiles": list(others),
        # 兼容 v1.4 单备选消费方：主对比基准（B 维较优者）
        "other_profile": alt_best.get("b_from"),
        "alt_best": alt_best,
        "verdict": verdict,
        "confidence": confidence,
        "suggest_profile": prof if verdict != "switch" else alt_best.get("b_from"),
        "thresholds": {
            "mdd_red_line": MDD_RED_LINE,
            "b_gap_switch": B_GAP_SWITCH,
            "c_gap_switch": C_GAP_SWITCH,
        },
        "votes": {
            "b_switch": b_vote_switch if B_cur is not None and B_alt is not None else None,
            "c_switch": c_gap_better if C_cur is not None and C_alt is not None else None,
            "votes_switch": votes_switch,
            "votes_total": votes_total,
        },
        "abstains": abstains,
        "abstain_reason": ("momentum 无 signal 表 score 行，A/D 仅 current 可算"
                           if "A" not in abstains else None),
        "B": {"current": B_cur, "alt": B_alt,
              "gap": (B_cur - B_alt) if (B_cur is not None and B_alt is not None) else None},
        "C": {"current": C_cur, "alt": C_alt,
              "gap": (C_cur - C_alt) if (C_cur is not None and C_alt is not None) else None},
        "red_line_triggered": red_line_triggered,
        "backtest_missing": bt_missing,
        # 兼容 v1.3 周报消费方：保留旧 IC 单维字段
        "score_ic_h5": (factor_ic(conn).get("ic") or {}).get("h5", {}).get("score"),
        "ic_thresholds_v13": {"keep_ge": IC_KEEP, "switch_le": IC_SWITCH},
        "note": ("建议需人工确认后改 config.signals.profile（不自动切换）；"
                 "v1.5 起 ≥1 票即建议 switch（2/2=high、1/2=low，low 提示人工复核权重）；"
                 "投票仅 B/C 两维有效（A/D 因 momentum 无 signal 行弃权）"),
    }
    return out


def _record_red_line_event(conn, prof, C_cur, bt_path):
    """MDD 红线 override 写 risk_event（同日去重，最多一天一条）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM risk_event WHERE rule='profile_verdict_red_line'"
            " AND ts LIKE ?", (today + "%",)).fetchone()[0]
        if n > 0:
            return  # 同日已记录过
    except sqlite3.OperationalError:
        # risk_event 表可能尚未建（冷启动场景），先建表再写
        try:
            from data.fetcher import DDL
            conn.executescript(DDL)
        except Exception:
            pass
    try:
        from risk.engine import record_event
        bt_generated = ""
        try:
            bt_generated = json.loads(bt_path.read_text(encoding="utf-8")).get("generated_at", "")
        except Exception:
            pass
        detail = (f"MDD 红线触发: profile={prof} MDD={C_cur:.2%} < {MDD_RED_LINE:.0%}; "
                  f"backtest_result={bt_generated}; "
                  f"回测-生产口径不一致警告（report §6.3）")
        record_event(conn, "profile_verdict_red_line", detail)
        log.warning("红线 override 已写 risk_event: %s", detail)
    except Exception as e:
        log.warning("红线 override 写 risk_event 失败（不阻断主流程）: %s", repr(e))


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


def _persist_latest(payload: dict) -> dict:
    """把 evaluate() 输出落盘到 logs/signal_eval/YYYY-MM-DD.json + latest.json。

    原子策略：先写日期文件（追加式落盘不可丢历史），再用 Path.replace 覆盖
    latest.json（同分区原子 rename，软链在 Windows 上跨平台有问题所以用拷贝）。
    失败仅记 warning，不抛异常（与 _audit_snapshot 风格一致）。
    """
    out = {"date": None, "path": None, "latest_path": None, "error": None}
    try:
        eval_dir = _signal_eval_dir()
        eval_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        dated = eval_dir / (date_str + ".json")
        dated.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        out["date"] = date_str
        out["path"] = str(dated)
        # latest.json 用 replace 原子覆盖
        latest = eval_dir / "latest.json"
        latest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out["latest_path"] = str(latest)
        log.info("signal_eval 落盘: %s + latest.json", dated.name)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        log.warning("signal_eval 落盘失败（不阻断）: %s", repr(e))
    return out


def main():
    from data.fetcher import get_conn
    conn = get_conn()
    try:
        payload = evaluate(conn)
        persist = _persist_latest(payload)
        if persist.get("error"):
            payload["persist_error"] = persist["error"]
        payload["persist"] = persist
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
