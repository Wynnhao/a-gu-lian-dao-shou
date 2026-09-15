"""测试聚合入口：跑全部 8+ 个测试文件（兼作 CI 入口）。

用法：python3 tests/run_all.py          # 全部
     python3 tests/run_all.py quick    # 跳过较慢的 execution/ai（调试用）
"""
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SLOW = {"test_execution.py", "test_ai_pipeline.py"}

FILES = [
    "test_risk_engine.py", "test_regime.py", "test_execution.py", "test_ai_pipeline.py",
    "test_signals.py", "test_review.py", "test_news_macro.py",
    "test_quotes.py", "test_fetcher_breaker.py", "test_audit.py", "test_movers_hot.py",
    "test_webapp.py", "test_pipeline.py", "test_market.py",
]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    files = [f for f in FILES if not (args and args[0] == "quick" and f in SLOW)]
    t0 = time.time()
    failed = []
    for f in files:
        p = BASE / "tests" / f
        if not p.is_file():
            continue
        proc = subprocess.run([sys.executable, str(p)], cwd=str(BASE),
                              capture_output=True, text=True)
        status = "OK " if proc.returncode == 0 else "FAIL"
        print("%s %s" % (status, f))
        if proc.returncode != 0:
            failed.append(f)
            print((proc.stdout or "")[-1500:])
            print((proc.stderr or "")[-1500:])
    print("\n%d/%d 文件通过（%.1fs）" % (len(files) - len(failed), len(files),
                                        time.time() - t0))
    if failed:
        print("失败: %s" % ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
