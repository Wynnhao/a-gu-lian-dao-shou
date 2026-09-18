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


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


def _tencent_payload(market="sh", name="贵州茅台", code="600519",
                     price="1275.16", prev="1285.13", open_="1280.00",
                     ts="20260911161451", high="1290.00", low="1270.00",
                     ask1_price="", ask1_vol="", float_mv="",
                     limit_up="", limit_down="") -> str:
    """按 **2026-09-19 只读 GET qt.gtimg.cn 实测协议** 构造腾讯报文
    （Sprint4 W-A4：600519/300750/000001 三票交叉核验）。

    关键索引（实测锚定）：f3=现价 f4=昨收 f5=开盘 f6=成交量(手) f19/f20=卖一价/量(手)
    f21/f22=卖二价/量 f30=时间戳 f33/f34=最高/最低 f37=成交额(万) f42=当日最低
    f43=振幅% f44=流通市值(亿) f45=总市值(亿) f46=PB f47/f48=涨停/跌停价。

    旧 fixture 的 f21/f22=ask1、f42=float_mv、f45/f46=limit 是错位口径
    （f21 实为卖二、f42 实为当日最低、f45/f46 实为总市值/PB），已按实测重锚。
    """
    f = [""] * 50   # 实测协议至少 49 字段（f[48]=跌停价），留 1 余量
    f[0] = "100" if market == "sh" else "51"
    f[1] = name
    f[2] = code
    f[3] = price
    f[4] = prev
    f[5] = open_
    f[19] = ask1_price
    f[20] = ask1_vol
    f[30] = ts
    f[33] = high
    f[34] = low
    f[42] = low                       # f42=当日最低（实测与 f34 相同）
    f[44] = float_mv                  # 流通市值，亿元
    f[47] = limit_up
    f[48] = limit_down
    return 'v_%s%s="%s";' % (market, code, "~".join(f))


def _legacy_tencent_payload() -> str:
    """旧版双行 fixture（保留 test_tencent_parse 回归测试）。"""
    return (
        _tencent_payload("sh", "贵州茅台", "600519",
                         "1275.16", "1285.13", "1280.00",
                         "20260911161451", "1290.00", "1270.00") + "\n"
        + _tencent_payload("sz", "平安银行", "000001",
                           "11.74", "11.85", "11.80",
                           "20260911161454", "11.90", "11.60")
    )


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
        text = _legacy_tencent_payload()
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
    # Sprint 1 任务3：新增 5 个字段在旧 fixture（缺数据）时必须为 None，不得抛异常
    for k in ("ask1_price", "ask1_vol", "float_mv", "limit_up", "limit_down"):
        assert q[k] is None, "%s 应为 None（旧 fixture 不含），实得 %r" % (k, q[k])


@test
def test_tencent_parse_new_fields():
    """W-A4：按 2026-09-19 实测协议解析新增 5 字段（规则21 三条件）。

    实测样例（只读 GET qt.gtimg.cn，sh600519 @2026-09-18 收盘后）：
    price=1257.12 prev=1266.98 f19=1257.13 f20=1 f44=15715.03 f47=1393.68 f48=1140.28。
    涨跌停价与 common.market.limit_price 的 Decimal 取整口径精确吻合
    （1266.98×1.10=1393.678→1393.68；×0.90=1140.282→1140.28）。
    """
    class FakeResp:
        text = _tencent_payload(
            price="1257.12", prev="1266.98", open_="1262.99",
            ts="20260918161436", high="1265.88", low="1256.10",
            ask1_price="1257.13", ask1_vol="1",
            float_mv="15715.03",          # 流通市值，亿元（实测值）
            limit_up="1393.68", limit_down="1140.28")
        encoding = ""
    orig = quotes.requests.get
    quotes.requests.get = lambda url, timeout: FakeResp()
    try:
        out = quotes._fetch_tencent(["600519"])
    finally:
        quotes.requests.get = orig
    q = out["600519"]
    assert q["price"] == 1257.12 and q["prev_close"] == 1266.98
    assert q["ask1_price"] == 1257.13
    assert q["ask1_vol"] == 1.0                      # 卖一量，手（实测 1 手）
    assert q["float_mv"] == 15715.03 * 1e8           # 亿元 → 元
    assert q["limit_up"] == 1393.68
    assert q["limit_down"] == 1140.28
    # 涨跌停价与统一口径交叉验证（量纲修后自洽）
    from common.market import limit_price
    assert q["limit_up"] == limit_price(1266.98, 0.10, up=True)
    assert q["limit_down"] == limit_price(1266.98, 0.10, up=False)
    assert q["high"] == 1265.88 and q["low"] == 1256.10


@test
def test_tencent_parse_sample_300750_units():
    """W-A4 实测样例 2（sz300750 宁德时代 @2026-09-18）：单位口径交叉验证——
    f44 流通市值(12864.89 亿) < f45 总市值(13971.93 亿)，×1e8 后为元。"""
    class FakeResp:
        text = _tencent_payload(
            market="sz", name="宁德时代", code="300750",
            price="301.95", prev="304.30", open_="309.77",
            ts="20260918161412", high="310.00", low="300.27",
            ask1_price="301.96", ask1_vol="5",
            float_mv="12864.89",
            limit_up="365.16", limit_down="243.44")
        encoding = ""
    orig = quotes.requests.get
    quotes.requests.get = lambda url, timeout: FakeResp()
    try:
        out = quotes._fetch_tencent(["300750"])
    finally:
        quotes.requests.get = orig
    q = out["300750"]
    assert q["ask1_price"] == 301.96 and q["ask1_vol"] == 5.0
    assert q["float_mv"] == 12864.89 * 1e8           # ≈1.29 万亿（流通，元）
    # 创业板 20%：304.30×0.80=243.44、×1.20=365.16 精确吻合（Decimal 取整同口径）
    from common.market import limit_price
    assert q["limit_down"] == 243.44 == limit_price(304.30, 0.20, up=False)
    assert q["limit_up"] == 365.16 == limit_price(304.30, 0.20, up=True)


@test
def test_tencent_parse_empty_seal_volume_is_none():
    """边界：f20 卖一量为空字符串（跌停开板 / 未形成封单）时落 None，不抛异常。"""
    class FakeResp:
        # ask1_price 有但 ask1_vol 空，模拟"无封单"情形
        text = _tencent_payload(ask1_price="1257.13", ask1_vol="")
        encoding = ""
    orig = quotes.requests.get
    quotes.requests.get = lambda url, timeout: FakeResp()
    try:
        out = quotes._fetch_tencent(["600519"])
    finally:
        quotes.requests.get = orig
    q = out["600519"]
    assert q["ask1_price"] == 1257.13
    assert q["ask1_vol"] is None


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


# ---------------- C-ARC-3a/T5：录制专用 fetch_snapshot ----------------

def _snapshot_payload() -> str:
    """混编报文：茅台（sh600519）+ 上证指数（sh000001，显式表命中）+ 停牌票。"""
    return "\n".join([
        _tencent_payload("sh", "贵州茅台", "600519", "1275.16", "1285.13"),
        'v_sh000001="%s";' % "~".join([
            "100", "上证指数", "000001", "3123.11", "3105.55", "3108.00",
            "256000", "3100.00", "0", "0", "", "", "", "", "", "", "", "", "",
            "", "", "", "", "", "", "", "", "", "", "20260919103000",
            "", "", "3150.00", "3100.00", "", "", "", "31234.5"]),
        'v_sz000001="%s";' % "~".join(
            ["51", "平安银行", "000001", "0.00", "11.85", "11.80"] + [""] * 42),
    ])


@test
def test_fetch_snapshot_index_symbol_mapping():
    """指数走显式符号表：000001 必须拼成 sh000001（上证指数），
    绝不许经 _exchange_prefix 变成 sz000001（平安银行）。"""
    seen = {"url": ""}

    class FakeResp:
        text = _snapshot_payload()
        encoding = ""

    def fake_get(url, timeout=None, headers=None):
        seen["url"] = url
        return FakeResp()

    orig = quotes.requests.get
    quotes.requests.get = fake_get
    try:
        out = quotes.fetch_snapshot(["600519"], index_codes=["000001", "000300"])
    finally:
        quotes.requests.get = orig
    assert "sh600519" in seen["url"] and "sh000001" in seen["url"], seen["url"]
    assert "sz000001" not in seen["url"], "000001 被误拼成深市前缀！"
    assert "sh000300" in seen["url"]
    assert set(out) == {"600519", "000001"}, out.keys()
    assert out["000001"]["price"] == 3123.11          # 上证指数的价，非平安银行
    assert out["000001"]["source"] == "tencent_snapshot"


@test
def test_fetch_snapshot_volume_amount_units():
    """量纲：f[6] 手→股 ×100；f[37] 万元→元 ×10000（与 spot_tx 兜底口径一致）。"""
    class FakeResp:
        text = _snapshot_payload()
        encoding = ""

    orig = quotes.requests.get
    quotes.requests.get = lambda url, timeout=None, headers=None: FakeResp()
    try:
        out = quotes.fetch_snapshot(["600519"], index_codes=["000001"])
    finally:
        quotes.requests.get = orig
    q = out["600519"]
    # _tencent_payload 未填 f[6]/f[37]（空串）→ None，不抛异常
    assert q["volume"] is None and q["amount"] is None
    # 显式构造带量额的指数行：256000 手 → 25,600,000 股；31234.5 万 → 3.12345 亿元
    assert out["000001"]["volume"] == 256000 * 100.0
    assert out["000001"]["amount"] == 31234.5 * 10000.0


@test
def test_fetch_snapshot_skips_suspended():
    """停牌过滤：f[3]<=0 的行不进结果（照 _fetch_tencent 口径）。"""
    class FakeResp:
        text = _snapshot_payload()   # sz000001 price=0.00（停牌）
        encoding = ""

    orig = quotes.requests.get
    quotes.requests.get = lambda url, timeout=None, headers=None: FakeResp()
    try:
        out = quotes.fetch_snapshot(["600519", "000001"])
    finally:
        quotes.requests.get = orig
    assert "000001" not in out and "600519" in out


@test
def test_fetch_snapshot_sends_user_agent():
    """请求必须带 UA（免费源对无 UA 请求更易封禁，照 fetcher Session 教训）。"""
    captured = {}

    class FakeResp:
        text = _snapshot_payload()
        encoding = ""

    def fake_get(url, timeout=None, headers=None):
        captured["headers"] = headers
        return FakeResp()

    orig = quotes.requests.get
    quotes.requests.get = fake_get
    try:
        quotes.fetch_snapshot(["600519"])
    finally:
        quotes.requests.get = orig
    assert captured["headers"] and "User-Agent" in captured["headers"]
    assert "Mozilla" in captured["headers"]["User-Agent"]


@test
def test_fetch_snapshot_cooldown_persisted():
    """独立冷却：连续失败 ≥5 次（跨进程累计，count 落盘）→ 冷却期直接返回 {}；
    健康文件原子落盘到 AGSICKLE_STATE_DIR（与 fetcher akshare 熔断器互不影响）。"""
    import time as _t
    state = tempfile.mkdtemp(prefix="quotes_cooldown_")
    old_state = os.environ.get("AGSICKLE_STATE_DIR")
    os.environ["AGSICKLE_STATE_DIR"] = state

    def boom(url, timeout=None, headers=None):
        raise ConnectionError("down")

    orig = quotes.requests.get
    quotes.requests.get = boom
    try:
        for i in range(5):
            out = quotes.fetch_snapshot(["600519"])
            assert out == {}
            if i < 4:
                assert not quotes.recorder_blocked()   # 未达阈值不冷却
        assert quotes.recorder_blocked()               # 第 5 次失败触发冷却
        hf = Path(state) / "recorder_health.json"
        assert hf.is_file()
        blob = json.loads(hf.read_text(encoding="utf-8"))
        assert blob["blocked_until"] > _t.time()
        assert blob["fail_count"] == 0                 # 进入冷却后计数清零
        quotes.requests.get = orig
        assert quotes.fetch_snapshot(["600519"]) == {}  # 冷却期不发起网络请求
    finally:
        quotes.requests.get = orig
        if old_state is None:
            os.environ.pop("AGSICKLE_STATE_DIR", None)
        else:
            os.environ["AGSICKLE_STATE_DIR"] = old_state
        import shutil
        shutil.rmtree(state, ignore_errors=True)


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
