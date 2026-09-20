"""数据质量审计与修复：入库校验规则 + 存量修复 + 库备份。

用法：
    python3 -m data.audit            # 只体检，输出问题清单
    python3 -m data.audit --fix      # 修复可自动处理的量纲混杂，再体检
    python3 -m data.audit --backup   # 体检 + VACUUM INTO 备份（保留 30 份）

背景（2026-09 实测发现）：东财封禁期间全量走腾讯兜底，daily_bar 混入「股」口径
volume（与「手」差 100 倍）、首行 pct_chg 丢真值——fetch_log 全记 ok，无校验器
则完全不可见。本模块是发现 1/2/3 类问题的系统性兜底：
- 逐行体检：OHLC 关系 / 涨跌幅超边界（按板块判断停板幅度）/ 量纲自检；
- --fix：量纲可疑行按 amount/close 隐含股数归一为「手」；
- --backup：VACUUM INTO 到 logs/backup/，SQLite 文件级损坏时唯一可恢复来源。
"""
import argparse
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from common import market as _market  # noqa: E402

log = logging.getLogger("audit")
log.setLevel(logging.INFO)
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("audit.log"))
    log.addHandler(logging.StreamHandler())
log.propagate = False

# 测试隔离：AGSICKLE_BACKUP_DIR 覆盖备份目录（import 期读 env，与 runner.ORDERS_DIR 同模式）
# ——堵住 postclose→audit(backup=True) 在测试里向真实 logs/backup 写 VACUUM 快照的缺口
BACKUP_DIR = Path(os.environ.get("AGSICKLE_BACKUP_DIR") or (BASE / "logs" / "backup"))
BACKUP_KEEP = 30

# 停板幅度：common/market.py 唯一口径 × 100，+0.5pp 是「百分数舍入容差」而非 ST
# 容差——audit 不区分 ST（超 ±5% 会被 10% 上限误报，靠报告人工确认，不做静默修正）
def _limit_pct(code: str) -> float:
    return _market.limit_pct(code) * 100 + 0.5


# ----------------------------------------------------------------
# tx pct 工程判据单一口径源（P1-7 / 批次3b 任务5，2026-09-21）
# audit.check_db（本文件）与 fetcher.recalc_tx_pct（写入口径 owner）共用以下两个
# 常量——此前 audit 内联 0.01/1.0、recalc 内联 0.01/0.1，构成「报警 >1pp、
# 修复 >0.1pp」双口径（审查 P2「两套判据并存」）。统一取报警侧 1.0pp：报警与
# 修复同门、--fix 收敛性质不变、且不新增报警（若统一取 0.1pp 会对存量
# 141 行/9 码立即新增 divergent 报警，属行为变化而非对齐）。后续收紧只改这里。
TX_PCT_EXDAY_JUMP = 0.01      # d(t)=close−close_qfq 跳变阈值（除权事件签名）
TX_PCT_DIVERGE_TOL_PP = 1.0   # pct 与除权日合法口径的背离容忍（百分点）


def _pct_is_recalc_exday(pct, c, cq, pc, pcq) -> bool:
    """ex 事件日 pct 是否落在 recalc 写入口径（qfq 环比，±TX_PCT_DIVERGE_TOL_PP）。

    recalc（fetcher.recalc_tx_pct）对除权跳变日写入 pct = qfq 环比＝含分红/送转
    的总回报。该口径在「除权日涨跌停 + 分红」时天然超过停板幅度（如 002271
    2024-09-26 +12.22% = 官方除息口径涨停 ~10.04% 叠加分红回报），旧豁免判据
    「|qfq环比|≤停板」对 recalc 写入值恒假 → 40 行/16 码 pct_out_of_range 永久
    报警且 --fix 无修法（审查 P1-7）。本判定把 audit 豁免/比对判据对齐到 recalc
    的实际写入口径：pct ≈ qfq 环比（±统一容差）即视为合法除权日口径。
    前置条件：d(t) 跳变已由调用方判定。任一价格缺失返回 False（保守照报）。
    """
    if None in (pct, c, cq, pc, pcq) or not pcq or pcq <= 0:
        return False
    qfq_pct = (float(cq) / float(pcq) - 1.0) * 100.0
    return abs(float(pct) - qfq_pct) <= TX_PCT_DIVERGE_TOL_PP


def _norm_volume(volume, amount, close):
    """审计口径量纲归一：判别核心在 common/market.py；无法判定返回 None（体检报问题）
    ——与 fetcher 薄壳的原值回退不同（红线4）。"""
    try:
        v, amt, c = float(volume), float(amount), float(close)
    except (TypeError, ValueError):
        return None
    if v <= 0 or amt <= 0 or c <= 0:
        return None
    if _market.volume_unit_is_lots(v, amt, c):
        return v                         # 原值已是「手」
    return round(v / 100.0, 2)           # 原值是「股」


def check_db(conn: sqlite3.Connection, limit: int = 200) -> list:
    """全表逐行体检，返回问题清单 [{kind, code, date, detail}]（最多 limit 条）。

    pct_out_of_range 三类豁免（2026-09-15 人工复核 145 条历史告警后落地，误报清零：
    123 条除权除息假跌 + 13 条 302 段口径误判 + 9 条新股无限制日，无一条真数据错误）：
    - 上市前 5 个交易日：全面注册制各板块均无涨跌幅限制（前提是全史回补、
      库内首行≈上市日；老票回补起点晚于上市时此豁免不必要但无害）；
    - 除权除息日：腾讯源 pct_chg 按未复权昨收自算，送转/分红日呈假暴跌——
      前复权涨跌幅仍在停板内的行视为除权假跌（qfq 缺失时保守照报）；
    - 302 创业板新段按 ±20% 判（此前只认 300/301/688/689）。

    W-B3（Sprint4，P1-10）：新增 tx_pct_divergent——source='tx' 行 pct_chg 与
    前复权环比背离超统一容差 TX_PCT_DIVERGE_TOL_PP 且 d(t) 跳变（除权事件）才报
    （除权日假跌的另一半：跌幅未超停板但方向/幅度已错，例如 000001 2024-06-14
    存 -5.74% 实为 -0.71%）。--fix 按 qfq 环比重算，与报警共用同一套常量
    （批次3b 任务5：报警 1.0pp / 修复 0.1pp 双口径已并一，见文件头单一口径源）。

    P1-7（批次3b，2026-09-21）：除权事件日（d(t) 跳变）且 pct 就是 recalc 写入
    的 qfq 环比总回报口径（_pct_is_recalc_exday）→ pct_out_of_range 豁免——
    该口径含分红回报，涨跌停叠加分红日可合法超停板（40 行/16 码永久报警根因）。
    qfq 环比超板但 pct 与之背离超容差的行仍照报（真异常不豁免）。
    """
    issues = []
    rows = conn.execute(
        "SELECT code, trade_date, open, high, low, close, volume, amount, pct_chg, close_qfq, source "
        "FROM daily_bar ORDER BY code, trade_date").fetchall()
    first_date, prev_qfq, prev_close, row_no = {}, {}, {}, {}
    for code, td, o, h, l, c, v, amt, pct, cq, source in rows:
        n = row_no.setdefault(code, 0)
        first_date.setdefault(code, td)
        prev_cq = prev_qfq.get(code)
        prev_c = prev_close.get(code)
        prev_qfq[code] = cq   # 本行处理完后即下一行的「上一行」（bad_close continue 也要更新）
        prev_close[code] = c
        row_no[code] = n + 1
        ctx = {"kind": "", "code": code, "date": td, "detail": ""}
        def flag(kind, detail):
            ctx2 = dict(ctx)
            ctx2.update(kind=kind, detail=detail)
            issues.append(ctx2)
            # P1 修复：每条 issue 落日志，pipeline WARN「详见 logs/audit.log」才能真「详见」
            log.warning("数据问题 [%s] %s %s: %s", kind, code, td, detail)
        if c is None or c <= 0:
            flag("bad_close", f"close={c}")
            continue
        if o is not None and h is not None and l is not None:
            if h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9 or h < l:
                flag("ohlc_broken", f"o={o} h={h} l={l} c={c}")
        # d(t) 跳变（除权事件签名）——pct 豁免与 divergent 判据共用（常量单一口径源）
        exday_jump = (cq is not None and prev_cq is not None
                      and prev_c is not None and c is not None
                      and abs((c - cq) - (prev_c - prev_cq)) > TX_PCT_EXDAY_JUMP)
        if pct is not None and td != first_date[code]:
            lp = _limit_pct(code)
            if abs(pct) > lp:
                if n <= 4:
                    pass  # 上市前 5 个交易日无涨跌幅限制
                elif cq and prev_cq and abs((cq / prev_cq - 1) * 100) <= lp:
                    pass  # 除权假跌：前复权后真实涨跌幅在停板内
                elif exday_jump and _pct_is_recalc_exday(pct, c, cq, prev_c, prev_cq):
                    # P1-7：recalc 写入的除权日总回报口径（qfq 环比），涨跌停叠加
                    # 分红可合法超板——豁免判据与 recalc 写入口径对齐
                    pass
                else:
                    flag("pct_out_of_range", f"pct_chg={pct} 超过停板幅度±{lp}%")
            # W-B3：tx 行 pct 与 qfq 环比背离超统一容差且 d(t) 跳变（除权事件）才报——
            # tx 加法型复权在除权段内正常日两口径天然不同，只看背离会误报上万行；
            # P1-7 对齐：pct 已是 recalc 写入的 qfq 环比口径（±同一容差）不算背离
            if (source == "tx" and exday_jump and pct is not None
                    and prev_cq and prev_cq > 0
                    and not _pct_is_recalc_exday(pct, c, cq, prev_c, prev_cq)):
                flag("tx_pct_divergent",
                     f"pct_chg={pct} vs qfq环比={(cq / prev_cq - 1) * 100:.2f}%")
        if amt and amt > 0 and v is not None and v > 0:
            implied = amt / c
            shares = v * 100  # 假定库内已是「手」
            if not (0.5 < shares / implied < 2.0):
                flag("volume_unit_suspect",
                     f"volume={v}(手?) amount/close={implied:.0f}股, "
                     f"偏差 {shares / implied:.2f}x")
    # P1 修复：原 `issues and len(issues) or 0` 在 issues 超 limit 时
    # 返回的是原列表的全量长度（与 issues[:limit] 截断后不一致），
    # 导致 pipeline WARN 文案「数据体检发现 N 个问题」与 by_kind 之和偏差。
    # 改成 `min(len(issues), limit)`，与 by_kind 上限统一为 limit。
    return issues[:limit], min(len(issues), limit) if issues else 0


def fix_volume_units(conn: sqlite3.Connection) -> int:
    """把量纲可疑行归一为「手」，返回修复行数。只在显式 --fix 时调用。"""
    rows = conn.execute(
        "SELECT code, trade_date, volume, amount, close FROM daily_bar "
        "WHERE volume IS NOT NULL AND volume > 0 AND amount IS NOT NULL "
        "AND amount > 0 AND close IS NOT NULL AND close > 0").fetchall()
    fixed = 0
    for code, td, v, amt, c in rows:
        implied = amt / c
        if 0.5 < (v * 100) / implied < 2.0:
            continue  # 已是「手」，正常
        nv = _norm_volume(v, amt, c)
        if nv is not None and abs(nv - v) > 1e-9:
            conn.execute("UPDATE daily_bar SET volume=? WHERE code=? AND trade_date=?",
                         (nv, code, td))
            fixed += 1
    conn.commit()
    log.info("量纲修复: %d 行（volume 归一为「手」）", fixed)
    return fixed


def fix_tx_pct(conn: sqlite3.Connection) -> tuple:
    """W-B3（P1-10）：tx 源 pct_chg 按 qfq 环比重算（divergent 行），只在
    显式 --fix 时调用。返回 (fixed, skipped_no_qfq)——skipped 为该票该行或其
    前行无 close_qfq 而无法重算的行数（存量汇报口径）。"""
    from data.fetcher import recalc_tx_pct
    fixed, skipped = recalc_tx_pct(conn)
    conn.commit()
    log.info("tx pct 修复: %d 行（qfq 环比口径），无 qfq 跳过 %d 行", fixed, skipped)
    return fixed, skipped


def backup_db(conn: sqlite3.Connection) -> Path:
    """VACUUM INTO 快照备份，保留最近 BACKUP_KEEP 份；失败不阻塞主流程。

    批次3a（多agent审查 2026-09-21 P1-5）：幂等——目标已存在（同分钟二跑，
    2026-09-20 08:29 实测）→ 跳过并记"已存在"，**不得覆盖**：VACUUM INTO 对
    已存在目标直接抛 `output file already exists`，此前该异常会顺着 run()
    冒泡把已完成的体检结论一起吞掉。不同分钟名的新目标仍正常落盘（当日多份
    快照语义保留，轮转清理不变）。
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / (
        "market-" + datetime.now().strftime("%Y%m%d-%H%M") + ".db")
    if path.exists():
        log.info("备份目标已存在，跳过（幂等，不覆盖）: %s", path.name)
        return path
    conn.execute("VACUUM INTO ?", (str(path),))
    backups = sorted(BACKUP_DIR.glob("market-*.db"))
    for old in backups[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    log.info("库已备份: %s（保留 %d 份）", path.name,
             min(len(backups), BACKUP_KEEP))
    return path


def run(fix: bool = False, backup: bool = False, limit: int = 200) -> dict:
    from data.fetcher import get_conn
    conn = get_conn()
    try:
        if fix:
            fix_volume_units(conn)
            fix_tx_pct(conn)  # W-B3：tx pct 除权修复收纳进 --fix
        issues, total = check_db(conn, limit=limit)
        by_kind = {}
        for it in issues:
            by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
        result = {"total": total, "by_kind": by_kind, "issues": issues,
                  "limit": limit}
        if backup:
            # 批次3a（P1-5）：体检结果独立落盘——备份失败只记 backup_error，
            # 不再让 by_kind/issues 随异常一起丢失（postclose 步骤2.0 的
            # "数据体检发现 N 个问题" 必须看到真实体检结论）。
            try:
                result["backup"] = str(backup_db(conn))
            except Exception as e:  # noqa: BLE001
                log.error("库备份 FAIL（体检结论不受影响）: %r", e)
                result["backup_error"] = repr(e)
        return result
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="数据质量审计与修复")
    ap.add_argument("--fix", action="store_true", help="修复量纲混杂（归一为手）")
    ap.add_argument("--backup", action="store_true", help="VACUUM INTO 备份")
    args = ap.parse_args()
    r = run(fix=args.fix, backup=args.backup)
    print(f"== 数据体检: {r['total']} 个问题 {r['by_kind']} ==")
    for it in r["issues"][:30]:
        print(f"  [{it['kind']}] {it['code']} {it['date']}: {it['detail']}")
    if r["total"] > 30:
        # P1 修复：明示 limit 截断，避免「800 个」与 by_kind「200」偏差困惑
        print(f"  ... 本次扫描 limit={r['limit']}，已显示前 30 条；完整明细详见 logs/audit.log")


if __name__ == "__main__":
    main()
