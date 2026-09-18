"""数据访问收敛层（结构性重构 Phase 4，docs/结构性重构实施方案.md）。

只依赖 stdlib（sqlite3/contextlib/typing），不 import 任何项目模块——连接由调用方
传入，与 fetcher/engine/paper 等所有潜在导入方无循环。

事务红线（改动本模块或调用方前必读）：
1. repo 函数**一律不 commit**，事务控制权留给调用方；
2. risk_event.record_event **保持自提交**（risk/engine.py，19 个调用点依赖
   「拒绝路径留痕后 raise」——若改为不提交，raise 时回滚会连留痕一起丢）；
3. paper.buy/sell 的「position+trade 单事务」原子性不得拆；
4. runner._execute 的「成交/状态两段提交」恢复语义不得合并；
5. decide.save 批量一 commit vs runner 逐条 commit 的粒度差异保留现状。

返回值保持被替换处现状形状（ADR #3）：标量 / tuple / sqlite3.Row（要求连接
row_factory=sqlite3.Row——fetcher.get_conn 已统一；Row 同时支持 row[0] 与 row["name"]，
对既有 tuple 索引风格向后兼容）。JSON 解析器 runner/daily 两份 list 语义合一为
parse_json_list；webapp.parse_json_field 的通用 fallback 语义不同，保留在原处。

不进 repo（方案 §3 Phase 4）：fetcher 的 DDL/_MIGRATIONS（表结构单一事实源，tests
直接 import）、fetcher 采集型 upsert、webapp 一次性聚合展示查询（api_health/
api_data_status/equity_curve）、signals/macro/trade_cal/universe800/news 领域写入
（二期）、kill 查询主体。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional

# 「有效成交」过滤的唯一定义（此前 paper._EFFECTIVE / daily._TRADE_OK / runner 内联三份）
TRADE_EFFECTIVE_SQL = ("(status IS NULL OR status NOT IN "
                       "('rejected','cancelled','canceled','pending'))")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """事务上下文：块内成功 commit、异常 rollback。仅新代码使用，旧代码不动。"""
    with conn:
        yield conn


# ---------------------------------------------------------------- trade 域

def cash_flows(conn: sqlite3.Connection, as_of: Optional[str] = None) -> Dict[str, float]:
    """有效成交的现金净流合计 {side: amount}（buy 为流出额、sell 为流入额）。

    as_of=None 时全史口径（paper.cash）；指定日期时含当日（daily.mark_to_market
    的现金还原口径 trade_date<=as_of）。
    """
    sql = ("SELECT side, COALESCE(SUM(amount), 0.0) FROM trade "
           "WHERE side IN ('buy','sell') AND " + TRADE_EFFECTIVE_SQL)
    args: tuple = ()
    if as_of is not None:
        sql += " AND trade_date<=?"
        args = (as_of,)
    sql += " GROUP BY side"
    return {str(side): float(amt) for side, amt in conn.execute(sql, args).fetchall()}


def has_effective_trade(conn: sqlite3.Connection, decision_id: int) -> bool:
    """该决策是否已有有效成交（幂等拒绝判据）。"""
    row = conn.execute(
        "SELECT 1 FROM trade WHERE decision_id=? AND " + TRADE_EFFECTIVE_SQL + " LIMIT 1",
        (decision_id,)).fetchone()
    return row is not None


def insert_trade(conn: sqlite3.Connection, *, trade_date: str, code: str, name: str,
                 side: str, price: float, shares: int, amount: float,
                 order_id: str, status: str = "filled",
                 decision_id: Optional[int] = None, shots: str = "[]",
                 confirmed_by: str = "", created_at: Optional[str] = None) -> int:
    """成交落库，返回新 trade id。不 commit（红线 3/4：事务归属调用方）。

    created_at 缺省取当前时刻（isoformat 秒级，与迁移前格式逐字节一致）。
    """
    from datetime import datetime
    cur = conn.execute(
        "INSERT INTO trade (trade_date, code, name, side, price, shares, amount,"
        " order_id, status, decision_id, shots, confirmed_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_date, code, name, side, float(price), int(shares), float(amount),
         order_id, status, decision_id, shots, confirmed_by,
         created_at or datetime.now().isoformat(timespec="seconds")))
    return int(cur.lastrowid)


def effective_trades(conn: sqlite3.Connection, code: str) -> List[tuple]:
    """单票全部有效成交（trade_date, side, price, shares, amount, status），按 id 序。

    供 paper.readback 的流水重放校验（重放语义 = 现状，不改）。
    """
    return conn.execute(
        "SELECT trade_date, side, price, shares, amount, status FROM trade"
        " WHERE code=? AND " + TRADE_EFFECTIVE_SQL + " ORDER BY id", (code,)).fetchall()


def sold_shares(conn: sqlite3.Connection, code: str) -> int:
    """单票累计有效卖出股数。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(shares),0) FROM trade WHERE code=? AND side='sell' AND "
        + TRADE_EFFECTIVE_SQL, (code,)).fetchone()
    return int(row[0])


def day_side_shares(conn: sqlite3.Connection, trade_date: str, code: str,
                    side: str) -> int:
    """单票单日单方向有效成交股数合计（daily._per_code_day_pnl 的 T+1 可卖基数）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(shares),0) FROM trade WHERE trade_date=? AND code=? "
        "AND side=? AND " + TRADE_EFFECTIVE_SQL, (trade_date, code, side)).fetchone()
    return int(row[0])


# ---------------------------------------------------------------- daily_bar 域

def latest_trade_date(conn: sqlite3.Connection) -> Optional[str]:
    """全库最新交易日（8 处独立实现收敛于此）。"""
    row = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()
    return row[0] if row and row[0] else None


def latest_dates_by_code(conn: sqlite3.Connection) -> Dict[str, str]:
    """每票最新交易日 {code: date}（新鲜度统计 / 全市场口径）。"""
    return {str(r[0]): str(r[1]) for r in conn.execute(
        "SELECT code, MAX(trade_date) FROM daily_bar GROUP BY code").fetchall()}


def latest_bar_date(conn: sqlite3.Connection, code: str,
                    qfq_only: bool = False) -> Optional[str]:
    """单票最新 bar 日期（增量拉取起点判据）；qfq_only 时只看已回填复权的行。"""
    sql = "SELECT MAX(trade_date) FROM daily_bar WHERE code=?"
    if qfq_only:
        sql += " AND close_qfq IS NOT NULL"
    row = conn.execute(sql, (code,)).fetchone()
    return row[0] if row and row[0] else None


def latest_close(conn: sqlite3.Connection, code: str,
                 offset: int = 0) -> Optional[float]:
    """单票最新收盘（offset=1 为次新 bar——paper.prev_close 的取法）。"""
    row = conn.execute(
        "SELECT close FROM daily_bar WHERE code=? ORDER BY trade_date DESC LIMIT 1 OFFSET ?",
        (code, int(offset))).fetchone()
    return float(row[0]) if row and row[0] is not None else None


# ---------------------------------------------------------------- portfolio_state 域

def latest_state(conn: sqlite3.Connection):
    """最新盯市快照行（Row：date/cash/market_value/total/drawdown/kill_switch/note）。"""
    return conn.execute(
        "SELECT date, cash, market_value, total, drawdown, kill_switch, note "
        "FROM portfolio_state ORDER BY date DESC LIMIT 1").fetchone()


def state_on(conn: sqlite3.Connection, date: str):
    """指定交易日的盯市快照行，无则 None。"""
    return conn.execute(
        "SELECT date, cash, market_value, total, drawdown, kill_switch, note "
        "FROM portfolio_state WHERE date=?", (date,)).fetchone()


def has_state(conn: sqlite3.Connection, date: str) -> bool:
    """指定交易日是否已有盯市行（catchup 缺日补跑判据）。"""
    return conn.execute(
        "SELECT 1 FROM portfolio_state WHERE date=?", (date,)).fetchone() is not None


def peak_total(conn: sqlite3.Connection, before: Optional[str] = None,
               window: Optional[int] = None) -> Optional[float]:
    """历史总资产峰值（回撤口径），三参数化合一：
    - before=日期：只看该日之前（daily.mark_to_market 的当日回撤基准）；
    - window=N：只看最近 N 行（runner.build_context 的 250 行窗口——一条坏数据
      不再永久抬高峰值）；
    - 两者都不传：全史峰值（runner 重置杀峰值用）。
    """
    if window is not None:
        row = conn.execute(
            "SELECT MAX(total) FROM (SELECT total FROM portfolio_state "
            "ORDER BY date DESC LIMIT ?)", (int(window),)).fetchone()
    elif before is not None:
        row = conn.execute(
            "SELECT MAX(total) FROM portfolio_state WHERE date < ?", (before,)).fetchone()
    else:
        row = conn.execute("SELECT MAX(total) FROM portfolio_state").fetchone()
    return float(row[0]) if row and row[0] is not None else None


# ---------------------------------------------------------------- decision 域

def insert_decision(conn: sqlite3.Connection, decision: dict, run_date: str,
                    *, status: str = "proposed", input_snapshot: Optional[str] = None,
                    trade_date: Optional[str] = None, model: Optional[str] = None,
                    prompt_version: Optional[str] = None,
                    created_at: Optional[str] = None) -> int:
    """决策落库，返回新 id。不 commit（红线 5：粒度差异由调用方保留）。

    runner.propose（逐条 commit）与 decide.save（批量 commit）此前两份 INSERT
    列不一致——此处对齐为全列可选：trade_date/model/prompt_version 缺省 NULL。
    input_snapshot 缺省取决策 JSON 全文（runner 语义）。
    """
    cur = conn.execute(
        "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
        " reasons, risk_notes, input_snapshot, status, created_at,"
        " trade_date, model, prompt_version, emergency_scan)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_date, decision.get("code"), decision.get("action"),
         decision.get("target_weight"), decision.get("confidence"),
         _dumps(decision.get("reasons", [])), _dumps(decision.get("risk_notes", [])),
         input_snapshot if input_snapshot is not None else _dumps(decision),
         status, created_at or datetime.now().isoformat(timespec="seconds"),
         trade_date, model, prompt_version,
         1 if decision.get("emergency_scan") else 0))
    return int(cur.lastrowid)


def get_decision_row(conn: sqlite3.Connection, decision_id: int):
    """decision 行（10 列，与 runner._get_decision 现状一致），无则 None。"""
    return conn.execute(
        "SELECT id, run_date, code, action, target_weight, confidence, reasons,"
        " risk_notes, input_snapshot, status FROM decision WHERE id=?",
        (decision_id,)).fetchone()


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def parse_json_list(raw, split_bullets: bool = False) -> List[str]:
    """decision.reasons / risk_notes 的 JSON 数组串 → 字符串列表。

    - 解析失败退化为 [str(raw)]；split_bullets=True（日报渲染专用）时再按
      markdown bullet 逐行拆分；
    - webapp.parse_json_field 的通用 fallback 语义与此不同（非 list 强制），
      保留在 server 原处。
    """
    if raw is None or str(raw).strip() == "":
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        text = str(raw)
        if split_bullets:
            lines = [ln.strip("-• ").strip() for ln in text.splitlines() if ln.strip()]
            return lines or [text]
        return [text]
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


# ---------------------------------------------------------------- stock_info 域

def all_codes(conn: sqlite3.Connection) -> List[str]:
    """全部自选/已入库票代码（止损自检、动态池等 8 处收敛）。"""
    return [str(r[0]) for r in
            conn.execute("SELECT code FROM stock_info").fetchall()]


def name_map(conn: sqlite3.Connection) -> Dict[str, str]:
    """{code: name} 全量映射。"""
    return {str(r[0]): str(r[1] or "") for r in
            conn.execute("SELECT code, name FROM stock_info").fetchall()}


def stock_names(conn: sqlite3.Connection, codes: List[str]) -> Dict[str, str]:
    """批量查票名 {code: name}（缺失码不出现键；N+1 备用批量版）。"""
    if not codes:
        return {}
    ph = ",".join("?" * len(codes))
    return {str(r[0]): str(r[1] or "") for r in conn.execute(
        "SELECT code, name FROM stock_info WHERE code IN (%s)" % ph,
        tuple(codes)).fetchall()}


# ---------------------------------------------------------------- risk_event / trace 域（只读）

def events_by_decisions(conn: sqlite3.Connection,
                        decision_ids: List[int]) -> Dict[int, List[dict]]:
    """批量取决策关联风控事件 {decision_id: [{ts,rule,detail}]}（api_workflow N+1 消除）。

    返回 dict 形状——保持被替换处 q_all 的现状（api 响应 JSON 直接序列化，ADR #3）。
    """
    if not decision_ids:
        return {}
    ph = ",".join("?" * len(decision_ids))
    out: Dict[int, List[dict]] = {}
    for r in conn.execute(
            "SELECT ts, rule, detail, decision_id FROM risk_event "
            "WHERE decision_id IN (%s) ORDER BY ts" % ph,
            tuple(decision_ids)).fetchall():
        out.setdefault(int(r["decision_id"]), []).append(
            {"ts": r["ts"], "rule": r["rule"], "detail": r["detail"]})
    return out


def trade_by_decisions(conn: sqlite3.Connection,
                       decision_ids: List[int]) -> Dict[int, dict]:
    """批量取决策首笔成交 {decision_id: dict}（每决策 LIMIT 1 语义保留：取 id 最小）。

    dict 形状保持被替换处 q_one 的现状（ADR #3）。
    """
    if not decision_ids:
        return {}
    ph = ",".join("?" * len(decision_ids))
    out: Dict[int, dict] = {}
    for r in conn.execute(
            "SELECT id, side, price, shares, amount, status, confirmed_by, decision_id "
            "FROM trade WHERE decision_id IN (%s) ORDER BY id" % ph,
            tuple(decision_ids)).fetchall():
        did = int(r["decision_id"])
        if did not in out:  # ORDER BY id：首笔即保留
            out[did] = {k: r[k] for k in ("id", "side", "price", "shares", "amount",
                                          "status", "confirmed_by")}
    return out


# ---------------------------------------------------------------- minute_snapshot 域（C-ARC-3b/T7，只读）

def get_minute_series(conn: sqlite3.Connection, code: str, date_str: str) -> List[tuple]:
    """单票单日分钟快照序列 [(ts, price, volume, amount, source)]，ts 升序
    （盘中时点回放读取接口：止损触线时点、尾盘决策、limit_halt 应急的回放/回测消费）。

    空 list = 该票该日无录制数据（录制器未上线前的日期一律如此，调用方自行回退日线）。
    """
    return [tuple(r) for r in conn.execute(
        "SELECT ts, price, volume, amount, source FROM minute_snapshot "
        "WHERE code=? AND ts LIKE ? ORDER BY ts", (str(code), date_str + "%"))]
