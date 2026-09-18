"""复盘层·每日：组合盯市 mark_to_market、当日损益 portfolio_pnl、每日复盘报告 generate_daily_report。

口径说明（重要）：
- 现金还原：cash = paper_start_cash - Σ买入amount + Σ卖出amount（全部有效成交流水，
  status 为 rejected/cancelled/pending 的记录不计；假定 trade.amount 已含费用）。
  无任何成交且无持仓时 cash = paper_start_cash（1000000）。
  持仓表有值但无任何成交流水时（手工造数场景），cash = paper_start_cash - Σ成本×持股。
- 盯市：市值 = Σ shares × 当日收盘；无当日收盘则用该票最新可得收盘并在 note/报告中注明日期。
- 回撤：drawdown = max(0, 1 - total / portfolio_state 历史 peak(total))（历史为空则 0；
  创新高时按惯例记 0）。
- 当日盈亏：优先 portfolio_state 相邻两日 total 之差；缺上日状态时按
  "期初持股 × (当日收盘 - 前收盘)" 还原，其中期初持股 = 当前持股 - 当日买入 + 当日卖出，
  即剔除 trade_date 当日成交对持股数的影响（简化口径，适用于以最新交易日为当日的复盘）。
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import os
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from common.config import snapshot
from data import repo
from data.fetcher import get_conn

CFG = snapshot()  # 统一配置层：import 期冻结 + 硬键校验 fail-fast
START_CASH = float(CFG.get("execution", {}).get("paper_start_cash", 1000000.0))

# 不影响资金/持股还原的成交状态（未成交、已撤单）


# ---------------------------------------------------------------- 基础工具

def today_str() -> str:
    return date.today().isoformat()


def latest_trade_date(conn: sqlite3.Connection) -> str:
    """daily_bar 中最新交易日（周末/节假日跑报告时避免拿到空数据）。"""
    return repo.latest_trade_date(conn) or today_str()


def _close_on_or_before(conn: sqlite3.Connection, code: str, trade_date: str) -> Optional[Tuple[str, float, Optional[float]]]:
    """取 code 在 trade_date（含）之前最新收盘；完全没有则取其后最早可得（并让调用方注明滞后）。

    返回 (bar_date, close, pct_chg) 或 None。
    """
    row = conn.execute(
        "SELECT trade_date, close, pct_chg FROM daily_bar "
        "WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
        (code, trade_date),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT trade_date, close, pct_chg FROM daily_bar "
            "WHERE code=? AND trade_date>? ORDER BY trade_date ASC LIMIT 1",
            (code, trade_date),
        ).fetchone()
    if row is None or row[1] is None:
        return None
    return (row[0], float(row[1]), None if row[2] is None else float(row[2]))


def _fmt_money(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:,.2f}"


def _fmt_pct(v: Optional[float]) -> str:
    """v 为小数比例（0.01 -> +1.00%）。"""
    return "n/a" if v is None else f"{v * 100:+.2f}%"


def _parse_json_list(raw: Optional[str]) -> List[str]:
    """decision.reasons / risk_notes 为 JSON 数组串；解析失败按 markdown bullet 拆行（日报渲染增强）。"""
    return repo.parse_json_list(raw, split_bullets=True)


def _connect(conn: Optional[sqlite3.Connection]) -> Tuple[sqlite3.Connection, bool]:
    """conn 为 None 时自建真实库连接（调用方负责关闭）。"""
    if conn is None:
        return get_conn(), True
    return conn, False


# ---------------------------------------------------------------- 核心计算

def mark_to_market(trade_date: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
    """按持仓 × 当日收盘盯市，落库 portfolio_state 当日行并返回快照。

    - trade_date 为 None 时取 daily_bar 最新交易日。
    - 现金/回撤/当日盈亏口径见模块 docstring。
    - kill_switch 沿用当日已有值（无则 0）；note 记录价格滞后等信息。
    """
    conn, own = _connect(conn)
    try:
        if trade_date is None:
            trade_date = latest_trade_date(conn)

        rows = conn.execute(
            "SELECT code, name, shares, cost FROM position WHERE shares > 0 ORDER BY code"
        ).fetchall()

        positions: List[Dict[str, Any]] = []
        market_value = 0.0
        stale_notes: List[str] = []
        for code, name, shares, cost in rows:
            shares = int(shares)
            info = _close_on_or_before(conn, code, trade_date)
            if info is None:
                stale_notes.append(f"{code}:无任何行情")
                close, bar_date, pct_chg = None, None, None
                mv = 0.0
            else:
                bar_date, close, pct_chg = info
                mv = shares * close
                if bar_date != trade_date:
                    stale_notes.append(f"{code}@{bar_date}")
            market_value += mv
            positions.append({
                "code": code, "name": name or "", "shares": shares,
                "cost": float(cost) if cost is not None else None,
                "close": close, "bar_date": bar_date, "pct_chg": pct_chg,
                "market_value": mv,
                "float_pnl": None if close is None or cost is None else shares * (close - float(cost)),
                "stale": bar_date is None or bar_date != trade_date,
            })

        # ---- 现金还原 ----
        flow_map: Dict[str, float] = repo.cash_flows(conn, as_of=trade_date)
        has_flows = ("buy" in flow_map) or ("sell" in flow_map)
        if has_flows:
            cash = START_CASH - flow_map.get("buy", 0.0) + flow_map.get("sell", 0.0)
        elif rows:
            cash = START_CASH - sum(int(r[2]) * float(r[3] or 0.0) for r in rows)
        else:
            cash = START_CASH

        total = cash + market_value

        # ---- 回撤（历史 peak）----
        # W-A3⑤：峰值统一走 runner.effective_peak（dd_base 感知 + 250 行窗口 +
        # clamp），与引擎/midday/intraday 同一定径——避免"报表说破线、引擎不 kill"
        # 的口径分叉（此前 daily 用全史峰值、引擎用 250 行窗口，两套数字）。
        from execution.runner import effective_peak as _effective_peak
        peak = _effective_peak(conn, before=trade_date)
        drawdown = 0.0
        if peak is not None and float(peak) > 0:
            drawdown = max(0.0, 1.0 - total / float(peak))

        # ---- 上一交易日 total ----
        prev_row = conn.execute(
            "SELECT date, total FROM portfolio_state WHERE date < ? ORDER BY date DESC LIMIT 1",
            (trade_date,),
        ).fetchone()
        prev_total = float(prev_row[1]) if prev_row and prev_row[1] is not None else None

        # ---- kill_switch 沿用当日已有值 ----
        ks_row = conn.execute(
            "SELECT kill_switch FROM portfolio_state WHERE date=?", (trade_date,)
        ).fetchone()
        kill_switch = int(ks_row[0]) if ks_row and ks_row[0] is not None else 0

        note_parts = [f"mark_to_market@{datetime.now().isoformat(timespec='seconds')}"]
        if stale_notes:
            note_parts.append("价格滞后:" + ",".join(stale_notes))
        else:
            note_parts.append(f"价格日期={trade_date}")
        note = "; ".join(note_parts)

        conn.execute(
            "INSERT OR REPLACE INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
            (trade_date, cash, market_value, total, drawdown, kill_switch, note),
        )
        conn.commit()

        return {
            "trade_date": trade_date,
            "cash": cash,
            "market_value": market_value,
            "total": total,
            "drawdown": drawdown,
            "kill_switch": kill_switch,
            "prev_total": prev_total,
            "peak": float(peak) if peak is not None else None,
            "positions": positions,
            "stale_codes": stale_notes,
            "note": note,
        }
    finally:
        if own:
            conn.close()


def _per_code_day_pnl(conn: sqlite3.Connection, trade_date: str) -> Dict[str, Optional[float]]:
    """简化口径逐票当日贡献：期初持股 × (当日收盘 - 前收盘)。

    期初持股 = 当前持股 - 当日买入 + 当日卖出（剔除当日成交影响）。
    """
    out: Dict[str, Optional[float]] = {}
    for code, shares in conn.execute(
        "SELECT code, shares FROM position WHERE shares > 0"
    ).fetchall():
        shares = int(shares)
        buy_sh = repo.day_side_shares(conn, trade_date, code, "buy")
        sell_sh = repo.day_side_shares(conn, trade_date, code, "sell")
        shares_prev = shares - int(buy_sh) + int(sell_sh)

        cur = _close_on_or_before(conn, code, trade_date)
        prev = conn.execute(
            "SELECT close FROM daily_bar WHERE code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
            (code, trade_date),
        ).fetchone()
        if cur is None or prev is None or cur[0] != trade_date:
            out[code] = None  # 当日无收盘，无法计算
            continue
        out[code] = shares_prev * (float(cur[1]) - float(prev[0]))
    return out


def portfolio_pnl(trade_date: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    """组合当日盈亏。

    - method='portfolio_state_diff'：当日 total - 上一交易日 total（portfolio_state）。
    - method='reconstructed_from_prev_close'：缺上日状态时，按
      Σ 期初持股 × (当日收盘 - 前收盘) 自行推算；期初持股已剔除当日买卖影响（见 docstring）。
    - 当日无 portfolio_state 行时会先调用 mark_to_market 生成（会写库）。
    """
    conn, own = _connect(conn)
    try:
        row = conn.execute(
            "SELECT total FROM portfolio_state WHERE date=?", (trade_date,)
        ).fetchone()
        if row is None or row[0] is None:
            row = (mark_to_market(trade_date, conn)["total"],)
        total = float(row[0])

        prev = conn.execute(
            "SELECT date, total FROM portfolio_state WHERE date<? ORDER BY date DESC LIMIT 1",
            (trade_date,),
        ).fetchone()

        per_code = _per_code_day_pnl(conn, trade_date)

        if prev and prev[1] is not None:
            return {
                "trade_date": trade_date,
                "total": total,
                "prev_date": prev[0],
                "prev_total": float(prev[1]),
                "day_pnl": total - float(prev[1]),
                "method": "portfolio_state_diff",
                "per_code": per_code,
            }
        reconstructed = sum(v for v in per_code.values() if v is not None)
        return {
            "trade_date": trade_date,
            "total": total,
            "prev_date": None,
            "prev_total": None,
            "day_pnl": reconstructed,
            "method": "reconstructed_from_prev_close",
            "per_code": per_code,
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 决策结果回填（闭环）

def backfill_decision_outcomes(conn: sqlite3.Connection,
                               as_of: Optional[str] = None) -> int:
    """回填 decision.t1_ret / direction_hit（决策→结果闭环，此前完全缺失）。

    对 trade_date < as_of 且 t1_ret 为空的 buy/sell 决策：
    - t1_ret = 决策交易日后首个有行情日的 close 相对决策日最新收盘的涨跌
      （用 close_qfq 优先，除权不误判方向）；
    - direction_hit：buy 且 t1_ret>0 → 1；sell 且 t1_ret<0 → 1；否则 0。
    返回回填条数。盘后流水线每个交易日调用一次。
    """
    as_of = as_of or latest_trade_date(conn)
    rows = conn.execute(
        "SELECT id, trade_date, code, action FROM decision "
        "WHERE trade_date IS NOT NULL AND trade_date < ? AND t1_ret IS NULL "
        "AND action IN ('buy','sell')", (as_of,)).fetchall()
    filled = 0
    for did, tdate, code, action in rows:
        base = conn.execute(
            "SELECT trade_date, COALESCE(close_qfq, close) FROM daily_bar "
            "WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
            (code, tdate)).fetchone()
        nxt = conn.execute(
            "SELECT trade_date, COALESCE(close_qfq, close) FROM daily_bar "
            "WHERE code=? AND trade_date>? ORDER BY trade_date ASC LIMIT 1",
            (code, tdate)).fetchone()
        if not base or not nxt or not base[1] or not nxt[1]:
            continue  # 行情未齐，下个交易日再试
        ret = float(nxt[1]) / float(base[1]) - 1.0
        hit = 1 if ((action == "buy" and ret > 0) or (action == "sell" and ret < 0)) else 0
        conn.execute("UPDATE decision SET t1_ret=?, direction_hit=? WHERE id=?",
                     (round(ret, 6), hit, did))
        filled += 1
    conn.commit()
    return filled


# ---------------------------------------------------------------- 报告生成

def _sec_decisions(conn: sqlite3.Connection, trade_date: str) -> str:
    rows = conn.execute(
        "SELECT id, code, action, target_weight, confidence, reasons, status, created_at, "
        "t1_ret, direction_hit, model, prompt_version "
        "FROM decision WHERE run_date=? ORDER BY id",
        (trade_date,),
    ).fetchall()
    if not rows:
        return "暂无数据"
    lines = [
        "| id | 代码 | 动作 | 目标权重 | 置信度 | 状态 | 次日实际 | 方向 | 生成时间 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for did, code, action, tw, conf, reasons, status, created, t1r, hit, model, pv in rows:
        hit_s = "n/a" if hit is None else ("✓" if int(hit) == 1 else "✗")
        lines.append(
            f"| {did} | {code} | {action} | {_fmt_pct(tw)} | "
            f"{'n/a' if conf is None else f'{float(conf):.2f}'} | {status or 'n/a'} | "
            f"{_fmt_pct(t1r)} | {hit_s} | {created or 'n/a'} |"
        )
    lines.append("")
    lines.append("**决策理由（reasons 原文）**")
    for did, code, action, tw, conf, reasons, status, created, t1r, hit, model, pv in rows:
        lines.append(f"- decision#{did} {code} {action}:")
        items = _parse_json_list(reasons)
        if items:
            lines.extend(f"  - {it}" for it in items)
        else:
            lines.append("  - （未填写理由）")
    return "\n".join(lines)


def _sec_trades(conn: sqlite3.Connection, trade_date: str) -> str:
    rows = conn.execute(
        "SELECT created_at, code, name, side, price, shares, amount, status, confirmed_by "
        "FROM trade WHERE trade_date=? ORDER BY created_at, id",
        (trade_date,),
    ).fetchall()
    if not rows:
        return "暂无数据"
    lines = [
        "| 时间 | 代码 | 名称 | 方向 | 价格 | 数量 | 金额 | 状态 | 确认人 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for created, code, name, side, price, shares, amount, status, confirmed in rows:
        t = (created or "")[-8:]
        lines.append(
            f"| {t} | {code} | {name or ''} | {'买入' if side == 'buy' else '卖出'} "
            f"| {_fmt_money(price)} | {shares} | {_fmt_money(amount)} | {status or 'n/a'} | {confirmed or ''} |"
        )
    return "\n".join(lines)


def _sec_positions(conn: sqlite3.Connection, trade_date: str) -> str:
    st = mark_to_market(trade_date, conn)
    pnl = portfolio_pnl(trade_date, conn)
    lines: List[str] = []
    if not st["positions"]:
        lines.append("暂无数据")
    else:
        lines.append("| 代码 | 名称 | 持股数 | 成本 | 最新收盘(日期) | 当日涨跌幅 | 持仓市值 | 浮动盈亏 | 当日贡献 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for p in st["positions"]:
            if p["close"] is None:
                close_s = "n/a"
                pct_s = "n/a"
            else:
                close_s = f"{p['close']:.2f} ({p['bar_date']})"
                pct_s = "n/a" if p["pct_chg"] is None else f"{p['pct_chg']:+.2f}%"
            contrib = pnl["per_code"].get(p["code"])
            lines.append(
                f"| {p['code']} | {p['name']} | {p['shares']} | {_fmt_money(p['cost'])} "
                f"| {close_s} | {pct_s} | {_fmt_money(p['market_value'])} "
                f"| {_fmt_money(p['float_pnl'])} | {_fmt_money(contrib)} |"
            )
    lines.append("")
    lines.append(f"- 组合当日盈亏合计：**{_fmt_money(pnl['day_pnl'])}**"
                 f"（口径：{pnl['method']}，{'上日 ' + str(pnl['prev_date']) + ' total=' + _fmt_money(pnl['prev_total']) if pnl['prev_total'] is not None else '无上日状态，按前收盘×期初持股还原，当日成交已剔除'}）")
    cum = st["total"] / START_CASH - 1.0
    lines.append(f"- 组合总资产：{_fmt_money(st['total'])}（现金 {_fmt_money(st['cash'])} + 市值 {_fmt_money(st['market_value'])}）")
    lines.append(f"- 累计收益率：{_fmt_pct(cum)}（期初资金 {_fmt_money(START_CASH)}）")
    lines.append(f"- 当前回撤：{_fmt_pct(st['drawdown'])}；kill_switch：{st['kill_switch']}")
    if st["stale_codes"]:
        lines.append(f"- 注意：以下标的未用到 {trade_date} 收盘价（价格滞后）：{'、'.join(st['stale_codes'])}")
    return "\n".join(lines)


def _sec_benchmark(conn: sqlite3.Connection, trade_date: str, day_pnl: Optional[float], prev_total: Optional[float]) -> str:
    cur = conn.execute(
        "SELECT trade_date, close FROM index_daily WHERE index_code='000300' AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 1",
        (trade_date,),
    ).fetchone()
    prev = conn.execute(
        "SELECT trade_date, close FROM index_daily WHERE index_code='000300' AND trade_date<? "
        "ORDER BY trade_date DESC LIMIT 1",
        (trade_date,),
    ).fetchone()
    if cur is None or prev is None:
        return "基准数据缺失（index_daily 无 '000300' 当日/前日数据，可运行 review/weekly.py 自动补齐）"
    # W-B2（Sprint4，P0-5）：基准滞后守卫——index_daily 缺当日行时 cur/prev 塌缩为
    # 同一行，bench_ret≡0 被渲染成"当日 +0.00%"（09-16 实报 +0.68% 被写成 0.00%）。
    # 基准不是当日 → 显式标注滞后日期；相对收益仅在基准两端同日时输出。
    if cur[0] != trade_date:
        lines = [
            f"- 基准滞后（最新 {cur[0]}，无 {trade_date} 收盘）："
            f"{float(prev[1]):.2f}（{prev[0]}）→ {float(cur[1]):.2f}（{cur[0]}）",
            "- 当日相对收益：n/a（基准非当日，错日相减已禁止；请补齐 index_daily）",
        ]
        return "\n".join(lines)
    bench_ret = float(cur[1]) / float(prev[1]) - 1.0
    lines = [
        f"- 沪深300收盘：{float(prev[1]):.2f}（{prev[0]}）→ {float(cur[1]):.2f}（{cur[0]}），当日 {_fmt_pct(bench_ret)}",
    ]
    if prev_total is None or float(prev_total) <= 0 or day_pnl is None:
        lines.append("- 组合当日收益率：n/a（无上日总资产，无法计算相对收益）")
    else:
        port_ret = float(day_pnl) / float(prev_total)
        lines.append(f"- 组合当日收益率：{_fmt_pct(port_ret)}")
        lines.append(f"- 当日相对收益（组合-基准）：**{_fmt_pct(port_ret - bench_ret)}**")
    return "\n".join(lines)


def _sec_self_review(conn: sqlite3.Connection, trade_date: str) -> str:
    rows = conn.execute(
        "SELECT id, code, action, risk_notes FROM decision WHERE run_date=? ORDER BY id",
        (trade_date,),
    ).fetchall()
    if not rows:
        return "当日无决策"
    lines: List[str] = []
    for did, code, action, risk_notes in rows:
        items = _parse_json_list(risk_notes)
        lines.append(f"- decision#{did} {code} {action} risk_notes 原文：")
        if items:
            lines.extend(f"  - {it}" for it in items)
        else:
            lines.append("  - （该决策未填写风险提示）")
    return "\n".join(lines)


def generate_daily_report(trade_date: Optional[str] = None,
                          conn: Optional[sqlite3.Connection] = None,
                          out_dir: Optional[Path] = None) -> Path:
    """生成 logs/reports/YYYY-MM-DD.md 每日复盘报告，返回报告路径。空表优雅降级，不抛异常。"""
    conn, own = _connect(conn)
    try:
        if trade_date is None:
            trade_date = latest_trade_date(conn)
        pnl = portfolio_pnl(trade_date, conn)

        sections: List[Tuple[str, str]] = []
        for title, fn in [
            ("决策回顾", lambda: _sec_decisions(conn, trade_date)),
            ("成交明细", lambda: _sec_trades(conn, trade_date)),
            ("持仓与当日盈亏", lambda: _sec_positions(conn, trade_date)),
            ("基准对比", lambda: _sec_benchmark(conn, trade_date, pnl.get("day_pnl"), pnl.get("prev_total"))),
            ("AI自我评估", lambda: _sec_self_review(conn, trade_date)),
        ]:
            try:
                sections.append((title, fn()))
            except Exception as e:  # 单小节失败不影响整篇报告
                sections.append((title, f"（该小节生成出错：{type(e).__name__}: {e}）"))

        lines = [
            f"# 每日复盘报告 {trade_date}",
            "",
            f"> 生成时间：{datetime.now().isoformat(timespec='seconds')}｜期初资金：{_fmt_money(START_CASH)}",
            "",
        ]
        for title, body in sections:
            lines.append(f"## {title}")
            lines.append("")
            lines.append(body)
            lines.append("")

        # 报告目录调用时读 env（测试隔离 AGSICKLE_REPORTS_DIR；与 fetcher.AGSICKLE_DB 同模式）
        target_dir = Path(out_dir) if out_dir is not None else Path(
            os.environ.get("AGSICKLE_REPORTS_DIR") or (BASE / "logs" / "reports"))
        target_dir.mkdir(parents=True, exist_ok=True)
        # P0 修复：trade_date 早于今日时绝不覆写昨日日报，写 PENDING 兜底；
        # postclose 已做此校验，这里是兜底保护（其它入口如 catchup 调到这里）。
        today_iso = date.today().isoformat()
        if trade_date < today_iso:
            pending_path = target_dir / f"PENDING-{today_iso}.md"
            pending_path.write_text(
                "# 待清算日报 %s\n\n"
                "> generate_daily_report 检测到 trade_date=%s < today=%s\n\n"
                "- 为避免覆写历史日报，已改写本兜底文件。\n"
                "- 跑 `python -m pipeline.catchup --date %s` 补数据后再重跑 postclose。\n"
                % (today_iso, trade_date, today_iso, today_iso),
                encoding="utf-8")
            return pending_path
        path = target_dir / f"{trade_date}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="A股AI模拟交易 · 每日复盘报告")
    parser.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD（默认 daily_bar 最新交易日）")
    args = parser.parse_args(argv)

    conn = get_conn()
    try:
        td = args.date or latest_trade_date(conn)
        st = mark_to_market(td, conn)
        path = generate_daily_report(td, conn)
    finally:
        conn.close()

    print(f"[daily] 交易日={st['trade_date']} 现金={st['cash']:,.2f} 市值={st['market_value']:,.2f} "
          f"总资产={st['total']:,.2f} 回撤={st['drawdown']:.2%} kill_switch={st['kill_switch']}")
    print(f"[daily] 报告已生成: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
