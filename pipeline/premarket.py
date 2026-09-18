"""盘前流水线（9:00 触发）：行情->资讯->估值->体检->T+1解锁->补清算->信号->决策输入包，失败降级不中断。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import logging
import logging.handlers
from datetime import date, datetime

from data import fetcher, news
from data import macro as macro_mod
from risk.blacklist import check_blacklist, health_check
from risk.engine import record_event
from risk.notify import notify
from signals.signals import compute_all
from ai import bundle as ai_bundle

log = logging.getLogger("pipeline.premarket")
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

WATCHDOG_HEARTBEAT = BASE / "logs" / "state" / "catchup_heartbeat"
WATCHDOG_STALE_HOURS = 3


def check_watchdog(conn) -> list:
    """看门狗心跳体检：catchup 心跳文件超过 3 小时未更新 → 告警留痕。

    此前 launchd 因 TCC 授权失败连续 13+ 次拉起 catchup 全部静默失败，
    系统其他部分完全不感知，三层兜底实际只剩 cron 一层。
    """
    problems = []
    now = datetime.now()
    if now.weekday() < 5:
        try:
            mtime = datetime.fromtimestamp(WATCHDOG_HEARTBEAT.stat().st_mtime)
            age_h = (now - mtime).total_seconds() / 3600
            if age_h > WATCHDOG_STALE_HOURS:
                problems.append("看门狗心跳已停 %.0f 小时（%s），盘中回撤兜底可能失效"
                                % (age_h, mtime.strftime("%m-%d %H:%M")))
        except FileNotFoundError:
            problems.append("看门狗心跳文件不存在（看门狗从未成功运行？检查 launchd 授权）")
        except OSError:
            pass
    for p in problems:
        log.warning(p)
        try:
            record_event(conn, "watchdog_stale", p)
        except Exception:  # noqa: BLE001
            pass
    return problems


def refresh_bond_etf() -> None:
    """步骤 3.5：10Y 国债收益率 + ETF 份额刷新（Fix-2 pipeline 接入）。

    regime.compute_regime 消费这两张表；任一失败只 warning 继续，不阻断盘前。
    """
    try:
        macro_mod.fetch_bond_yield()
        log.info("步骤3.5 fetch_bond_yield 完成")
    except Exception as e:
        log.error("步骤3.5 fetch_bond_yield FAIL（继续）: %s", repr(e))
    try:
        macro_mod.fetch_etf_share()
        log.info("步骤3.5 fetch_etf_share 完成")
    except Exception as e:
        log.error("步骤3.5 fetch_etf_share FAIL（继续）: %s", repr(e))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="盘前流水线：数据准备 + 决策输入包落盘")
    ap.add_argument("--date", default=None, dest="run_date",
                    help="运行日期 YYYY-MM-DD（默认今天=预期执行日；证据自动取最新交易日）")
    args = ap.parse_args(argv)
    log.info("==== premarket start ====")

    # 0.5 跌停应急单超时兜底（Fix-4 / D1：昨日未 confirm 的 emergency_scan 单，
    # 09:14 后自动 confirm(confirmed_by=emergency_timeout_failsafe)；此前仅通知）
    try:
        from signals import limit_halt
        fs = limit_halt.premarket_failsafe()
        if fs.get("pending"):
            log.info("步骤0.5 应急单兜底：pending=%s executed=%s notified=%s",
                     fs["pending"], fs["executed"], fs["notified"])
    except Exception as e:
        log.error("步骤0.5 应急单兜底 FAIL（继续）: %s", repr(e))

    # 0. 看门狗心跳体检（兜底层自检——此前兜底失效无人知晓）
    watchdog_problems: list = []

    # 1. 增量行情
    try:
        fetcher.run()
    except Exception as e:
        log.error("步骤1 fetcher.run FAIL（继续）: %s", repr(e))

    conn = fetcher.get_conn()
    try:
        latest = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()[0]
        if not latest:
            msg = "行情全失败：daily_bar 无任何数据，放弃盘前流程"
            log.error(msg)
            print("[premarket] " + msg)
            return 1
        run_date = args.run_date or date.today().isoformat()
        watchdog_problems = check_watchdog(conn)

        # 2. 资讯
        try:
            news_stats = news.fetch_all()
            log.info("步骤2 资讯完成: %s", news_stats)
        except Exception as e:
            news_stats = {}
            log.error("步骤2 news.fetch_all FAIL（继续）: %s", repr(e))

        # 3. 指数估值：最新日期距今 >3 天才刷新（省时）
        try:
            row = conn.execute("SELECT MAX(trade_date) FROM index_valuation").fetchone()[0]
            lag = ((date.today() - datetime.strptime(str(row)[:10], "%Y-%m-%d").date()).days
                   if row else 9999)
            if lag > 3:
                log.info("步骤3 index_valuation 最新 %s 滞后 %d 天 >3，刷新估值", row, lag)
                macro_mod.fetch_index_valuation()
            else:
                log.info("步骤3 index_valuation 最新 %s（滞后 %d 天 ≤3），跳过", row, lag)
        except Exception as e:
            log.error("步骤3 估值刷新 FAIL（继续）: %s", repr(e))

        # 3.5 国债收益率 + ETF 份额（Sprint 2 任务 1 / Fix-2：接 pipeline 防数据永陈旧）
        refresh_bond_etf()

        # 4. 黑名单 + 数据健康
        try:
            bl = check_blacklist(conn)
        except Exception as e:
            bl = {}
            log.error("步骤4 check_blacklist FAIL（继续）: %s", repr(e))
        try:
            issues = health_check(conn)
        except Exception as e:
            issues = ["health_check 自身失败: %s" % repr(e)]
            log.error("步骤4 health_check FAIL（继续）: %s", repr(e))
        if issues:
            log.warning("【今日只出报告不下单】数据健康异常: %s", "；".join(issues))

        # 5. T+1 解锁：隔夜后昨日买入全部可卖（当日买入除外——盘中补跑不得提前解锁）
        try:
            from execution.paper import PaperBroker
            n = PaperBroker().unlock_t_plus_1(conn)
            log.info("步骤5 T+1 解锁完成，更新 %d 行", n)
        except Exception as e:
            log.error("步骤5 T+1 解锁 FAIL（继续）: %s", repr(e))

        # 5.5 kill 递延补清算（此前 T+1 不可卖的残仓会在停机期锁死 72 小时）
        try:
            from execution import runner
            with runner._exec_lock():
                n_liq = runner.resolve_liquidations(conn)
            if n_liq:
                log.info("步骤5.5 kill 补清算 propose %d 单", n_liq)
        except Exception as e:
            log.error("步骤5.5 补清算 FAIL（继续）: %s", repr(e))

        # 6. 信号
        try:
            sigs = compute_all()
            log.info("步骤6 信号计算完成: %d 票", len(sigs))
        except Exception as e:
            sigs = []
            log.error("步骤6 signals.compute_all FAIL（继续）: %s", repr(e))

        # 6.5 动态池（异动/热门）：盘前禁用全市场快照口径（9:00 快照是昨日收盘价
        # 却带今日盘前量比，写成昨日 added_date 会口径混存），只用日线五规则
        try:
            from signals import movers, hot
            m = movers.refresh(conn, market_mode=False)
            h = hot.refresh(conn)
            log.info("步骤6.5 动态池：异动 %d 只（%s口径）、热门题材 %d/个股 %d",
                     m["count"], m["mode"], len(h["themes"]), len(h["stocks"]))
        except Exception as e:
            log.error("步骤6.5 动态池 FAIL（继续）: %s", repr(e))

        # 6.6 业绩预告关键词分类（Sprint 2 任务 2 P1-4）：写 news_earnings 表，
        # bundle.py 步骤 7 读取 → LLM 决策依据
        try:
            from signals import earnings
            ee = earnings.refresh(conn=conn, days=3, min_net_score=2)
            log.info("步骤6.6 业绩预告：%d 票命中关键词（净分 ≥ 2）", len(ee))
        except Exception as e:
            log.error("步骤6.6 earnings.refresh FAIL（继续）: %s", repr(e))

        # 6.7 市场宽度（Sprint 2 任务 3 P1-1）：采集涨停/跌停家数等 → 写
        # breadth_daily 表；regime.py compute_regime 自动读
        try:
            from data import breadth
            br = breadth.fetch_breadth_daily(conn=conn)
            log.info("步骤6.7 市场宽度：source=%s, 涨停=%s, 跌停=%s",
                     br.get("source"),
                     br.get("limit_up_count"),
                     br.get("limit_down_count"))
        except Exception as e:
            log.error("步骤6.7 fetch_breadth_daily FAIL（继续）: %s", repr(e))

        # 6.8 信号有效性评估 + 落盘（任务 1）：必须在写 bundle 前完成，
        # 否则 bundle 读 latest.json 时取到的是昨日旧值。
        try:
            from review.signal_eval import evaluate, _persist_latest
            payload = evaluate(conn)
            persist = _persist_latest(payload)
            if persist.get("error"):
                log.warning("步骤6.8 signal_eval 落盘失败（不阻断）: %s",
                            persist["error"])
            else:
                log.info("步骤6.8 signal_eval 落盘 OK: %s",
                         persist.get("date"))
        except Exception as e:
            log.error("步骤6.8 signal_eval FAIL（不阻断）: %s", repr(e))

        # 7. 决策输入包
        try:
            json_path, md_path = ai_bundle.write_bundle(run_date)
        except Exception as e:
            log.error("步骤7 write_bundle FAIL: %s", repr(e))
            print("[premarket] 决策输入包落盘失败：%r" % e)
            return 1
    finally:
        conn.close()

    # 8. 总结
    print("[premarket] ===== 盘前流程完成 run_date=%s =====" % run_date)
    print("[premarket] 数据状态: 最新交易日=%s" % latest)
    if watchdog_problems:
        print("[premarket] ⚠️ 看门狗体检: " + "；".join(watchdog_problems))
    print("[premarket] 资讯新增: " + (", ".join(f"{k}: +{v}" for k, v in news_stats.items())
                                       if news_stats else "（本轮无统计）"))
    print("[premarket] 黑名单 BLOCK: " + (
        ", ".join(f"{c}({bl[c][1]})" for c in sorted(bl) if not bl[c][0]) or "无"))
    print("[premarket] 数据健康: " + ("；".join(issues) if issues else "OK"))
    if issues:
        print("[premarket] >>> 【今日只出报告不下单】<<<")
    print("[premarket] 信号摘要（%d 票）:" % len(sigs))
    print("    %-8s %-6s %-8s %-8s %-10s %s" % ("code", "trend", "mom5d", "rsi14", "close", "score"))
    for s in sigs:
        g = s.get("signals") or {}
        print("    %-8s %-6s %-8s %-8s %-10s %.3f" % (
            s.get("code"), g.get("ma_trend"),
            "n/a" if g.get("mom_5d") is None else f"{g['mom_5d']:+.3f}",
            "n/a" if g.get("rsi_14") is None else f"{g['rsi_14']:.1f}",
            "n/a" if g.get("close") is None else f"{g['close']:.2f}",
            s.get("score", 0.0)))
    print("[premarket] 决策输入包:")
    print("    %s" % json_path)
    print("    %s" % md_path)
    if watchdog_problems:
        notify("盘前体检：看门狗异常", "；".join(watchdog_problems))
    log.info("==== premarket done exit=0 ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
