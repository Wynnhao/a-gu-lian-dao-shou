"""fetcher 数据源熔断器测试（2026-09-15 审查修复：状态落盘跨进程持久）。

此前 _fail_counts/_blocked_until 是纯进程内 dict 且用 monotonic 时钟，而 fetcher
总在 launchd/catchup 拉起的短命进程里执行，冷却期跨不过进程边界。修复后冷却
状态写 logs/state/source_health.json（墙钟时间戳），新进程 import 期恢复。

直接运行：python3 tests/test_fetcher_breaker.py
"""
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import fetcher


class _BreakerSandbox:
    """把熔断状态重定向到临时目录：测试不读写真实 logs/state，互不污染。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="breaker_test_")
        fetcher._BREAKER_FILE = Path(self._tmp.name) / "source_health.json"
        fetcher._fail_counts.clear()
        fetcher._blocked_until.clear()
        return fetcher

    def __exit__(self, *exc):
        fetcher._fail_counts.clear()
        fetcher._blocked_until.clear()
        self._tmp.cleanup()
        return False


def test_trip_persists_across_process_restart():
    """连续失败达上限 → 熔断落盘；模拟新进程（内存清零+重载文件）后仍处冷却。"""
    with _BreakerSandbox():
        for _ in range(fetcher.SOURCE_MAX_FAIL):
            fetcher._mark_source("em", False)
        assert fetcher.source_blocked("em")
        assert fetcher._BREAKER_FILE.exists()
        fetcher._fail_counts.clear()
        fetcher._blocked_until.clear()
        fetcher._blocked_until.update(fetcher._load_breaker())
        assert fetcher.source_blocked("em")
        # 熔断生效：call_ak 直接抛 ConnectionError，不再触达数据源
        try:
            fetcher.call_ak("em", lambda: 1 / 0)
            raise AssertionError("熔断中 call_ak 应直接抛 ConnectionError")
        except ConnectionError:
            pass


def test_success_resets_fail_count():
    """成功即清零计数；未达上限不熔断、不落盘。"""
    with _BreakerSandbox():
        fetcher._mark_source("tx", False)
        fetcher._mark_source("tx", False)
        fetcher._mark_source("tx", True)
        for _ in range(fetcher.SOURCE_MAX_FAIL - 1):
            fetcher._mark_source("tx", False)
        assert not fetcher.source_blocked("tx")
        assert not fetcher._BREAKER_FILE.exists()


def test_expired_cooldown_pruned_on_load():
    """载入冷却状态时丢弃已过期项；墙钟时间戳保证跨进程语义一致。"""
    with _BreakerSandbox():
        fetcher._BREAKER_FILE.write_text(
            json.dumps({"em": time.time() - 10, "tx": time.time() + 600}),
            encoding="utf-8")
        loaded = fetcher._load_breaker()
        assert "em" not in loaded and "tx" in loaded
        assert not fetcher.source_blocked("em")


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
