"""webapp/api 拆分模块（结构性重构 Phase 6，纯结构搬移——不改 SQL 语义与响应 JSON 形状）。"""
"""十段流水线聚合状态 + 当日决策追踪链（批量查询 O(1)）。"""
import sqlite3
from datetime import datetime
from typing import List, Optional

from data import repo  # noqa: F401  （api_workflow 批量查询，Phase 4）
from webapp.api.common import (blacklist_of, fnum, health_issues_of,
                               parse_json_field, q_all, q_one)


def _stage(sid: str, name: str, desc: str, status: str, detail: str,
           ts: Optional[str] = None) -> dict:
    return {"id": sid, "name": name, "desc": desc, "status": status,
            "detail": detail, "ts": ts}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def api_workflow(conn: sqlite3.Connection, qs: dict) -> dict:
    """聚合十段流水线的实时状态 + 当日决策追踪链。status: ok/warn/fail/idle。"""
    row = q_one(conn, "SELECT MAX(trade_date) AS d FROM daily_bar")
    run_date = row["d"] if row else None
    stages: List[dict] = []
    issues = health_issues_of(conn)

    # ① 行情采集
    fl = q_one(conn, "SELECT MAX(run_at) AS ts FROM fetch_log")
    bars = q_all(conn, "SELECT code, MAX(trade_date) AS d FROM daily_bar GROUP BY code")
    fresh = sum(1 for b in bars if b["d"] == run_date)
    stages.append(_stage(
        "fetch", "行情采集", "东财/腾讯双源日K增量入库",
        "ok" if bars and fresh == len(bars) else ("warn" if bars else "idle"),
        "%d/%d 票数据至 %s" % (fresh, len(bars), run_date or "—"), fl["ts"]))

    # ② 资讯与估值
    nw = q_one(conn, "SELECT MAX(fetched_at) AS ts, COUNT(*) AS n FROM news")
    val = q_one(conn, "SELECT MAX(trade_date) AS d FROM index_valuation")
    val_lag = None
    if val["d"] and run_date:
        val_lag = (datetime.fromisoformat(run_date) - datetime.fromisoformat(val["d"])).days
    stages.append(_stage(
        "news", "资讯/估值", "个股+市场新闻；三指数PE/PB分位",
        "ok" if nw["n"] and (val_lag is not None and val_lag <= 3) else
        ("warn" if nw["n"] else "idle"),
        "新闻 %d 条；估值 %s（滞后 %s 天）"
        % (nw["n"], val["d"] or "—", val_lag if val_lag is not None else "—"), nw["ts"]))

    # ③ 数据体检（黑名单 + 健康）
    bl = blacklist_of(conn)
    blocked = sum(1 for b in bl if not b["ok"])
    stages.append(_stage(
        "health", "数据体检", "黑名单过滤 + 数据健康检查",
        "fail" if issues else "ok",
        ("健康告警 %d 项；" % len(issues)) if issues else "健康 OK；"
        + "黑名单拦截 %d/%d" % (blocked, len(bl))))

    # ④ 信号计算
    sig = q_one(conn, "SELECT MAX(as_of) AS d, COUNT(*) AS n FROM signal")
    sig_ok = sig["d"] == run_date and sig["n"] > 0
    stages.append(_stage(
        "signals", "信号计算", "MA/RSI/动量/换手分位 → score",
        "ok" if sig_ok else ("warn" if sig["n"] else "idle"),
        "%d 票 as_of=%s" % (sig["n"], sig["d"] or "—")))

    # ⑤ 输入包
    from webapp import server as _srv
    bpath = _srv.SESSION_DIR / str(run_date) / "bundle.md"
    bts = datetime.fromtimestamp(bpath.stat().st_mtime) if bpath.is_file() else None
    stages.append(_stage(
        "bundle", "输入包组装", "信号+新闻+宏观+账户 → bundle.md",
        "ok" if bts else "idle",
        "bundle %s" % ("生成于 %s" % bts.strftime("%m-%d %H:%M") if bts else "未生成"),
        _iso(bts)))

    # ⑥~⑧ 决策/裁决/闸门
    decs = q_all(conn, "SELECT * FROM decision WHERE run_date=? ORDER BY id", (run_date,))
    d_ids = [d["id"] for d in decs]
    st_cnt: Dict[str, int] = {}
    for d in decs:
        st_cnt[d["status"]] = st_cnt.get(d["status"], 0) + 1
    llm_ts = min((d["created_at"] for d in decs), default=None)
    stages.append(_stage(
        "llm", "LLM决策", "读bundle输出结构化建议（校验失败整包放弃）",
        "ok" if decs else ("idle" if not bts else "warn"),
        "%d 条决策 %s" % (len(decs),
                          "、".join("%s=%d" % kv for kv in sorted(st_cnt.items())) or "")
        if decs else "当日尚无决策（待盘前流程）",
        llm_ts))
    rk = q_one(conn,
               "SELECT COUNT(*) AS n, MAX(ts) AS ts FROM risk_event "
               "WHERE decision_id IN (%s)" % ",".join("?" * len(d_ids)) if d_ids
               else "SELECT 0 AS n, NULL AS ts",
               tuple(d_ids) if d_ids else ())
    stages.append(_stage(
        "risk", "规则裁决", "15条硬规则：仓位/价格/T+1/涨跌停/kill",
        "ok" if decs else "idle",
        "风控事件 %d 条；approved=%d rejected=%d report_only=%d"
        % (rk["n"], st_cnt.get("approved", 0) + st_cnt.get("executed", 0),
           st_cnt.get("rejected", 0), st_cnt.get("report_only", 0)), rk["ts"]))
    pend = sorted(_srv.ORDERS_DIR.glob(str(run_date) + "/pending_*.json")) if run_date else []
    p_ts = datetime.fromtimestamp(pend[0].stat().st_mtime) if pend else None
    stages.append(_stage(
        "gate", "人工闸门", "pending待确认单，confirm时重跑风控",
        "warn" if pend else "ok",
        "%d 单待人工确认" % len(pend) if pend else "无待确认单", _iso(p_ts)))

    # ⑨ 执行
    tr = q_one(conn, "SELECT COUNT(*) AS n, MAX(created_at) AS ts FROM trade WHERE trade_date=?",
               (run_date,))
    stages.append(_stage(
        "exec", "模拟执行", "PaperBroker成交(T+1)+回读校验",
        "ok" if tr["n"] else ("idle" if not decs else "warn"),
        "%d 笔成交" % tr["n"] if tr["n"] else "当日无成交", tr["ts"]))

    # ⑩ 复盘
    rep = _srv.REPORTS_DIR / (str(run_date) + ".md")
    rts = datetime.fromtimestamp(rep.stat().st_mtime) if rep.is_file() else None
    ps = q_one(conn, "SELECT total, drawdown FROM portfolio_state WHERE date=?", (run_date,))
    stages.append(_stage(
        "review", "盘后复盘", "盯市+日报+归因",
        "ok" if rts else "idle",
        "日报 %s；总资产 %s" % (rts.strftime("%m-%d %H:%M") if rts else "未生成",
                               fnum(ps["total"]) if ps else "—"), _iso(rts)))

    # 决策追踪链（批量查询 O(1)：此前每条 trace 做 2 次子查询，30s 轮询下是主要 DB 开销）
    ev_by = repo.events_by_decisions(conn, d_ids)
    tr_by = repo.trade_by_decisions(conn, d_ids)
    traces: List[dict] = []
    for d in decs:
        traces.append({
            "id": d["id"], "code": d["code"], "action": d["action"],
            "target_weight": d["target_weight"], "confidence": d["confidence"],
            "status": d["status"], "created_at": d["created_at"],
            "reasons": parse_json_field(d["reasons"], []),
            "risk_events": ev_by.get(d["id"], []),
            "trade": tr_by.get(d["id"]),
            "pending": (_srv.ORDERS_DIR / str(run_date) / ("pending_%d.json" % d["id"])).is_file(),
        })
    ks = q_one(conn, "SELECT kill_switch, drawdown FROM portfolio_state "
                     "WHERE date=(SELECT MAX(date) FROM portfolio_state)")
    return {"run_date": run_date, "generated_at": _iso(datetime.now()),
            "stages": stages, "traces": traces,
            "kill_switch": bool(ks and ks["kill_switch"]),
            "health_issues": issues}

