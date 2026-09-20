"""AI决策输入包：组装行情/信号/新闻/宏观/组合上下文，落盘 bundle.json 与 bundle.md 供 LLM 决策。

日期口径（2026-09 口径断裂修复）：
- run_date = 今天（决策运行日 = 预期执行日 T），session 目录、decision.run_date、
  日报"决策回顾"查询统一用它——此前 run_date 取 daily_bar 最新交易日（盘前=T-1），
  日报按 T 查询永远查空；
- evidence_date = daily_bar 最新交易日（T-1，证据截至日），信号/数据质量按它对齐。

信息密度（token 预算）：
- 黑名单 PASS 行折叠为一行汇总；数据质量只列核心池（core_codes）滞后票，
  非 core 滞后折叠为一行计数（W-C1：universe800 停更后 730 只滞后票曾把预算吃穿）；
- 新闻读取侧已做同事件去重与相关性排序（data.news.get_recent_news）；
- bundle.md 超预算时降级次序（W-C1 + 批次6）：先折叠滞后票明细（先砍墙）、
  再折叠缠论/对冲证据节明细、再逐级降新闻正文长度（120→60→0 字）、末位整节
  舍弃研究证据参考（§3.7：先砍既有证据墙，最后砍本字段）。

已实现盈亏口径（W-C2）：
- realized_pnl = 移动平均成本配比（卖出按持仓移动成本结转，已平仓口径）；
- net_cash_outlay = Σ卖出 − Σ买入（净投入现金，副口径；空仓时两者相等）。
"""
import os
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

from common.config import active_profile, core_codes, core_watchlist, load, snapshot
from data import repo
from data.fetcher import get_conn
from data.news import get_recent_news
from risk.blacklist import check_blacklist, health_check

CFG = snapshot()  # 统一配置层：import 期冻结 + 硬键校验 fail-fast
WATCHLIST = core_watchlist(CFG)          # 策略可交易池（51 只）
WATCHLIST_EXT = CFG.get("watchlist_extended", [])  # 仅供观察
START_CASH = float(CFG.get("execution", {}).get("paper_start_cash", 1000000.0))
PRICE_GUARD_PCT = float(CFG.get("risk", {}).get("price_guard_pct", 0.02))
MAX_SINGLE_WEIGHT = float(CFG.get("risk", {}).get("max_single_weight", 0.20))
MAX_TOTAL_WEIGHT = float(CFG.get("risk", {}).get("max_total_weight", 0.80))

PROMPT_VERSION = "2026-09.2"   # 固定文案/输出规则版本，落 decision.prompt_version 供迭代归因（2026-09.2：批次6 研究证据参考节合入）
MD_BUDGET = 45000              # bundle.md 字符数软预算（超限降级新闻正文）
NAME_OF = {str(w["code"]): str(w.get("name") or "") for w in WATCHLIST}

log = logging.getLogger("ai.bundle")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    _fh = rotating_handler("ai.log")
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
        return p if p in ("reversal_lowvol", "reversal_lowvol_v2", "momentum") \
            else "reversal_lowvol"
    except Exception:
        return "reversal_lowvol"


def _profile_verdict_latest() -> tuple:
    """读 signal_eval/latest.json 取最新一次 evaluate 输出里的 profile_verdict
    （目录经 review.signal_eval._signal_eval_dir()，支持环境变量注入做测试隔离）。

    返回 (verdict_dict_or_None, missing_reason_or_None)。
    """
    try:
        from review.signal_eval import _signal_eval_dir
        latest = _signal_eval_dir() / "latest.json"
        if not latest.exists():
            return None, "signal_eval/latest.json 不存在（先跑 review.signal_eval）"
        payload = json.loads(latest.read_text(encoding="utf-8"))
        v = payload.get("profile_verdict")
        if not v:
            return None, "latest.json 无 profile_verdict 字段"
        return v, None
    except (OSError, ValueError) as e:
        return None, f"latest.json 读取失败：{type(e).__name__}: {e}"


def _factor_crowding() -> dict:
    """读 factor_crowding.json（任务 5：规则 20 熔断态；路径同 signals 模块）。

    永远返回 dict；缺文件/解析失败 → crowded=False + reason 说明。
    """
    from signals.signals import _factor_crowding_path
    path = _factor_crowding_path()
    try:
        if not path.exists():
            return {"crowded": False, "reason": "factor_crowding.json 不存在（首次运行）"}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"crowded": False, "reason": f"读取失败：{type(e).__name__}: {e}"}


def _bond_etf_signals(conn: sqlite3.Connection) -> dict:
    """读 index_bond_yield + index_etf_share 最新一行（任务 1：P1-3）。

    永远返回 dict；缺表/空表 → 各自为空 dict + reason。
    """
    out = {"bond_yield": {}, "etf_share": {}, "reason": ""}
    try:
        row = conn.execute(
            "SELECT trade_date, yield, delta_20d_bp, source FROM index_bond_yield"
            " WHERE index_code='10Y_CN' ORDER BY trade_date DESC LIMIT 1").fetchone()
        if row:
            out["bond_yield"] = {
                "trade_date": row[0], "yield": row[1],
                "delta_20d_bp": row[2], "source": row[3],
            }
    except Exception as e:  # noqa: BLE001
        out["reason"] += f"bond 读取失败：{type(e).__name__}: {e}; "
    try:
        rows = conn.execute(
            "SELECT etf_code, trade_date, share, pct_chg_1d FROM index_etf_share"
            " WHERE etf_code IN ('510300','510500')"
            " ORDER BY etf_code, trade_date DESC").fetchall()
        seen = set()
        for code, d, s, pct in rows:
            if code in seen:
                continue
            seen.add(code)
            out["etf_share"][code] = {"trade_date": d, "share": s, "pct_chg_1d": pct}
    except Exception as e:  # noqa: BLE001
        out["reason"] += f"etf 读取失败：{type(e).__name__}: {e}; "
    if not out["bond_yield"] and not out["etf_share"]:
        out["reason"] = ("index_bond_yield 与 index_etf_share 均为空"
                          "（先跑 data.macro）")
    return out


# ---------------------------------------------------------------- 研究证据参考（全量打包批 批次6）

# §3.7（施工方案 2026-09-20）：字段只增——对冲稳定配对清单（批次1 生成函数复用）+
# 配对触发日 watch 提示 + 缠论结构状态摘要（批次2 标签流）。**决策策略语义零变化**：
# 字段仅进 LLM 上下文，不进规则引擎；对冲线 gate H0 已 FAIL 归档、缠论机械线已归档，
# 两节均带研究结论披露防 LLM 高估证据强度。

def _hedge_pair_returns(conn: sqlite3.Connection):
    """core 池 close_qfq 日收益面板（批次1 stable_pairs_current 的输入口径）。"""
    import pandas as pd

    codes = core_codes()
    if not codes:
        return None, None
    ph = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, trade_date, close_qfq FROM daily_bar WHERE code IN ({ph})"
        " ORDER BY trade_date", codes).fetchall()
    if not rows:
        return None, None
    px = {}
    for code, d, cq in rows:
        px.setdefault(d, {})[code] = cq
    dates = sorted(px)
    idx = [d for d in dates if len(px[d]) >= 2]
    df = pd.DataFrame([px[d] for d in idx], index=idx, columns=codes).astype(float)
    return df.pct_change(), idx


def hedge_evidence(conn: sqlite3.Connection, evidence_date: Optional[str] = None) -> dict:
    """对冲稳定配对清单（月度重估规则复用批次1）+ 最新交易日触发 watch 提示。

    生效月 = evidence_date 所在月；白名单 = 截至上月末 250 日窗重估（月频红线）。
    触发提示仅观察参考（A 急跌/B 同涨 → watch 参考，**不做机械调权**，§3.7）。
    """
    from signals import hedge_pair_research as hpr
    import pandas as pd

    ret, dates = _hedge_pair_returns(conn)
    out = {"effective_month": None, "estimated_at": None, "pairs": [],
           "detail": [], "hints": [], "note": ""}
    if ret is None or ret.dropna(how="all").empty:
        out["note"] = "core 池 daily_bar 无数据"
        return out
    asof = (evidence_date or dates[-1])
    cur = hpr.stable_pairs_current(ret, asof_month=str(asof)[:7])
    if cur is None:
        out["note"] = "重估窗口不足 250 日（warmup）"
        return out
    out["effective_month"] = cur["effective_month"]
    out["estimated_at"] = cur["estimated_at"]
    out["pairs"] = [list(p) for p in cur["pairs"]]
    out["detail"] = cur["detail"]
    out["note"] = ("对冲配对研究批 Gate H0 无分辨率 FAIL 归档（2026-09-20）；"
                   "清单与提示仅供 LLM watch 参考，非机械调权信号")
    # 触发提示：最新交易日（尾盘口径观察）——A 跌 <-1.0% 且 B 涨 >0（双向）
    last = dates[-1]
    row = ret.loc[last] if last in ret.index else None
    if row is not None:
        for a, b in cur["pairs"]:
            ra, rb = row.get(a), row.get(b)
            if ra is None or rb is None or pd.isna(ra) or pd.isna(rb):
                continue
            if ra < hpr.TRIGGER_THR and rb > 0:
                out["hints"].append({"date": last, "fall": a, "rise": b,
                                     "fall_ret": round(float(ra), 4),
                                     "rise_ret": round(float(rb), 4)})
            elif rb < hpr.TRIGGER_THR and ra > 0:
                out["hints"].append({"date": last, "fall": b, "rise": a,
                                     "fall_ret": round(float(rb), 4),
                                     "rise_ret": round(float(ra), 4)})
    return out


def _chan_one_line(lab: dict) -> str:
    """批次2 规格的单行结构摘要（≤80 字符）：三买状态(附注)｜笔/分型/中枢。"""
    kind = lab.get("tb_exit_kind")
    state = str(lab.get("tb_state") or "无")
    seg = f"{lab.get('code')} {state}"
    if state == "三买持仓" and lab.get("tb_confirm"):
        seg += f"(确认{lab['tb_confirm']},{lab.get('tb_days_since_confirm')}日前)"
    elif state == "三买结束":
        seg += f"(出场:{kind or '-'})"
    stroke = lab.get("stroke_dir")
    fr_type, fr_days = lab.get("last_fractal"), lab.get("last_fractal_days")
    zd, zg, pos = lab.get("pivot_zd"), lab.get("pivot_zg"), lab.get("pivot_pos")
    seg += f"｜笔:{stroke or '-'} 分型:{(fr_type + str(fr_days) + '日前') if fr_type else '-'}"
    if zd is not None and zg is not None:
        seg += f" 中枢:[{zd:.1f},{zg:.1f}]{pos or '-'}"
    return seg[:80]


def chan_evidence_lines(conn: sqlite3.Connection, codes: Optional[list] = None) -> dict:
    """core 池逐票缠论结构摘要（批次2 标签流末行；无标签票折叠为计数，W-C1 惯例）。

    返回 {"lines": [单行摘要...], "no_label": N, "note": 披露}。纯内存计算，
    复用批次2 evidence_pipeline（mode=ro 语义由调用方连接决定，本函数不写任何表）。
    """
    from signals import chan_data as cd
    from signals import chan_evidence as ce
    import pandas as pd

    codes = list(codes) if codes is not None else cd.core_codes()
    cal = [str(r[0]) for r in conn.execute(
        "SELECT date FROM trade_calendar ORDER BY date")]
    lines, no_label = [], 0
    for code in codes:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, close_qfq, high_qfq,"
            " low_qfq FROM daily_bar WHERE code=? ORDER BY trade_date",
            (code,)).fetchall()
        if not rows or not cal:
            no_label += 1
            continue
        df = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low",
                                         "close", "close_qfq", "high_qfq",
                                         "low_qfq"])
        df = cd.with_open_qfq(df)
        try:
            r = ce.evidence_pipeline(df, cal, str(code))
        except Exception:  # noqa: BLE001 — 单票结构失败不阻断整节
            no_label += 1
            continue
        labels = r.get("labels") or []
        if labels:
            lines.append(_chan_one_line(labels[-1]))
        else:
            no_label += 1
    return {"lines": lines, "no_label": no_label,
            "note": ("缠论机械线 R3 已归档（重画率 FAIL）；标签为软证据，"
                     "首发确认即锁定（批次2 口径）")}


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
        bundle["watchlist_extended"] = [dict(x) for x in WATCHLIST_EXT]

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

        # ---- 信号（当前 profile + 可交易池；错配修复：不混口径、不含扩展观察池）----
        try:
            _want = active_profile()
            _wl = set(core_codes())
            # 口径选择：优先当前 profile；该日无行时回退到当日行数最多的 profile
            # （场景：刚切 profile 尚未重算，回退比给出空包更有用，且如实标注）
            _pick = c.execute(
                "SELECT profile, COUNT(*) AS n FROM signal WHERE as_of=?"
                " GROUP BY profile ORDER BY (profile=?) DESC, n DESC LIMIT 1",
                (ev, _want)).fetchone()
            _prof = _pick[0] if _pick else _want
            rows = c.execute(
                "SELECT code, signals, score, as_of FROM signal"
                " WHERE as_of=? AND profile=? ORDER BY code",
                (ev, _prof)).fetchall()
            sigs = []
            _dropped = 0
            for code, raw, score, as_of in rows:
                if _wl and str(code) not in _wl:
                    _dropped += 1          # 扩展观察池/全市场票不进决策包
                    continue
                try:
                    parsed = json.loads(raw) if raw else {}
                except Exception:
                    parsed = {"_parse_error": str(raw)[:200]}
                sigs.append({"code": code, "signals": parsed,
                             "score": _f(score), "as_of": as_of})
            bundle["signals"] = sigs
            _pool_key = ("watchlist_core" if (load().get("watchlist_core")
                                            or load().get("watchlist_extended"))
                         else "watchlist")
            bundle["signal_scope"] = {"profile": _prof, "pool": _pool_key,
                                      "codes": len(sigs),
                                      "dropped_non_core": _dropped,
                                      "profile_fallback": bool(_pick and _prof != _want),
                                      "requested_profile": _want}
            if not sigs:
                latest = c.execute("SELECT MAX(as_of) FROM signal WHERE profile=?",
                                   (_prof,)).fetchone()[0]
                bundle["signals_missing"] = (
                    f"signal 表无 as_of={ev} / profile={_prof} 的行"
                    + (f"（该 profile 最新 as_of={latest}，需先运行 signals.compute_all）"
                       if latest else "（该 profile 无数据，需先运行 signals.compute_all）"))
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

        # ---- profile_verdict（任务 1：读 latest.json 注入决策层）----
        verdict, v_missing = _profile_verdict_latest()
        bundle["profile_verdict_latest"] = verdict
        if v_missing:
            bundle["profile_verdict_missing"] = v_missing

        # ---- factor_crowding（任务 5：规则 20 熔断态注入 LLM）----
        bundle["factor_crowding"] = _factor_crowding()

        # ---- bond_yield + etf_share（Sprint 2 任务 1：P1-3 regime 旁路）----
        try:
            bundle["bond_etf_signals"] = _bond_etf_signals(c)
        except Exception as e:
            bundle["bond_etf_signals"] = {"reason": f"读取失败：{type(e).__name__}: {e}"}

        # ---- earnings events（Sprint 2 任务 2：P1-4 业绩预告关键词事件）----
        try:
            from signals.earnings import refresh as _earnings_refresh
            bundle["earnings_events_latest"] = _earnings_refresh(conn=c, days=3)
        except Exception as e:
            bundle["earnings_events_latest"] = None
            bundle["earnings_events_missing"] = f"earnings.refresh 失败：{type(e).__name__}: {e}"

        # ---- market breadth（Sprint 2 任务 3：P1-1 市场宽度）----
        try:
            from signals.breadth import read_breadth
            bundle["breadth_composite"] = read_breadth(c)
        except Exception as e:
            bundle["breadth_composite"] = {"reason": f"读取失败：{type(e).__name__}: {e}"}

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

        # ---- 已实现盈亏（W-C2：移动平均成本配比，已平仓口径）----
        # 此前 Σ卖出−Σ买入 会把未平仓买入全算成"已实现亏损"（P1-20：建仓后 LLM
        # 看到接近持仓成本的假巨亏）。现按每票移动平均成本结转：卖出额 − 卖出股数×
        # 持仓移动成本；全部平仓后与"净投入现金"口径数值一致。
        # 口径差（P2⑭，2026-09-20 核实留档，计算不变）：本处移动成本用 trade.amount
        # （含费用：买入=价×股+佣金，paper.py L4/L91），而 paper 持仓 position.cost =
        # 成交价加权平均**不含费用**（paper.py L8/L286，费用只影响现金）——两处成本
        # 每股差买入佣金分摊。realized_pnl 是已平仓口径（费用计入损益合理），
        # position.cost 是持仓均价口径；note 文案已注明"（含佣金印花税）"。
        try:
            trows = c.execute(
                "SELECT code, side, amount, shares FROM trade WHERE status='filled' "
                "ORDER BY trade_date, id").fetchall()
            lots: dict = {}          # code -> [持有股数, 移动平均成本]
            realized = 0.0
            orphan_sells = 0
            for code, side, amount, shares in trows:
                shares = int(shares or 0)
                amount = float(amount or 0.0)
                if shares <= 0:
                    continue
                lot = lots.setdefault(str(code), [0, 0.0])
                if side == "buy":
                    total = lot[0] + shares
                    lot[1] = (lot[1] * lot[0] + amount) / total   # amount 含费用
                    lot[0] = total
                else:  # sell
                    matched = min(lot[0], shares)
                    if lot[0] > 0:
                        realized += amount * (matched / shares) - lot[1] * matched
                        lot[0] -= matched
                    if shares > matched:
                        # 无持仓卖出（数据异常/手工单）：无成本可结转，按卖出额全额
                        # 计入并留痕降级，不让静默数字失真
                        orphan_sells += 1
                        realized += amount * ((shares - matched) / shares)
            note = "已平仓口径：卖出按持仓移动平均成本结转（含佣金印花税），未平仓不计"
            if orphan_sells:
                note += f"；含 {orphan_sells} 笔无持仓卖出（成本不可考，按全额计入）"
            bundle["realized_pnl"] = _f(realized)
            bundle["realized_pnl_note"] = note
            # 副口径：净投入现金（Σ卖出−Σ买入），保留给账务对账
            row = c.execute(
                "SELECT COALESCE(SUM(CASE WHEN side='sell' THEN amount ELSE 0 END),0.0), "
                "COALESCE(SUM(CASE WHEN side='buy' THEN amount ELSE 0 END),0.0) "
                "FROM trade WHERE status='filled'").fetchone()
            bundle["net_cash_outlay"] = _f(float(row[0]) - float(row[1]))
            bundle["net_cash_outlay_note"] = \
                "净投入现金 = Σ卖出 − Σ买入（含费用；非已实现盈亏，空仓时两者一致）"
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

        # ---- 数据质量（W-C1：只统计 core 池；非 core 滞后折叠为计数）----
        # universe800 回补停更后全库 730 只"滞后票"≈1.4 万字符，把 45000 预算吃穿
        # 导致新闻正文连续三天被降级清零（P1-19），且滞后告警常态化淹没真异常。
        try:
            dq_all = repo.latest_dates_by_code(c)
            core = set(core_codes())
            dq_core = {code: d for code, d in dq_all.items() if code in core}
            bundle["data_quality"] = dict(sorted(dq_core.items()))
            bundle["data_quality_noncore_total"] = len(dq_all) - len(dq_core)
            bundle["data_quality_noncore_lag"] = sum(
                1 for code, d in dq_all.items()
                if code not in core and d != bundle["evidence_date"])
            if not dq_all:
                bundle["data_quality_missing"] = "daily_bar 为空（行情拉取全失败或尚未初始化）"
        except Exception as e:
            bundle["data_quality"] = {}
            bundle["data_quality_missing"] = f"行情质量读取失败：{type(e).__name__}: {e}"

        # ---- 研究证据参考（批次6：字段只增，仅进上下文不进规则引擎，§3.7）----
        try:
            bundle["hedge_pairs"] = hedge_evidence(c, evidence_date=ev)
        except Exception as e:  # noqa: BLE001
            bundle["hedge_pairs"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            bundle["chan_structure"] = chan_evidence_lines(c)
        except Exception as e:  # noqa: BLE001
            bundle["chan_structure"] = {"error": f"{type(e).__name__}: {e}"}

        # ---- 当前可行动空间（W-C3：按 regime/拥挤/黑名单把硬边界显式算给 LLM）----
        # 红线被误读为交易禁令（P1-26）的解法之一：边界数字化，红线语义另行澄清。
        try:
            rg_cap = (bundle.get("regime") or {}).get("cap")
            total_cap = float(rg_cap) if rg_cap is not None else MAX_TOTAL_WEIGHT
            crowded = bool((bundle.get("factor_crowding") or {}).get("crowded"))
            single_cap = min(MAX_SINGLE_WEIGHT, 0.05) if crowded else MAX_SINGLE_WEIGHT
            core_set = set(core_codes())
            blocked_core = sorted(
                code for code, v in (bundle.get("blacklist") or {}).items()
                if not v.get("ok") and code in core_set)
            invested = sum(
                p["weight"] for p in (bundle.get("positions") or [])
                if p.get("weight") is not None)
            bundle["actionable_space"] = {
                "total_weight_cap": _f(total_cap),
                "total_cap_source": ("regime 动态闸" if rg_cap is not None
                                     else "静态 max_total_weight"),
                "current_invested_weight": _f(invested),
                "remaining_buy_budget": _f(max(0.0, total_cap - invested)),
                "single_weight_cap": _f(single_cap),
                "crowding_capped_to_5pct": crowded,
                "blacklist_blocked_core": blocked_core,
                "note": ("仓位比例以最新 portfolio_state.total 为分母（盯市可能滞后）；"
                         "本节是风控硬边界，与红线评估信号（降置信，非禁令）互不替代"),
            }
        except Exception as e:
            bundle["actionable_space"] = {"error": f"{type(e).__name__}: {e}"}
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
   （见 regime 一节，当前 {captop}）；触及上限时优先输出减仓/持有，不要输出加仓。
8. **因子拥挤熔断（Sprint 1 任务 5）**：当 bundle.factor_crowding.crowded=True 时，
   buy 单 confidence 必须 ≥ 0.7（否则改 hold/watch），且 target_weight ≤ 5%（即使人工填更高，
   风控规则 20 也会自动压回 5%）。
9. **业绩预告事件（Sprint 2 任务 2）**：当 bundle.earnings_events_latest[code].net ≤ -2 时，
   该票禁止 buy（即使其他信号看好）。
10. **回测 MDD 红线的语义（重要，勿误读）**：红线触发（profile_verdict.red_line_triggered
   或 verdict=hold）只是**评估信号，不是交易禁令**——含义是：对新开仓**降低置信度、
   要求更强证据、只做小仓位试探**（仍在总仓位上限、单票上限、拥挤熔断 5% 之内）；
   有充分证据时按正常流程输出 buy/sell，**不得因红线无条件空仓或拒绝一切新开仓**。
   "当前可行动空间"一节给出了当前实际允许的最大单票权重与总仓位上限。
11. **红线数据缺失**：bundle 无 profile_verdict、或其中无回测 B/C（MDD）数字时，
   按"无红线信息"处理，并在 risk_notes 中注明"红线数据缺失"；**严禁臆造任何
   回测/MDD 数字**；引用红线数字必须同时带其数据版本（backtest 元数据），
   版本未知时须注明"数据版本未知"。"""


def _md_table(headers: list, rows: list) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def bundle_to_markdown(bundle: dict, news_content_len: int = 120,
                       lag_detail: bool = True, evidence_detail: bool = True,
                       include_evidence: bool = True) -> str:
    """把 bundle dict 渲染为人话版 markdown（末尾附决策输出要求固定文案）。

    news_content_len 供 token 预算降级用（write_bundle 超预算时逐级缩短）；
    lag_detail=False 时数据质量节折叠为一行计数（W-C1 降级次序：先砍滞后票墙，
    再砍新闻正文）。批次6 新增：evidence_detail=False 折叠缠论/对冲节明细
    （滞后墙之后、新闻降级之前）；include_evidence=False 整节舍弃（降级链末位——
    §3.7 先砍既有证据墙，最后砍本字段）。
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

    # profile_verdict（v1.4：B+C 投票 + MDD 红线；W-C3：红线=评估信号非禁令，
    # 引用红线数字必须带数据版本，缺失时按"无红线信息"处理、不得臆造）
    v = bundle.get("profile_verdict_latest")
    if v:
        lines.append("## Score Profile Verdict（v1.4）")
        lines.append("")
        lines.append(f"- profile: **{v.get('profile')}**（备选: {v.get('other_profile')}）")
        lines.append(f"- verdict: **{v.get('verdict')}** → suggest: `{v.get('suggest_profile')}`")
        if v.get("red_line_triggered"):
            th = (v.get("thresholds") or {}).get("mdd_red_line")
            th_s = "n/a" if th is None else f"{th:.0%}"
            c_cur = (v.get("C") or {}).get("current")
            c_cur_s = "n/a" if c_cur is None else f"{c_cur:.2%}"
            bt_ver = v.get("backtest_generated_at") or v.get("data_version")
            ver_s = bt_ver if bt_ver else "数据版本未知（verdict 未携带 backtest 元数据）"
            lines.append(f"- ⚠️ **MDD 红线触发**（阈值 {th_s}，current={c_cur_s}，"
                         f"数据版本：{ver_s}）→ **评估信号**：新开仓降置信 + 需更强证据 + "
                         f"仅小仓位试探（总仓位/单票/拥挤上限内）；**不是交易禁令**，"
                         f"不得据此无条件空仓")
        votes = v.get("votes") or {}
        lines.append(f"- 投票: B_switch={votes.get('b_switch')}, "
                     f"C_switch={votes.get('c_switch')}, "
                     f"{votes.get('votes_switch')}/{votes.get('votes_total')}")
        B, C = v.get("B") or {}, v.get("C") or {}
        if B.get("current") is not None or B.get("alt") is not None:
            lines.append(f"- B (年化超额 vs HS300): cur={_pct(B.get('current'))}, "
                         f"alt={_pct(B.get('alt'))}, gap={_pct(B.get('gap'))}")
        if C.get("current") is not None or C.get("alt") is not None:
            lines.append(f"- C (MDD): cur={_pct(C.get('current'))}, "
                         f"alt={_pct(C.get('alt'))}, gap={_pct(C.get('gap'))}")
        if v.get("backtest_missing"):
            lines.append(f"- ⚠️ **红线数据缺失**：{v['backtest_missing']} → "
                         f"按\"无红线信息\"处理并在 risk_notes 注明，不得臆造回测数字")
        abstains = v.get("abstains") or []
        if abstains:
            lines.append(f"- 弃权维度: {', '.join(abstains)}（{v.get('abstain_reason', '')}）")
        lines.append(f"- 切换永远需要人工确认并留痕，系统不自动切")
        lines.append("")
    elif bundle.get("profile_verdict_missing"):
        lines.append(f"## Score Profile Verdict（v1.4）")
        lines.append("")
        lines.append(f"- ⚠️ {bundle['profile_verdict_missing']}")
        lines.append("- **红线数据缺失**：按\"无红线信息\"处理并在 risk_notes 注明"
                     "\"红线数据缺失\"；不得臆造任何回测/MDD 数字；有充分证据仍可"
                     "按正常流程输出决策（红线缺失≠禁仓）。")
        lines.append("")

    # factor_crowding（任务 5：规则 20）
    fc = bundle.get("factor_crowding")
    if fc:
        lines.append("## Factor Crowding（规则 20 熔断态）")
        lines.append("")
        if fc.get("crowded"):
            lines.append(f"- ⚠️ **拥挤熔断生效**：buy 单 target_weight > 5% 自动压回")
            lines.append(f"- μ60={fc.get('mu60')}, σ60={fc.get('sigma60')}, "
                         f"buckets={fc.get('n_buckets')}")
            lines.append("- LLM 倾向 hold（confidence ≥ 0.7 才允许 buy）")
        else:
            lines.append(f"- 正常：{fc.get('reason', '')}")
        lines.append("")

    # bond_yield + etf_share（Sprint 2 任务 1：P1-3）
    be = bundle.get("bond_etf_signals")
    if be:
        lines.append("## 国债 + ETF 旁路（P1-3）")
        lines.append("")
        by = be.get("bond_yield") or {}
        if by:
            delta = by.get("delta_20d_bp")
            delta_str = f"{delta:+.2f}bp" if isinstance(delta, (int, float)) else "n/a"
            lines.append(f"- **10Y 国债收益率**：{by.get('yield', 'n/a'):.3f}% "
                         f"（20 日变动 {delta_str}，{by.get('trade_date', '?')}，"
                         f"source={by.get('source', '?')}）")
            if isinstance(delta, (int, float)) and delta < -15:
                lines.append("- ⚠️ 国债下行 → cap × 0.8（避险情绪）")
        else:
            lines.append("- 10Y 国债：缺数据（先跑 `python3 -m data.macro` 抓国债）")
        et = be.get("etf_share") or {}
        for code in ("510300", "510500"):
            row = et.get(code)
            if row:
                pct = row.get("pct_chg_1d")
                pct_str = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "n/a"
                lines.append(f"- **ETF {code}**：{pct_str}（{row.get('trade_date', '?')}）")
                if isinstance(pct, (int, float)) and pct > 2.0:
                    lines.append(f"  - ⚠️ 份额 +{pct:.2f}% → 避险档升到半配")
                elif isinstance(pct, (int, float)) and pct < -2.0:
                    lines.append(f"  - ⚠️ 份额 {pct:.2f}% → 满配档降到半配")
        if not et:
            lines.append("- ETF 510300/510500：缺数据")
        if be.get("reason") and (not by and not et):
            lines.append(f"- ⚠️ {be['reason']}")
        lines.append("")

    # earnings events（Sprint 2 任务 2：P1-4 业绩预告）
    ee = bundle.get("earnings_events_latest")
    if ee:
        lines.append("## 业绩预告事件（P1-4 · 近 3 日）")
        lines.append("")
        if not ee:
            lines.append("- 无正/负面业绩信号命中（news 表 3 日内 0 关键词命中）")
        else:
            lines.append("| code | positive | negative | net | 摘要 |")
            lines.append("|---|---:|---:|---:|---|")
            for code in sorted(ee.keys()):
                d = ee[code]
                pos = d.get("positive", 0)
                neg = d.get("negative", 0)
                net = d.get("net", 0)
                samples = d.get("samples_pos", [])[:1] + d.get("samples_neg", [])[:1]
                sample_str = "；".join(samples)[:80] if samples else "—"
                flag = "🚫禁买" if net <= -2 else ("⚠️利好" if net >= 2 else "")
                lines.append(f"| {code} | {pos} | {neg} | {net} {flag} | {sample_str} |")
        lines.append("")
    elif bundle.get("earnings_events_missing"):
        lines.append(f"## 业绩预告事件（P1-4）")
        lines.append("")
        lines.append(f"- ⚠️ {bundle['earnings_events_missing']}")
        lines.append("")

    # market breadth（Sprint 2 任务 3：P1-1）
    br = bundle.get("breadth_composite")
    if br:
        lines.append("## 市场宽度（P1-1 · A 股微盘踩踏预警）")
        lines.append("")
        if br.get("date"):
            lines.append(f"- date: **{br['date']}** · source: {br.get('source', '?')}")
            lines.append(f"- 涨停家数: {br.get('limit_up_count', 'n/a')}")
            lines.append(f"- 跌停家数: {br.get('limit_down_count', 'n/a')}")
            if br.get("limit_up_seal_rate") is not None:
                lines.append(f"- 封板率: {br['limit_up_seal_rate']:.0%}")
            if br.get("advance_decline_ratio") is not None:
                lines.append(f"- 涨跌家数比: {br['advance_decline_ratio']:.2f}")
            if br.get("new_high_minus_new_low") is not None:
                lines.append(f"- 新高新低差: {br['new_high_minus_new_low']:+d}")
            comp = br.get("breadth_composite")
            if comp is not None:
                flag = "🚫极端避险" if comp < -2 else ("⚠️弱势" if comp < -1 else "")
                lines.append(f"- **breadth_composite**: {comp:.2f} {flag}")
            else:
                lines.append("- breadth_composite: n/a（数据不足）")
        else:
            lines.append(f"- ⚠️ {br.get('reason', 'breadth_daily 空（先跑 data.breadth）')}")
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

    # 当前可行动空间（W-C3：把硬边界显式算给 LLM，防红线被误读为全面禁仓）
    act = bundle.get("actionable_space")
    if act:
        lines += ["## 当前可行动空间（硬边界）", ""]
        if act.get("error"):
            lines.append(f"- 计算失败：{act['error']}")
        else:
            lines.append(f"- 总仓位上限：{act['total_weight_cap']:.0%}"
                         f"（{act['total_cap_source']}）；卖出不受限")
            inv = act.get("current_invested_weight")
            inv_s = "n/a" if inv is None else f"{inv:.1%}"
            rem = act.get("remaining_buy_budget")
            rem_s = "n/a" if rem is None else f"{rem:.1%}"
            lines.append(f"- 当前总仓位（盯市口径）：{inv_s}；剩余可买入预算：{rem_s}")
            lines.append(f"- 单票权重上限：{act['single_weight_cap']:.0%}"
                         + ("（拥挤熔断生效，压至 5%）" if act.get("crowding_capped_to_5pct") else ""))
            bl_blocked = act.get("blacklist_blocked_core") or []
            lines.append("- 核心池内黑名单禁交易：%s"
                         % (("、".join(bl_blocked) if bl_blocked else "无")))
            lines.append(f"- 口径：{act.get('note', '')}")
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
        lines.append(f"- 累计已实现盈亏（已平仓口径）：{_money(bundle.get('realized_pnl'))}"
                     f"（{bundle.get('realized_pnl_note', '')}）")
    if bundle.get("net_cash_outlay") is not None:
        lines.append(f"- 净投入现金（副口径）：{_money(bundle.get('net_cash_outlay'))}"
                     f"（{bundle.get('net_cash_outlay_note', '')}）")
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

    # 数据质量（W-C1：只列核心池滞后票；非 core 折叠计数；预算降级时整体折叠）
    lines += [f"## 数据质量（证据日 {ev}）", ""]
    dq = bundle.get("data_quality")
    if dq:
        lag = {c: d for c, d in sorted(dq.items()) if d != ev}
        if lag_detail:
            if lag:
                lines.append(f"- 核心池滞后票 {len(lag)} 只：" +
                             "、".join(f"{c}({d})" for c, d in lag.items()))
            else:
                lines.append(f"- 核心池 {len(dq)} 只票行情已更新至 {ev}")
        else:
            lines.append(f"- （预算降级）核心池 {len(dq)} 只中滞后 {len(lag)} 只"
                         "（明细折叠，见 bundle.json）")
        noncore_lag = bundle.get("data_quality_noncore_lag") or 0
        if noncore_lag:
            noncore_total = bundle.get("data_quality_noncore_total")
            total_s = f"/{noncore_total} 只" if noncore_total else ""
            lines.append(f"- 另有 {noncore_lag} 只非核心票滞后{total_s}"
                         "（不计入决策包，仅计数）")
    else:
        lines.append(f"- 数据缺失：{bundle.get('data_quality_missing', '无数据')}")
    lines.append("")

    # 研究证据参考（批次6：字段只增，仅进上下文不进规则引擎；预算降级次序
    # 滞后墙→缠论/对冲折叠→新闻→整节舍弃——先砍既有证据墙，最后砍本字段）
    if include_evidence:
        lines += ["## 研究证据参考（仅上下文，非机械调权）", ""]
        hp = bundle.get("hedge_pairs") or {}
        if hp.get("error"):
            lines.append(f"- 对冲配对：生成失败（{hp['error']}）")
        elif hp.get("pairs"):
            det = {(r["a"], r["b"]): r for r in (hp.get("detail") or [])}
            def _rho(v) -> str:
                return f"{v:.3f}" if isinstance(v, float) else str(v)
            pair_s = "、".join(
                f"{a}↔{b}(ρ250={_rho(det.get((a, b), {}).get('rho_250d'))})"
                for a, b in hp["pairs"])
            lines.append(f"- 对冲稳定配对（{hp.get('effective_month')} 重估，"
                         f"截至 {hp.get('estimated_at')}）：{pair_s}")
            for h in hp.get("hints") or []:
                lines.append(f"- 配对触发观察（{h['date']}）：{h['fall']} 跌 "
                             f"{h['fall_ret']:.1%} ↔ {h['rise']} 涨 "
                             f"{h['rise_ret']:.1%} → 观察 {h['rise']}（watch 参考）")
        else:
            lines.append(f"- 对冲配对：当期无生效稳定配对（{hp.get('note', '')}）")
        if hp.get("note") and not hp.get("error"):
            lines.append(f"- 对冲披露：{hp['note']}")
        cs = bundle.get("chan_structure") or {}
        if cs.get("error"):
            lines.append(f"- 缠论结构：生成失败（{cs['error']}）")
        elif evidence_detail:
            for ln in cs.get("lines") or []:
                lines.append(f"- {ln}")
            if cs.get("no_label"):
                lines.append(f"- 其余 {cs['no_label']} 只无标签（无数据/warmup 不足/结构失败）")
        else:
            n_hold = sum(1 for ln in (cs.get("lines") or []) if "三买持仓" in ln)
            lines.append(f"- （预算降级）缠论结构：{len(cs.get('lines') or [])} 只有标签"
                         f"（三买持仓 {n_hold}），明细折叠见 bundle.json")
        if cs.get("note"):
            lines.append(f"- 缠论披露：{cs['note']}")
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

BUNDLE_STAMP_KEEP = 10         # bundle.HHMM.md 带时间戳副本保留份数（防审计链无限增长）


def _session_root() -> Path:
    """session 产物根目录；AGSICKLE_SESSION_DIR 调用时读（测试隔离，与 midday 同模式）。"""
    return Path(os.environ.get("AGSICKLE_SESSION_DIR")
                or str(BASE / "logs" / "session"))


def write_bundle(run_date: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> Tuple[Path, Path]:
    """落盘 logs/session/<run_date>/bundle.json 与 bundle.md，返回 (json_path, md_path)。

    token 预算降级次序（W-C1）：超预算先折叠滞后票明细（先砍墙），仍超再逐级
    降新闻正文（120→60→0 字）——此前 730 只滞后票墙直接把正文吃穿清零。
    版本化（W-C8）：bundle.md 之外另写 bundle.HHMM.md 带时间戳副本（防傍晚整套
    重跑覆盖早晨证据，保留最近 BUNDLE_STAMP_KEEP 份）。
    """
    b = build_bundle(run_date, conn=conn)
    target = _session_root() / str(b["run_date"])
    target.mkdir(parents=True, exist_ok=True)
    md = bundle_to_markdown(b)
    if len(md) > MD_BUDGET:
        md = bundle_to_markdown(b, lag_detail=False)
        log.info("bundle.md 超预算（%d 字符），先砍滞后票墙（明细折叠为计数）", len(md))
    if len(md) > MD_BUDGET:
        md = bundle_to_markdown(b, lag_detail=False, evidence_detail=False)
        log.info("bundle.md 仍超预算（%d 字符），缠论/对冲证据节折叠（批次6 降级档）", len(md))
    if len(md) > MD_BUDGET:
        md = bundle_to_markdown(b, lag_detail=False, evidence_detail=False,
                                news_content_len=60)
        log.info("bundle.md 仍超预算（%d 字符），新闻正文降级至 60 字", len(md))
    if len(md) > MD_BUDGET:
        md = bundle_to_markdown(b, lag_detail=False, evidence_detail=False,
                                news_content_len=0)
        log.info("bundle.md 仍超预算（%d 字符），新闻正文置空", len(md))
    if len(md) > MD_BUDGET:
        md = bundle_to_markdown(b, lag_detail=False, evidence_detail=False,
                                news_content_len=0, include_evidence=False)
        log.info("bundle.md 仍超预算（%d 字符），研究证据参考整节舍弃（降级链末位）",
                 len(md))
    json_path = target / "bundle.json"
    md_path = target / "bundle.md"
    json_path.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    # W-C8：带时间戳副本（同分钟重跑覆盖同名副本，接受）+ 保留最近 N 份
    stamp_path = target / ("bundle.%s.md" % datetime.now().strftime("%H%M"))
    stamp_path.write_text(md, encoding="utf-8")
    stamped = sorted(target.glob("bundle.????.md"))
    for old in stamped[:-BUNDLE_STAMP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    log.info("bundle 已落盘 run_date=%s -> %s（时间戳副本 %s）",
             b["run_date"], json_path, stamp_path.name)
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
