"""测试聚合入口：跑全部 8+ 个测试文件（兼作 CI 入口）。

用法：python3 tests/run_all.py          # 全部
     python3 tests/run_all.py quick    # 跳过较慢的 execution/ai（调试用）
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SLOW = {"test_execution.py", "test_ai_pipeline.py"}
# 逐文件 subprocess 直跑（pytest 的 tests/conftest.py 沙箱在此不生效）：
# 统一注入 signal_eval 沙箱目录，防止 compute_all/规则20 读写**生产**
# logs/signal_eval/（合成拥挤状态写回生产会污染真实风控，2026-09-17 实测）。
SIGNAL_EVAL_SANDBOX = tempfile.mkdtemp(prefix="agsickle_runall_se_")

FILES = [
    "test_risk_engine.py", "test_regime.py", "test_execution.py", "test_ai_pipeline.py",
    "test_signals.py", "test_review.py", "test_news_macro.py",
    "test_quotes.py", "test_fetcher_breaker.py", "test_audit.py", "test_movers_hot.py",
    "test_webapp.py", "test_pipeline.py", "test_pipeline_full.py", "test_market.py",
    "test_repo.py", "test_limit_halt.py", "test_breadth.py", "test_macro.py",
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
                              capture_output=True, text=True,
                              env={**os.environ,
                                   "AGSICKLE_SIGNAL_EVAL_DIR": SIGNAL_EVAL_SANDBOX})
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
