"""AI决策输入包：组装行情/信号/新闻/宏观/组合上下文，落盘 bundle.json 与 bundle.md 供 LLM 决策。

日期口径（2026-09 口径断裂修复）：
- run_date = 今天（决策运行日 = 预期执行日 T），session 目录、decision.run_date、
  日报"决策回顾"查询统一用它——此前 run_date 取 daily_bar 最新交易日（盘前=T-1），
  日报按 T 查询永远查空；
- evidence_date = daily_bar 最新交易日（T-1，证据截至日），信号/数据质量按它对齐。

信息密度（token 预算）：
- 黑名单 PASS 行折叠为一行汇总；数据质量只列滞后票；
- 新闻读取侧已做同事件去重与相关性排序（data.news.get_recent_news）；
- bundle.md 超预算时逐级降级新闻正文长度（120→60→0 字）。
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import logging.handlers
import sqlite3
from datetime import date, datetime
from typing import Optional, Tuple

from common.config import load, snapshot
from data import repo
from data.fetcher import get_conn
from data.news import get_recent_news
from risk.blacklist import check_blacklist, health_check

CFG = snapshot()  # 统一配置层：import 期冻结 + 硬键校验 fail-fast
WATCHLIST = CFG.get("watchlist", [])
START_CASH = float(CFG.get("execution", {}).get("paper_start_cash", 1000000.0))
PRICE_GUARD_PCT = float(CFG.get("risk", {}).get("price_guard_pct", 0.02))
MAX_SINGLE_WEIGHT = float(CFG.get("risk", {}).get("max_single_weight", 0.20))
MAX_TOTAL_WEIGHT = float(CFG.get("risk", {}).get("max_total_weight", 0.80))

PROMPT_VERSION = "2026-09.1"   # 固定文案/输出规则版本，落 decision.prompt_version 供迭代归因
MD_BUDGET = 45000              # bundle.md 字符数软预算（超限降级新闻正文）
NAME_OF = {str(w["code"]): str(w.get("name") or "") for w in WATCHLIST}

log = logging.getLogger("ai.bundle")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _fh = logging.handlers.RotatingFileHandler(BASE / "logs" / "ai.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3)
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
log.propagate = False


# ---------------------------------------------------------------- 小工具

def _latest_trade_date(conn: sqlite3.Connection) -> str:
    """daily_bar 最新交易日；表空时退回今天（调用方负责据此提示数据缺失）。"""
    return repo.latest_trade_date(conn) or date.today().isoformat()


def _trim_news(items: list, content_len: int = 120) -> list:
    """新闻精简：title + source + published_at + content 截断 + 相关性标注。"""
    out = []
    for n in items:
        out.append({
            "title": str(n.get("title") or ""),
            "source": str(n.get("source") or ""),
            "published_at": str(n.get("published_at") or ""),
            "content": str(n.get("content") or "")[:content_len],
            **({"relevance": n["relevance"]} if n.get("relevance") else {}),
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


def _signals_profile() -> str:
    """当前 score profile（热读 config，避免为此引入 signals 导入链）。"""
    try:
        p = load().get("signals", {}).get("profile", "reversal_lowvol")
        return p if p in ("reversal_lowvol", "momentum") else "reversal_lowvol"
    except Exception:
        return "reversal_lowvol"


# ---------------------------------------------------------------- 组装

def build_bundle(run_date: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> dict:
    """组装决策输入包（run_date=今天；evidence_date=daily_bar 最新交易日）。

    任何小节失败均降级写明原因，不抛异常。
    """
    own = conn is None
    c = get_conn() if own else conn
    bundle = {}
    try:
        # ---- 日期口径：run_date=今天（预期执行日），evidence_date=最新交易日 ----
        bundle["run_date"] = run_date or date.today().isoformat()
        try:
            bundle["evidence_date"] = _latest_trade_date(c)
            if bundle["evidence_date"] == bundle["run_date"]:
                bundle["evidence_date_note"] = "最新交易日=今天（盘后口径）"
        except Exception as e:
            bundle["evidence_date"] = bundle["run_date"]
            bundle["evidence_date_note"] = f"读取 daily_bar 失败：{type(e).__name__}: {e}"
        ev = bundle["evidence_date"]
        bundle["generated_at"] = datetime.now().isoformat(timespec="seconds")
        bundle["prompt_version"] = PROMPT_VERSION
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

        # ---- 信号（signal 表 evidence_date 全部行）----
        try:
            rows = c.execute(
                "SELECT code, signals, score, as_of FROM signal WHERE as_of=? ORDER BY code",
                (ev,)).fetchall()
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
                    f"signal 表无 as_of={ev} 的行"
                    + (f"（最新 as_of={latest}，需先运行 signals.compute_all）" if latest
                       else "（表为空，需先运行 signals.compute_all）"))
        except Exception as e:
            bundle["signals"] = []
            bundle["signals_missing"] = f"信号读取失败：{type(e).__name__}: {e}"

        # ---- 新闻（近3天，个股前5条、市场前8条；读取侧已去重+相关性排序）----
        news_out, news_err = {}, {}
        for item in WATCHLIST:
            code = item["code"]
            try:
                news_out[code] = _trim_news(
                    get_recent_news(c, code, days=3, name=item.get("name") or "")[:5])
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
            # 陈旧性标注：池子刷新日期落后证据日 2 个交易日以上要显式告警
            stale = []
            for pname, prows in dp.items():
                as_of = dynpool.pool_as_of(c, pname)
                if as_of and as_of < ev:
                    stale.append(f"{pname}(最新刷新 {as_of})")
            if stale:
                bundle["dynamic_pools_stale"] = (
                    "以下池子数据可能过期（证据日 " + ev + "）：" + "、".join(stale))
        except Exception as e:
            bundle["dynamic_pools"] = {}
            bundle["dynamic_pools_error"] = f"动态池读取失败：{type(e).__name__}: {e}"

        # ---- 市场环境总闸（regime/波动率目标 → 动态总仓位上限，风控引擎强制）----
        try:
            from risk import regime as _regime
            cap_info = _regime.position_cap(c, CFG)
            bundle["regime"] = cap_info
            bundle["score_profile"] = _signals_profile()
        except Exception as e:
            bundle["regime"] = {"error": f"{type(e).__name__}: {e}"}

        # ---- 组合状态（持仓补现价/市值/浮盈/权重/止损参考价）----
        try:
            ps_row = repo.latest_state(c)
            total_equity = _f(ps_row[3]) if ps_row else None
            pos_rows = c.execute(
                "SELECT code, name, shares, avail_shares, cost, updated_at "
                "FROM position ORDER BY code").fetchall()
            atr_map: dict = {}
            try:
                from risk import regime as regime_mod
                atr_map = regime_mod.latest_atr_pct(
                    c, [r[0] for r in pos_rows]) if pos_rows else {}
            except Exception:
                atr_map = {}
            stop_base = float(CFG.get("risk", {}).get("stop_loss_pct", 0.08))
            stop_mult = float(CFG.get("risk", {}).get("atr_stop_mult", 2.0))
            from risk.regime import stop_loss_line as _stop_line
            positions = []
            for r in pos_rows:
                code, name, shares, avail, cost = r[0], r[1] or "", int(r[2] or 0), \
                    int(r[3] or 0), _f(r[4])
                close_row = c.execute(
                    "SELECT close FROM daily_bar WHERE code=? ORDER BY trade_date DESC "
                    "LIMIT 1", (code,)).fetchone()
                last_close = _f(close_row[0]) if close_row else None
                mv = shares * last_close if (last_close is not None and shares) else None
                unrealized = shares * (last_close - cost) \
                    if (last_close is not None and cost is not None and shares) else None
                # 止损参考价：ATR 自适应止损线（risk/regime）作用于成本价
                line = _stop_line(stop_base, atr_map.get(code), stop_mult)
                stop_price = round(cost * (1.0 - line), 2) \
                    if (cost and line) else None
                positions.append({
                    "code": code, "name": name, "shares": shares, "avail_shares": avail,
                    "cost": cost, "last_close": last_close,
                    "market_value": _f(mv), "unrealized_pnl": _f(unrealized),
                    "unrealized_pct": _f(unrealized / (cost * shares))
                    if unrealized is not None and cost else None,
                    "weight": _f(mv / total_equity)
                    if (mv is not None and total_equity and total_equity > 0) else None,
                    "stop_line_pct": _f(line), "stop_price": _f(stop_price),
                    "updated_at": r[5]})
            bundle["positions"] = positions
        except Exception as e:
            bundle["positions"] = []
            bundle["positions_error"] = f"持仓读取失败：{type(e).__name__}: {e}"
        try:
            row = repo.latest_state(c)
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

        # ---- 已实现盈亏（trade 流水口径，含费用）----
        try:
            row = c.execute(
                "SELECT COALESCE(SUM(CASE WHEN side='sell' THEN amount ELSE 0 END),0.0), "
                "COALESCE(SUM(CASE WHEN side='buy' THEN amount ELSE 0 END),0.0) "
                "FROM trade WHERE status='filled'").fetchone()
            bundle["realized_pnl"] = _f(float(row[0]) - float(row[1]))
            bundle["realized_pnl_note"] = "卖出净入账 − 买入总支出（含佣金印花税，未平仓不计）"
        except Exception as e:
            bundle["realized_pnl_error"] = f"已实现盈亏读取失败：{type(e).__name__}: {e}"

        # ---- 最近5条决策（带结果反馈：t1_ret/direction_hit 盘后回填）----
        try:
            rows = c.execute(
                "SELECT id, run_date, action, code, status, confidence, reasons, "
                "t1_ret, direction_hit FROM decision ORDER BY id DESC LIMIT 5").fetchall()
            bundle["recent_decisions"] = [
                {"id": r[0], "run_date": r[1], "action": r[2], "code": r[3],
                 "status": r[4], "confidence": _f(r[5]),
                 "reason_preview": (json.loads(r[6])[0][:50]
                                    if r[6] and r[6].startswith("[") else ""),
                 "t1_ret": _f(r[7]), "direction_hit": None if r[8] is None else int(r[8])}
                for r in rows]
        except Exception as e:
            bundle["recent_decisions"] = []
            bundle["recent_decisions_error"] = f"决策历史读取失败：{type(e).__name__}: {e}"

        # ---- 数据质量（各票最新bar日期）----
        try:
            dq = dict(sorted(repo.latest_dates_by_code(c).items()))
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

_OUTPUT_RULES = """## 决策输出要求（prompt_version={pv}）

1. 只能输出符合 schema 的 **JSON 数组**（不要附加其他解释文字），每条必须包含：
   `action`（buy|sell|hold|watch）、`code`（6位字符串，须在 watchlist 内）、
   `target_weight`、`confidence`、`reasons`、`risk_notes`。
2. `reasons` 至少 **2 条非空**，且必须引用本输入包中的具体数据（信号值/估值分位/收盘价）或新闻标题。
3. `action` 为 buy/sell 时另需 `order` 对象：`{{"side": 与action一致, "price": 委托价, "shares": 股数}}`；
   委托价参考最新收盘价并遵守 **±2% 价格保护**（price_guard_pct={guard}），买入数量为 100 股整数倍。
4. **不交易黑名单票**（见"黑名单"一节中 BLOCK 的标的）。
5. 无合适标的时输出 `[]`，或对标的输出 `hold`。
6. `confidence` 取值 [0,1]；`target_weight` 取值 [0, {maxw}]（hold/watch 的 target_weight 恒为 0）；
   置信度 < {minconf} 时当日只出报告不下单。
7. **遵守"市场环境总闸"**：当日全部买入的 target_weight 合计不得超过当前总仓位上限
   （见 regime 一节，当前 {captop}）；触及上限时优先输出减仓/持有，不要输出加仓。"""


def _md_table(headers: list, rows: list) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def bundle_to_markdown(bundle: dict, news_content_len: int = 120) -> str:
    """把 bundle dict 渲染为人话版 markdown（末尾附决策输出要求固定文案）。

    news_content_len 供 token 预算降级用（write_bundle 超预算时逐级缩短）。
    """
    run_date = bundle.get("run_date", "")
    ev = bundle.get("evidence_date", run_date)
    lines = [f"# 决策输入包 {run_date}", ""]
    if bundle.get("generated_at"):
        lines.append(f"> 生成时间：{bundle['generated_at']}｜证据截至：{ev}｜"
                     f"prompt_version={bundle.get('prompt_version', '-')}")
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

    # 黑名单（PASS 行折叠为一行汇总——此前 30 行里 29 行是 "PASS -" 纯噪音）
    lines += ["## 黑名单", ""]
    bl = bundle.get("blacklist") or {}
    if bl:
        blocked = [(c, v) for c, v in sorted(bl.items()) if not v.get("ok")]
        lines.append(f"- PASS {len(bl) - len(blocked)} 只（略）")
        if blocked:
            lines.append(_md_table(["代码", "原因"],
                                   [(c, v.get("reason", "")) for c, v in blocked]))
    else:
        lines.append("（无黑名单数据）")
    lines.append("")

    # 信号（补 ATR占比/换手分位/5日动量/均线结构/价格口径——md 此前丢掉了决策关键列）
    lines += [f"## 技术信号（as_of={ev}，score profile={bundle.get('score_profile', '见config')}）", ""]
    sigs = bundle.get("signals") or []
    if sigs:
        rows = []
        for s in sigs:
            g = s.get("signals") or {}
            pct = g.get("pct_chg")
            atrp = g.get("atr_pct")
            tpp = g.get("turnover_pct")
            rows.append((s.get("code"), g.get("ma_trend"),
                         _pct(g.get("mom_5d")),
                         _pct(g.get("mom_20d")),
                         "n/a" if g.get("rsi_14") is None else f"{g['rsi_14']:.0f}",
                         "n/a" if atrp is None else f"{atrp * 100:.1f}%",
                         "n/a" if tpp is None else f"{tpp:.2f}",
                         "✓" if g.get("above_ma60") else "✗",
                         _f(g.get("close")),
                         "n/a" if pct is None else f"{float(pct):+.2f}%",
                         g.get("price_basis", "-"),
                         s.get("score")))
        lines.append(_md_table(
            ["代码", "MA趋势", "5日动量", "20日动量", "RSI14", "ATR占比", "换手分位",
             "站上MA60", "收盘", "当日涨跌", "价格口径", "score"], rows))
    else:
        lines.append(f"- 信号缺失：{bundle.get('signals_missing', '无信号数据')}")
    lines.append("")

    # 新闻
    lines += [f"## 近3日新闻（证据日 {ev}）", ""]
    news_all = bundle.get("news") or {}
    for item in WATCHLIST:
        code = item["code"]
        items = news_all.get(code) or []
        lines.append(f"### {code} {item.get('name', '')}（最多5条）")
        if items:
            lines += [f"- 《{n['title']}》（{n['source']}，{n['published_at']}"
                      + (f"，相关度{n['relevance']}" if n.get("relevance") else "")
                      + f"）：{n['content'][:news_content_len]}"
                      for n in items]
        else:
            lines.append("- （近3天无新闻）")
        lines.append("")
    lines.append("### 市场级（最多8条）")
    mk = news_all.get("market") or []
    if mk:
        lines += [f"- 《{n['title']}》（{n['source']}，{n['published_at']}）"
                  f"：{n['content'][:news_content_len]}"
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

    # 市场环境总闸（策略库 Top3：RSRS+二八三档 + 波动率目标仓位）
    rg = bundle.get("regime") or {}
    lines += ["## 市场环境总闸（regime）", ""]
    if rg.get("error"):
        lines.append(f"- 计算失败（fail-open，无动态闸）：{rg['error']}")
    else:
        r = rg.get("regime") or {}
        v = rg.get("vol_target") or {}
        cap = rg.get("cap")
        tier = r.get("tier")
        if cap is None:
            lines.append("- 当前无动态约束（engine 按静态上限执行）")
        else:
            lines.append(
                f"- **总仓位上限 {cap:.0%}（{tier or '-'}档）**：风控引擎强制，"
                f"建议全部 target_weight 合计 ≤ {cap:.0%}；卖出不受限")
        rs = r.get("rsrs") or {}
        if rs:
            ztxt = "n/a" if rs.get("z") is None else f"z={rs['z']}"
            lines.append(f"- RSRS（{rs.get('as_of', '-')}）：{ztxt}，"
                         f"β={rs.get('beta', 'n/a')}（z>+1 满配 / z<−1 避险 / 其余半配）")
        dm = r.get("dual_mom") or {}
        if dm:
            lines.append(f"- 二八动量（{dm.get('window', '-')}日）：大盘 "
                         f"{_pct(dm.get('big'))}，小盘 {_pct(dm.get('small'))}"
                         f"——{dm.get('signal', '')}")
        if v:
            if v.get("cap") is not None:
                lines.append(f"- 波动率目标：组合年化波动 {v.get('sigma_ann', 0):.1%} "
                             f"vs 目标 {v.get('target_ann_vol', 0):.0%} → scale="
                             f"{v.get('scale')}, cap={v.get('cap'):.0%}")
            elif v.get("note"):
                lines.append(f"- 波动率目标：{v['note']}")
            elif v.get("error"):
                lines.append(f"- 波动率目标计算失败：{v['error']}")
    lines.append("")

    # 动态池
    dp = bundle.get("dynamic_pools") or {}
    lines += ["## 异动池 / 热门池（评估参考）", ""]
    if bundle.get("dynamic_pools_stale"):
        lines.append(f"- ⚠️ {bundle['dynamic_pools_stale']}")
    movers_rows = dp.get("movers") or []
    if movers_rows:
        lines.append(_md_table(
            ["代码", "名称", "异动原因", "强度", "口径", "入池日"],
            [(r["code"], r.get("name"), "；".join(r.get("reasons") or []),
              _f(r.get("strength")), r.get("mode") or "-", r.get("added_date", "-"))
             for r in movers_rows]))
    else:
        lines.append("- 异动池：当前无（阈值见 config.pools.movers）")
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
              "> 注意：动态池内非自选池标的仅可输出 watch（观察），buy/sell 仍限自选池（config.watchlist）；"
              "黑名单票仅展示（blacklist 标注），不可交易。"]
    lines.append("")

    # 组合（补现价/市值/浮盈/权重 + 已实现盈亏）
    lines += ["## 组合状态", ""]
    lines.append(f"- 期初模拟资金：{_money(bundle.get('paper_start_cash'))}")
    pos = bundle.get("positions") or []
    if pos:
        lines.append(_md_table(
            ["代码", "名称", "持股", "可卖", "成本", "现价", "市值", "浮动盈亏",
             "当前权重", "止损参考价"],
            [(p["code"], p["name"], p["shares"], p["avail_shares"], _money(p["cost"]),
              _money(p.get("last_close")), _money(p.get("market_value")),
              _pct(p.get("unrealized_pct")) + f"（{_money(p.get('unrealized_pnl'))}）",
              None if p.get("weight") is None else f"{p['weight']:.1%}",
              _money(p.get("stop_price")))
             for p in pos]))
        lines.append("- 止损参考价 = 成本 × (1 − 止损线)；止损线 = max(8%, 2×ATR占比)"
                     "（ATR 自适应，触发后应止损卖出而非加仓，规则16）")
    else:
        lines.append("- 当前无持仓")
    if bundle.get("realized_pnl") is not None:
        lines.append(f"- 累计已实现盈亏：{_money(bundle.get('realized_pnl'))}"
                     f"（{bundle.get('realized_pnl_note', '')}）")
    st = bundle.get("portfolio_state")
    if st:
        lines.append(
            f"- 最新组合状态（{st['date']}）：现金 {_money(st['cash'])}＋市值 "
            f"{_money(st['market_value'])}＝总资产 {_money(st['total'])}；"
            f"回撤 {_pct(st['drawdown'])}；kill_switch={st['kill_switch']}")
    else:
        lines.append(f"- 组合状态缺失：{bundle.get('portfolio_state_missing', '无数据')}")
    lines.append("")

    # 最近决策（带结果反馈闭环）
    lines += ["## 最近5条决策（含结果反馈）", ""]
    rd = bundle.get("recent_decisions") or []
    if rd:
        for d in rd:
            fb = ""
            if d.get("t1_ret") is not None:
                fb = f"｜次日实际 {_pct(d['t1_ret'])}" + \
                     ("（方向✓）" if d.get("direction_hit") == 1 else "（方向✗）")
            lines.append(f"- #{d.get('id')} {d.get('run_date')} {d.get('code')} "
                         f"{d.get('action')} status={d.get('status')} "
                         f"conf={d.get('confidence')}{fb}｜{d.get('reason_preview', '')}")
        lines.append("- 提醒：保持决策连续性，无新证据不要反复打脸自己的昨日判断。")
    else:
        lines.append("- （decision 表为空，暂无历史决策）")
    lines.append("")

    # 数据质量（只列滞后票——此前 30 行逐票列相同日期全是噪音）
    lines += [f"## 数据质量（证据日 {ev}）", ""]
    dq = bundle.get("data_quality") or {}
    if dq:
        lag = {c: d for c, d in sorted(dq.items()) if d != ev}
        if lag:
            lines.append(f"- 滞后票 {len(lag)} 只：" +
                         "、".join(f"{c}({d})" for c, d in lag.items()))
        else:
            lines.append(f"- 全部 {len(dq)} 只票行情已更新至 {ev}")
    else:
        lines.append(f"- 数据缺失：{bundle.get('data_quality_missing', '无数据')}")
    lines.append("")

    # 固定文案
    rg_cap = ((bundle.get("regime") or {}).get("cap"))
    captop = ("无动态约束，静态 %.0f%%" % (MAX_TOTAL_WEIGHT * 100)) if rg_cap is None \
        else ("%.0f%%（regime 动态闸）" % (rg_cap * 100))
    lines.append(_OUTPUT_RULES.format(pv=bundle.get("prompt_version", "-"),
                                      guard=PRICE_GUARD_PCT,
                                      maxw=MAX_SINGLE_WEIGHT,
                                      captop=captop,
                                      minconf=CFG.get("risk", {}).get("min_confidence", 0.60)))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 落盘与 CLI

def write_bundle(run_date: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> Tuple[Path, Path]:
    """落盘 logs/session/<run_date>/bundle.json 与 bundle.md，返回 (json_path, md_path)。

    token 预算：md 超 MD_BUDGET 时逐级降级新闻正文长度（120→60→0 字）。
    """
    b = build_bundle(run_date, conn=conn)
    target = BASE / "logs" / "session" / str(b["run_date"])
    target.mkdir(parents=True, exist_ok=True)
    md = bundle_to_markdown(b)
    if len(md) > MD_BUDGET:
        md = bundle_to_markdown(b, news_content_len=60)
        log.info("bundle.md 超预算（%d 字符），新闻正文降级至 60 字", len(md))
    if len(md) > MD_BUDGET:
        md = bundle_to_markdown(b, news_content_len=0)
        log.info("bundle.md 仍超预算（%d 字符），新闻正文置空", len(md))
    json_path = target / "bundle.json"
    md_path = target / "bundle.md"
    json_path.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    log.info("bundle 已落盘 run_date=%s -> %s", b["run_date"], json_path)
    return json_path, md_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="组装 AI 决策输入包并落盘（bundle.json + bundle.md）")
    ap.add_argument("--date", default=None, dest="run_date",
                    help="运行日期 YYYY-MM-DD（默认今天，证据取 daily_bar 最新交易日）")
    args = ap.parse_args(argv)
    json_path, md_path = write_bundle(args.run_date)
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
