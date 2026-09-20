"""盘后流水线（15:30 触发）：补齐当日日线 -> 盯市 -> 每日复盘报告（周五或 --weekly 加跑周报）。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import fcntl
import logging
import logging.handlers
import os
from datetime import date, datetime, timedelta
from typing import Optional

from data import fetcher
from data import repo
from review import daily, weekly

log = logging.getLogger("pipeline.postclose")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    _fh = rotating_handler("pipeline.log")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
log.propagate = False


# 测试隔离：AGSICKLE_REPORTS_DIR 覆盖产物目录（import 期读 env，与 runner.ORDERS_DIR 同模式）
REPORTS_DIR = Path(os.environ.get("AGSICKLE_REPORTS_DIR") or (BASE / "logs" / "reports"))

# 单实例锁：catchup 步骤4 调起 postclose 子进程，与 15:30 cron 同时触发时只允许一份跑。
# POSIX flock 在进程退出时由内核自动释放（close-on-exec 语义），无需 finally 显式 unlock。
LOCK_FILE = BASE / "logs" / ".postclose.lock"


def _weekly_cleanup(conn, now: Optional[datetime] = None) -> dict:
    """周度清理（C-ARC-3b/T7，周五或 --weekly 执行）：
    - minute_snapshot 留 2 年（盘中回放用不了更久，存储 ~100 万行/年量级）；
    - logs/quotes/*.jsonl 留 90 天（录制器不写 jsonl，但 confirm/盯市路径仍写，
      审核发现的存量无清理问题一并纳入）。

    jsonl 目录支持 AGSICKLE_QUOTES_DIR 调用时读（测试隔离）。返回清理计数。
    """
    now = now or datetime.now()
    cutoff = (now - timedelta(days=730)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM minute_snapshot WHERE ts < ?", (cutoff,))
    n_rows = cur.rowcount
    conn.commit()
    quotes_dir = Path(os.environ.get("AGSICKLE_QUOTES_DIR")
                      or (BASE / "logs" / "quotes"))
    n_files = 0
    cutoff_ts = (now - timedelta(days=90)).timestamp()
    if quotes_dir.is_dir():
        for f in quotes_dir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff_ts:
                    f.unlink()
                    n_files += 1
            except OSError:
                continue
    return {"minute_rows": max(0, n_rows), "jsonl_files": n_files}


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
        "- 数据源恢复后跑（catchup 自动检测缺口并补齐，无需参数）：\n"
        "  ```\n"
        "  .venv/bin/python3 pipeline/catchup.py\n"
        "  .venv/bin/python3 pipeline/postclose.py --date %s\n"
        "  ```\n"
        "- 止损自检（步骤2.5）已独立于 daily_bar 完成，详见 logs/risk_event 表。\n"
    ) % (today_iso, datetime.now().isoformat(timespec="seconds"),
         trade_date, today_iso, trade_date, trade_date,
         today_iso)
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

    codes = repo.all_codes(conn)
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


def _clear_pending_today(trade_date: Optional[str] = None) -> bool:
    """W-D1 代码部分（P1-17）：postclose 成功路径顺手清当日 PENDING 兜底文件。

    P2⑲（全量打包批 2026-09-20）：仅当本次成功的 postclose 跑的就是"今日"
    （trade_date==今天）才清——`--date 补历史` 成功不得清今日 PENDING：
    今日 daily_bar 仍缺时该提醒物必须保留（此前成功尾部一律 unlink 今日
    PENDING，历史补跑成功会误删今日提醒）。删除失败不阻断（只影响兜底
    文件残留，下次成功 postclose 再清）。返回是否实际清除。
    """
    today_iso = date.today().isoformat()
    if trade_date is not None and trade_date != today_iso:
        return False   # --date 非今日的成功补跑：今日 PENDING 与它无关
    pending_today = REPORTS_DIR / ("PENDING-%s.md" % today_iso)
    try:
        if pending_today.is_file():
            pending_today.unlink()
            log.info("当日 PENDING 兜底文件已清除（postclose 成功）: %s",
                     pending_today)
            return True
    except OSError as e:
        log.warning("当日 PENDING 清除失败（不阻断）: %s", e)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="盘后流水线：盯市 + 每日复盘报告（周五/指定时含周报）")
    ap.add_argument("--date", default=None, dest="trade_date",
                    help="交易日 YYYY-MM-DD（默认 daily_bar 最新交易日）")
    ap.add_argument("--weekly", action="store_true", help="强制加跑周度复盘报告")
    ap.add_argument("--skip-stop-loss-check", action="store_true",
                    help="跳过步骤2.5 止损自检（调试用）")
    args = ap.parse_args(argv)

    # 单实例锁：避免 catchup 子进程 + cron 双触发并发写 market.db。
    # 锁文件 mtime 由内核维护；进程死亡立即释放，不依赖 stale 阈值。
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _lock_fh = open(LOCK_FILE, "w")
    except OSError as e:
        log.error("postclose 锁文件初始化失败：%s", e)
        return 1
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("已有实例在跑（锁 %s 被占），本次退出", LOCK_FILE)
        print("[postclose] 已有实例在跑（锁 %s 被占），本次退出" % LOCK_FILE)
        return 0

    log.info("==== postclose start ====")

    # 1. 补齐当日日线
    try:
        fetcher.run()
    except Exception as e:
        log.error("步骤1 fetcher.run FAIL（继续用已有数据复盘）: %s", repr(e))

    # 1.1 指数日线刷新（W-B2 / P1-9 / P0-5 守卫的根因修复：日报基准此前无任何
    # 盘后自动刷新链路，缺当日行时 cur/prev 塌缩同一天 → 恒 +0.00%。自开短连接
    # 逐码 ensure，失败仅告警；AGSICKLE_DISABLE_FETCHER=1 时与日线采集同门短路）
    if os.environ.get("AGSICKLE_DISABLE_FETCHER") != "1":
        try:
            from data.macro import INDEX_CODES
            _conn_idx = fetcher.get_conn()
            try:
                for _code in INDEX_CODES:
                    n_idx = fetcher.ensure_index_daily(_conn_idx, _code)
                    log.info("步骤1.1 ensure_index_daily %s: +%s 行", _code, n_idx)
            finally:
                _conn_idx.close()
        except Exception as e:  # noqa: BLE001
            log.error("步骤1.1 指数日线刷新 FAIL（继续）: %s", repr(e))

    # 1.15 市场宽度补采（W-B6 / P1-13：此前只有盘前 9:00 采集，当日收盘后的
    # 涨跌停/涨跌家数无采集点，advance_decline_ratio 长期为空）
    if os.environ.get("AGSICKLE_DISABLE_FETCHER") != "1":
        try:
            from data import breadth as _breadth
            _conn_br = fetcher.get_conn()
            try:
                br = _breadth.fetch_breadth_daily(conn=_conn_br)
                log.info("步骤1.15 市场宽度补采：source=%s, 涨停=%s, 跌停=%s, ADR=%s",
                         br.get("source"), br.get("limit_up_count"),
                         br.get("limit_down_count"), br.get("advance_decline_ratio"))
            finally:
                _conn_br.close()
        except Exception as e:  # noqa: BLE001
            log.error("步骤1.15 市场宽度补采 FAIL（继续）: %s", repr(e))

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
    stale_note: Optional[str] = None
    try:
        trade_date = args.trade_date or daily.latest_trade_date(conn)

        # W-D3（P1-18）：带 --date 补跑且目标日 daily_bar 缺行 → 不再无痕放行。
        # 此前守卫 `if trade_date < today_iso and not args.trade_date` 对 --date
        # 完全短路——09-18 实证：零行 09-18 日线的情况下照样出了"正式"日报。
        # 现在缺行时：报告头加"价格滞后"标注（stale_note 传入日报生成）+
        # notify 一次 + risk_event 留痕（降级可有痕，不可无痕）。
        if args.trade_date:
            try:
                n_bars = conn.execute(
                    "SELECT COUNT(*) FROM daily_bar WHERE trade_date=?",
                    (trade_date,)).fetchone()[0]
            except Exception as e:  # noqa: BLE001
                n_bars = 0
                log.error("W-D3 守卫查询 daily_bar FAIL（按缺行处理）: %s", repr(e))
            if not n_bars:
                stale_note = ("⚠ 价格滞后（目标日 %s 无日线，盯市基于最近可得收盘）"
                              % trade_date)
                log.warning("W-D3 --date 守卫：%s", stale_note)
                try:
                    from risk.engine import record_event as _record_event
                    _record_event(conn, "postclose_price_stale",
                                  "postclose --date %s 目标日 daily_bar 缺行，"
                                  "盯市/日报基于最近可得收盘（P1-18 守卫）" % trade_date)
                except Exception as e:  # noqa: BLE001
                    log.error("W-D3 --date 守卫 risk_event 留痕 FAIL（继续）: %s", repr(e))
                try:
                    from risk.notify import notify as _notify
                    _notify("盘后补跑价格滞后", stale_note)
                except Exception as e:  # noqa: BLE001
                    log.error("W-D3 --date 守卫 notify FAIL（继续）: %s", repr(e))

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
        print("[postclose] 今日 daily_bar 缺失，已写 PENDING-YYYY-MM-DD.md 兜底，需 catchup 补跑（无参数，自动检测缺口）")
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
        report_path = daily.generate_daily_report(trade_date, stale_note=stale_note)
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

    # 3.5 规则 21 跌停应急主动扫描（Fix-4 / D1：扫描 → propose 应急单（run_date=次日）
    # → 通知；confirm 优先，次日 09:14 未确认由 premarket 兜底自动执行）
    try:
        from signals import limit_halt
        r = limit_halt.run_postclose_scan()
        log.info("步骤3.5 跌停应急扫描：scanned=%s proposed=%s skipped=%s",
                 r["scanned"], r["proposed"], r["skipped"])
    except Exception as e:
        log.error("步骤3.5 跌停应急扫描 FAIL（继续）: %s", repr(e))

    # 3.6 周度（周五或 --weekly）清理：minute_snapshot 留 2 年、盘中快照审计
    # jsonl 留 90 天（C-ARC-3b/T7）
    if date.today().weekday() == 4 or args.weekly:
        try:
            _conn = fetcher.get_conn()
            try:
                r = _weekly_cleanup(_conn)
                log.info("步骤3.6 周度清理：minute_snapshot 删 %d 行、quotes jsonl 删 %d 个",
                         r["minute_rows"], r["jsonl_files"])
            finally:
                _conn.close()
        except Exception as e:  # noqa: BLE001
            log.error("步骤3.6 周度清理 FAIL（继续）: %s", repr(e))

    # 4. 输出
    print("[postclose] ===== 盘后流程完成 trade_date=%s =====" % trade_date)
    if st:
        print("[postclose] 盯市: 现金=%.2f 市值=%.2f 总资产=%.2f 回撤=%.2f%% kill_switch=%s"
              % (st.get("cash", 0.0), st.get("market_value", 0.0), st.get("total", 0.0),
                 st.get("drawdown", 0.0) * 100, st.get("kill_switch", 0)))
    print("[postclose] 每日报告: %s" % report_path)
    if weekly_path:
        print("[postclose] 周度报告: %s" % weekly_path)

    # 5. W-D1 代码部分（P1-17）：postclose 成功路径顺手清当日 PENDING 兜底文件。
    # 此前清除逻辑只在 catchup 步骤4（launchd 死亡期间从未运行）——PENDING-09-17/18.md
    # 至今残留。盯市/日报已完成（走到这里即 exit 0），当日 PENDING 已无意义；
    # P2⑲：--date 补历史成功不清今日 PENDING（见 _clear_pending_today）。
    _clear_pending_today(trade_date)

    log.info("==== postclose done exit=0 ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
