"""日志文件路径统一解析：AGSICKLE_LOG_DIR 逃生门（Sprint4 批次0 W0-1）。

所有模块的日志文件 handler 必须经 rotating_handler() 构造，禁止在 import 期
直接硬编码 BASE / "logs"。设置 AGSICKLE_LOG_DIR 后全部落到该目录（自动创建），
跑测试/隔离运行不再写生产 logs/（2026-09-18 审查 P0-7：exec.log 曾混入
exec_test_orders_* 测试成交，signal.log 曾被 test_limit_halt 夹具灌入 40 处
假"跌停应急单"）。

handler 在各模块 import 期构造，因此该变量必须在 import 前生效——双落点：
tests/run_all.py 子进程 env 与 tests/conftest.py 模块顶层。
副作用知情：root basicConfig 改命名 logger 后，akshare 等第三方经 root 传播
的日志不再进 fetch.log 等文件（计划 v2 已接受）。
"""
import logging.handlers
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def log_dir() -> Path:
    env = os.environ.get("AGSICKLE_LOG_DIR")
    if env:
        d = Path(env)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return BASE / "logs"


def rotating_handler(name: str) -> logging.handlers.RotatingFileHandler:
    """统一规格的文件 handler（与原各文件硬编码参数一致）。"""
    return logging.handlers.RotatingFileHandler(
        log_dir() / name, encoding="utf-8", maxBytes=5_000_000, backupCount=3)
