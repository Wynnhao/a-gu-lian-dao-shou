"""common/market.py 唯一口径测试（结构性重构 Phase 2 验收）。

锁定语义红线（见 market.py 模块 docstring）：
- 板块前缀 → 涨跌停幅度（创业/科创 20% 含 302、北交所 30% 含 920 新段）；
- limit_price 的 Decimal ROUND_HALF_UP .005 边界（银行家舍入会差 1 分钱）；
- 秒级 in_trading_session 与分钟级 is_trading_time 的边界与差集；
- 量纲判别核心 + 两侧薄壳各自的失败回退（fetcher 原值 / audit None）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
if str(BASE / "tests") not in sys.path:
    sys.path.insert(0, str(BASE / "tests"))

import traceback
from datetime import datetime

from common import market
from data import audit as data_audit
from data import fetcher
from risk.engine import limit_pct as engine_limit_pct
from signals.factors import limit_pct as factors_limit_pct


WED = datetime(2026, 9, 16, 10, 0, 0)   # 周三（固定日期避免测试依赖真实今天）
SAT = datetime(2026, 9, 19, 10, 0, 0)   # 周六


def test_limit_pct_board_prefixes():
    """板块前缀断言：创业/科创→0.20（含 302 新段），北交所→0.30（含 920 新段），主板→0.10。"""
    for code, expect in [
        ("300750", 0.20), ("301236", 0.20), ("302132", 0.20),     # 创业板含 302
        ("688801", 0.20), ("689009", 0.20),                       # 科创板含 689 CDR
        ("430047", 0.30), ("830799", 0.30), ("870799", 0.30),     # 北交所
        ("920002", 0.30), ("889999", 0.30),                       # 920/88 新段
        ("600519", 0.10), ("000001", 0.10), ("002230", 0.10),
        ("601318", 0.10),
    ]:
        assert market.limit_pct(code) == expect, code
        assert engine_limit_pct(code) == expect, code      # engine re-export 同源
        assert factors_limit_pct(code) == expect, code     # factors re-export 同源
    # 字符串数字与 int 输入容错
    assert market.limit_pct(300750) == 0.20


def test_limit_price_decimal_half_up_boundary():
    """.005 边界：Decimal ROUND_HALF_UP 入，银行家舍入(round)会差 1 分钱。"""
    assert market.limit_price(100.05, 0.10, True) == 110.06   # 110.055 → 110.06
    # 对照：本例浮点表示恰为 110.055，round() 也得 110.06；Half-Up 语义由
    # Decimal(str()) 精确十进制保证（裸乘浮点在 100.05×1.08 等组合会差 1 分钱）
    assert market.limit_price(100.05, 0.08, True) == 108.05   # 108.054 精确截到 108.05
    assert market.limit_price(10.0, 0.10, True) == 11.00
    assert market.limit_price(10.0, 0.10, False) == 9.00
    assert market.limit_price(1500.0, 0.20, True) == 1800.00
    # 北交所 30%：9.55×1.3=12.415 → 12.42（HALF_UP），round() 银家 12.42 但浮点表示可能 12.41
    assert market.limit_price(9.55, 0.30, True) == 12.42


def test_in_trading_session_second_level_boundaries():
    """秒级时段窗（风控规则4 语义）：边界含 11:30:00、15:00:00，11:30:01/15:00:01 ∉。"""
    cases = [
        (datetime(2026, 9, 16, 9, 29, 59), False),
        (datetime(2026, 9, 16, 9, 30, 0), True),
        (datetime(2026, 9, 16, 11, 30, 0), True),
        (datetime(2026, 9, 16, 11, 30, 1), False),    # 秒级：1 秒即出窗
        (datetime(2026, 9, 16, 13, 0, 0), True),
        (datetime(2026, 9, 16, 15, 0, 0), True),
        (datetime(2026, 9, 16, 15, 0, 1), False),
        (SAT, False),                                  # 周末
    ]
    for dt, expect in cases:
        assert market.in_trading_session(dt) is expect, dt


def test_is_trading_time_minute_level_and_diff_set():
    """分钟级行情门控：11:30:30 ∈（与秒级窗的差集，红线3）。"""
    assert market.is_trading_time(datetime(2026, 9, 16, 11, 30, 30)) is True
    assert market.is_trading_time(datetime(2026, 9, 16, 11, 30, 30)) != \
        market.in_trading_session(datetime(2026, 9, 16, 11, 30, 30))
    assert market.is_trading_time(datetime(2026, 9, 16, 15, 0, 59)) is True
    assert market.is_trading_time(datetime(2026, 9, 16, 15, 1, 0)) is False
    assert market.is_trading_time(SAT) is False
    assert market.is_trading_time() in (True, False)   # 缺省参数不抛异常
    # quotes 薄壳 re-export 同源
    from data.quotes import is_trading_time as quotes_itt
    assert quotes_itt(datetime(2026, 9, 16, 11, 30, 30)) is True


def test_norm_volume_core_and_shell_fallbacks():
    """量纲判别核心 + 两侧薄壳回退差异（fetcher 原值 / audit None，红线4）。"""
    # 原值是「股」（v 接近 implied）→ 除以 100
    assert market.volume_unit_is_lots(1000000.0, 10000000.0, 10.0) is False
    assert fetcher._norm_volume(1000000.0, 10000000.0, 10.0) == 10000.0
    assert data_audit._norm_volume(1000000.0, 10000000.0, 10.0) == 10000.0
    # 原值已是「手」（v*100 接近 implied）
    assert market.volume_unit_is_lots(10000.0, 10000000.0, 10.0) is True
    assert fetcher._norm_volume(10000.0, 10000000.0, 10.0) == 10000.0
    assert data_audit._norm_volume(10000.0, 10000000.0, 10.0) == 10000.0
    # 非法输入回退：fetcher 原值返回、audit None
    assert fetcher._norm_volume("abc", 100.0, 10.0) == "abc"
    assert data_audit._norm_volume("abc", 100.0, 10.0) is None
    assert fetcher._norm_volume(-5.0, 100.0, 10.0) == -5.0
    assert data_audit._norm_volume(-5.0, 100.0, 10.0) is None
    # 判别核心自身对非法输入返回 True（=不转换，回退语义留给薄壳）
    assert market.volume_unit_is_lots("abc", 100.0, 10.0) is True


def test_audit_limit_pct_shell_tolerance():
    """audit 薄壳 = market.limit_pct×100 + 0.5pp（舍入容差，非 ST 容差，红线1）。"""
    assert data_audit._limit_pct("300750") == 20.5
    assert data_audit._limit_pct("302132") == 20.5
    assert data_audit._limit_pct("688801") == 20.5
    assert data_audit._limit_pct("830799") == 30.5
    assert data_audit._limit_pct("920002") == 30.5
    assert data_audit._limit_pct("600519") == 10.5


def test_backtest_limit_up_price_uses_market_pct():
    """backtest 第五处收敛：pct 走 market（北交所 30% 分支补齐），裸乘法保留。"""
    from signals import backtest
    assert backtest._limit_up_price(10.0, "600519") == 11.0
    assert backtest._limit_up_price(10.0, "300750") == 12.0
    assert backtest._limit_up_price(10.0, "830799") == 13.0    # 收敛前是 11.0（缺北交所）


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
