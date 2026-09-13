"""兜底补跑器：机器漏开时错过的定时任务，在下次开机/唤醒后自动补齐。

触发方式（二选一或并存）：
1. launchd 看门狗（deploy/com.agsickle.catchup.plist，StartInterval=1800）——
   macOS launchd 语义：错过的 StartInterval 触发会在唤醒后合并执行一次，
   这正是"没开机"场景的兜底入口（cron/ZCode cron 均为纯跳过）。
2. 手动/任意会话运行：python3 pipeline/catchup.py（幂等，随时可跑）。

补跑内容（全部确定性脚本，不含 LLM 决策）：
- 增量行情（交易时段自动防盘中部分bar，见 fetcher P1-1 防护）；
- 历史缺失日的盯市 + 日报（+周五周报）——纯 DB/文件操作，可完全自动；
- 盘中兜底（交易日 9:00-15:00）：盘前 bundle 过期则重跑盘前流水线；
  11:00 后午间包缺失则重跑午评准备；有持仓则跑尾盘级 kill 安全网扫描；
- 动态池刷新（锚点滞后于最新交易日时）。

LLM 决策缺失的安全语义：当天没有决策 = 当天不交易（fail-safe），不自动伪造。
盘中补决策路径：补跑器把 bundle 备好后，由用户在任意 ZCode 会话说"补今天的决策"
（读 bundle → decide.py --date → propose-db → 人工闸门），与正常流程同一链路。

退出码：0=无可补或已补齐；1=部分补跑失败（详见输出）；2=盘中触发过 kill。
"""
import fcntl
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import fetcher                       # noqa: E402
from review import daily, weekly               # noqa: E402

REPORTS_DIR = BASE / "logs" / "reports"
SESSION_DIR = BASE / "logs" / "session"
STATE_DIR = BASE / "logs" / "state"
HEARTBEAT_FILE = STATE_DIR / "catchup_heartbeat"
LOCK_FILE = BASE / "logs" / ".catchup.lock"
LOG_TAG = "[catchup]"
BACKFILL_WINDOW = 25   # 回补窗口（此前 10 天，停机超两周的缺失日永久漏补）


def _say(msg: str) -> None:
    print("%s %s" % (LOG_TAG, msg))


def _heartbeat() -> None:
    """写心跳文件（mtime=本次运行时刻），供盘前体检判断看门狗是否实际在跑。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.touch()
    except OSError:
        pass


def _latest_trade_date(conn) -> Optional[str]:
    row = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()
    return row[0] if row and row[0] else None


def _recent_trade_dates(conn, n: int = BACKFILL_WINDOW) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_bar ORDER BY trade_date DESC LIMIT ?",
        (n,)).fetchall()]


def _is_trading_day(conn, d: date) -> bool:
    """交易日判断：交易日历优先，缺失退化 weekday（此前节假日照常跑流水线）。"""
    try:
        from data.calendar import is_trading_day
        return is_trading_day(conn, d)
    except Exception:  # noqa: BLE001
        return d.weekday() < 5


def _run_script(rel: str, timeout: int = 900, extra: Optional[List[str]] = None) -> bool:
    """运行项目内脚本（子进程，继承配置与环境），返回是否成功。"""
    cmd = [sys.executable, str(BASE / rel)] + (extra or [])
    try:
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True,
                              text=True, timeout=timeout)
        ok = proc.returncode == 0
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        _say("  ↳ %s %s%s" % (rel, "OK" if ok else "FAIL(rc=%d)" % proc.returncode,
                              ("：" + " / ".join(tail)) if not ok else ""))
        return ok
    except subprocess.TimeoutExpired:
        _say("  ↳ %s 超时(%ds)" % (rel, timeout))
        return False
    except Exception as e:  # noqa: BLE001
        _say("  ↳ %s 异常: %r" % (rel, e))
        return False


def _file_fresh_today(p: Path) -> bool:
    return p.is_file() and datetime.fromtimestamp(p.stat().st_mtime).date() == date.today()


def catch_up(now: Optional[datetime] = None) -> int:
    now = now or datetime.now()
    today = now.date()
    today_str = today.isoformat()
    failures = 0
    killed = False

    _heartbeat()
    conn = fetcher.get_conn()
    try:
        # 单实例锁：launchd 每30分钟 + cron 四时点 + 手动可并发，
        # 并发跑 premarket 子进程有 SQLite 写冲突与重复拉数据风险
        lock_fh = open(LOCK_FILE, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _say("已有实例在跑（锁 %s 被占），本次退出" % LOCK_FILE)
            return 0

        trading_day = _is_trading_day(conn, today)
        # 行情增量：交易日 8:30 后全天可跑（fetcher 自带盘中防半根bar窗口，
        # 盘后拉当日安全——此前 15:30 后开机当天行情当日不补）
        can_fetch = trading_day and (now.hour, now.minute) >= (8, 30)
        # 盘中兜底窗口（bundle/午评/kill 扫描只在盘中补）
        in_trading_window = trading_day and (now.hour, now.minute) >= (8, 30) \
            and (now.hour, now.minute) <= (15, 30)

        _say("==== 兜底补跑开始 %s（交易日=%s 可拉行情=%s 盘中窗口=%s）===="
             % (now.strftime("%Y-%m-%d %H:%M:%S"), "是" if trading_day else "否",
                "是" if can_fetch else "否",
                "是" if in_trading_window else "否"))

        if not trading_day:
            _say("非交易日（交易日历判断），秒退")
            return 0

        latest_td = _latest_trade_date(conn)
        tds = _recent_trade_dates(conn)

        # ---- 0) 行情增量 ----
        if can_fetch:
            _say("步骤0 增量行情（盘中自动防部分bar）")
            if not _run_script("data/fetcher.py", timeout=600):
                failures += 1
            latest_td = _latest_trade_date(conn)

        # ---- 1) 历史缺失日：盯市 + 日报（+周五周报）----
        oldest_missing = None
        for td in tds:
            if td >= today_str:
                continue
            has_state = conn.execute(
                "SELECT 1 FROM portfolio_state WHERE date=?", (td,)).fetchone()
            report = REPORTS_DIR / (td + ".md")
            if has_state and report.is_file():
                continue
            _say("步骤1 补跑 %s 的盯市/日报（盘后任务缺失）" % td)
            try:
                daily.mark_to_market(td)
                daily.generate_daily_report(td)
                w = date.fromisoformat(td)
                if w.weekday() == 4 and not (REPORTS_DIR / (
                        "%d-W%02d.md" % (w.isocalendar()[0], w.isocalendar()[1]))).is_file():
                    weekly.weekly_report(td)
            except Exception as e:  # noqa: BLE001
                failures += 1
                _say("  ↳ 补跑 %s 失败: %r" % (td, e))
        # 回补窗口之外的缺失日显式警告（此前静默漏补）
        try:
            from data.calendar import recent_trade_days
            older = [d for d in recent_trade_days(conn, 60)
                     if d < today_str and d < (min(tds) if tds else today_str)]
            missing_old = [d for d in older
                           if not conn.execute(
                               "SELECT 1 FROM portfolio_state WHERE date=?",
                               (d,)).fetchone()
                           and not (REPORTS_DIR / (d + ".md")).is_file()]
            if missing_old:
                _say("⚠ 回补窗口(%d交易日)外仍有 %d 个缺失日（最早 %s），请手动补齐"
                     % (BACKFILL_WINDOW, len(missing_old), missing_old[0]))
        except Exception:  # noqa: BLE001
            pass  # 日历不可用时跳过窗口外检查

        # ---- 2) 盘中兜底（仅交易日盘中窗口）----
        if in_trading_window and latest_td:
            # 2a) 盘前 bundle 过期 或 信号未对齐最新交易日 → 重跑盘前流水线
            # （此前只看 bundle.md mtime：信号步骤失败但 bundle 已写出时，
            #  signals_missing 会原样喂给 LLM 一整天）
            bundle = SESSION_DIR / latest_td / "bundle.md"
            sig_latest = conn.execute("SELECT MAX(as_of) FROM signal").fetchone()[0]
            need_premarket = (not _file_fresh_today(bundle)) or (sig_latest != latest_td)
            if need_premarket:
                why = "当日盘前流程缺失" if not _file_fresh_today(bundle) \
                    else "信号未对齐最新交易日（signal as_of=%s ≠ %s）" % (sig_latest, latest_td)
                _say("步骤2a %s，补跑（LLM 决策需会话补做）" % why)
                if not _run_script("pipeline/premarket.py", timeout=900):
                    failures += 1
            # 2b) 午间包缺失（11:00 后）→ 补午评准备
            midday_bundle = SESSION_DIR / latest_td / "midday_bundle.md"
            if now.hour >= 11 and not _file_fresh_today(midday_bundle):
                _say("步骤2b 当日午间包缺失，补跑午评准备")
                if not _run_script("pipeline/midday.py", timeout=600):
                    failures += 1
            # 2c) 有持仓 → kill 安全网扫描（触发即清仓+停机，退出码2）
            pos_n = conn.execute("SELECT COUNT(*) FROM position").fetchone()[0]
            if pos_n:
                _say("步骤2c 持仓 %d 只 → 尾盘级风控安全网扫描" % pos_n)
                proc = subprocess.run(
                    [sys.executable, str(BASE / "pipeline/intraday_check.py")],
                    cwd=str(BASE), capture_output=True, text=True, timeout=600)
                if proc.returncode == 2:
                    killed = True
                    _say("  ↳ ⚠ kill switch 已触发（清仓+停机），详见输出/报告")
                elif proc.returncode != 0:
                    failures += 1
                    _say("  ↳ 扫描失败 rc=%d" % proc.returncode)

        # ---- 3) 动态池锚点滞后 → 刷新 ----
        if latest_td:
            stale = conn.execute(
                "SELECT COUNT(DISTINCT pool) FROM dynamic_pool WHERE added_date=?",
                (latest_td,)).fetchone()[0] < 2
            if stale:
                _say("步骤3 动态池锚点滞后（最新 %s），刷新" % latest_td)
                try:
                    from signals import movers, hot
                    m = movers.refresh(conn)
                    h = hot.refresh(conn)
                    _say("  ↳ 异动 %d / 题材 %d / 热门股 %d"
                         % (m["count"], len(h["themes"]), len(h["stocks"])))
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    _say("  ↳ 动态池刷新失败: %r" % e)
    finally:
        conn.close()

    _say("==== 补跑结束：%s ====" % ("有失败项 %d" % failures if failures else "全部正常"))
    if killed:
        print("%s ⚠⚠ kill switch 已在本次补跑中触发，请立即查看看板/日报 ⚠⚠" % LOG_TAG)
    return 2 if killed else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(catch_up())
