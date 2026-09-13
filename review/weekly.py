"""复盘层·每周：自然周组合收益对比沪深300、简单选股/择时归因，生成周度复盘报告（Markdown）。

口径说明（重要）：
- 报告窗口：含 end_date 的自然周（周一 ~ end_date）；默认 end_date = daily_bar 最新交易日。
- 组合周收益 = 期末 total / 基准日 total - 1；基准日优先取自然周之前最后一个有
  portfolio_state 的交易日（通常为上周五），缺失时退化为周内首行并在报告注明。
- 基准周收益同构（index_daily '000300' 同两日收盘之比）。
- 简单归因（简化口径，未还原周内调仓、未计费用）：
    期初权重 w_i = shares_i × close_i(基准日) / total_base（期初持仓以 position 表当前快照近似）
    个票周收益 r_i = close_i(end) / close_i(base) - 1
    选股贡献 = Σ(w_i × r_i) - 基准周收益 × 期初总仓位 Σw_i
    择时贡献 = 组合周收益 - 选股贡献
- ensure_benchmark：index_daily 无 '000300' 近 30 天数据时，用 akshare 抓取补入
  （东财 index_zh_a_hist 优先，新浪 stock_zh_a_index_daily 兜底），失败则报告降级。
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from data.fetcher import get_conn

FETCHER = Callable[[str, str], Any]  # (start_yyyymmdd, end_yyyymmdd) -> DataFrame(date, close)


# ---------------------------------------------------------------- 基础工具

def _to_date(s: str) -> date:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _fmt_money(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:,.2f}"


def _fmt_pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:+.2f}%"


def latest_trade_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()
    return row[0] if row and row[0] else date.today().isoformat()


def _close_on_or_before(conn: sqlite3.Connection, code: str, trade_date: str) -> Optional[Tuple[str, float]]:
    row = conn.execute(
        "SELECT trade_date, close FROM daily_bar WHERE code=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 1",
        (code, trade_date),
    ).fetchone()
    if row is None or row[1] is None:
        return None
    return (row[0], float(row[1]))


def _index_close_on_or_before(conn: sqlite3.Connection, trade_date: str) -> Optional[Tuple[str, float]]:
    row = conn.execute(
        "SELECT trade_date, close FROM index_daily WHERE index_code='000300' AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 1",
        (trade_date,),
    ).fetchone()
    if row is None or row[1] is None:
        return None
    return (row[0], float(row[1]))


# ---------------------------------------------------------------- 基准补数

def _fetch_benchmark_em(start8: str, end8: str):
    """东财沪深300日线（akshare index_zh_a_hist，中文列名）。"""
    import pandas as pd
    import akshare as ak
    df = ak.index_zh_a_hist(symbol="000300", start_date=start8, end_date=end8)
    if df is None or df.empty:
        raise RuntimeError("index_zh_a_hist 返回空")
    date_col = "日期" if "日期" in df.columns else "date"
    close_col = "收盘" if "收盘" in df.columns else "close"
    return pd.DataFrame({"date": df[date_col], "close": df[close_col]})


def _fetch_benchmark_sina(start8: str, end8: str):
    """新浪兜底：stock_zh_index_daily 返回 sh000300 全量历史，按窗口截取。"""
    import pandas as pd
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol="sh000300")
    if df is None or df.empty:
        raise RuntimeError("stock_zh_index_daily 返回空")
    d = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    start10 = f"{start8[:4]}-{start8[4:6]}-{start8[6:8]}"
    end10 = f"{end8[:4]}-{end8[4:6]}-{end8[6:8]}"
    mask = (d >= start10) & (d <= end10)
    return pd.DataFrame({"date": d[mask], "close": df["close"][mask]})


def ensure_benchmark(end_date: str, conn: Optional[sqlite3.Connection] = None,
                     fetcher: Optional[FETCHER] = None) -> Dict[str, Any]:
    """保证 index_daily 有 '000300' 近期（近30天）数据；缺失则经 akshare 补入。

    返回 {"ok": bool, "source": str, "rows": int, "error": Optional[str]}，绝不抛异常。
    """
    conn, own = (get_conn(), True) if conn is None else (conn, False)
    try:
        end_d = _to_date(end_date)
        start = (end_d - timedelta(days=30)).isoformat()
        n = conn.execute(
            "SELECT COUNT(*) FROM index_daily WHERE index_code='000300' AND trade_date>=? AND trade_date<=?",
            (start, end_d.isoformat()),
        ).fetchone()[0]
        if n and n > 0:
            return {"ok": True, "source": "existing", "rows": 0, "error": None}

        chain = [fetcher] if fetcher is not None else [_fetch_benchmark_em, _fetch_benchmark_sina]
        last_err: Optional[str] = None
        for fn in chain:
            try:
                df = fn(start.replace("-", ""), end_d.strftime("%Y%m%d"))
                rows = []
                for _, r in df.iterrows():
                    d = str(r["date"])[:10]
                    c = float(r["close"])
                    rows.append(("000300", d, c))
                if not rows:
                    last_err = f"{fn.__name__}: 空数据"
                    continue
                conn.executemany("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES (?,?,?)", rows)
                conn.commit()
                return {"ok": True, "source": fn.__name__, "rows": len(rows), "error": None}
            except Exception as e:
                last_err = f"{fn.__name__}: {repr(e)[:160]}"
        return {"ok": False, "source": "none", "rows": 0, "error": last_err}
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 周报生成

def _sec_performance(base_row: Tuple[str, float], end_row: Tuple[str, float]) -> Tuple[str, Optional[float]]:
    base_date, base_total = base_row
    end_date, end_total = end_row
    week_ret = (end_total / base_total - 1.0) if base_total and base_total > 0 else None
    lines = [
        f"- 期初总资产（{base_date}）：{_fmt_money(base_total)}",
        f"- 期末总资产（{end_date}）：{_fmt_money(end_total)}",
        f"- 周收益率：**{_fmt_pct(week_ret)}**",
    ]
    return "\n".join(lines), week_ret


def weekly_report(end_date: Optional[str] = None,
                  conn: Optional[sqlite3.Connection] = None,
                  out_dir: Optional[Path] = None,
                  fetcher: Optional[FETCHER] = None) -> Path:
    """生成 logs/reports/YYYY-Www.md 周度复盘报告，返回路径。空数据优雅降级，不抛异常。"""
    conn, own = (get_conn(), True) if conn is None else (conn, False)
    try:
        if end_date is None:
            end_date = latest_trade_date(conn)
        end_d = _to_date(end_date)
        monday = end_d - timedelta(days=end_d.weekday())
        iso_year, iso_week, _ = end_d.isocalendar()

        bench_info = ensure_benchmark(end_date, conn, fetcher=fetcher)

        notes: List[str] = []
        if not bench_info.get("ok"):
            notes.append(f"基准数据抓取失败，已降级：{bench_info.get('error')}")
        elif bench_info.get("rows"):
            notes.append(f"已自动补齐沪深300数据（来源 {bench_info.get('source')}，{bench_info.get('rows')} 行）")

        # ---- 组合窗口与基准日 ----
        rows = conn.execute(
            "SELECT date, total FROM portfolio_state WHERE date>=? AND date<=? ORDER BY date",
            (monday.isoformat(), end_d.isoformat()),
        ).fetchall()
        base_row = conn.execute(
            "SELECT date, total FROM portfolio_state WHERE date<? ORDER BY date DESC LIMIT 1",
            (monday.isoformat(),),
        ).fetchone()
        base_is_prev_week = base_row is not None
        if base_row is None and rows:
            base_row = rows[0]
            notes.append("缺上周组合状态，周收益以周内首日为基准（口径偏保守/偏窄）")
        if not rows:
            body_perf, week_ret = "暂无数据（portfolio_state 在该周无记录）", None
        elif base_row is None:
            body_perf, week_ret = "暂无数据（无任何 portfolio_state 记录）", None
        else:
            end_row = (rows[-1][0], float(rows[-1][1]))
            base_row = (base_row[0], float(base_row[1]))
            body_perf, week_ret = _sec_performance(base_row, end_row)
            ks_row = conn.execute(
                "SELECT drawdown, kill_switch FROM portfolio_state WHERE date=?", (end_row[0],)
            ).fetchone()
            if ks_row:
                body_perf += f"\n- 期末回撤：{_fmt_pct(ks_row[0])}；kill_switch：{ks_row[1]}"

        # ---- 基准（沪深300）----
        body_bench = ""
        bench_ret: Optional[float] = None
        if base_row is not None and rows:
            i_cur = _index_close_on_or_before(conn, end_d.isoformat())
            i_prev = _index_close_on_or_before(conn, base_row[0])
            if i_cur is None or i_prev is None or i_prev[1] <= 0:
                body_bench = "基准数据缺失（index_daily 无 '000300' 覆盖窗口两端）"
            else:
                bench_ret = i_cur[1] / i_prev[1] - 1.0
                body_bench = (
                    f"- 沪深300：{i_prev[1]:.2f}（{i_prev[0]}）→ {i_cur[1]:.2f}（{i_cur[0]}），周收益 {_fmt_pct(bench_ret)}\n"
                    f"- 超额收益（组合-基准）：**{_fmt_pct(None if week_ret is None else week_ret - bench_ret)}**\n"
                    f"- 基准数据来源：{bench_info.get('source')}"
                )

        # ---- 简单归因 ----
        body_attr = ""
        if base_row is None or not rows:
            body_attr = "暂无数据（缺组合期初/期末状态，无法归因）"
        elif bench_ret is None:
            body_attr = "基准数据缺失，无法计算相对归因"
        else:
            base_date, total_base = base_row
            if total_base is None or total_base <= 0:
                body_attr = "期初总资产非正，无法归因"
            else:
                pos_rows = conn.execute("SELECT code, name, shares FROM position WHERE shares>0").fetchall()
                if not pos_rows:
                    body_attr = "暂无数据（无持仓）"
                else:
                    lines = [
                        "| 代码 | 名称 | 期初权重 | 个票周收益 | 贡献 w_i×r_i |",
                        "|---|---|---|---|---|",
                    ]
                    sel_sum = 0.0
                    invested = 0.0
                    for code, name, shares in pos_rows:
                        c0 = _close_on_or_before(conn, code, base_date)
                        c1 = _close_on_or_before(conn, code, end_d.isoformat())
                        if c0 is None or c1 is None or c0[1] <= 0:
                            lines.append(f"| {code} | {name or ''} | n/a | n/a（行情缺失） | n/a |")
                            continue
                        w = int(shares) * c0[1] / float(total_base)
                        r = c1[1] / c0[1] - 1.0
                        invested += w
                        sel_sum += w * r
                        lines.append(f"| {code} | {name or ''} | {w:.2%} | {_fmt_pct(r)} | {_fmt_pct(w * r)} |")
                    selection = sel_sum - bench_ret * invested
                    timing = None if week_ret is None else week_ret - selection
                    lines.append("")
                    lines.append(f"- 期初总仓位：{invested:.2%}")
                    lines.append(f"- 选股贡献 = Σ(期初权重×个票周收益) - 基准周收益×期初总仓位 = **{_fmt_pct(selection)}**")
                    lines.append(f"- 择时贡献 = 组合周收益 - 选股贡献 = **{_fmt_pct(timing)}**")
                    lines.append("- 口径：期初持仓以 position 表当前快照近似，未还原周内调仓，未计交易费用。")
                    body_attr = "\n".join(lines)

        # ---- 周内成交 ----
        tr = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN side='buy' THEN amount ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN side='sell' THEN amount ELSE 0 END),0) "
            "FROM trade WHERE trade_date>=? AND trade_date<=?",
            (monday.isoformat(), end_d.isoformat()),
        ).fetchone()
        if tr and tr[0]:
            body_trades = (
                f"- 周内成交 {tr[0]} 笔：买入合计 {_fmt_money(tr[1])}，卖出合计 {_fmt_money(tr[2])}"
            )
        else:
            body_trades = "暂无数据"

        # ---- 决策质量（AI 闭环：命中率/置信度校准/风控拒绝分布/信号有效性）----
        body_quality = _sec_decision_quality(conn, monday.isoformat(), end_d.isoformat())

        content_lines = [
            f"# 周度复盘报告 {iso_year}-W{iso_week:02d}（{monday.isoformat()} ~ {end_d.isoformat()}）",
            "",
            f"> 生成时间：{datetime.now().isoformat(timespec='seconds')}",
            "",
            "## 组合表现",
            "",
            body_perf,
            "",
            "## 基准对比（沪深300 000300）",
            "",
            body_bench if body_bench else "基准数据缺失",
            "",
            "## 简单归因（简化口径）",
            "",
            body_attr if body_attr else "暂无数据",
            "",
            "## 周内成交",
            "",
            body_trades,
            "",
            "## 决策质量（AI 闭环）",
            "",
            body_quality,
            "",
            "## 附注",
            "",
        ]
        if notes:
            content_lines.extend(f"- {n}" for n in notes)
        else:
            content_lines.append("- 无")
        content_lines.append("")

        target_dir = Path(out_dir) if out_dir is not None else (BASE / "logs" / "reports")
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{iso_year}-W{iso_week:02d}.md"
        path.write_text("\n".join(content_lines), encoding="utf-8")
        return path
    finally:
        if own:
            conn.close()


def _sec_decision_quality(conn: sqlite3.Connection, start: str, end: str) -> str:
    """决策→结果闭环质量统计（此前周报归因完全不覆盖 AI 决策质量）。

    - 方向命中率（backfill_decision_outcomes 回填的 direction_hit）；
    - 置信度校准：高置信组 vs 低置信组命中率对比；
    - 风控拒绝次数（risk_event）；
    - 信号有效性（review/signal_eval.evaluate，样本不足自动标注）。
    """
    lines: List[str] = []
    row = conn.execute(
        "SELECT COUNT(*), SUM(direction_hit=1), AVG(confidence), "
        "SUM(confidence>=0.7 AND direction_hit=1), SUM(confidence>=0.7 AND "
        "direction_hit IS NOT NULL) FROM decision "
        "WHERE trade_date BETWEEN ? AND ? AND direction_hit IS NOT NULL",
        (start, end)).fetchone()
    n, hits, avg_conf, hi_hits, hi_n = row
    if not n:
        lines.append("- 本周无已回填方向的 buy/sell 决策（尚未产生成交或结果未到回填窗口）")
    else:
        hits = int(hits or 0)
        lines.append(f"- 方向命中：{hits}/{n}（胜率 {hits / n:.0%}）"
                     f"｜平均置信度 {'n/a' if avg_conf is None else '%.2f' % float(avg_conf)}")
        if hi_n:
            lines.append(f"- 置信度校准：conf≥0.7 组命中 {int(hi_hits or 0)}/{int(hi_n)}"
                         f"（{int(hi_hits or 0) / int(hi_n):.0%}）——若长期不高于低置信组，"
                         f"说明自报置信度缺乏区分度，应收紧置信度门槛")
    rej = conn.execute(
        "SELECT COUNT(*) FROM risk_event WHERE rule IN ('risk_check','risk_check_reconfirm') "
        "AND substr(ts,1,10) BETWEEN ? AND ?", (start, end)).fetchone()[0]
    if rej:
        lines.append(f"- 风控拦截 {rej} 次（详见看板 risk_event）")
    try:
        from review.signal_eval import evaluate
        ev = evaluate(conn)
        import json as _json
        lines.append("- 信号有效性（样本不足时仅供参考）：")
        lines.append("  ```json")
        lines.append("  " + _json.dumps(ev, ensure_ascii=False)[:800])
        lines.append("  ```")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 信号有效性评估失败：{type(e).__name__}: {e}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="A股AI模拟交易 · 周度复盘报告")
    parser.add_argument("--end", default=None, help="截止日期 YYYY-MM-DD（默认 daily_bar 最新交易日）")
    args = parser.parse_args(argv)

    conn = get_conn()
    try:
        end = args.end or latest_trade_date(conn)
        path = weekly_report(end, conn)
    finally:
        conn.close()
    print(f"[weekly] 报告已生成: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
