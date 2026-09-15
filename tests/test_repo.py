"""data/repo.py 收敛层测试（结构性重构 Phase 4 验收）。

- trade 流水可重放还原抽查（总验收清单项）：清空 position 按 trade 重放，
  现金/持仓/成本与重放前一致；
- repo 各域函数行为 + 返回现状形状（tuple 索引兼容 / dict JSON 可序列化）；
- decision INSERT 双来源列对齐（runner 10 列 / decide 13 列合一时缺省列语义）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
if str(BASE / "tests") not in sys.path:
    sys.path.insert(0, str(BASE / "tests"))

import json
import os
import sqlite3
import tempfile
import traceback
from datetime import datetime

os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")
os.environ.setdefault("AGSICKLE_DISABLE_FETCHER", "1")

from data.fetcher import DDL
from data import repo
from execution import paper as paper_mod

START_CASH = 1000000.0


def fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def _insert_trade(conn, side, code, price, shares, decision_id=None, amount=None,
                  status="filled", trade_date="2026-09-16"):
    amt = amount if amount is not None else price * shares
    return repo.insert_trade(
        conn, trade_date=trade_date, code=code, name="测试票", side=side,
        price=price, shares=shares, amount=amt, order_id="PAPER-t",
        status=status, decision_id=decision_id,
        created_at=datetime.now().isoformat(timespec="seconds"))


# ---------------- trade 重放还原抽查（总验收清单项） ----------------

def test_trade_replay_restores_position_and_cash():
    """清空 position 表按 trade 流水重放：现金/持仓/加权成本与重放前一致。"""
    conn = fresh_conn()
    # 流水：100@10 买入 → 50@11 卖出 → 200@12 买入 → 30@12.5 卖出（含费用口径 amount）
    # 期望终态：220 股、加权成本 11.6（(10×50+12×200)/250）
    fees_cfg = {"commission_rate": 0.00025, "min_commission": 5.0, "stamp_tax_rate": 0.0005}
    compute = paper_mod.compute_fees
    trades = [("buy", 10.0, 100), ("sell", 11.0, 50), ("buy", 12.0, 200), ("sell", 12.5, 30)]
    for side, price, shares in trades:
        f = compute(side, price, shares, fees_cfg)
        _insert_trade(conn, side, "600519", price, shares, amount=f["amount"])
    # 账本终态（mark_to_market 同款现金还原 + 加权成本）——重放的基准
    conn.execute("INSERT INTO position VALUES (?,?,?,?,?,?)",
                 ("600519", "贵州茅台", 220, 220, 11.6, "2026-09-16T09:00:00"))
    flow = repo.cash_flows(conn)
    cash_before = round(START_CASH - flow.get("buy", 0.0) + flow.get("sell", 0.0), 2)
    pos_row = conn.execute("SELECT shares, cost FROM position WHERE code='600519'").fetchone()

    # ---- 重放：清空 position，按 effective_trades 逐笔还原 ----
    conn.execute("DELETE FROM position")
    shares, cost = 0, 0.0
    for (_, side, price, sh, amount, _st) in repo.effective_trades(conn, "600519"):
        if side == "buy":
            cost = (cost * shares + float(price) * int(sh)) / (shares + int(sh))
            shares += int(sh)
        else:
            shares -= int(sh)
            if shares <= 0:
                shares, cost = 0, 0.0
    conn.execute("INSERT INTO position VALUES (?,?,?,?,?,?)",
                 ("600519", "贵州茅台", shares, shares, round(cost, 6),
                  "2026-09-16T10:00:00"))
    conn.commit()

    pos_after = conn.execute("SELECT shares, cost FROM position WHERE code='600519'").fetchone()
    flow2 = repo.cash_flows(conn)
    cash_after = round(START_CASH - flow2.get("buy", 0.0) + flow2.get("sell", 0.0), 2)
    assert cash_after == cash_before, "现金重放还原: %s vs %s" % (cash_after, cash_before)
    assert int(pos_after[0]) == int(pos_row[0]) == 220, "持仓股数重放还原"
    assert abs(float(pos_after[1]) - float(pos_row[1])) < 0.01, "加权成本重放还原"


# ---------------- trade 域 ----------------

def test_cash_flows_two_scopes():
    conn = fresh_conn()
    _insert_trade(conn, "buy", "600519", 10.0, 100, amount=1005.0, trade_date="2026-09-15")
    _insert_trade(conn, "sell", "600519", 11.0, 50, amount=494.5, trade_date="2026-09-16")
    _insert_trade(conn, "buy", "600519", 12.0, 10, amount=120.25, trade_date="2026-09-16",
                  status="rejected")  # 无效成交不计
    full = repo.cash_flows(conn)
    assert abs(full["buy"] - 1005.0) < 1e-9 and abs(full["sell"] - 494.5) < 1e-9
    as_of = repo.cash_flows(conn, as_of="2026-09-15")
    assert abs(as_of["buy"] - 1005.0) < 1e-9 and "sell" not in as_of


def test_has_effective_trade_and_insert_trade():
    conn = fresh_conn()
    tid = _insert_trade(conn, "buy", "600519", 10.0, 100, decision_id=7)
    assert isinstance(tid, int) and tid >= 1
    assert repo.has_effective_trade(conn, 7) is True
    # 无效成交不算
    _insert_trade(conn, "buy", "600519", 10.0, 100, decision_id=8, status="rejected")
    assert repo.has_effective_trade(conn, 8) is False
    # created_at 为 isoformat 秒级格式（与迁移前一致）
    row = conn.execute("SELECT created_at, status, shots FROM trade WHERE id=?", (tid,)).fetchone()
    assert "T" in row[0] and len(row[0]) == 19 and row[1] == "filled" and row[2] == "[]"


def test_sold_and_day_side_shares():
    conn = fresh_conn()
    _insert_trade(conn, "sell", "600519", 10.0, 300, trade_date="2026-09-15")
    _insert_trade(conn, "sell", "600519", 10.0, 200, trade_date="2026-09-16")
    _insert_trade(conn, "sell", "600519", 10.0, 50, trade_date="2026-09-16", status="cancelled")
    _insert_trade(conn, "buy", "600519", 10.0, 400, trade_date="2026-09-16")
    assert repo.sold_shares(conn, "600519") == 500
    assert repo.day_side_shares(conn, "2026-09-16", "600519", "sell") == 200
    assert repo.day_side_shares(conn, "2026-09-16", "600519", "buy") == 400


# ---------------- daily_bar / portfolio_state 域 ----------------

def _seed_bars(conn):
    for code, dates in {"600519": ["2026-09-15", "2026-09-16"],
                        "000001": ["2026-09-16"]}.items():
        for td in dates:
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, td, 10, 10, 10, 10.5, 1000, 1e6, 0.5, 1.0))


def test_daily_bar_domain():
    conn = fresh_conn()
    _seed_bars(conn)
    assert repo.latest_trade_date(conn) == "2026-09-16"
    assert repo.latest_dates_by_code(conn) == {"600519": "2026-09-16", "000001": "2026-09-16"}
    assert repo.latest_bar_date(conn, "600519") == "2026-09-16"
    assert repo.latest_bar_date(conn, "000001", qfq_only=True) is None
    assert repo.latest_close(conn, "600519") == 10.5
    assert repo.latest_close(conn, "600519", offset=1) == 10.5   # 次新 bar（同价 seed）
    assert repo.latest_close(conn, "300750") is None


def test_portfolio_state_domain():
    conn = fresh_conn()
    _seed_bars(conn)
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 ("2026-09-15", 900.0, 100.0, 1000.0, 0.0, 0, "d1"))
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 ("2026-09-16", 950.0, 100.0, 1050.0, 0.0, 0, "d2"))
    latest = repo.latest_state(conn)
    assert latest[0] == "2026-09-16" and float(latest[3]) == 1050.0   # tuple 索引兼容
    on = repo.state_on(conn, "2026-09-15")
    assert on is not None and on["note"] == "d1"                      # 键访问兼容
    assert repo.state_on(conn, "2026-09-10") is None
    assert repo.has_state(conn, "2026-09-16") is True
    assert repo.has_state(conn, "2026-09-10") is False
    assert repo.peak_total(conn) == 1050.0                            # 全史
    assert repo.peak_total(conn, before="2026-09-16") == 1000.0       # before 口径
    assert repo.peak_total(conn, window=1) == 1050.0                  # 窗口口径
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 ("2026-09-17", 960.0, 100.0, 500.0, 0.0, 0, "d3"))   # 坏数据只抬 1 行
    assert repo.peak_total(conn, window=2) == 1050.0                  # 窗口排除更早正常行


# ---------------- decision 域 ----------------

def test_insert_decision_column_alignment():
    """runner 10 列 / decide 13 列两来源对齐：缺省列 NULL、created_at 格式一致。"""
    conn = fresh_conn()
    d = {"code": "600519", "action": "buy", "target_weight": 0.05, "confidence": 0.8,
         "reasons": ["r1"], "risk_notes": []}
    # runner 语义：input_snapshot=决策 JSON 全文，status=proposed，其余缺省
    id1 = repo.insert_decision(conn, d, "2026-09-16")
    row1 = conn.execute("SELECT * FROM decision WHERE id=?", (id1,)).fetchone()
    cols = [c[1] for c in conn.execute("PRAGMA table_info(decision)").fetchall()]
    got = {c: row1[i] for i, c in enumerate(cols)}
    assert got["status"] == "proposed"
    assert got["trade_date"] is None and got["model"] is None and got["prompt_version"] is None
    assert json.loads(got["input_snapshot"]) == d
    assert json.loads(got["reasons"]) == ["r1"]
    assert "T" in got["created_at"]
    # decide 语义：全列显式（批量 commit 由调用方负责——repo 不 commit）
    id2 = repo.insert_decision(conn, d, "2026-09-16", status="report_only",
                               input_snapshot="bundle 全文", trade_date="2026-09-17",
                               model="gpt", prompt_version="2026-09.1",
                               created_at="2026-09-16T09:35:00")
    row2 = conn.execute("SELECT * FROM decision WHERE id=?", (id2,)).fetchone()
    got2 = {c: row2[i] for i, c in enumerate(cols)}
    assert got2["input_snapshot"] == "bundle 全文" and got2["trade_date"] == "2026-09-17"
    assert got2["model"] == "gpt" and got2["created_at"] == "2026-09-16T09:35:00"
    # repo 不 commit：同连接可见即可，不要求持久化语义（红线1）
    assert repo.get_decision_row(conn, id1) is not None
    assert repo.get_decision_row(conn, 999) is None


def test_parse_json_list_variants():
    assert repo.parse_json_list(None) == []
    assert repo.parse_json_list("") == []
    assert repo.parse_json_list('["a","b"]') == ["a", "b"]
    assert repo.parse_json_list("单个字符串") == ["单个字符串"]          # runner 语义
    text = "- 要点一\n- 要点二\n"
    assert repo.parse_json_list(text, split_bullets=True) == ["要点一", "要点二"]
    assert repo.parse_json_list(text) == [text]                        # 不拆行


# ---------------- stock_info / N+1 批量域 ----------------

def test_stock_info_domain():
    conn = fresh_conn()
    conn.execute("INSERT INTO stock_info VALUES ('600519','贵州茅台','2024-01-02','t')")
    conn.execute("INSERT INTO stock_info VALUES ('000001','平安银行','2024-01-02','t')")
    assert sorted(repo.all_codes(conn)) == ["000001", "600519"]
    nm = repo.name_map(conn)
    assert nm["600519"] == "贵州茅台"
    assert repo.stock_names(conn, ["600519", "300750"]) == {"600519": "贵州茅台"}
    assert repo.stock_names(conn, []) == {}


def test_batch_trace_queries_dict_shape():
    """N+1 批量函数：dict 形状（JSON 可序列化）、每决策 LIMIT 1 语义保留。"""
    conn = fresh_conn()
    d1 = repo.insert_decision(conn, {"code": "600519", "action": "buy"}, "2026-09-16")
    d2 = repo.insert_decision(conn, {"code": "000001", "action": "hold"}, "2026-09-16")
    for i in (1, 2):
        conn.execute("INSERT INTO risk_event (ts, rule, detail, decision_id) VALUES (?,?,?,?)",
                     ("2026-09-16T09:0%d:00" % i, "risk_check", "e%d" % i, d1))
    conn.execute("INSERT INTO risk_event (ts, rule, detail, decision_id) VALUES (?,?,?,?)",
                 ("2026-09-16T09:03:00", "manual_reject", "e3", d2))
    _insert_trade(conn, "buy", "600519", 10.0, 100, decision_id=d1)
    d3 = repo.insert_decision(conn, {"code": "600519", "action": "buy"}, "2026-09-16")
    _insert_trade(conn, "buy", "600519", 10.1, 50, decision_id=d3)
    conn.commit()
    ev = repo.events_by_decisions(conn, [d1, d2])
    assert set(ev.keys()) == {d1, d2} and len(ev[d1]) == 2
    assert json.dumps(ev[d1][0]) and ev[d1][0]["rule"] == "risk_check"   # dict + 排序
    tr = repo.trade_by_decisions(conn, [d1, d2])
    assert tr[d1]["shares"] == 100 and d2 not in tr                   # 首笔 + 无成交决策缺席
    assert repo.events_by_decisions(conn, []) == {} and repo.trade_by_decisions(conn, []) == {}


def test_transaction_context():
    conn = fresh_conn()
    with repo.transaction(conn):
        conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                     ("2026-09-16", 1, 1, 2, 0, 0, "ok"))
    assert repo.has_state(conn, "2026-09-16")
    try:
        with repo.transaction(conn):
            conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                         ("2026-09-17", 1, 1, 2, 0, 0, "rollback-me"))
            raise RuntimeError("触发回滚")
    except RuntimeError:
        pass
    assert not repo.has_state(conn, "2026-09-17")


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception:
            failed += 1
            print("FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
