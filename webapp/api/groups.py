"""webapp/api 拆分模块（结构性重构 Phase 6，纯结构搬移——不改 SQL 语义与响应 JSON 形状）。"""
"""自选股概念分组 + 动态池（异动/热门）接口。"""
import sqlite3
from typing import Dict, List

from webapp.api.common import blacklist_of, q_all
from webapp.api.market import api_signals


def _srv():
    from webapp import server
    return server


def api_concepts(conn: sqlite3.Connection, qs: dict) -> dict:
    """按概念分组返回自选股，附最新收盘/涨跌幅/信号分/黑名单状态。

    分组来自 config.watchlist[].concepts（可多归属）；无标签的归入"未分组"。
    """
    from common.config import core_codes
    wl = _srv().load_config().get("watchlist", [])
    core = set(core_codes())          # 可交易池（watchlist_core）；其余成员仅观察
    bl = {b["code"]: b for b in blacklist_of(conn)}
    sig = {r["code"]: r for r in api_signals(conn, {})}
    bars = {}
    for code, td, close, pct, amount, src in conn.execute(
            "SELECT code, trade_date, close, pct_chg, amount, source FROM daily_bar "
            "WHERE (code, trade_date) IN (SELECT code, MAX(trade_date) FROM daily_bar GROUP BY code)"):
        bars[code] = {"date": td, "close": close, "pct_chg": pct,
                      "amount": amount, "source": src}

    def stock_row(code: str, name: str) -> dict:
        s = sig.get(code) or {}
        sg = s.get("signals") or {}
        bar = bars.get(code) or {}
        b = bl.get(code) or {}
        return {"code": code, "name": name,
                "tradable": (code in core) if core else True,
                "close": bar.get("close"), "pct_chg": bar.get("pct_chg"),
                "bar_date": bar.get("date"), "amount": bar.get("amount"),
                "source": bar.get("source"),
                "score": s.get("score"), "ma_trend": sg.get("ma_trend"),
                "rsi_14": sg.get("rsi_14"), "mom_20d": sg.get("mom_20d"),
                "blacklisted": (not b.get("ok", True)) if code in bl else False,
                "blacklist_reason": b.get("reason", "")}

    groups: Dict[str, list] = {}
    order: List[str] = []
    ungrouped: List[dict] = []
    for w in wl:
        row = stock_row(str(w.get("code")), str(w.get("name") or ""))
        tags = w.get("concepts") or []
        if not tags:
            ungrouped.append(row)
        for t in tags:
            if t not in groups:
                groups[t] = []
                order.append(t)
            groups[t].append(row)
    def _group(name: str, stocks: list) -> dict:
        n_tradable = sum(1 for x in stocks if x.get("tradable"))
        return {"name": name, "stocks": stocks,
                "tradable_count": n_tradable,
                "observation_only": n_tradable == 0,   # 整组不可交易 → 看板标"仅观察"
                }
    out = [_group(t, groups[t]) for t in order]
    if ungrouped:
        out.append(_group("未分组", ungrouped))
    return {"total": len(wl),
            "core_total": len(core) if core else len(wl),
            "concepts": out}


# ---------------------------------------------------------------- 动态池（异动/热门）

def api_dynamic_pools(conn: sqlite3.Connection, qs: dict) -> dict:
    """当前异动池/热门池成员 + 最新一次刷新的板块榜（如已缓存）。"""
    from signals import dynpool, hot as hot_mod
    movers = dynpool.current_pool(conn, "movers")
    themes = dynpool.current_pool(conn, "hot_theme")
    stocks = dynpool.current_pool(conn, "hot_stock")
    # 概念板块榜是易变增强数据不入库，现算（失败为空）
    boards = []
    if qs.get("with_boards", ["0"])[0] == "1":  # 默认关：东财挂时首次调用也要 15s（审查P2-3）
        try:
            boards = hot_mod.board_hot()
        except Exception:  # noqa: BLE001
            boards = []
    dates = {p: dynpool.pool_dates(conn, p) for p in dynpool.POOLS}
    return {"movers": movers, "hot_theme": themes, "hot_stock": stocks,
            "boards": boards, "dates": dates,
            "updated_at": max(dates[p][0] for p in dynpool.POOLS
                              if dates[p]) if any(dates.values()) else None}


# ---------------------------------------------------------------- 数据状态

_AUDIT_CACHE: dict = {"at": 0.0, "summary": None}   # 体检全表扫描有成本，进程内缓存

