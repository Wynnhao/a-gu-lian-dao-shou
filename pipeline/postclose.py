"""盘后流水线（15:30 触发）：补齐当日日线 -> 盯市 -> 每日复盘报告（周五或 --weekly 加跑周报）。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import logging
import logging.handlers
import os
from datetime import date, datetime
from typing import Optional

from data import fetcher
from review import daily, weekly

log = logging.getLogger("pipeline.postclose")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _fh = logging.handlers.RotatingFileHandler(BASE / "logs" / "pipeline.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3)
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
log.propagate = False


# 测试隔离：AGSICKLE_REPORTS_DIR 覆盖产物目录（import 期读 env，与 runner.ORDERS_DIR 同模式）
REPORTS_DIR = Path(os.environ.get("AGSICKLE_REPORTS_DIR") or (BASE / "logs" / "reports"))


def _write_pending(trade_date: str, today_iso: str) -> Path:
    """今日 daily_bar 缺失时,写 PENDING-YYYY-MM-DD.md 兜底（绝不覆写昨日日报）。"""
    pending_path = REPORTS_DIR / ("PENDING-%s.md" % today_iso)
    body = (
        "# 待清算日报 %s\n\n"
        "> 生成时间：%s｜盘后流水线检测到今日 daily_bar 仍为空\n\n"
        "## 状态\n"
        "- 库内最新交易日：**%s**（早于今日 %s）\n"
        "- 当前持仓按 %s 盯市（口径：portfolio_state.date=%s）\n"
        "- 今日真实盈亏未入账\n\n"
        "## 处置\n"
        "- **不要**根据本文件做任何下单决策；盯市与止损沿用上一交易日数据。\n"
        "- 数据源恢复后跑：\n"
        "  ```\n"
        "  python -m pipeline.catchup --date %s\n"
        "  python -m pipeline.postclose --date %s\n"
        "  ```\n"
        "- 止损自检（步骤2.5）已独立于 daily_bar 完成，详见 logs/risk_event 表。\n"
    ) % (today_iso, datetime.now().isoformat(timespec="seconds"),
         trade_date, today_iso, trade_date, trade_date,
         today_iso, today_iso)
    pending_path.write_text(body, encoding="utf-8")
    log.warning("今日 daily_bar 缺失，写 PENDING 兜底 → %s", pending_path)
    return pending_path


def _postclose_stop_loss_check(conn, now: datetime) -> list:
    """盘后止损自检（与 daily_bar 入库状态解耦，强制走实时价）。

    复用 intraday_check.py 的实时价 + stop_loss_breaches 路径：扫到 breaches
    只留痕 + notify，不自动生成 sell decision（用户协议：仅留痕 + 告警）。

    注意：build_context 在 is_trading_time()=False 时不读 live_quotes，会回退
    daily_bar 最新收盘，所以这里直接读 live quotes + position 表自己组装 breaches。
    """
    from data import quotes as _quotes
    from execution import runner as _runner
    from risk.engine import record_event
    from risk.notify import notify
    from risk.regime import stop_loss_line as _line, latest_atr_pct as _atr

    codes = [r[0] for r in conn.execute("SELECT code FROM stock_info").fetchall()]
    if not codes:
        log.info("步骤2.5 止损自检：stock_info 为空，跳过")
        return []
    live = _quotes.get_live_prices([str(c) for c in codes], force=True)
    risk_cfg = _runner.CFG.get("risk", {}) or {}
    base = float(risk_cfg.get("stop_loss_pct", 0.08) or 0)
    mult = float(risk_cfg.get("atr_stop_mult", 2.0) or 2.0)
    atr_pct = _atr(conn, codes) if base > 0 else {}

    breaches = []
    for code in codes:
        pos_row = conn.execute(
            "SELECT cost, shares FROM position WHERE code=? AND shares>0",
            (code,)).fetchone()
        if not pos_row:
            continue
        cost = float(pos_row[0] or 0)
        if cost <= 0:
            continue
        q = live.get(code) or {}
        price = q.get("price")
        if price is None or float(price) <= 0:
            continue
        loss = 1.0 - float(price) / cost
        line = _line(base, atr_pct.get(code), mult)
        if loss >= line - 1e-12:
            breaches.append((code, loss, line))
    for code, loss, line in breaches:
        msg = "盘后止损自检：%s 浮亏 %.1f%% ≥ 止损线 %.0f%%，建议人工评估止损卖出" % (
            code, loss * 100, line * 100)
        try:
            record_event(conn, "postclose_stop_loss_alert", msg)
        except Exception as e:  # noqa: BLE001
            log.error("record_event 写 risk_event FAIL: %s", repr(e))
        log.warning(msg)
    if breaches:
        notify("单票止损预警",
               "；".join("%s %.1f%%" % (c, l * 100) for c, _, _ in breaches))
    return breaches


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="盘后流水线：盯市 + 每日复盘报告（周五/指定时含周报）")
    ap.add_argument("--date", default=None, dest="trade_date",
                    help="交易日 YYYY-MM-DD（默认 daily_bar 最新交易日）")
    ap.add_argument("--weekly", action="store_true", help="强制加跑周度复盘报告")
    ap.add_argument("--skip-stop-loss-check", action="store_true",
                    help="跳过步骤2.5 止损自检（调试用）")
    args = ap.parse_args(argv)
    log.info("==== postclose start ====")

    # 1. 补齐当日日线
    try:
        fetcher.run()
    except Exception as e:
        log.error("步骤1 fetcher.run FAIL（继续用已有数据复盘）: %s", repr(e))

    # 1.5 动态池刷新（收盘正式口径：异动规则基于当日完整日线）
    try:
        conn0 = fetcher.get_conn()
        try:
            from signals import movers, hot
            m = movers.refresh(conn0)
            h = hot.refresh(conn0)
            log.info("步骤1.5 动态池：异动 %d 只（%s口径）、热门题材 %d/个股 %d",
                     m["count"], m["mode"], len(h["themes"]), len(h["stocks"]))
        finally:
            conn0.close()
    except Exception as e:
        log.error("步骤1.5 动态池 FAIL（继续）: %s", repr(e))

    # 2. 盯市 + 每日复盘报告
    conn = fetcher.get_conn()
    try:
        trade_date = args.trade_date or daily.latest_trade_date(conn)

        # 2.0 数据体检 + 库备份（此前无任何备份，SQLite 文件级损坏即全损）
        try:
            from data import audit as data_audit
            r = data_audit.run(backup=True)
            log.info("步骤2.0 数据体检: %s", r["by_kind"] or "无问题")
            if r["total"]:
                log.warning("数据体检发现 %d 个问题（详见 logs/audit.log）", r["total"])
        except Exception as e:  # noqa: BLE001
            log.error("步骤2.0 数据体检/备份 FAIL（继续）: %s", repr(e))

        # 2.1 决策结果回填（t1_ret/direction_hit——决策→结果闭环此前完全缺失）
        try:
            n = daily.backfill_decision_outcomes(conn, trade_date)
            if n:
                log.info("步骤2.1 决策结果回填 %d 条", n)
        except Exception as e:  # noqa: BLE001
            log.error("步骤2.1 决策结果回填 FAIL（继续）: %s", repr(e))

        # 2.5 实时价止损自检（与 daily_bar 入库状态解耦；规则16 盘中执行端扩展到盘后）
        if not args.skip_stop_loss_check:
            try:
                breaches = _postclose_stop_loss_check(conn, datetime.now())
                if breaches:
                    log.warning("步骤2.5 止损自检：发现 %d 条 breaches", len(breaches))
                else:
                    log.info("步骤2.5 止损自检：未发现 breaches")
            except Exception as e:  # noqa: BLE001
                log.error("步骤2.5 止损自检 FAIL（继续）: %s", repr(e))
    finally:
        conn.close()

    today_iso = date.today().isoformat()
    # 2.x daily_bar 缺失日：跳过盯市与日报（避免覆写昨日），写 PENDING + notify + exit=2
    if trade_date < today_iso and not args.trade_date:
        try:
            from risk.notify import notify as _notify
            pending_path = _write_pending(trade_date, today_iso)
            _notify("盘后数据缺失",
                    "今日 %s daily_bar 仍为空（latest=%s）；已写 PENDING 兜底，需 catchup 补跑。"
                    % (today_iso, trade_date))
        except Exception as e:  # noqa: BLE001
            log.error("PENDING 兜底写盘 FAIL（继续）: %s", repr(e))
        log.warning("==== postclose 提前退出 exit=2（数据缺失） ====")
        print("[postclose] 今日 daily_bar 缺失，已写 PENDING-YYYY-MM-DD.md 兜底，需 catchup --date %s 补跑" % today_iso)
        return 2

    try:
        st = daily.mark_to_market(trade_date)
        log.info("步骤2 盯市完成 %s: total=%.2f drawdown=%.2f%% kill_switch=%s",
                 st["trade_date"], st["total"], st["drawdown"] * 100, st["kill_switch"])
    except Exception as e:
        st = {}
        log.error("步骤2 mark_to_market FAIL: %s", repr(e))
        try:
            from risk.notify import notify as _notify
            _notify("盘后盯市失败", repr(e)[:200])
        except Exception:
            pass
    try:
        report_path = daily.generate_daily_report(trade_date)
    except Exception as e:
        log.error("步骤2 generate_daily_report FAIL: %s", repr(e))
        try:
            from risk.notify import notify as _notify
            _notify("日报生成失败", repr(e)[:200])
        except Exception:
            pass
        print("[postclose] 每日报告生成失败：%r" % e)
        return 1

    # 3. 周五（或 --weekly）加跑周报
    weekly_path: Optional[Path] = None
    if date.today().weekday() == 4 or args.weekly:
        try:
            weekly_path = weekly.weekly_report(trade_date)
            log.info("步骤3 周报完成: %s" % weekly_path)
        except Exception as e:
            log.error("步骤3 weekly_report FAIL（不影响日报）: %s", repr(e))
    else:
        log.info("步骤3 今天非周五且未指定 --weekly，跳过周报")

    # 4. 输出
    print("[postclose] ===== 盘后流程完成 trade_date=%s =====" % trade_date)
    if st:
        print("[postclose] 盯市: 现金=%.2f 市值=%.2f 总资产=%.2f 回撤=%.2f%% kill_switch=%s"
              % (st.get("cash", 0.0), st.get("market_value", 0.0), st.get("total", 0.0),
                 st.get("drawdown", 0.0) * 100, st.get("kill_switch", 0)))
    print("[postclose] 每日报告: %s" % report_path)
    if weekly_path:
        print("[postclose] 周度报告: %s" % weekly_path)
    log.info("==== postclose done exit=0 ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
