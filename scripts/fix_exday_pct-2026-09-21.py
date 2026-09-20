#!/usr/bin/env python3
"""P1-7 数据修复脚本：ex 事件日 pct_chg 改官方除息参考价口径（40 行/16 码）。

                    ⚠️ 待用户授权后执行（施工方案 §5 待授权清单②）⚠️
                    ⚠️ 本脚本随批次3b 落盘，构建批未执行、未写生产库 ⚠️

背景（docs/多agent全库审查报告-2026-09-21.md P1-7）：
- recalc_tx_pct 对除权跳变日写入 pct = qfq 环比（含分红总回报），涨跌停叠加
  分红日可合法超停板（002271 2024-09-26 +12.22% = 官方除息口径 ~+10.04% 叠加
  分红回报），与 audit 旧豁免判据互斥 → 40 行/16 码 pct_out_of_range 永久报警；
- 批次3b 已把 audit 豁免判据对齐到 recalc 写入口径（data/audit.py
  _pct_is_recalc_exday），生产库**不改数据**即不再报警——本脚本是审查给出的
  另一条路（"ex 日 pct 改官方除息参考价口径"）的可执行载体，是否执行、
  保留哪种口径由用户裁决。

脚本行为：
- 默认 DRY-RUN：mode=ro 扫描目标行（判据见 scan_targets），在线拉 em 官方
  「涨跌幅」（stock_zh_a_hist，除息日按除息参考价口径计算）做比对展示，零写入；
- `--apply`：单事务 UPDATE 目标行 pct_chg=em 官方值 + fetch_log 留痕
  （status='exday_pct_official_fix'）。任一 em 取数失败/任一目标日期缺官方值
  → 整体中止零写入（fail-closed，不 Mock、不半改）。另需环境变量
  AGSICKLE_ALLOW_PROD_WRITE=1 双闸（防误触生产库）。

已知权衡（执行前必读）：
1. 官方口径执行后，若再运行 `audit --fix`，recalc_tx_pct 会把这些行重写回
   qfq 环比口径（两口径差 = 当期除权净额占比）——audit 对两种口径都不再永久
   报警，但"库内最终保留哪种口径"目前无 source 级标记，属数据口径决策，
   待用户拍板（如需固化官方口径，须另行批准 recalc 的 ex 行停写规则）。
2. 库内仅 tx 单源，官方除息参考价真值不可离线推导（审查 §八 无法核实项），
   故本脚本在线依赖 em；em K 线域 2026-09-20 起曾源级不可达，恢复后才可执行。
3. 目标行执行前后均落 logs/state/fix_exday_pct-<ts>.json 审计留痕。

用法：
    ./.venv/bin/python3 scripts/fix_exday_pct-2026-09-21.py            # DRY-RUN
    AGSICKLE_ALLOW_PROD_WRITE=1 ./.venv/bin/python3 scripts/... --apply  # 授权后执行
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from common import market as _market  # noqa: E402
from data.audit import (TX_PCT_DIVERGE_TOL_PP, TX_PCT_EXDAY_JUMP,  # noqa: E402
                        _pct_is_recalc_exday)

PROD_DB = BASE / "data" / "market.db"
STATE_DIR = BASE / "logs" / "state"


def _limit_pct(code: str) -> float:
    return _market.limit_pct(code) * 100 + 0.5


def scan_targets(conn: sqlite3.Connection) -> list:
    """目标行 = 旧 audit 判据下 pct_out_of_range 且属 recalc 除权总回报口径的行。

    （与批次3b 对齐后的 audit 互补：对齐后这些行不再报警，但仍是"官方口径
    修复"的候选集。非 recalc 口径的超板行不在此列——那是真异常，另案处理。）
    """
    rows = conn.execute(
        "SELECT code, trade_date, close, close_qfq, pct_chg, source FROM daily_bar "
        "ORDER BY code, trade_date").fetchall()
    prev = {}
    first = {}
    row_no = {}
    targets = []
    for code, td, c, cq, pct, src in rows:
        n = row_no.setdefault(code, 0)
        first.setdefault(code, td)
        row_no[code] = n + 1
        pc, pcq = prev.get(code, (None, None))
        prev[code] = (c, cq)
        if pct is None or td == first[code] or c is None or c <= 0:
            continue
        lp = _limit_pct(code)
        if abs(pct) <= lp:
            continue
        if n <= 4:
            continue
        if cq and pcq and abs((cq / pcq - 1) * 100) <= lp:
            continue  # 除权假跌类，官方口径同样适用但本批只处理 recalc 口径残渣
        exday = (cq is not None and pcq is not None and pc is not None
                 and abs((c - cq) - (pc - pcq)) > TX_PCT_EXDAY_JUMP)
        if exday and _pct_is_recalc_exday(pct, c, cq, pc, pcq):
            targets.append({"code": code, "trade_date": td, "pct_now": pct,
                            "source": src,
                            "qfq_pct": round((cq / pcq - 1) * 100, 4)})
    return targets


def fetch_em_official_pct(targets: list) -> dict:
    """在线拉 em 官方涨跌幅（除息参考价口径）→ {(code, date): pct}。

    任一票失败或任一目标日期缺值 → RuntimeError（fail-closed，不 Mock）。
    """
    import akshare as ak
    from data.fetcher import call_ak
    out = {}
    codes = sorted({t["code"] for t in targets})
    want = {(t["code"], t["trade_date"]) for t in targets}
    for code in codes:
        df = call_ak("em", ak.stock_zh_a_hist, symbol=code, period="daily",
                     start_date="20240101",
                     end_date=datetime.now().strftime("%Y%m%d"), adjust="")
        got = {}
        if df is not None and not df.empty and "涨跌幅" in df.columns:
            for _, r in df.iterrows():
                d = str(r["日期"])[:10]
                try:
                    got[d] = float(r["涨跌幅"])
                except (TypeError, ValueError):
                    continue
        missing = [d for (c, d) in want if c == code and d not in got]
        if missing:
            raise RuntimeError(f"em 官方涨跌幅缺值: {code} {missing}（中止，零写入）")
        for (c, d) in want:
            if c == code:
                out[(c, d)] = got[d]
        time.sleep(0.8)  # 温和限速
    return out


def main() -> int:
    apply_mode = "--apply" in sys.argv
    if apply_mode:
        import os
        if os.environ.get("AGSICKLE_ALLOW_PROD_WRITE") != "1":
            print("拒绝执行：--apply 需同时设置 AGSICKLE_ALLOW_PROD_WRITE=1（双闸）")
            return 2
        print("⚠️ 授权写模式：将 UPDATE 生产库 daily_bar.pct_chg（P1-7 待授权清单②）")
    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro" if not apply_mode
                           else str(PROD_DB), uri=not apply_mode)
    try:
        targets = scan_targets(conn)
        print(f"目标行 {len(targets)} 行 / {len({t['code'] for t in targets})} 码")
        if not targets:
            print("无目标行（官方口径已落地或判据漂移），退出")
            return 0
        try:
            official = fetch_em_official_pct(targets)
        except Exception as e:  # noqa: BLE001
            print(f"em 官方涨跌幅取数失败（fail-closed，零写入中止）: {type(e).__name__}: "
                  f"{repr(e)[:200]}\nem K 线域恢复在线后重试（见脚本头注释 2）")
            return 3
        plan = []
        for t in targets:
            key = (t["code"], t["trade_date"])
            t["pct_official"] = official[key]
            t["delta"] = round(official[key] - t["pct_now"], 4)
            plan.append(t)
            print(f"  {t['code']} {t['trade_date']}: pct {t['pct_now']} -> "
                  f"{t['pct_official']}（Δ{t['delta']:+.2f}pp, source={t['source']}）")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"fix_exday_pct-{stamp}.json").write_text(
            json.dumps({"generated_at": stamp, "applied": apply_mode,
                        "targets": plan}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        if not apply_mode:
            print("DRY-RUN 完成（零写入）；审计留痕 logs/state/fix_exday_pct-*.json")
            return 0
        # ---- 授权写路径：复核目标行未漂移 → 单事务 UPDATE + fetch_log 留痕 ----
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            now_iso = datetime.now().isoformat(timespec="seconds")
            for t in plan:
                row = cur.execute(
                    "SELECT pct_chg FROM daily_bar WHERE code=? AND trade_date=?",
                    (t["code"], t["trade_date"])).fetchone()
                if not row or row[0] != t["pct_now"]:
                    raise RuntimeError(
                        f"目标行漂移（pct 现值≠扫描值）: {t['code']} {t['trade_date']}，中止")
                cur.execute(
                    "UPDATE daily_bar SET pct_chg=? WHERE code=? AND trade_date=?",
                    (t["pct_official"], t["code"], t["trade_date"]))
                cur.execute(
                    "INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                    (t["code"], now_iso, "exday_pct_official_fix", 1,
                     f"P1-7 ex日 pct 改官方除息口径: {t['pct_now']}->{t['pct_official']}"))
            conn.commit()
            print(f"已提交 {len(plan)} 行 UPDATE（fetch_log status=exday_pct_official_fix）")
            return 0
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
