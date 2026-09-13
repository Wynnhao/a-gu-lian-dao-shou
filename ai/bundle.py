"""AI决策输入包：组装行情/信号/新闻/宏观/组合上下文，落盘 bundle.json 与 bundle.md 供 LLM 决策。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import sqlite3
from datetime import date, datetime
from typing import Optional, Tuple

from data.fetcher import get_conn
from data.news import get_recent_news
from risk.blacklist import check_blacklist, health_check

CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
WATCHLIST = CFG.get("watchlist", [])
START_CASH = float(CFG.get("execution", {}).get("paper_start_cash", 1000000.0))
PRICE_GUARD_PCT = float(CFG.get("risk", {}).get("price_guard_pct", 0.02))
MAX_SINGLE_WEIGHT = float(CFG.get("risk", {}).get("max_single_weight", 0.20))

log = logging.getLogger("ai.bundle")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _fh = logging.FileHandler(BASE / "logs" / "ai.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
log.propagate = False


# ---------------------------------------------------------------- 小工具

def _latest_trade_date(conn: sqlite3.Connection) -> str:
    """daily_bar 最新交易日；表空时退回今天（调用方负责据此提示数据缺失）。"""
    row = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()
    return row[0] if row and row[0] else date.today().isoformat()


def _trim_news(items: list) -> list:
    """新闻精简：title + source + published_at + content 前120字。"""
    out = []
    for n in items:
        out.append({
            "title": str(n.get("title") or ""),
            "source": str(n.get("source") or ""),
            "published_at": str(n.get("published_at") or ""),
            "content": str(n.get("content") or "")[:120],
        })
    return out


def _f(v) -> Optional[float]:
    """None/NaN 安全转 float。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(v) -> str:
    return "n/a" if v is None else f"{float(v):,.2f}"


def _pct(v) -> str:
    return "n/a" if v is None else f"{float(v) * 100:+.2f}%"


# ---------------------------------------------------------------- 组装

def build_bundle(run_date: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> dict:
    """组装决策输入包（run_date 默认 daily_bar 最新交易日）。任何小节失败均降级写明原因，不抛异常。"""
    own = conn is None
    c = get_conn() if own else conn
    bundle = {}
    try:
        # ---- run_date 与行情可用性 ----
        try:
            if run_date is None:
                run_date = _latest_trade_date(c)
            else:
                has = c.execute("SELECT 1 FROM daily_bar WHERE trade_date=? LIMIT 1",
                                (run_date,)).fetchone()
                if has is None:
                    real = _latest_trade_date(c)
                    bundle["run_date_note"] = (
                        f"指定的 run_date={run_date} 在 daily_bar 中无行情，"
                        f"参考最新交易日={real}")
        except Exception as e:
            run_date = run_date or date.today().isoformat()
            bundle["run_date_note"] = f"读取 daily_bar 失败：{type(e).__name__}: {e}"
        bundle["run_date"] = run_date
        bundle["generated_at"] = datetime.now().isoformat(timespec="seconds")
        bundle["watchlist"] = [dict(x) for x in WATCHLIST]

        # ---- 数据健康 ----
        try:
            issues = health_check(c)
        except Exception as e:
            issues = []
            bundle["health_check_error"] = f"health_check 失败：{type(e).__name__}: {e}"
        bundle["health_issues"] = issues
        if issues:
            bundle["notice"] = "【今日只出报告不下单】数据健康异常：" + "；".join(issues)

        # ---- 黑名单 ----
        try:
            bl = check_blacklist(c)
            bundle["blacklist"] = {code: {"ok": bool(ok), "reason": reason}
                                   for code, (ok, reason) in bl.items()}
        except Exception as e:
            bundle["blacklist"] = {}
            bundle["blacklist_error"] = f"黑名单检查失败：{type(e).__name__}: {e}"

        # ---- 信号（signal 表当日全部行）----
        try:
            rows = c.execute(
                "SELECT code, signals, score, as_of FROM signal WHERE as_of=? ORDER BY code",
                (run_date,)).fetchall()
            sigs = []
            for code, raw, score, as_of in rows:
                try:
                    parsed = json.loads(raw) if raw else {}
                except Exception:
                    parsed = {"_parse_error": str(raw)[:200]}
                sigs.append({"code": code, "signals": parsed,
                             "score": _f(score), "as_of": as_of})
            bundle["signals"] = sigs
            if not sigs:
                latest = c.execute("SELECT MAX(as_of) FROM signal").fetchone()[0]
                bundle["signals_missing"] = (
                    f"signal 表无 as_of={run_date} 的行"
                    + (f"（最新 as_of={latest}，需先运行 signals.compute_all）" if latest
                       else "（表为空，需先运行 signals.compute_all）"))
        except Exception as e:
            bundle["signals"] = []
            bundle["signals_missing"] = f"信号读取失败：{type(e).__name__}: {e}"

        # ---- 新闻（近3天，个股前5条、市场前8条）----
        news_out, news_err = {}, {}
        for item in WATCHLIST:
            code = item["code"]
            try:
                news_out[code] = _trim_news(get_recent_news(c, code, days=3)[:5])
            except Exception as e:
                news_out[code] = []
                news_err[code] = f"读取失败：{type(e).__name__}: {e}"
        try:
            news_out["market"] = _trim_news(get_recent_news(c, "", days=3)[:8])
        except Exception as e:
            news_out["market"] = []
            news_err["market"] = f"读取失败：{type(e).__name__}: {e}"
        bundle["news"] = news_out
        if news_err:
            bundle["news_errors"] = news_err

        # ---- 宏观估值（各指数最新一行）----
        try:
            macro = {}
            codes = [r[0] for r in c.execute(
                "SELECT DISTINCT index_code FROM index_valuation ORDER BY index_code").fetchall()]
            for ic in codes:
                row = c.execute(
                    "SELECT trade_date, pe, pe_pct, pb, pb_pct, close FROM index_valuation "
                    "WHERE index_code=? ORDER BY trade_date DESC LIMIT 1", (ic,)).fetchone()
                if row:
                    macro[ic] = {"date": row[0], "pe": _f(row[1]), "pe_pct": _f(row[2]),
                                 "pb": _f(row[3]), "pb_pct": _f(row[4]), "close": _f(row[5])}
            bundle["macro"] = macro
            if not macro:
                bundle["macro_missing"] = "index_valuation 表为空（需运行 data/macro.py）"
        except Exception as e:
            bundle["macro"] = {}
            bundle["macro_missing"] = f"估值读取失败：{type(e).__name__}: {e}"

        # ---- 动态池（异动/热门）：评估参考，池内票 LLM 只可输出 watch，不可直接买卖 ----
        try:
            from signals import dynpool
            dp = {
                "movers": dynpool.current_pool(c, "movers"),
                "hot_theme": dynpool.current_pool(c, "hot_theme"),
                "hot_stock": dynpool.current_pool(c, "hot_stock"),
            }
            bundle["dynamic_pools"] = dp
        except Exception as e:
            bundle["dynamic_pools"] = {}
            bundle["dynamic_pools_error"] = f"动态池读取失败：{type(e).__name__}: {e}"

        # ---- 组合状态 ----
        try:
            pos_rows = c.execute(
                "SELECT code, name, shares, avail_shares, cost, updated_at "
                "FROM position ORDER BY code").fetchall()
            bundle["positions"] = [
                {"code": r[0], "name": r[1] or "", "shares": int(r[2] or 0),
                 "avail_shares": int(r[3] or 0), "cost": _f(r[4]), "updated_at": r[5]}
                for r in pos_rows]
        except Exception as e:
            bundle["positions"] = []
            bundle["positions_error"] = f"持仓读取失败：{type(e).__name__}: {e}"
        try:
            row = c.execute(
                "SELECT date, cash, market_value, total, drawdown, kill_switch, note "
                "FROM portfolio_state ORDER BY date DESC LIMIT 1").fetchone()
            bundle["portfolio_state"] = (None if row is None else {
                "date": row[0], "cash": _f(row[1]), "market_value": _f(row[2]),
                "total": _f(row[3]), "drawdown": _f(row[4]),
                "kill_switch": int(row[5] or 0), "note": row[6]})
            if bundle["portfolio_state"] is None:
                bundle["portfolio_state_missing"] = "portfolio_state 表为空（盘后 mark_to_market 尚未运行）"
        except Exception as e:
            bundle["portfolio_state"] = None
            bundle["portfolio_state_missing"] = f"组合状态读取失败：{type(e).__name__}: {e}"
        bundle["paper_start_cash"] = START_CASH

        # ---- 最近3条决策 ----
        try:
            rows = c.execute(
                "SELECT id, action, code, status, confidence FROM decision "
                "ORDER BY id DESC LIMIT 3").fetchall()
            bundle["recent_decisions"] = [
                {"id": r[0], "action": r[1], "code": r[2], "status": r[3],
                 "confidence": _f(r[4])} for r in rows]
        except Exception as e:
            bundle["recent_decisions"] = []
            bundle["recent_decisions_error"] = f"决策历史读取失败：{type(e).__name__}: {e}"

        # ---- 数据质量（各票最新bar日期）----
        try:
            dq = {r[0]: r[1] for r in c.execute(
                "SELECT code, MAX(trade_date) FROM daily_bar GROUP BY code ORDER BY code").fetchall()}
            bundle["data_quality"] = dq
            if not dq:
                bundle["data_quality_missing"] = "daily_bar 为空（行情拉取全失败或尚未初始化）"
        except Exception as e:
            bundle["data_quality"] = {}
            bundle["data_quality_missing"] = f"行情质量读取失败：{type(e).__name__}: {e}"
        return bundle
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


# ---------------------------------------------------------------- Markdown

_OUTPUT_RULES = """## 决策输出要求

1. 只能输出符合 schema 的 **JSON 数组**（不要附加其他解释文字），每条必须包含：
   `action`（buy|sell|hold|watch）、`code`（6位字符串，须在 watchlist 内）、
   `target_weight`、`confidence`、`reasons`、`risk_notes`。
2. `reasons` 至少 **2 条非空**，且必须引用本输入包中的具体数据（信号值/估值分位/收盘价）或新闻标题。
3. `action` 为 buy/sell 时另需 `order` 对象：`{{"side": 与action一致, "price": 委托价, "shares": 股数}}`；
   委托价参考最新收盘价并遵守 **±2% 价格保护**（price_guard_pct={guard}），买入数量为 100 股整数倍。
4. **不交易黑名单票**（见"黑名单"一节中 PASS=false 的标的）。
5. 无合适标的时输出 `[]`，或对标的输出 `hold`。
6. `confidence` 取值 [0,1]；`target_weight` 取值 [0, {maxw}]；置信度 < {minconf} 时当日只出报告不下单。"""


def _md_table(headers: list, rows: list) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def bundle_to_markdown(bundle: dict) -> str:
    """把 bundle dict 渲染为人话版 markdown（末尾附决策输出要求固定文案）。"""
    run_date = bundle.get("run_date", "")
    lines = [f"# 决策输入包 {run_date}", ""]
    if bundle.get("generated_at"):
        wl = "、".join(f"{x['code']} {x.get('name', '')}" for x in bundle.get("watchlist", []))
        lines.append(f"> 生成时间：{bundle['generated_at']}｜自选池：{wl}")
        lines.append("")
    if bundle.get("notice"):
        lines += [f"**{bundle['notice']}**", ""]

    # 数据健康
    lines += ["## 数据健康", ""]
    issues = bundle.get("health_issues") or []
    if issues:
        lines += [f"- {i}" for i in issues]
    else:
        lines.append("- OK")
    if bundle.get("health_check_error"):
        lines.append(f"- （{bundle['health_check_error']}）")
    lines.append("")

    # 黑名单
    lines += ["## 黑名单", ""]
    bl = bundle.get("blacklist") or {}
    if bl:
        lines.append(_md_table(
            ["代码", "状态", "原因"],
            [(c, "PASS" if v.get("ok") else "BLOCK", v.get("reason", ""))
             for c, v in sorted(bl.items())]))
    else:
        lines.append("（无黑名单数据）")
    lines.append("")

    # 信号
    lines += [f"## 技术信号（as_of={run_date}）", ""]
    sigs = bundle.get("signals") or []
    if sigs:
        rows = []
        for s in sigs:
            g = s.get("signals") or {}
            pct = g.get("pct_chg")
            rows.append((s.get("code"), g.get("ma_trend"),
                         _pct(g.get("mom_20d")),  # mom_20d 为小数比例
                         "n/a" if g.get("rsi_14") is None else f"{g['rsi_14']:.1f}",
                         _f(g.get("close")),
                         "n/a" if pct is None else f"{float(pct):+.2f}%",  # pct_chg 已是百分数
                         s.get("score")))
        lines.append(_md_table(["代码", "MA趋势", "20日动量", "RSI14", "收盘", "当日涨跌", "score"], rows))
    else:
        lines.append(f"- 信号缺失：{bundle.get('signals_missing', '无信号数据')}")
    lines.append("")

    # 新闻
    lines += ["## 近3日新闻", ""]
    news_all = bundle.get("news") or {}
    for item in WATCHLIST:
        code = item["code"]
        items = news_all.get(code) or []
        lines.append(f"### {code} {item.get('name', '')}（最多5条）")
        if items:
            lines += [f"- 《{n['title']}》（{n['source']}，{n['published_at']}）：{n['content']}"
                      for n in items]
        else:
            lines.append("- （近3天无新闻）")
        lines.append("")
    lines.append("### 市场级（最多8条）")
    mk = news_all.get("market") or []
    if mk:
        lines += [f"- 《{n['title']}》（{n['source']}，{n['published_at']}）：{n['content']}"
                  for n in mk]
    else:
        lines.append("- （近3天无市场级新闻）")
    lines.append("")

    # 宏观
    lines += ["## 宏观估值（指数最新一行）", ""]
    macro = bundle.get("macro") or {}
    if macro:
        lines.append(_md_table(
            ["指数", "日期", "PE", "PE分位", "PB", "PB分位", "收盘"],
            [(ic, m.get("date"), _f(m.get("pe")),
              None if m.get("pe_pct") is None else f"{m['pe_pct']:.0%}",
              _f(m.get("pb")),
              None if m.get("pb_pct") is None else f"{m['pb_pct']:.0%}",
              _f(m.get("close"))) for ic, m in sorted(macro.items())]))
    else:
        lines.append(f"- 估值缺失：{bundle.get('macro_missing', '无数据')}")
    lines.append("")

    # 动态池
    dp = bundle.get("dynamic_pools") or {}
    lines += ["## 异动池 / 热门池（评估参考）", ""]
    movers_rows = dp.get("movers") or []
    if movers_rows:
        lines.append(_md_table(
            ["代码", "名称", "异动原因", "强度"],
            [(r["code"], r.get("name"), "；".join(r.get("reasons") or []),
              _f(r.get("strength"))) for r in movers_rows]))
    else:
        lines.append("- 异动池：当前无（自选池口径，阈值见 config.pools.movers）")
    themes = dp.get("hot_theme") or []
    if themes:
        lines.append("")
        lines.append("热门题材：" + "；".join(
            f"{r['name']}（{';'.join((r.get('reasons') or [])[:1])}）" for r in themes))
    stocks = dp.get("hot_stock") or []
    if stocks:
        lines.append("热门个股：" + "；".join(
            f"{r['code']} {r.get('name')}" for r in stocks))
    if not movers_rows and not themes and not stocks:
        lines.append("（无动态池数据）")
    lines += ["",
              "> 注意：动态池内非自选池标的仅可输出 watch（观察），buy/sell 仍限自选池 30 只。"]
    lines.append("")

    # 组合
    lines += ["## 组合状态", ""]
    lines.append(f"- 期初模拟资金：{_money(bundle.get('paper_start_cash'))}")
    pos = bundle.get("positions") or []
    if pos:
        lines.append(_md_table(
            ["代码", "名称", "持股", "可卖", "成本"],
            [(p["code"], p["name"], p["shares"], p["avail_shares"], _money(p["cost"]))
             for p in pos]))
    else:
        lines.append("- 当前无持仓")
    st = bundle.get("portfolio_state")
    if st:
        lines.append(
            f"- 最新组合状态（{st['date']}）：现金 {_money(st['cash'])}＋市值 "
            f"{_money(st['market_value'])}＝总资产 {_money(st['total'])}；"
            f"回撤 {_pct(st['drawdown'])}；kill_switch={st['kill_switch']}")
    else:
        lines.append(f"- 组合状态缺失：{bundle.get('portfolio_state_missing', '无数据')}")
    lines.append("")

    # 最近决策
    lines += ["## 最近3条决策", ""]
    rd = bundle.get("recent_decisions") or []
    if rd:
        lines += [f"- #{d.get('id')} {d.get('code')} {d.get('action')} "
                  f"status={d.get('status')} confidence={d.get('confidence')}" for d in rd]
    else:
        lines.append("- （decision 表为空，暂无历史决策）")
    lines.append("")

    # 数据质量
    lines += ["## 数据质量（各票最新bar日期）", ""]
    dq = bundle.get("data_quality") or {}
    if dq:
        for code in sorted(dq):
            mark = "" if dq[code] == run_date else f"（非 {run_date}，滞后）"
            lines.append(f"- {code}: {dq[code]}{mark}")
    else:
        lines.append(f"- 数据缺失：{bundle.get('data_quality_missing', '无数据')}")
    lines.append("")

    # 固定文案
    lines.append(_OUTPUT_RULES.format(guard=PRICE_GUARD_PCT,
                                      maxw=MAX_SINGLE_WEIGHT,
                                      minconf=CFG.get("risk", {}).get("min_confidence", 0.60)))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 落盘与 CLI

def write_bundle(run_date: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> Tuple[Path, Path]:
    """落盘 logs/session/<run_date>/bundle.json 与 bundle.md，返回 (json_path, md_path)。"""
    b = build_bundle(run_date, conn=conn)
    target = BASE / "logs" / "session" / str(b["run_date"])
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "bundle.json"
    md_path = target / "bundle.md"
    json_path.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(bundle_to_markdown(b), encoding="utf-8")
    log.info("bundle 已落盘 run_date=%s -> %s", b["run_date"], json_path)
    return json_path, md_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="组装 AI 决策输入包并落盘（bundle.json + bundle.md）")
    ap.add_argument("--date", default=None, dest="run_date",
                    help="运行日期 YYYY-MM-DD（默认 daily_bar 最新交易日）")
    args = ap.parse_args(argv)
    json_path, md_path = write_bundle(args.run_date)
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
