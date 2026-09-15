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
import sqlite3
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

log = logging.getLogger("audit")
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.handlers.RotatingFileHandler(BASE / "logs" / "audit.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3))
    log.addHandler(logging.StreamHandler())
log.propagate = False

BACKUP_DIR = BASE / "logs" / "backup"
BACKUP_KEEP = 30

# 停板幅度按代码前缀：创业/科创 ±20%（300/301/302/688/689，302 为创业板新代码段），
# 北交所 ±30%，其余主板 ±10%（ST 不区分，超 ±5% 会被主板规则误报——用 10% 上限 +
# audit 报告人工确认，不做静默修正）
def _limit_pct(code: str) -> float:
    if code.startswith(("300", "301", "302", "688", "689")):
        return 20.5
    if code.startswith(("83", "87", "88", "43", "92")):
        return 30.5
    return 10.5


def _norm_volume(volume, amount, close):
    """与 fetcher 同款判定：返回归一为「手」的 volume；无法判定返回 None。"""
    try:
        v, amt, c = float(volume), float(amount), float(close)
    except (TypeError, ValueError):
        return None
    if v <= 0 or amt <= 0 or c <= 0:
        return None
    implied = amt / c
    if abs(v - implied) <= abs(v * 100 - implied):
        return round(v / 100.0, 2)  # 原值是「股」
    return v                         # 原值已是「手」


def check_db(conn: sqlite3.Connection, limit: int = 200) -> list:
    """全表逐行体检，返回问题清单 [{kind, code, date, detail}]（最多 limit 条）。

    pct_out_of_range 三类豁免（2026-09-15 人工复核 145 条历史告警后落地，误报清零：
    123 条除权除息假跌 + 13 条 302 段口径误判 + 9 条新股无限制日，无一条真数据错误）：
    - 上市前 5 个交易日：全面注册制各板块均无涨跌幅限制（前提是全史回补、
      库内首行≈上市日；老票回补起点晚于上市时此豁免不必要但无害）；
    - 除权除息日：腾讯源 pct_chg 按未复权昨收自算，送转/分红日呈假暴跌——
      前复权涨跌幅仍在停板内的行视为除权假跌（qfq 缺失时保守照报）；
    - 302 创业板新段按 ±20% 判（此前只认 300/301/688/689）。
    """
    issues = []
    rows = conn.execute(
        "SELECT code, trade_date, open, high, low, close, volume, amount, pct_chg, close_qfq "
        "FROM daily_bar ORDER BY code, trade_date").fetchall()
    first_date, prev_qfq, row_no = {}, {}, {}
    for code, td, o, h, l, c, v, amt, pct, cq in rows:
        n = row_no.setdefault(code, 0)
        first_date.setdefault(code, td)
        prev_cq = prev_qfq.get(code)
        prev_qfq[code] = cq   # 本行处理完后即下一行的「上一行」（bad_close continue 也要更新）
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
        if pct is not None and td != first_date[code]:
            lp = _limit_pct(code)
            if abs(pct) > lp:
                if n <= 4:
                    pass  # 上市前 5 个交易日无涨跌幅限制
                elif cq and prev_cq and abs((cq / prev_cq - 1) * 100) <= lp:
                    pass  # 除权假跌：前复权后真实涨跌幅在停板内
                else:
                    flag("pct_out_of_range", f"pct_chg={pct} 超过停板幅度±{lp}%")
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


def backup_db(conn: sqlite3.Connection) -> Path:
    """VACUUM INTO 快照备份，保留最近 BACKUP_KEEP 份；失败不阻塞主流程。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / (
        "market-" + datetime.now().strftime("%Y%m%d-%H%M") + ".db")
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
        issues, total = check_db(conn, limit=limit)
        by_kind = {}
        for it in issues:
            by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
        if backup:
            backup_db(conn)
        return {"total": total, "by_kind": by_kind, "issues": issues, "limit": limit}
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
