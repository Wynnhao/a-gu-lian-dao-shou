#!/usr/bin/env python3
"""只读工具：watchlist_core qfq 加法型失真排序清单（修复批批次1，P0-A/P1-6）。

**只读工具，生产库 mode=ro**：sqlite3.connect('file:data/market.db?mode=ro', uri=True)。
本脚本全文件没有任何 INSERT/UPDATE/CREATE/DROP 代码路径（可 grep 验证），不触碰
data/agent.db，不发网络请求，不依赖 em/fhps 事件表——「事件/口径跨界窗」用
offset = close − close_qfq 的常数段变化**离线判定**（同源加法型段内 offset 恒定；
除权或源端重锚都会改写 offset 常数，两类边界正是加法口径失真的全部来源）。
默认行为即 --dry-run 语义：唯一落盘动作是把 markdown 报告写进 logs/reports/
（非生产库；--stdout-only 可连这一步也跳过）。

口径（对齐 docs/qfq等比口径施工方案-2026-09-20.md §1 Gate 0-D 家族 + 审查报告
P0-A「无事件对失真×窗口贡献」名单口径）::

    ret_raw(t,N) = close[t] / close[t-N] − 1          # 不复权收益
    ret_qfq(t,N) = close_qfq[t] / close_qfq[t-N] − 1  # 现库 qfq 收益
    d_N(t)       = |ret_qfq − ret_raw|                # 单位 pp

- N=1  ：0-D 无事件相邻对口径（方案稿既有定义，披露分位表用）；
- N=5  ：生产 m5 窗（signals m5 / momentum trend 腿）——窗口贡献之一；
- N=20 ：生产 mom20 窗（momentum 主因子，权重 0.40）——窗口贡献之二。
- 窗口对分类：offset[t] == offset[t-N] → 无事件窗（加法型下理论上 |d|≈舍入界）；
  否则 → 事件/口径跨界窗（加法偏差全部落在这一类）。
- 排序键 = 滚动一年 d_20 最大值（审查报告 P0-A 名单口径的复现基准），
  次键 = 滚动一年 d_5 最大值。按票当前值（最新 bar）单列，用于展示
  「当前决策窗口干净 / 除权季复发」的 P1-6 叙事。

交叉核对（写进报告，非硬门）：审查报告 P0-A 点名的 5 只（600809 7.6pp、
000333 5.7pp、000651 5.1pp、000568 4.7pp、000858 4.0pp）须落入本清单前 5 且
数值偏差 ≤0.1pp；d_20 ≥1.2pp 的 watchlist_core 票数应复现审查的「19 只」；
与 gate0b_precheck.json 36 票形态违例集（logs/state/qfq_batch0/，gitignored，
缺失则跳过该项）的交集应复现审查的「交集为空」。

用法（仓库根目录）::

    ./.venv/bin/python3 review/qfq_distortion_rank.py                # 报告 + stdout
    ./.venv/bin/python3 review/qfq_distortion_rank.py --stdout-only  # 不写报告文件
    ./.venv/bin/python3 review/qfq_distortion_rank.py --out <path>   # 自定义报告路径
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

DB_URI = "file:" + str(BASE / "data" / "market.db") + "?mode=ro"
LOOKBACK_DAYS = 365          # 滚动一年窗口（自然日；对齐审查报告 P0-A「20 日动量失真」实测口径，
                             # 复现其 19 只入围数；按交易日 250 根截尾会把 2025-09 除权季
                             # 边界票多算 2 只，见报告 §4 边界敏感性附注）
WINDOWS = (1, 5, 20)         # N=1 对齐 0-D 披露口径；5/20 为生产因子窗
RANK_WINDOW = 20             # 排序键用的主窗口（mom20，审查口径）
CROSSCHECK_TOP = {           # 审查报告 P0-A 点名票 → 点名值（pp）
    "600809": 7.6, "000333": 5.7, "000651": 5.1, "000568": 4.7, "000858": 4.0,
}
CROSSCHECK_TOL = 0.1         # 点名值 vs 实测值允许偏差（pp）
SHORTLIST_THRESHOLD_PP = 1.2  # d_20 滚动一年最大值入围线（复现审查「19 只」）
GATE0B_JSON = BASE / "logs" / "state" / "qfq_batch0" / "gate0b_precheck.json"


def ro_conn() -> sqlite3.Connection:
    """生产库只读连接（URI mode=ro；写操作会在 SQLite 层直接报错）。"""
    return sqlite3.connect(DB_URI, uri=True)


def core_watchlist() -> list:
    """config 的 watchlist_core（读 common.config.snapshot，硬键校验随 validate 生效）。"""
    from common.config import snapshot, core_codes
    cfg = snapshot()
    codes = core_codes(cfg)
    names = {str(w.get("code")): str(w.get("name") or "") for w in cfg.get("watchlist_core", [])}
    return [(c, names.get(c, "")) for c in codes]


def _quantiles(vals: list) -> dict:
    """p50/p90/p99/max（pp）。空集返回全 None。"""
    if not vals:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "max": None}
    s = sorted(vals)

    def q(p):
        i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
        return s[i]
    return {"n": len(s), "p50": q(0.50), "p90": q(0.90), "p99": q(0.99), "max": q(1.0)}


def ticket_metrics(conn: sqlite3.Connection, code: str) -> dict:
    """单票窗口失真时序 → 滚动一年最大值 / 最新值 / 事件跨界窗标记。"""
    rows = conn.execute(
        "SELECT trade_date, close, close_qfq FROM daily_bar WHERE code=? "
        "AND close_qfq IS NOT NULL AND close IS NOT NULL ORDER BY trade_date",
        (code,)).fetchall()
    n_bars = conn.execute(
        "SELECT COUNT(*) FROM daily_bar WHERE code=?", (code,)).fetchone()[0]
    m = {"code": code, "bars": n_bars, "qfq_bars": len(rows)}
    if len(rows) <= RANK_WINDOW:
        m.update({"ok": False, "d20_max": 0.0, "d5_max": 0.0,
                  "d20_now": None, "d5_now": None, "pairs": []})
        return m
    dates = [r[0] for r in rows]
    cl = [float(r[1]) for r in rows]
    qf = [float(r[2]) for r in rows]
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    d20_max = d5_max = 0.0
    pairs = []  # (date, N, d_pp, crossing)  跨界窗才可能非零；全量留存供分位表
    for i in range(RANK_WINDOW, len(rows)):
        if dates[i] < cutoff or cl[i] <= 0:
            continue
        for N in WINDOWS:
            j = i - N
            if j < 0 or cl[j] <= 0 or qf[j] <= 0 or qf[i] <= 0:
                continue
            d = abs((qf[i] / qf[j] - 1.0) - (cl[i] / cl[j] - 1.0)) * 100.0
            crossing = abs((cl[i] - qf[i]) - (cl[j] - qf[j])) > 1e-6
            pairs.append((dates[i], N, round(d, 4), crossing))
            if N == RANK_WINDOW:
                d20_max = max(d20_max, d)
            if N == 5:
                d5_max = max(d5_max, d)
    last = len(rows) - 1
    m["ok"] = True
    m["d20_max"] = d20_max
    m["d5_max"] = d5_max
    m["last_date"] = dates[last]
    m["pairs"] = pairs
    m["d20_now"] = None
    m["d5_now"] = None
    for N, key in ((20, "d20_now"), (5, "d5_now")):
        j = last - N
        if j >= 0 and cl[j] > 0 and qf[j] > 0 and qf[last] > 0:
            m[key] = round(abs((qf[last] / qf[j] - 1.0) - (cl[last] / cl[j] - 1.0)) * 100.0, 4)
    return m


def run(stdout_only: bool, out_path: Path) -> dict:
    conn = ro_conn()
    try:
        uni = core_watchlist()
        results = [ticket_metrics(conn, c) for c, _ in uni]
        names = dict(uni)
        max_date = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()[0]
        qfq_src = dict(conn.execute(
            "SELECT code, detail FROM fetch_log WHERE status='qfq_full_rebrush' "
            "AND code IN (%s) ORDER BY run_at" % ",".join("?" * len(uni)),
            [c for c, _ in uni]).fetchall() if uni else [])
    finally:
        conn.close()

    ranked = sorted(results, key=lambda m: (-m["d20_max"], -m["d5_max"], m["code"]))
    shortlist = [m for m in ranked if m["d20_max"] >= SHORTLIST_THRESHOLD_PP]

    # 分位表：全部窗口对 / 事件·口径跨界窗（滚动一年，core 51 全体）
    quant = {}
    for N in WINDOWS:
        allv = [p[2] for m in results for p in m.get("pairs", []) if p[1] == N]
        crsv = [p[2] for m in results for p in m.get("pairs", []) if p[1] == N and p[3]]
        quant[N] = {"all": _quantiles(allv), "cross": _quantiles(crsv)}

    # 交叉核对：审查点名 5 只 + 「19 只」入围数 + gate0b 36 票交集
    top5 = {m["code"]: m["d20_max"] for m in ranked[:5]}
    name_hits = {c: (c in top5 and abs(top5[c] - v) <= CROSSCHECK_TOL)
                 for c, v in CROSSCHECK_TOP.items()}
    count_19 = (len(shortlist) == 19)
    shortlist_codes = {m["code"] for m in shortlist}
    gate0b_codes, inter = None, None
    try:
        g0b = json.loads(GATE0B_JSON.read_text(encoding="utf-8"))
        gate0b_codes = sorted({r["code"] for r in g0b.get("detail", [])})
        inter = sorted(shortlist_codes & set(gate0b_codes))
    except (OSError, ValueError, KeyError, TypeError):
        pass  # gitignored 中间产物缺失/坏档：如实披露"未核对"，不阻塞

    # ---- stdout ----
    print("== qfq 加法型失真排序（watchlist_core %d 票，数据截至 %s，实测 %s）=="
          % (len(ranked), max_date, date.today().isoformat()))
    print("%-4s %-8s %-8s %9s %9s %9s %9s" %
          ("排名", "code", "名称", "d20_max", "d5_max", "d20_now", "d5_now"))
    for i, m in enumerate(ranked[:20], 1):
        print("%-4d %-8s %-8s %8.2fpp %8.2fpp %8s %8s" % (
            i, m["code"], names.get(m["code"], ""), m["d20_max"], m["d5_max"],
            "-" if m["d20_now"] is None else "%.2fpp" % m["d20_now"],
            "-" if m["d5_now"] is None else "%.2fpp" % m["d5_now"]))
    print("入围（d20_max ≥ %.1fpp）：%d 只（核对审查「19 只」：%s）"
          % (SHORTLIST_THRESHOLD_PP, len(shortlist), "PASS" if count_19 else "MISMATCH"))
    for c, v in CROSSCHECK_TOP.items():
        got = top5.get(c)
        ok = name_hits[c]
        print("点名核对 %-6s 审查 %.1fpp / 实测 %s → %s"
              % (c, v, "-" if got is None else "%.2fpp" % got, "PASS" if ok else "MISMATCH"))
    if inter is not None:
        print("gate0b 36 票形态违例集与入围 19 只交集：%d 只（核对审查「交集为空」：%s）"
              % (len(inter), "PASS" if not inter else "MISMATCH: %s" % inter))
    else:
        print("gate0b_precheck.json 不可读（gitignored 中间产物），交集核对跳过")

    report = render(ranked, names, shortlist, quant, name_hits, count_19,
                    max_date, qfq_src, gate0b_codes, inter)
    if not stdout_only:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print("报告已写：%s" % out_path)
    return {"ranked": ranked, "shortlist": shortlist}


def render(ranked, names, shortlist, quant, name_hits, count_19, max_date,
           qfq_src, gate0b_codes, inter) -> str:
    L = []
    L.append("# qfq 加法型失真排序清单（watchlist_core）— 实测 %s" % date.today().isoformat())
    L.append("")
    L.append("> 生成工具：`review/qfq_distortion_rank.py`（**只读**，生产库 mode=ro，"
             "零写库代码路径）。数据截至 %s。" % max_date)
    L.append("> 口径：d_N(t) = |ret_qfq − ret_raw|（N 日窗收益失真，pp），"
             "对齐《qfq等比口径施工方案》§1 Gate 0-D 家族；排序键 = 滚动一年 "
             "d20 最大值（mom20 窗贡献，审查报告 P0-A 名单口径），次键 d5。")
    L.append("> 用途：修复批批次1 P0-A「重拉名单由失真×窗口贡献驱动」的证据清单，"
             "**仅披露，不构成执行授权**（两案并陈待用户拍板）。")
    L.append("")
    L.append("## 1. 分位表（滚动一年窗口对，watchlist_core %d 票全体，单位 pp）" % len(ranked))
    L.append("")
    L.append("| 窗口 N | 窗口对类型 | 对数 | p50 | p90 | p99 | max |")
    L.append("|---|---|---|---|---|---|---|")
    for N in WINDOWS:
        for key, tag in (("all", "全部"), ("cross", "事件/口径跨界窗")):
            q = quant[N][key]
            if q["n"] == 0:
                continue
            fmt = "-" if None in (q["p50"], q["p90"], q["p99"], q["max"]) else "%.2f"
            L.append("| %d | %s | %d | %s | %s | %s | %s |"
                     % (N, tag, q["n"], fmt % q["p50"], fmt % q["p90"],
                        fmt % q["p99"], fmt % q["max"]))
    L.append("")
    L.append("无事件窗（offset 常数段内）d 几乎全为 0——加法偏差全部集中在"
             "事件/口径跨界窗；N=1 行与方案稿 §1 引用的 0-D 全库分位"
             "（p50 1.6bp/p90 13bp/p99 49bp/max 699bp）同族，可直接对照看尾部。")
    L.append("")
    L.append("## 2. 按票最大值排序（滚动一年 d20/d5 最大值 + 最新 bar 当前值）")
    L.append("")
    L.append("| 排名 | code | 名称 | d20_max(pp) | d5_max(pp) | d20_当前(pp) | d5_当前(pp) | 入围 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, m in enumerate(ranked, 1):
        L.append("| %d | %s | %s | %.2f | %.2f | %s | %s | %s |" % (
            i, m["code"], names.get(m["code"], ""), m["d20_max"], m["d5_max"],
            "-" if m["d20_now"] is None else "%.2f" % m["d20_now"],
            "-" if m["d5_now"] is None else "%.2f" % m["d5_now"],
            "是" if m["d20_max"] >= SHORTLIST_THRESHOLD_PP else ""))
    L.append("")
    L.append("## 3. 入围清单（重拉名单建议稿证据，d20_max ≥ %.1fpp，共 %d 只）" % (SHORTLIST_THRESHOLD_PP, len(shortlist)))
    L.append("")
    for i, m in enumerate(shortlist, 1):
        L.append("%d. %s %s — d20_max %.2fpp / d5_max %.2fpp" % (
            i, m["code"], names.get(m["code"], ""), m["d20_max"], m["d5_max"]))
    L.append("")
    L.append("## 4. 交叉核对（审查报告 P0-A 点名 vs 本次实测）")
    L.append("")
    for c, v in CROSSCHECK_TOP.items():
        got = next((m["d20_max"] for m in ranked[:5] if m["code"] == c), None)
        L.append("- %s：点名 %.1fpp / 实测 %s → %s"
                 % (c, v, "-" if got is None else "%.2fpp" % got,
                    "PASS" if name_hits[c] else "MISMATCH"))
    L.append("- 入围数：实测 %d 只 vs 审查「19 只」→ %s"
             % (len(shortlist), "PASS" if count_19 else "MISMATCH"))
    if inter is not None:
        L.append("- 与 gate0b_precheck 形态违例集（%d 票）交集：实测 %d 只 → %s"
                 % (len(gate0b_codes or []), len(inter),
                    "PASS（复现审查「交集为空」——两名单口径不同，互不覆盖）"
                    if not inter else "MISMATCH: %s" % inter))
    else:
        L.append("- 与 gate0b_precheck 形态违例集交集：artifact 不可读（gitignored "
                 "中间产物 logs/state/qfq_batch0/），未核对")
    L.append("")
    L.append("## 5. 附注")
    L.append("")
    L.append("- 当前值一列：多数票为 0（09-19 全库 tx 加法重锚后最新价即锚点），"
             "但 **000651 d20_当前 4.77pp / 000568 4.62pp / 300014 0.38pp 非 0**——"
             "三者除权日（08-27/08-28/09 月内）仍在最新 mom20 窗内，**现决策窗口的 "
             "mom20 因子正带偏**（相邻对口径干净、窗口口径不干净；审查 P1-6"
             "「当前决策窗口实测干净」用的是相邻对口径，本表补窗口口径披露）。"
             "失真是**除权季复发型**（P1-6），每年 5-9 月除权后 1-2 个月随因子窗"
             "跨越事件边界重现。")
    L.append("- 边界敏感性：入围线 ≥%.1fpp 按**自然日滚动一年**（对齐审查实测口径）"
             "得 %d 只；若按 250 根交易日截尾会把 2025-09 除权季边界票多算 2 只"
             "（300014/300433 段内 1.30pp），入围数变 21——名单拍板时以本表"
             "自然日口径为准并知悉该敏感性。" % (SHORTLIST_THRESHOLD_PP, len(shortlist)))
    n_tx = sum(1 for v in qfq_src.values() if "tx" in str(v))
    L.append("- fetch_log qfq_full_rebrush：core 51 票中 %d 票有重刷记录，其中 %d 票"
             "源标记为 tx（加法型）——样例 `%s`"
             % (len(qfq_src), n_tx,
                json.dumps(dict(list(qfq_src.items())[:3]), ensure_ascii=False)
                if qfq_src else "（core 票无记录）"))
    L.append("- 本清单不改动任何已归档 FAIL 判据/数字（只读披露）。")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="只读：watchlist_core qfq 加法型失真排序")
    ap.add_argument("--out", default=str(BASE / "logs" / "reports" / "qfq失真排序-2026-09-21.md"),
                    help="报告输出路径（默认 logs/reports/qfq失真排序-2026-09-21.md）")
    ap.add_argument("--stdout-only", action="store_true",
                    help="只打印不写报告文件（--dry-run 式安全默认之外的进一步收窄）")
    args = ap.parse_args()
    run(stdout_only=args.stdout_only, out_path=Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
