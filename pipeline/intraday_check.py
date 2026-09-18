"""尾盘风控扫描（14:50，纯脚本不激活LLM）：实时回撤 kill 检查 + pending 单价格漂移报告。

职责：
1. 强制刷新实时行情（快照自动审计到 logs/quotes/）；
2. 用实时价重算组合回撤：触及 kill 阈值且仍有持仓 → 构造合成决策走 runner.propose，
   复用风控引擎既定的 kill 路径（清仓 + 停机72h，不受人工闸门约束——方案 §4.1）；
3. 盘点当日未确认 pending 单：实时价 vs 委托价漂移超过保护阈值75%的预警（确认前最后机会）；
4. 生成 logs/reports/intraday-<日期>.md 留痕。

注意：绝不写 daily_bar（盘中部分bar会污染日线历史）；--now 仅用于回放/测试。
退出码：0 正常；2 kill 触发（供调度层告警）。
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import repo                     # noqa: E402
from data.fetcher import get_conn          # noqa: E402
from data import quotes                    # noqa: E402
from execution import runner               # noqa: E402
from execution.paper import PaperBroker    # noqa: E402
from risk.engine import (record_event, stop_loss_breaches,  # noqa: E402
                         stop_loss_line)
from risk.notify import notify             # noqa: E402

# 测试隔离：AGSICKLE_REPORTS_DIR 覆盖产物目录（import 期读 env，与 runner.ORDERS_DIR 同模式）
REPORTS_DIR = Path(os.environ.get("AGSICKLE_REPORTS_DIR") or (BASE / "logs" / "reports"))


def _pending_drift(conn, now: datetime) -> list:
    """未确认 pending 单的价格漂移盘点。"""
    cfg_guard = float(runner.CFG.get("risk", {}).get("price_guard_pct", 0.02))
    rows = []
    for p in runner.list_pending():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        d = obj.get("decision") or {}
        order = d.get("order") or {}
        price, code = order.get("price"), str(d.get("code") or "")
        if not price or not code:
            continue
        live = (quotes.get_live_prices([code]).get(code) or {}).get("price")
        drift = (abs(live - float(price)) / float(price)) if live else None
        rows.append({
            "decision_id": obj.get("decision_id"), "code": code,
            "order_price": float(price), "live_price": live,
            "drift_pct": round(drift * 100, 2) if drift is not None else None,
            "warn": drift is not None and drift > cfg_guard * 0.75,
            "confirm_hint": obj.get("confirm_hint"),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="尾盘风控扫描")
    ap.add_argument("--now", default=None, help="覆盖当前时间（回放/测试）")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()

    conn = get_conn()
    try:
        # 1) 强制刷新实时行情（TTL 失效，确保 14:50 价格是新鲜的）
        codes = repo.all_codes(conn)
        live = quotes.get_live_prices([str(c) for c in codes], force=True)

        # 2) 实时价上下文 + 回撤
        ctx = runner.build_context(conn, now)
        peak = ctx.peak_equity or 0.0
        dd = (1 - ctx.total_equity / peak) if peak > 0 else 0.0
        kill_th = float(runner.CFG.get("risk", {}).get("max_drawdown_kill", 0.08))
        kill_fired = False

        # 3) kill 检查：仅在"有持仓 + 触线 + 未在停机期"时走引擎既定 kill 路径
        if (ctx.positions and dd >= kill_th
                and not (ctx.kill_switch_until and now < ctx.kill_switch_until)):
            first_code = sorted(ctx.positions)[0]
            print("[sweep] 回撤 %.2f%% ≥ %.2f%%，触发 kill 流程（引擎清仓+停机）"
                  % (dd * 100, kill_th * 100))
            runner.propose(conn, {
                "action": "watch", "code": first_code, "target_weight": 0.0,
                "confidence": 0.99,
                "reasons": ["尾盘风控扫描：实时回撤 %.2f%% 触及 kill 阈值 %.2f%%"
                            % (dd * 100, kill_th * 100)],
                "risk_notes": ["合成决策仅用于触发风控引擎 kill 路径"],
            }, decision_id=None, run_date=now.strftime("%Y-%m-%d"), now=now)
            kill_fired = True

        # 3.5) 单票止损扫描（规则16 的盘中执行端：只报告+留痕+通知，不自动卖；
        # 止损线为 ATR 自适应口径——高波票线更宽，见 risk/regime.stop_loss_line）
        risk_cfg = runner.CFG.get("risk", {})
        breaches = stop_loss_breaches(ctx, risk_cfg)
        for code, loss in breaches:
            msg = "单票止损预警：%s 浮亏 %.1f%% ≥ 止损线 %.0f%%，建议人工评估止损卖出" % (
                code, loss * 100, stop_loss_line(ctx, risk_cfg, code) * 100)
            record_event(conn, "stop_loss_alert", msg)
            print("[sweep] %s" % msg)
        if breaches:
            notify("单票止损预警", "；".join(f"{c} {l:.0%}" for c, l in breaches))

        # 3.6) 规则 21 跌停应急主动扫描（Fix-4：stuck 计数 + 5日预警 + ≥3只 kill）
        # Fix B：传当日实时快照判"当日跌停"——缺省回退日线收盘会把条件①退化成
        # 检测昨日的跌停（昨日跌停今日反弹仍计数，3 只即假触发 72h kill）；
        # prev_close 用快照自带昨收（当日跌停价的正确基准）
        try:
            from signals import limit_halt
            lp_live, pc_live = {}, {}
            for c, q in live.items():
                if not isinstance(q, dict):
                    continue
                if q.get("price") is not None:
                    lp_live[str(c)] = float(q["price"])
                if q.get("prev_close") is not None:
                    pc_live[str(c)] = float(q["prev_close"])
            lh = limit_halt.run_intraday_scan(conn, now=now, latest_prices=lp_live,
                                              prev_close=pc_live)
            if lh["hit"]:
                print("[sweep] 跌停应急扫描命中：%s（stuck days=%s）"
                      % (lh["hit"], lh["stuck"]["days"]))
                if lh["enforce"]["kill"]:
                    print("[sweep] 同日 ≥3 只 stuck → kill_switch 已触发")
            else:
                print("[sweep] 跌停应急扫描：无命中")
        except Exception as e:
            print("[sweep] 跌停应急扫描 FAIL（继续）: %r" % e)

        # 4) pending 单漂移
        drifts = _pending_drift(conn, now)

        # 5) 报告
        broker = PaperBroker()
        live_ratio = "%d/%d" % (len(live), len(codes)) if codes else "0/0"
        pos_lines = []
        for code, p in sorted(ctx.positions.items()):
            lp = ctx.latest_prices.get(code)
            pnl = (lp - p["cost"]) * p["shares"] if lp else None
            pos_lines.append("| %s %s | %d | %.2f | %s | %s |" % (
                code, p["name"], p["shares"], p["cost"],
                ("%.2f" % lp) if lp else "n/a",
                ("%+.2f" % pnl) if pnl is not None else "n/a"))
        md = [
            "# 尾盘风控扫描 %s" % now.strftime("%Y-%m-%d %H:%M"),
            "",
            "- 实时行情：%s 票可得" % live_ratio,
            "- 组合实时权益：%.2f（峰值 %.2f，实时回撤 %.2f%%，kill 阈值 %.2f%%）"
            % (ctx.total_equity, peak, dd * 100, kill_th * 100),
            "- kill 状态：%s" % ("已触发清仓+停机" if kill_fired else
                                 ("停机中（至 %s）" % ctx.kill_switch_until if ctx.kill_switch_until else "正常")),
        ]
        if breaches:
            md.append("- ⚠️ 单票止损预警：" +
                      "；".join("%s 浮亏 %.1f%%" % (c, l * 100) for c, l in breaches))
        md += [
            "",
            "## 持仓实时盈亏",
            "| 代码 | 名称 | 持股 | 成本 | 实时价 | 浮动盈亏 |",
            "|---|---|---|---|---|---|",
        ] + pos_lines
        md += ["", "## 待确认单价格漂移"]
        if drifts:
            md += ["| decision | 代码 | 委托价 | 实时价 | 漂移 | 预警 |",
                   "|---|---|---|---|---|---|"]
            for r in drifts:
                md.append("| #%s | %s | %.2f | %s | %s | %s |" % (
                    r["decision_id"], r["code"], r["order_price"],
                    ("%.2f" % r["live_price"]) if r["live_price"] else "n/a",
                    ("%s%%" % r["drift_pct"]) if r["drift_pct"] is not None else "n/a",
                    "⚠ 确认将被价格保护拒绝，建议重提" if r["warn"] else "正常"))
        else:
            md.append("无待确认单")
        text = "\n".join(md)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORTS_DIR / ("intraday-%s.md" % now.strftime("%Y-%m-%d"))
        out.write_text(text + "\n", encoding="utf-8")
        print(text)
        print("[sweep] 报告：%s" % out)
        return 2 if kill_fired else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
