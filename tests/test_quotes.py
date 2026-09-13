"""实时行情模块测试：解析/缓存/回退/开关，全部离线（mock 网络层）。

直跑：python3 tests/test_quotes.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"  # 测试保持离线（强制，不用setdefault）

from data import quotes            # noqa: E402
from execution.paper import PaperBroker  # noqa: E402
from data.fetcher import DDL       # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


def _tencent_payload() -> str:
    """构造符合真实结构的腾讯报文：f3现价 f4昨收 f5今开 f30时间 f33最高 f34最低。"""
    def line(market, name, code, price, prev, open_, ts, high, low):
        f = ["100" if market == "sh" else "51", name, code, price, prev, open_, "80000"]
        f += [""] * 23                      # f7..f29
        f += [ts, "-9.97", "-0.78", high, low, "", "80000", "102013",
              "0.48", "25.1", "", high, low, "1.55", "16000", "16000", "8.86"]
        return 'v_%s%s="%s";' % (market, code, "~".join(f))
    return (line("sh", "贵州茅台", "600519", "1275.16", "1285.13", "1280.00",
                 "20260911161451", "1290.00", "1270.00") + "\n"
            + line("sz", "平安银行", "000001", "11.74", "11.85", "11.80",
                   "20260911161454", "11.90", "11.60"))


@test
def test_is_trading_time_boundaries():
    cases = [
        ("2026-09-14 09:29:59", False), ("2026-09-14 09:30:00", True),
        ("2026-09-14 11:30:00", True), ("2026-09-14 11:31:00", False),
        ("2026-09-14 13:00:00", True), ("2026-09-14 15:00:00", True),
        ("2026-09-14 15:01:00", False), ("2026-09-12 10:00:00", False),  # 周六
    ]
    for s, want in cases:
        got = quotes.is_trading_time(datetime.fromisoformat(s))
        assert got == want, "%s -> %s, 期望 %s" % (s, got, want)


@test
def test_tencent_parse():
    class FakeResp:
        text = _tencent_payload()
        encoding = ""
    orig = quotes.requests.get
    quotes.requests.get = lambda url, timeout: FakeResp()
    try:
        out = quotes._fetch_tencent(["600519", "000001", "688801"])
    finally:
        quotes.requests.get = orig
    assert set(out) == {"600519", "000001"}, out.keys()
    q = out["600519"]
    assert q["price"] == 1275.16 and q["prev_close"] == 1285.13
    assert q["high"] == 1290.00 and q["low"] == 1270.00
    assert q["name"] == "贵州茅台" and q["source"] == "tencent"
    assert q["time"] == "20260911161451"


@test
def test_ttl_cache_and_force():
    calls = {"n": 0}

    def fake_fetch(codes):
        calls["n"] += 1
        return {c: {"price": 10.0 + calls["n"], "prev_close": 10.0, "open": 10.0,
                    "high": None, "low": None, "time": "t", "name": "", "source": "fake"}
                for c in codes}

    orig = quotes._fetch_tencent
    quotes._fetch_tencent = fake_fetch
    quotes.clear_cache()
    try:
        a = quotes.get_live_prices(["600519"], force=True)
        b = quotes.get_live_prices(["600519"])            # TTL 内命中缓存
        assert calls["n"] == 1 and a["600519"]["price"] == b["600519"]["price"]
        c = quotes.get_live_prices(["600519"], force=True)  # 强刷
        assert calls["n"] == 2
        d = quotes.get_live_prices(["000001"])             # 未缓存代码触发增量拉取
        assert "000001" in d
    finally:
        quotes._fetch_tencent = orig
        quotes.clear_cache()


@test
def test_network_failure_returns_empty():
    def boom(codes):
        raise ConnectionError("down")
    orig_t = quotes._fetch_tencent
    orig_e = quotes._fetch_eastmoney_one
    quotes._fetch_tencent = boom
    quotes._fetch_eastmoney_one = lambda code: (_ for _ in ()).throw(ConnectionError("down"))
    quotes.clear_cache()
    try:
        assert quotes.get_live_prices(["600519"], force=True) == {}
        assert quotes.get_live_price(["600519"], "600519") is None
    finally:
        quotes._fetch_tencent = orig_t
        quotes._fetch_eastmoney_one = orig_e
        quotes.clear_cache()


@test
def test_latest_price_falls_back_to_close_when_disabled():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    conn.execute("INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                 " volume, amount, pct_chg, turnover) VALUES "
                 "('600519','2026-09-11',1280,1290,1270,1275.16,100,1000,-0.77,0.5)")
    conn.commit()
    broker = PaperBroker()
    # 环境变量已禁用实时价 → 应返回日线收盘
    assert broker.latest_price(conn, "600519", live=True) == 1275.16
    assert broker.latest_price(conn, "600519", live=False) == 1275.16
    conn.close()


@test
def test_env_switch_respected():
    # _live_quote_price 在禁用环境下必须返回 None（即使交易时段）
    assert PaperBroker._live_quote_price("600519") is None


@test
def test_audit_snapshot_writes_jsonl():
    tmp = tempfile.mkdtemp()
    orig_dir = quotes.QUOTES_DIR
    quotes.QUOTES_DIR = Path(tmp)
    try:
        quotes._audit_snapshot({"600519": {"price": 1.0}})
        files = list(Path(tmp).glob("*.jsonl"))
        assert len(files) == 1
        rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        assert rec["quotes"]["600519"]["price"] == 1.0
    finally:
        quotes.QUOTES_DIR = orig_dir
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d tests passed" % (len(_TESTS) - failed, len(_TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
