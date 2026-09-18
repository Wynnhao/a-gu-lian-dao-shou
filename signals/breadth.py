"""Sprint 2 任务 3（P1-1）：regime 旁路读 + bundle 注入。

read_breadth() 读 breadth_daily 最新一行，返回 dict（含 override_cap 信号）。
compute_breadth_factor() 给 regime 提供 override_cap：breadth_composite < -2 → 0.1
（极端避险档）。
"""
import json
import logging
import logging.handlers
import sqlite3
import sys
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

log = logging.getLogger("signals.breadth")
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("signal.log"))
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
log.propagate = False


def read_breadth(conn: sqlite3.Connection) -> dict:
    """读 breadth_daily 最新一行。

    永远返回 dict；缺表/空表 → 全部 None + reason 说明。
    """
    out = {"date": None, "limit_up_count": None, "limit_up_seal_rate": None,
           "limit_down_count": None, "advance_decline_ratio": None,
           "new_high_minus_new_low": None, "breadth_composite": None,
           "source": None, "reason": ""}
    try:
        row = conn.execute(
            "SELECT date, limit_up_count, limit_up_seal_rate, limit_down_count,"
            "       advance_decline_ratio, new_high_minus_new_low, breadth_composite, source"
            " FROM breadth_daily ORDER BY date DESC LIMIT 1").fetchone()
        if row:
            (out["date"], out["limit_up_count"], out["limit_up_seal_rate"],
             out["limit_down_count"], out["advance_decline_ratio"],
             out["new_high_minus_new_low"], out["breadth_composite"],
             out["source"]) = row
        else:
            out["reason"] = "breadth_daily 为空（先跑 data.breadth）"
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"读取失败：{type(e).__name__}: {e}"
    return out


# Fix-2：阈值 config 化（config.json → "breadth" 段），代码缺省兜底
BREADTH_DEFAULTS = {"extreme_threshold": -2.0, "override_cap": 0.1}


def _breadth_cfg() -> dict:
    """读 config.json 的 breadth 段（热读；失败回退缺省，fail-open）。"""
    out = dict(BREADTH_DEFAULTS)
    try:
        from common.config import load
        out.update((load().get("breadth") or {}))
    except Exception:  # noqa: BLE001
        pass
    return out


def _freshness_floor(conn: sqlite3.Connection) -> Optional[str]:
    """W-B6（P2-5）新鲜度闸门地板：daily_bar 最近 2 个交易日中较早者。

    breadth 最新行早于该地板 → 视为陈旧。daily_bar 不足 2 个交易日 → None
    （无法判定，不启闸）。
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM daily_bar "
            "ORDER BY trade_date DESC LIMIT 2").fetchall()
        dates = sorted(str(r[0]) for r in rows if r and r[0])
        if len(dates) < 2:
            return None
        return dates[0]
    except Exception:  # noqa: BLE001
        return None


def compute_breadth_factor(conn: sqlite3.Connection, threshold: float = None,
                            override_cap: float = None) -> dict:
    """regime 旁路：breadth_composite < threshold → override_cap（极端避险）。

    threshold/override_cap 缺省从 config.json breadth 段读（extreme_threshold /
    override_cap），无 config 时用代码缺省 -2.0 / 0.1。
    返回 {"composite": float|None, "override_cap": float|None, "reason": str}。

    W-B6（P2-5）新鲜度闸门：最新行早于最近 2 个交易日 → composite 按 None 处理
    ——断供数周的陈旧 composite 不再无限生效（规则 20 极端避险档不被旧状态误触）。
    """
    cfg = _breadth_cfg()
    if threshold is None:
        threshold = float(cfg["extreme_threshold"])
    if override_cap is None:
        override_cap = float(cfg["override_cap"])
    data = read_breadth(conn)
    composite = data.get("breadth_composite")
    out = {"composite": composite, "override_cap": None, "reason": ""}
    floor = _freshness_floor(conn)
    if floor is not None and str(data.get("date") or "") < floor:
        out["composite"] = None
        out["reason"] = (f"breadth 数据陈旧（最新 {data.get('date')} < "
                         f"最近2交易日门槛 {floor}），闸门关闭不生效")
        return out
    if composite is None:
        out["reason"] = data.get("reason") or "breadth_composite 缺失"
        return out
    if composite < threshold:
        out["override_cap"] = override_cap
        out["reason"] = f"composite={composite:.2f} < {threshold}（极端避险）"
    return out
