"""兜底补跑器：机器漏开时错过的定时任务，在下次开机/唤醒后自动补齐。

触发方式（二选一或并存）：
1. 系统级节点：crontab 30 分钟兜底 tick（W-D1 收敛调度；launchd plist 从未
   成功——中文路径编码+TCC——plist 模板已删除（P2㉑），cron 错过的 tick 纯跳过，
   靠本脚本幂等补齐"没开机"场景）。
2. 手动/任意会话运行：.venv/bin/python3 pipeline/catchup.py（幂等，随时可跑）。

补跑内容（全部确定性脚本，不含 LLM 决策）：
- 增量行情（交易时段自动防盘中部分bar，见 fetcher P1-1 防护）；
- 历史缺失日的盯市 + 日报（+周五周报）——纯 DB/文件操作，可完全自动；
- 盘中兜底（交易日 9:00-15:00）：盘前 bundle 过期则重跑盘前流水线；
  11:00 后午间包缺失则重跑午评准备；有持仓则跑尾盘级 kill 安全网扫描；
- 盘后当日补全（交易日 15:10 后）：postclose 因当日日线未出而写 PENDING 退出后，
  日线一旦到位（fetcher 每 30 分钟自动补）即重跑 postclose 完成当日盯市/日报，
  并清除 PENDING 兜底文件——保证"每天日线当天收盘后补完"；
- 动态池刷新（锚点滞后于最新交易日时）。

LLM 决策缺失的安全语义：当天没有决策 = 当天不交易（fail-safe），不自动伪造。
盘中补决策路径：补跑器把 bundle 备好后，由用户在任意 ZCode 会话说"补今天的决策"
（读 bundle → decide.py --date → propose-db → 人工闸门），与正常流程同一链路。

退出码：0=无可补或已补齐；1=部分补跑失败（详见输出）；2=盘中触发过 kill。
"""
import fcntl
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import fetcher                       # noqa: E402
from data import repo                          # noqa: E402
from review import daily, weekly               # noqa: E402

# 测试隔离：AGSICKLE_REPORTS_DIR/SESSION_DIR 覆盖产物目录（import 期读 env，
# 与 runner.ORDERS_DIR 同模式）；STATE_DIR 承载心跳/锁文件，一并可隔离
REPORTS_DIR = Path(os.environ.get("AGSICKLE_REPORTS_DIR") or (BASE / "logs" / "reports"))
SESSION_DIR = Path(os.environ.get("AGSICKLE_SESSION_DIR") or (BASE / "logs" / "session"))
STATE_DIR = Path(os.environ.get("AGSICKLE_STATE_DIR") or (BASE / "logs" / "state"))
HEARTBEAT_FILE = STATE_DIR / "catchup_heartbeat"
LOCK_FILE = BASE / "logs" / ".catchup.lock"
LOG_TAG = "[catchup]"
BACKFILL_WINDOW = 25   # 回补窗口（此前 10 天，停机超两周的缺失日永久漏补）


def _say(msg: str) -> None:
    print("%s %s" % (LOG_TAG, msg))


def _heartbeat() -> None:
    """写心跳文件（mtime=本次运行时刻）——**单层语义**（Sprint4 W-D1 代码部分）。

    心跳只回答一个问题："catchup 最近有没有实际跑过"，供 premarket.check_watchdog
    按 mtime 做陈旧告警。它**不证明任何调度层（launchd/cron/ZCode automation）存活**
    ——调度层死亡检测归调度重建（W-D1 调度侧）负责，此处不做分层互保。
    写入口收敛：全仓仅本函数写 catchup_heartbeat（recorder_heartbeat 是另一个文件，
    语义独立）；读方（premarket.check_watchdog）只读 mtime，不写。
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.touch()
    except OSError:
        pass


def _latest_trade_date(conn) -> Optional[str]:
    return repo.latest_trade_date(conn)


def _recent_trade_dates(conn, n: int = BACKFILL_WINDOW) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_bar ORDER BY trade_date DESC LIMIT ?",
        (n,)).fetchall()]


def _is_trading_day(conn, d: date) -> bool:
    """交易日判断：交易日历优先，缺失退化 weekday（此前节假日照常跑流水线）。"""
    try:
        from data.trade_cal import is_trading_day
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


def _stale_codes_from_note(note):
    """解析 portfolio_state.note 中「价格滞后:」段列出的代码。

    mark_to_market 写入格式（review/daily.py）：`价格滞后:code@bar_date,code2:无任何行情`
    （分段以 "; " 连接）。无滞后段返回空列表。
    """
    if not note:
        return []
    out = []
    for part in note.split(";"):
        part = part.strip()
        if not part.startswith("价格滞后:"):
            continue
        payload = part[len("价格滞后:"):]
        for entry in payload.split(","):
            entry = entry.strip()
            if not entry:
                continue
            code = entry.split("@", 1)[0].split(":", 1)[0].strip()
            if code:
                out.append(code)
    return out


def _mm_done(ps, conn=None) -> bool:
    """W-D2（P1-16）+ P2⑬ 愈合分支：盯市已完成判定。

    ps 为 repo.state_on 的 sqlite3.Row（date/cash/market_value/total/drawdown/
    kill_switch/note）。此前读 ps[0]（date 列）与 note 子串比较 → 恒 False →
    日线到位后 postclose 每 30 分钟被整套重跑直到午夜。现读 note 列：
    含「价格日期=」（当日价盯市）或「价格滞后:」（停牌票按最近可得收盘，
    daily.mark_to_market 写入）均算完成——停牌票的残留滞后不得把已完成
    盯市的交易日误判为未完成而陷入重跑循环。

    P2⑬（全量打包批 2026-09-20）有痕降级日自愈：--date 补跑当日时 daily_bar
    未出，mark_to_market 按最近可得收盘降级盯市（note=价格滞后:…），当晚
    数据到位后此前无任何节点重跑。新增分支：note 含「价格滞后:」且**滞后
    代码中至少一个在该交易日已有 daily_bar 行** → 判未完成（conn 为 None
    时无法核对，维持 W-D2 旧语义算完成）。滞后代码仍全数无该日 bar（真
    停牌）→ 算完成，不触发重跑循环；愈合重跑后 note 由最新数据重写，仍
    缺 bar 的停牌票留在滞后段 → 收敛，每波数据到位至多重跑一次。
    """
    if not ps:
        return False
    note = ps["note"] or ""
    if "价格日期=" in note:
        return True
    if "价格滞后:" not in note:
        return False
    if conn is None:
        return True   # 无连接可核对 bar，维持 W-D2 语义（防停票残留循环）
    trade_date = ps["date"]
    for code in _stale_codes_from_note(note):
        has_bar = conn.execute(
            "SELECT 1 FROM daily_bar WHERE code=? AND trade_date=? LIMIT 1",
            (code, trade_date)).fetchone()
        if has_bar:
            return False   # 数据已到，降级盯市未完成 → 步骤4 重跑 postclose 愈合
    return True


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
            has_state = repo.has_state(conn, td)
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
                # W-C7（P1-21）：补跑产物校验——此前防覆写守卫把历史日报拦成
                # PENDING 且不报错，"补跑成功"是假的。现在盯市行与 td.md 缺一
                # 即计入 failures，不再静默。
                if not repo.has_state(conn, td):
                    failures += 1
                    _say("  ↳ 补跑 %s 后盯市行缺失（portfolio_state 无该日）" % td)
                elif not report.is_file():
                    failures += 1
                    _say("  ↳ 补跑 %s 后日报产物缺失（%s.md 未落盘）" % (td, td))
            except Exception as e:  # noqa: BLE001
                failures += 1
                _say("  ↳ 补跑 %s 失败: %r" % (td, e))
        # 回补窗口之外的缺失日显式警告（此前静默漏补）
        try:
            from data.trade_cal import recent_trade_days
            older = [d for d in recent_trade_days(conn, 60)
                     if d < today_str and d < (min(tds) if tds else today_str)]
            missing_old = [d for d in older
                           if not repo.has_state(conn, d)
                           and not (REPORTS_DIR / (d + ".md")).is_file()]
            if missing_old:
                _say("⚠ 回补窗口(%d交易日)外仍有 %d 个缺失日（最早 %s），请手动补齐"
                     % (BACKFILL_WINDOW, len(missing_old), missing_old[0]))
        except Exception:  # noqa: BLE001
            pass  # 日历不可用时跳过窗口外检查

        # ---- 2) 盘中兜底（仅交易日盘中窗口）----
        if in_trading_window and latest_td:
            # 2a) 盘前 bundle 缺失 → 重跑盘前流水线；信号未对齐仅 11:00 前才重跑
            #（W-C8 / P1-22：此前对齐检查在傍晚也成立——15:43 整套重跑 premarket
            #  覆盖早晨 bundle，事后复盘读到的是与决策时不同的证据。11:00 后
            #  bundle 已在即不再动它，信号对齐交给盘后流程）。
            #（完工审查修正：bundle/midday 落盘在**当日**会话目录 SESSION_DIR/<today>/
            #  ——premarket/midday 的 run_date=今天；原检查指向 latest_td（盘中
            #  daily_bar 最新=上一交易日）恒判"缺失"，叠加 */30 catchup 后每个
            #  tick 都整套重跑 premarket。目录修正后 bundle_missing 腿天然幂等
            #  （早晨生成过→mtime=今天→不再补），保持"任何时候补"语义不变。）
            bundle = SESSION_DIR / today_str / "bundle.md"
            sig_latest = conn.execute("SELECT MAX(as_of) FROM signal").fetchone()[0]
            bundle_missing = not _file_fresh_today(bundle)
            sig_misaligned = sig_latest != latest_td
            before_1100 = (now.hour, now.minute) < (11, 0)
            need_premarket = bundle_missing or (sig_misaligned and before_1100)
            if need_premarket:
                why = "当日盘前流程缺失" if bundle_missing \
                    else "信号未对齐最新交易日（signal as_of=%s ≠ %s，11:00 前允许重跑）" \
                    % (sig_latest, latest_td)
                _say("步骤2a %s，补跑（LLM 决策需会话补做）" % why)
                if not _run_script("pipeline/premarket.py", timeout=900):
                    failures += 1
            elif sig_misaligned:
                pass  # 11:00 后信号未对齐属盘后常态（当日 bar 15:30 后才入库），静默跳过
            # 2b) 午间包缺失（11:00 后）→ 补午评准备（midday 落盘同在当日会话目录）
            midday_bundle = SESSION_DIR / today_str / "midday_bundle.md"
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

        # ---- 4) 盘后当日补全：收盘后日线已到但盯市/日报未完成 ----
        # 场景：postclose 15:30 跑时数据源还没出当日日线 → 写 PENDING 退出(exit=2)；
        # 此后 fetcher 每半小时把日线补上了，但没有节点重跑当日盯市/日报，
        # 当天复盘就一直缺（2026-09-15 实测）。这里闭环：日线到位后重跑 postclose。
        after_close = (now.hour, now.minute) >= (15, 10)
        if trading_day and after_close and latest_td == today_str:
            state_ok = _mm_done(repo.state_on(conn, today_str), conn)
            pending_stale = (REPORTS_DIR / ("PENDING-" + today_str + ".md")).is_file()
            # W-D2：补齐判定与重跑解耦——盯市已完成时不再整套重跑 postclose
            #（已补齐则不再重跑），只清残留的 PENDING 兜底文件。
            # P2⑬（2026-09-20）：--date 降级盯市（价格滞后 note）在当日 bar
            # 到位后经 _mm_done 愈合分支判未完成 → 此处重跑 postclose 自愈。
            # P2⑳（留档确认）："盯市成功+日报失败"当日仍不自愈——次日 catchup
            # 步骤1（历史缺失日）按 has_state+report 缺一补跑，W-D2 刻意收窄，
            # 行为不变。
            if not state_ok:
                _say("步骤4 盘后当日补全（日线已到 %s，盯市/日报未完成）→ 重跑 postclose"
                     % today_str)
                if not _run_script("pipeline/postclose.py", timeout=900):
                    failures += 1
                else:
                    try:
                        (REPORTS_DIR / ("PENDING-" + today_str + ".md")).unlink()
                        _say("  ↳ PENDING 兜底文件已清除（数据已补齐）")
                    except OSError:
                        pass
            elif pending_stale:
                try:
                    (REPORTS_DIR / ("PENDING-" + today_str + ".md")).unlink()
                    _say("步骤4 盯市已完成，仅清除残留 PENDING 兜底文件（不重跑 postclose）")
                except OSError:
                    pass
    finally:
        conn.close()

    _say("==== 补跑结束：%s ====" % ("有失败项 %d" % failures if failures else "全部正常"))
    if killed:
        print("%s ⚠⚠ kill switch 已在本次补跑中触发，请立即查看看板/日报 ⚠⚠" % LOG_TAG)
    return 2 if killed else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(catch_up())
