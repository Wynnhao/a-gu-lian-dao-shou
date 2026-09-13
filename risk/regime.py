"""市场环境总闸与组合风控参数（策略库 Top2/Top3 落地，2026-09-14）。

组合仓位管理的三个层次（互为补充，不相互替代）：
- kill switch（engine 规则5）：事后熔断，回撤 ≥8% 清仓停机 72h；
- 本模块（事前降仓）：指数环境走弱/组合波动超标时压低总仓位上限，
  让 kill 更难被触发——弥补"5 只票只见树木不见森林"的缺陷；
- 单票止损（engine 规则16）：由固定 8% 升级为 ATR 自适应口径。

RSRS 大盘择时三档（光大证券研报口径，策略库 §6.3）：
- β = 过去 rsrs_window(18) 日指数最高价对最低价 OLS 斜率（支撑强于阻力的程度）；
- z = β 的 rsrs_zscore_window(600) 日标准分；
- z > rsrs_bull_z(+1) → 满配档；z < rsrs_bear_z(−1) → 避险档；其余 → 半配档。

二八轮动（策略库 §3.1，作为总仓位开关而非换仓标的）：
- 大盘(000300)与小盘(000905)过去 dual_mom_window(20) 日动量皆负 → 避险档；
  任一为正 → 不额外约束（满配档）。

组合规则：cap = min(RSRS 档位 cap, 二八档位 cap)——两者取更保守者；
再与波动率目标仓位 cap 取 min。最终 cap 为**绝对总仓位上限**（0~1），
由 execution.runner.build_context 注入 RiskContext.position_cap，
engine 规则7以 min(静态上限, 动态 cap) 强制执行。

波动率目标仓位（Moreira & Muir 2017，策略库 §6.1）：
- σ = portfolio_state.total 近 window(20) 日日收益年化标准差（组合已实现波动）；
- 组合 cap = min(1, target_ann_vol / σ) × 静态总仓上限；样本不足不约束。

失败语义：任何数据缺失/异常 → cap=None（不约束），静态规则仍在——
本模块是"锦上添花的事前降仓"，绝不因自身故障阻断交易（fail-open），
与 kill switch 的 fail-safe 性质不同。
"""
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

_DEFAULTS = {
    "enabled": True,
    "rsrs_index": "000300",
    "rsrs_window": 18,
    "rsrs_zscore_window": 600,
    "rsrs_min_beta": 250,      # β 历史不足此数时 z 不可用（视为无 RSRS 约束）
    "rsrs_bull_z": 1.0,
    "rsrs_bear_z": -1.0,
    "index_big": "000300",
    "index_small": "000905",
    "dual_mom_window": 20,
    "cap_half": 0.50,          # 半配档（绝对总仓位）
    "cap_shelter": 0.20,       # 避险档（绝对总仓位）
    "tier_names": ("满配", "半配", "避险"),
}
_VOL_DEFAULTS = {
    "enabled": True,
    "target_ann_vol": 0.30,    # 目标年化波动 30%
    "window": 20,              # 已实现波动回看窗口（交易日）
    "min_samples": 21,         # 至少需要 window+1 个净值点
}
STATIC_CAP_FALLBACK = 1.0     # cap 缺省（无约束；engine 仍取 min(静态上限, cap)）


def _cfg(root: Optional[dict]) -> Tuple[dict, dict]:
    cfg = root or {}
    r = dict(_DEFAULTS)
    r.update(cfg.get("regime", {}) or {})
    v = dict(_VOL_DEFAULTS)
    v.update(cfg.get("vol_target", {}) or {})
    return r, v


def _static_total_cap(root: Optional[dict]) -> float:
    try:
        cfg = root or json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        return float(cfg.get("risk", {}).get("max_total_weight", 0.80))
    except Exception:  # noqa: BLE001
        return 0.80


def _index_frame(conn, code: str, need_hl: bool = False) -> Optional[pd.DataFrame]:
    """index_daily → 按 trade_date 升序的 close(,high,low) DataFrame；无数据 None。"""
    cols = "trade_date, close" + (", high, low" if need_hl else "")
    rows = conn.execute(
        f"SELECT {cols} FROM index_daily WHERE index_code=? ORDER BY trade_date",
        (code,)).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "close"] + (["high", "low"] if need_hl else []))
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def rsrs_z(high: pd.Series, low: pd.Series, window: int = 18,
           zwindow: int = 600, min_beta: int = 250) -> Tuple[Optional[float], Optional[float]]:
    """RSRS 标准分：β = high 对 low 的滚动 OLS 斜率，再取 zwindow 日标准分。

    返回 (最新 z, 最新 β)；β 历史不足 min_beta 或方差为 0 → (None, β)。
    """
    if len(high) < max(window + 1, 30):
        return None, None
    # OLS 斜率 = Cov(high, low) / Var(low)（rolling cov/var 即滚动一元回归）
    cov = high.rolling(window).cov(low)
    var = low.rolling(window).var()
    beta = (cov / var).dropna()
    if len(beta) < min_beta:
        return None, (float(beta.iloc[-1]) if len(beta) else None)
    mu = beta.rolling(zwindow, min_periods=min_beta).mean()
    sd = beta.rolling(zwindow, min_periods=min_beta).std()
    z = (beta - mu) / sd
    z_last = z.iloc[-1]
    return (float(z_last) if np.isfinite(z_last) else None,
            float(beta.iloc[-1]))


def dual_momentum(close_big: pd.Series, close_small: pd.Series,
                  window: int = 20) -> Tuple[Optional[float], Optional[float]]:
    """二八动量：(大盘 20 日涨幅, 小盘 20 日涨幅)。"""
    def _mom(s: pd.Series) -> Optional[float]:
        s = s.dropna()
        if len(s) < window + 1 or float(s.iloc[-1 - window]) == 0.0:
            return None
        return float(s.iloc[-1] / s.iloc[-1 - window] - 1.0)
    return _mom(close_big), _mom(close_small)


def compute_regime(conn, root: Optional[dict] = None) -> dict:
    """RSRS 三档 + 二八开关 → (档位名, 绝对 cap, 明细)。数据缺失降级为无约束。"""
    cfg, _ = _cfg(root)
    static_cap = _static_total_cap(root)
    detail = {"rsrs": None, "dual_mom": None, "tier": None, "cap": None}
    if not cfg.get("enabled", True):
        detail["note"] = "regime disabled"
        return detail

    caps: list = []
    # ---- RSRS ----
    try:
        idx = _index_frame(conn, cfg["rsrs_index"], need_hl=True)
        if idx is not None and "high" in idx.columns \
                and idx["high"].notna().sum() > cfg["rsrs_window"] + 10:
            z, beta = rsrs_z(idx["high"], idx["low"], int(cfg["rsrs_window"]),
                             int(cfg["rsrs_zscore_window"]), int(cfg["rsrs_min_beta"]))
            detail["rsrs"] = {"z": round(z, 3) if z is not None else None,
                              "beta": round(beta, 4) if beta is not None else None,
                              "as_of": str(idx["date"].iloc[-1])}
            if z is not None:
                if z > float(cfg["rsrs_bull_z"]):
                    tier, cap = "满配", static_cap
                elif z < float(cfg["rsrs_bear_z"]):
                    tier, cap = "避险", float(cfg["cap_shelter"])
                else:
                    tier, cap = "半配", float(cfg["cap_half"])
                detail["tier"], detail["cap"] = tier, cap
                caps.append(cap)
            else:
                detail["rsrs"]["note"] = "β 历史不足，RSRS 无约束"
        else:
            detail["rsrs_note"] = "指数 H/L 数据缺失，RSRS 无约束"
    except Exception as e:  # noqa: BLE001
        detail["rsrs_error"] = repr(e)[:120]

    # ---- 二八 ----
    try:
        big = _index_frame(conn, cfg["index_big"])
        small = _index_frame(conn, cfg["index_small"])
        if big is not None and small is not None:
            mb, ms = dual_momentum(big["close"], small["close"],
                                   int(cfg["dual_mom_window"]))
            detail["dual_mom"] = {"big": round(mb, 4) if mb is not None else None,
                                  "small": round(ms, 4) if ms is not None else None,
                                  "window": int(cfg["dual_mom_window"])}
            if mb is not None and ms is not None and mb < 0 and ms < 0:
                cap = float(cfg["cap_shelter"])
                detail["tier"] = "避险" if detail["tier"] is None else detail["tier"]
                detail["dual_mom"]["signal"] = "二八皆弱 → 避险档"
                caps.append(cap)
            else:
                detail["dual_mom"]["signal"] = "任一为正 → 不额外约束"
    except Exception as e:  # noqa: BLE001
        detail["dual_mom_error"] = repr(e)[:120]

    if caps:
        cap_final = round(min(caps), 4)
        detail["cap"] = cap_final
        # 档位名统一按最终 cap 归档（RSRS 半配 × 二八避险 → 取更保守的避险档）
        if cap_final >= static_cap * 0.999:
            detail["tier"] = "满配"
        elif cap_final >= float(cfg["cap_half"]) * 0.999:
            detail["tier"] = "半配"
        else:
            detail["tier"] = "避险"
    return detail


def compute_vol_target(conn, root: Optional[dict] = None) -> dict:
    """组合已实现波动 → 波动率目标仓位 cap（绝对值）。样本不足 → cap=None。"""
    cfg, v = _cfg(root)
    static_cap = _static_total_cap(root)
    out = {"enabled": bool(v.get("enabled", True)), "cap": None}
    if not out["enabled"]:
        out["note"] = "vol_target disabled"
        return out
    try:
        rows = conn.execute(
            "SELECT total FROM portfolio_state ORDER BY date DESC LIMIT ?",
            (int(v["window"]) + 1,)).fetchall()
        totals = [float(r[0]) for r in rows if r[0] is not None and float(r[0]) > 0]
        if len(totals) < int(v["min_samples"]):
            out["note"] = ("净值样本 %d < %d，波动目标暂不约束"
                           % (len(totals), int(v["min_samples"])))
            return out
        totals = list(reversed(totals))
        rets = np.diff(totals) / np.array(totals[:-1])
        sigma_ann = float(np.std(rets, ddof=1) * np.sqrt(252))
        out["sigma_ann"] = round(sigma_ann, 4)
        out["target_ann_vol"] = float(v["target_ann_vol"])
        out["window"] = int(v["window"])
        if sigma_ann <= 0:
            out["cap"] = static_cap
            return out
        scale = min(1.0, float(v["target_ann_vol"]) / sigma_ann)
        out["scale"] = round(scale, 4)
        out["cap"] = round(scale * static_cap, 4)
    except Exception as e:  # noqa: BLE001
        out["error"] = repr(e)[:120]
    return out


def position_cap(conn, root: Optional[dict] = None) -> dict:
    """总仓位动态上限（regime ∩ 波动率目标），供 runner.build_context 注入。

    返回 {"cap": float|None, "regime": {...}, "vol_target": {...}}；
    cap=None 表示当前无附加约束（engine 用静态上限）。
    """
    try:
        regime = compute_regime(conn, root)
    except Exception as e:  # noqa: BLE001
        regime = {"error": repr(e)[:120]}
    try:
        vol = compute_vol_target(conn, root)
    except Exception as e:  # noqa: BLE001
        vol = {"error": repr(e)[:120]}
    caps = [c for c in (regime.get("cap"), vol.get("cap")) if c is not None]
    return {"cap": round(min(caps), 4) if caps else None,
            "regime": regime, "vol_target": vol}


def latest_atr_pct(conn, codes) -> Dict[str, float]:
    """signal 表各票最新 as_of 的 atr_pct（ATR 自适应止损与 bundle 止损参考价共用）。"""
    out: Dict[str, float] = {}
    for code in set(str(c) for c in codes):
        row = conn.execute(
            "SELECT signals FROM signal WHERE code=? ORDER BY as_of DESC LIMIT 1",
            (code,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            s = json.loads(row[0])
            v = s.get("atr_pct")
            if v is not None and float(v) > 0:
                out[code] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def stop_loss_line(base_pct: float, atr_pct: Optional[float],
                   atr_mult: float = 2.0) -> float:
    """单票止损线（浮亏口径）：max(基础线, atr_mult × ATR占比)。

    波动大的票止损更宽（海龟 2N 逻辑：止损只在超出 2 个典型日波幅的 adverse
    move 后触发，避免被噪声扫出）；低波票维持基础线。atr_pct 缺失 → 基础线。
    """
    base = float(base_pct or 0)
    if atr_pct is None or atr_pct <= 0:
        return base
    return max(base, float(atr_mult) * float(atr_pct))
