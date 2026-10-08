"""下午复核输入包（13:30）：盘中实时价+午评后增量新闻 → 增量决策输入包。

时点选择：13:30（连续竞价已开盘 30 分钟，避开早盘波动 + 午休空白，给 confirm
留 13:30~14:50 共 80 分钟可执行窗口）。此为 Sprint4 之后补的"方案B"——修复
2026-09-21 实测发现的结构性漏洞：AI 在 12:26 午评补提 buy 美的被规则4
（非交易时段）机械拦下后，13:00 后没有第二次 decide 入口，buy 卡死在 rejected。

职责（纯准备 + 一项确定性重提，核心决策仍不经 LLM 之外的通道）：
1. 强制刷新实时行情（绝不写 daily_bar——盘中部分bar会污染日线历史，日线只归盘后入库）；
2. 拉取 12:30 以来（午评之后）的新增资讯（个股+市场级，去重入库）；
3. 盘点：持仓实时盈亏、组合实时回撤、上午+午评决策执行情况、待确认 pending 单价格漂移；
3.5 规则4 拒单自动重提（裁决#2，出手率打磨批 2026-10-08）：当日被规则4
   （非交易时段）拒绝的 buy，13:00 后按最新价重估、conf 达门槛（全局/拥挤档）
   才重提，走完整风控链与人工闸门（幂等，重提单带 afternoon_resubmit_of 标记）；
4. 组装 logs/session/<日期>/afternoon_bundle.md（含"增量决策输出要求"固定文案）。

LLM（会话）读取该文件后做增量决策：证据无显著变化必须输出 []，有变化才出增量
决策（经 ai/decide.py 校验 → runner.py propose-db → 人工闸门，与盘前/午评同一链路）。

与 midday.py 的差异（保持简洁，仅时点+窗口+文件+LLM 文案+重提步骤五处差异，其他复用）：
- 默认时点 13:30（midday 默认 11:35）
- since 窗口 12:30:00（midday 用 09:00:00——午评取全上午新闻；下午复核取午评后增量）
- 文件名 afternoon_bundle.md/json（midday 用 midday_bundle）
- LLM 输出要求第 2 条：规则4 拒单已由系统步骤自动重提，LLM 无需重复输出
- 新增 resubmit_session_rejected_buys 步骤（3.5，LLM 复核之前）
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import repo                     # noqa: E402
from data.fetcher import get_conn          # noqa: E402
from data import quotes                    # noqa: E402
from data.news import fetch_all, get_recent_news  # noqa: E402
from execution import runner               # noqa: E402

# 测试隔离：AGSICKLE_SESSION_DIR 覆盖会话产物目录（import 期读 env，与 runner.ORDERS_DIR 同模式）
SESSION_DIR = Path(os.environ.get("AGSICKLE_SESSION_DIR") or (BASE / "logs" / "session"))

# 默认时点 13:30；since 窗口 12:30——与 midday.py 区别在此（其余代码复用结构）
DEFAULT_HOUR = 13
DEFAULT_MINUTE = 30
SINCE_HOUR = 12
SINCE_MINUTE = 30
BUNDLE_FILENAME = "afternoon_bundle"


def _latest_run_date(conn) -> str:
    """决策口径统一后的运行日期=今天（与 premarket/decide/日报一致）。"""
    return datetime.now().strftime("%Y-%m-%d")


def _resubmit_conf_floor() -> float:
    """重提 conf 门槛：全局 min_confidence，拥挤期 0.65（与 prompt 规则 8 同源）。"""
    base = float(runner.CFG.get("risk", {}).get("min_confidence", 0.55))
    try:
        from signals.signals import read_factor_crowding
        if read_factor_crowding().get("crowded"):
            return 0.65
    except Exception:  # noqa: BLE001
        pass
    return base


def resubmit_session_rejected_buys(conn, live: dict, ctx, now: datetime,
                                   orders_dir=None) -> list:
    """裁决#2（出手率打磨批 2026-10-08）：当日被规则4（非交易时段）拒绝的
    pending buy，13:00 后自动重提——修复 9/21 美的 conf=0.72 唯一合格买点死于
    「12:30 午评被拒 + 下午不重提」结构性作废的漏洞。

    挂在实时行情刷新与 build_context 之后、LLM 增量复核之前（系统确定性步骤，
    不经 LLM）。语义：
    - 候选：当日 action='buy' AND status='rejected'，且 risk_event 存在
      rule='risk_check' + detail LIKE '非交易时段%' + decision_id=该 id 的记录
      （排除因其他规则被拒的 buy）；
    - 幂等：重提单 input_snapshot 携带 afternoon_resubmit_of=<原id>，重提前
      查同日同标记（兼容两种 JSON 分隔形态）已存在即跳过——cron 重跑/catchup
      补跑不产生重复单；
    - conf 门槛：全局 risk.min_confidence，拥挤期 0.65；conf 已降不达门槛 →
      留痕跳过；
    - 价格：取最新实时价重估——委托价=实时价、股数=equity×target_weight/新价
      100 股整手；实时价缺失跳过；漂移超 price_guard_pct（±2%）→ 留痕不硬提
      （与既有 stale 处理同语义，硬提必被规则9 拒）；
    - 重提走 runner.propose 完整风控链（13:00 后在下午连续竞价时段内，规则4
      放行）与人工闸门；结果 rule='afternoon_resubmit' risk_event 留痕。

    返回 [{orig_id, code, outcome, detail}] 供 bundle 渲染（outcome ∈
    resubmitted / skip_not_session_rejected / skip_already_resubmitted /
    skip_conf_below / skip_no_live_price / skip_drift_over_guard /
    skip_no_lot / rejected_by_risk）。
    """
    out = []
    day = now.strftime("%Y-%m-%d")
    if now.hour < 13:
        return out  # 只在下午窗口（13:00 后）执行
    guard = float(runner.CFG.get("risk", {}).get("price_guard_pct", 0.02))
    conf_floor = _resubmit_conf_floor()
    rows = conn.execute(
        "SELECT id, code, target_weight, confidence, input_snapshot FROM decision"
        " WHERE run_date=? AND action='buy' AND status='rejected' ORDER BY id",
        (day,)).fetchall()
    for rid, code, tw, conf, snap_raw in rows:
        if tw is None or tw <= 0:
            continue
        # 只重提"因规则4 被拒"的 buy（其他拒因的黑名单/风控单不复活）
        n = conn.execute(
            "SELECT COUNT(*) FROM risk_event WHERE rule='risk_check'"
            " AND detail LIKE '非交易时段%' AND decision_id=?",
            (rid,)).fetchone()[0]
        if not n:
            out.append({"orig_id": rid, "code": code,
                        "outcome": "skip_not_session_rejected"})
            continue
        # 幂等：同日已存在本函数生成的重提单
        like_a = '%%"afternoon_resubmit_of": %d%%' % rid
        n2 = conn.execute(
            "SELECT COUNT(*) FROM decision WHERE run_date=?"
            " AND (input_snapshot LIKE ? OR input_snapshot LIKE ?)",
            (day, like_a,
             '%%"afternoon_resubmit_of":%d%%' % rid)).fetchone()[0]
        if n2:
            out.append({"orig_id": rid, "code": code,
                        "outcome": "skip_already_resubmitted"})
            continue
        if (conf or 0) < conf_floor:
            from risk.engine import record_event
            record_event(conn, "afternoon_resubmit",
                         "decision#%d %s 重提弃：conf %.2f < 门槛 %.2f"
                         % (rid, code, conf or 0, conf_floor), rid)
            out.append({"orig_id": rid, "code": code,
                        "outcome": "skip_conf_below",
                        "detail": "conf %.2f < %.2f" % (conf or 0, conf_floor)})
            continue
        q = live.get(code)
        if not q or q.get("price") is None:
            out.append({"orig_id": rid, "code": code,
                        "outcome": "skip_no_live_price"})
            continue
        px = float(q["price"])
        # 原委托价漂移检查（>±2% 价格保护线不硬提，留痕）
        orig_price = None
        try:
            orig = json.loads(snap_raw or "{}")
            if isinstance(orig, dict) and isinstance(orig.get("order"), dict):
                orig_price = orig["order"].get("price")
            elif isinstance(orig, dict):
                decs = orig.get("decisions")
                if isinstance(decs, list) and decs \
                        and isinstance(decs[0], dict):
                    orig_price = (decs[0].get("order") or {}).get("price")
        except (ValueError, TypeError):
            orig_price = None
        if orig_price:
            drift = abs(px / float(orig_price) - 1)
            if drift > guard:
                from risk.engine import record_event
                record_event(conn, "afternoon_resubmit",
                             "decision#%d %s 重提弃：最新价 %.2f 对原委托价 "
                             "%.2f 漂移 %.2f%% 超 ±%.0f%% 价格保护线（不硬提）"
                             % (rid, code, px, float(orig_price), drift * 100,
                                guard * 100), rid)
                out.append({"orig_id": rid, "code": code,
                            "outcome": "skip_drift_over_guard",
                            "detail": "drift %.2f%%" % (drift * 100)})
                continue
        # 股数按新价重算 100 股整手（金额口径 = equity × 目标权重）
        equity = float(ctx.total_equity or 0)
        shares = int(equity * float(tw) / px / 100) * 100 if equity > 0 else 0
        if shares < 100:
            out.append({"orig_id": rid, "code": code,
                        "outcome": "skip_no_lot",
                        "detail": "equity×%.0f%%@%.2f 不足一手" % (tw * 100, px)})
            continue
        from risk.engine import record_event
        new_decision = {
            "action": "buy", "code": code,
            "target_weight": float(tw), "confidence": float(conf),
            "reasons": [
                "[afternoon重提] 原 decision#%d 于午间被规则4（非交易时段）拒绝，"
                "13:00 后按最新价 %.2f 重估重提（conf %.2f ≥ 门槛 %.2f）"
                % (rid, px, float(conf), conf_floor)],
            "risk_notes": ["系统自动重提单：走完整风控链与人工闸门，"
                           "原决策理由见 decision#%d" % rid],
            "order": {"side": "buy", "price": px, "shares": shares},
            "afternoon_resubmit_of": int(rid),
        }
        v = runner.propose(conn, new_decision, decision_id=None,
                           run_date=day, now=now, orders_dir=orders_dir)
        if v.approved:
            record_event(conn, "afternoon_resubmit",
                         "decision#%d %s 重提成功：新价 %.2f × %d 股，挂人工闸门"
                         % (rid, code, px, shares), rid)
            out.append({"orig_id": rid, "code": code, "outcome": "resubmitted",
                        "detail": "%.2f×%d" % (px, shares)})
        else:
            record_event(conn, "afternoon_resubmit",
                         "decision#%d %s 重提被风控拒：%s"
                         % (rid, code, "; ".join(v.violations)[:160]), rid)
            out.append({"orig_id": rid, "code": code,
                        "outcome": "rejected_by_risk",
                        "detail": "; ".join(v.violations)[:160]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="下午复核输入包")
    ap.add_argument("--now", default=None, help="覆盖当前时间（回放/测试）")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()
    replay = args.now is not None
    day = now.strftime("%Y-%m-%d")
    since = "%s %02d:%02d:00" % (day, SINCE_HOUR, SINCE_MINUTE)

    conn = get_conn()
    try:
        run_date = _latest_run_date(conn)
        # 1) 实时行情（强刷）
        codes = repo.all_codes(conn)
        live = quotes.get_live_prices(codes, force=True)

        # 2) 12:30 以来新增资讯（网络失败降级）
        try:
            news_added = fetch_all(since=since)
        except Exception as e:  # noqa: BLE001
            news_added = {}
            print("[afternoon] 资讯刷新失败（降级继续）: %s" % repr(e)[:120])

        # 3) 风控上下文（实时价）与持仓盈亏
        # 13:30 在 is_trading_time 门控内 → build_context 走实时价路径；
        # 与 midday 差异：midday 11:35 需自拉实时快照作为 live_quotes_override；
        # afternoon 13:30 由 build_context 自身读取实时快照，无需 override 注入。
        ctx = runner.build_context(conn, now, live_quotes_override=None)
        peak = ctx.peak_equity or 0.0
        dd = (1 - ctx.total_equity / peak) if peak > 0 else 0.0

        # 3.5) 规则4 拒单自动重提（裁决#2，出手率打磨批 2026-10-08）：
        # 挂在刷新/build_context 之后、LLM 增量复核之前——系统确定性步骤。
        # 回放模式（--now 测试）不重提（产物隔离目录，不产生真实 pending 单）。
        resubmits = []
        if not replay:
            try:
                resubmits = resubmit_session_rejected_buys(
                    conn, live, ctx, now)
            except Exception as e:  # noqa: BLE001
                print("[afternoon] 规则4 拒单重提失败（不阻断）: %s"
                      % repr(e)[:160])
        else:
            print("[afternoon] 回放模式：跳过规则4 拒单重提")

        pos_rows = []
        for code, p in sorted(ctx.positions.items()):
            # 同 midday：持仓表"实时价"列只认 live——缺实时价的票显示 n/a，
            # 不拿昨收冒充实价
            q = live.get(code)
            lp = float(q["price"]) if (q and q.get("price") is not None) else None
            day_chg = None
            if q and q.get("prev_close"):
                day_chg = (q["price"] / q["prev_close"] - 1) * 100
            pnl = (lp - p["cost"]) * p["shares"] if lp else None
            pos_rows.append((code, p["name"], p["shares"], p["cost"], lp, day_chg, pnl))

        # 4) 全天决策执行情况（含上午 + 午评）
        all_dec = conn.execute(
            "SELECT id, code, action, target_weight, confidence, status, created_at "
            "FROM decision WHERE run_date=? ORDER BY id", (run_date,)).fetchall()

        # 4.5) 黑名单名单动态渲染（同 midday）
        try:
            from risk.blacklist import check_blacklist
            bl = check_blacklist(conn)
            blacklist_note = "、".join(c for c in sorted(bl) if not bl[c][0]) or "无"
        except Exception:  # noqa: BLE001
            blacklist_note = "见盘前输入包"

        # 5) pending 单漂移（同 midday）
        guard = float(runner.CFG.get("risk", {}).get("price_guard_pct", 0.02))
        drifts = []
        for p in runner.list_pending():
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            d = obj.get("decision") or {}
            order = d.get("order") or {}
            price, code = order.get("price"), str(d.get("code") or "")
            q = live.get(code) if code else None
            drift = (abs(q["price"] - float(price)) / float(price)) \
                if (q and price) else None
            drifts.append({
                "id": obj.get("decision_id"), "code": code, "order_price": price,
                "live_price": q["price"] if q else None,
                "drift_pct": round(drift * 100, 2) if drift is not None else None,
                "stale": drift is not None and drift > guard * 0.75,
            })

        # 6) 新新闻（精简）
        fresh = {}
        for code in [""] + codes:
            rows = [r for r in get_recent_news(conn, code=code, days=1)
                    if (r.get("published_at") or "") >= since]
            if rows:
                fresh[code or "market"] = rows[:6]

        # 7) 组装 markdown
        live_ratio = "%d/%d" % (len(live), len(codes))
        stale_note = ""
        if len(live) < len(codes):
            stale_note = ("\n\n> ⚠️ 实时行情仅 %s 票可得（缺实时价的票下列表格显示 n/a），"
                          "决策请以可得数据为限。" % live_ratio)
        if replay:
            stale_note += ("\n\n> ⚠️ 本包由 --now 回放生成（时间口径 %s），"
                           "行情并非真实盘中时点，仅供测试。\n" % now.strftime("%H:%M"))
        lines = [
            "# 下午复核输入包 %s（生成于 %s）" % (run_date, now.strftime("%H:%M:%S")),
            "",
            "> 口径：实时行情（非日线）；12:30 以来增量资讯（午评之后）。"
            "**绝不改动 daily_bar。**" + stale_note,
            "",
            "## 实时行情",
            "| 代码 | 名称 | 实时价 | 较昨收 | 行情时间 | 来源 |",
            "|---|---|---|---|---|---|",
        ]
        for code in codes:
            q = live.get(code)
            if not q:
                lines.append("| %s | | n/a | | | |" % code)
                continue
            chg = (q["price"] / q["prev_close"] - 1) * 100 if q.get("prev_close") else None
            lines.append("| %s | %s | %.2f | %s | %s | %s |" % (
                code, q.get("name", ""), q["price"],
                ("%+.2f%%" % chg) if chg is not None else "n/a",
                q.get("time"), q.get("source")))
        lines += [
            "",
            "## 组合实时状态",
            "- 权益 %.2f（峰值 %.2f，实时回撤 %.2f%%，kill 阈值 %.2f%%，停机至 %s）"
            % (ctx.total_equity, peak, dd * 100,
               float(runner.CFG.get("risk", {}).get("max_drawdown_kill", 0.08)) * 100,
               ctx.kill_switch_until or "—"),
            "- 今日已成交 %d 笔（上限 %d）/ 周换手 %.1f%%（上限 %.0f%%）"
            % (ctx.today_trades, float(runner.CFG.get("risk", {}).get("max_daily_trades", 3)),
               ctx.week_turnover * 100,
               float(runner.CFG.get("risk", {}).get("max_weekly_turnover", 2.0)) * 100),
            "",
            "## 持仓实时盈亏",
            "| 代码 | 名称 | 持股 | 成本 | 实时价 | 较昨收 | 浮动盈亏 |",
            "|---|---|---|---|---|---|---|",
        ]
        for code, name, shares, cost, lp, chg, pnl in pos_rows:
            lines.append("| %s | %s | %d | %.2f | %s | %s | %s |" % (
                code, name, shares, cost,
                ("%.2f" % lp) if lp else "n/a",
                ("%+.2f%%" % chg) if chg is not None else "n/a",
                ("%+.2f" % pnl) if pnl is not None else "n/a"))
        if not pos_rows:
            lines.append("| — | 无持仓（P6 未建仓） | | | | | |")
        lines += ["", "## 全天决策执行情况（盘前+午评）",
                  "| id | 代码 | 动作 | 权重 | 置信度 | 状态 | 生成时间 |",
                  "|---|---|---|---|---|---|---|"]
        for r in all_dec:
            lines.append("| #%d | %s | %s | %.0f%% | %.2f | %s | %s"
                         % (r[0], r[1], r[2], (r[3] or 0) * 100, r[4] or 0, r[5],
                            (r[6] or "")[:11]))
        if not all_dec:
            lines.append("| — | 今日无决策 | | | | | |")
        # 规则4 拒单重提结果（裁决#2）——LLM 复核前系统已处理的确定性步骤
        lines += ["", "## 规则4 拒单重提（系统已处理）", ""]
        if resubmits:
            _outcome_zh = {
                "resubmitted": "✅ 已重提挂人工闸门",
                "rejected_by_risk": "✗ 重提被风控拒",
                "skip_already_resubmitted": "↩ 已有重提单（幂等跳过）",
                "skip_conf_below": "✗ conf 低于门槛",
                "skip_no_live_price": "✗ 无实时价",
                "skip_drift_over_guard": "✗ 漂移超价格保护线（不硬提）",
                "skip_no_lot": "✗ 不足一手",
                "skip_not_session_rejected": "— 非规则4 拒单（不复活）",
            }
            for d in resubmits:
                lines.append("- 原 #%d %s：%s%s"
                             % (d["orig_id"], d["code"],
                                _outcome_zh.get(d["outcome"], d["outcome"]),
                                ("（%s）" % d["detail"]) if d.get("detail") else ""))
        else:
            lines.append("今日无符合重提条件的规则4 拒单")
        lines += ["", "## 待确认单漂移", ""]
        if drifts:
            for d in drifts:
                lines.append("- #%s %s 委托价 %.2f → 实时 %s（漂移 %s%%）%s"
                             % (d["id"], d["code"], d["order_price"],
                                ("%.2f" % d["live_price"]) if d["live_price"] else "n/a",
                                d["drift_pct"],
                                "⚠ 已接近价格保护阈值，建议按新价重提" if d["stale"] else ""))
        else:
            lines.append("无待确认单")
        lines += ["", "## 12:30 以来新增资讯"]
        if fresh:
            for key, rows in fresh.items():
                tag = "市场" if key == "market" else key
                for r in rows:
                    lines.append("- [%s] %s（%s，%s）" % (
                        tag, r.get("title"), r.get("source"), r.get("published_at")))
        else:
            lines.append("下午无新增资讯")
        lines += [
            "",
            "---",
            "## 增量决策输出要求（LLM 必读）",
            "",
            "1. **默认输出 `[]`**：午评结论仍然成立、行情未显著偏离时，不做任何动作；",
            "2. **规则4 拒单已由系统自动重提**——当日午间被规则4（非交易时段）拒绝"
            "的 buy 已在上方「规则4 拒单重提」节处理（最新价重估、conf 达门槛才提、"
            "走完整风控链与人工闸门），你**无需重复输出**该 buy；若新证据让你"
            "改变对该票的判断，输出增量 hold/watch 并说明理由；",
            "3. 仅当出现以下情形才输出增量决策（格式与盘前/午评一致，经"
            " ai/decide.py 校验）：",
            "   - pending 单价格漂移 >1.5% 且你仍维持原判断 → 按**新实时价**重提"
            "（order.price 用实时价，数量按新价重算 100 股整手）；",
            "   - 13:00~13:30 出现足以改变某票结论的新证据 → 增量 buy/sell/hold/watch；",
            "   - 触发风险情形（实时回撤接近 kill 阈值）→ 在 risk_notes 中说明并由 14:50 扫描兜底；",
            "4. 每条决策仍需 ≥2 条理由，必须引用本文件中的具体数字/新闻；",
            "5. 禁止推翻上午已 executed 的成交；禁止交易黑名单票（%s）；" % blacklist_note,
            "6. 所有增量决策同样要过 19 条风控规则与人工闸门；本时点产出后 13:30~14:50"
            " 是连续竞价可执行窗口，confirm 内置风控与执行价二次校验保留。",
        ]
        out_dir = SESSION_DIR / run_date
        if replay:
            out_dir = SESSION_DIR / "test"  # 回放产物隔离，不污染正式 session 目录
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (BUNDLE_FILENAME + ".md")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps({
            "run_date": run_date, "generated_at": now.isoformat(timespec="seconds"),
            "replay": replay, "live": live, "portfolio": {
                "equity": ctx.total_equity, "peak": peak, "drawdown": dd,
                "kill_until": ctx.kill_switch_until.isoformat(timespec="seconds")
                if ctx.kill_switch_until else None},
            "pending_drift": drifts,
            "resubmits": resubmits, "all_decisions": [
                {"id": r[0], "code": r[1], "action": r[2], "status": r[5]}
                for r in all_dec]},
            ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        print("[afternoon] run_date=%s 实时行情 %d/%d，新增资讯 %s"
              % (run_date, len(live), len(codes), news_added))
        print("[afternoon] 规则4 拒单重提：%d 条处理（%s）"
              % (len(resubmits),
                 "; ".join("%s#%d=%s" % (d["code"], d["orig_id"], d["outcome"])
                           for d in resubmits) or "无"))
        print("[afternoon] 组合实时回撤 %.2f%%；待确认单 %d 张（漂移预警 %d）"
              % (dd * 100, len(drifts), sum(1 for d in drifts if d["stale"])))
        print("[afternoon] 输入包：%s" % out)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())