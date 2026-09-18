#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股镰刀手 · AI交易员看板 —— 本地 Web 仪表盘后端。

仅用 Python 标准库（http.server + sqlite3 + json），无任何第三方依赖：
    python3 webapp/server.py            # 默认 127.0.0.1:8317
    python3 webapp/server.py --port 9000

设计约定：
- 只读展示为主，唯一写操作是 POST /api/confirm、/api/reject，
  二者通过 subprocess 调 execution/runner.py，不直接改库；
- 每个请求独立 sqlite3 连接，用完即关；
- 文件类端点（report/logs/session/静态资源）一律做路径白名单：
  resolve 后必须仍位于对应基准目录（或项目根）之内，防目录穿越；
- 接口内任何异常返回 500 + {"error": "..."}，绝不让服务崩溃。

API 契约（全部 JSON）：
  GET  /api/overview                 总览（权益/持仓/基准/健康/黑名单）
  GET  /api/equity_curve             权益曲线（组合 vs 沪深300 归一 + 回撤%）
  GET  /api/candles?code=&days=120   K线 + MA5/20/60（后端算，头部补 null）
  GET  /api/signals                  每票最新信号，score 降序
  GET  /api/decisions?limit=50       决策流水（reasons/risk_notes 已解析为数组）
  GET  /api/trades?limit=50          成交流水
  GET  /api/risk_events?limit=50     风控事件
  GET  /api/news?limit=30            {market:[...], by_code:{code:[...]}}
  GET  /api/macro                    三指数最新估值
  GET  /api/macro_history?index=&years=5   PE / PE分位 历史（pe_pct 0~1 原值）
  GET  /api/health                   数据健康 + fetch_log 最近20条 + 各票行数
  GET  /api/reports                  logs/reports 列表（新→旧）
  GET  /api/report?file=xx.md        报告原文（文件名白名单 [A-Za-z0-9._-]）
  GET  /api/logs?name=exec&lines=200 日志尾部（name 限 7 个白名单文件）
  GET  /api/pending                  待人工确认单（扫 logs/orders/*/pending_*.json）
  GET  /api/sessions                 决策输入包日期列表
  GET  /api/session?date=&kind=      bundle_md | bundle_json | decision 文本内容
  GET  /api/workflow                十段流水线状态 + 当日决策追踪链
  GET  /api/doc?name=               docs/ 决策策略/策略库 markdown
  GET  /api/concepts                自选股概念分组（多归属+黑名单标注）
  GET  /api/dynamic_pools           异动池/热门池（?with_boards=1 加板块榜）
  GET  /api/data_status             数据状态总览（新鲜度/数据源用量/体检摘要）
  GET  /api/backtest                 logs/backtest_result.json 原文（无则 {missing:true}）
  POST /api/confirm {decision_id, by}          跑 runner.py confirm
  POST /api/reject  {decision_id, reason, by}  跑 runner.py reject
"""
import argparse
import bisect
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

BASE = Path(__file__).resolve().parent.parent          # 项目根
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))                      # 供 /api/dynamic_pools 等端点导入项目模块
_dist = Path(__file__).resolve().parent / "dist"       # 新前端构建产物优先
STATIC_DIR = _dist if (_dist / "index.html").is_file() \
    else Path(__file__).resolve().parent / "static"
from common import config as _common_config  # noqa: E402
from data import repo  # noqa: E402
CONFIG_PATH = _common_config.CONFIG_PATH  # 默认与统一配置层同源；test_webapp 可替换此属性注入
LOGS_DIR = BASE / "logs"
REPORTS_DIR = LOGS_DIR / "reports"
SESSION_DIR = LOGS_DIR / "session"
ORDERS_DIR = LOGS_DIR / "orders"

MIME_OVERRIDE = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


# ---------------------------------------------------------------- 基础工具


_CONFIG_CACHE: Dict[str, Any] = {"mtime": None, "cfg": {}}


def load_config() -> dict:
    """读 config.json（mtime 缓存热读；坏 JSON 保留旧缓存下次重试）。

    转发 common.config.load——语义与原实现逐行一致；CONFIG_PATH/_CONFIG_CACHE
    模块属性保留为 test_webapp 注入 hook（运行时读取，替换即生效）。
    """
    from common.config import load
    return load(path=CONFIG_PATH, cache=_CONFIG_CACHE)


def db_file() -> Path:
    # AGSICKLE_DB：与 fetcher.get_conn 同款测试隔离逃生门（黑盒测试指向临时库）
    env = os.environ.get("AGSICKLE_DB")
    if env:
        return Path(env)
    cfg = load_config()
    p = Path(str(cfg.get("db_path") or "data/market.db"))
    if not p.is_absolute():
        p = BASE / p
    return p


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file()), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


# ---------------------------------------------------------------- 人工闸门（唯一写操作，走 runner.py CLI）

# 审查 P2-2：串行化 runner 子进程调用，防并发 confirm 同一决策双重成交
# 审查 P2-2：串行化 runner 子进程调用，防并发 confirm 同一决策双重成交
_RUNNER_LOCK = threading.Lock()


def run_runner(args: List[str], timeout: int = 300) -> Tuple[int, str]:
    """跑 runner.py 子进程。timeout 默认 300s（Sprint4 W-A8/P1-8：confirm 需
    build_context 批量拉行情+兜底+regime 计算，网络差时 60s 会在 commit 后、
    set_status 前杀掉子进程卡状态机——有 dup 防线不损账本，但仍应避免）。"""
    cmd = [sys.executable, "execution/runner.py"] + args
    with _RUNNER_LOCK:
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True,
                              text=True, timeout=timeout)
    out = (proc.stdout or "")
    if proc.stderr:
        out += ("\n[stderr]\n" + proc.stderr) if out else proc.stderr
    return proc.returncode, out.strip() or "(无输出)"


# ---------------------------------------------------------------- API 实现（webapp/api/ 按域拆分，Phase 6 纯结构搬移）

from webapp.api.common import (q_all, q_one, parse_json_field, safe_join,  # noqa: E402,F401
                               clamp_int, fnum, health_issues_of, blacklist_of,
                               latest_bar, bench_close_at)
from webapp.api.market import (api_overview, api_equity_curve, api_candles,  # noqa: E402
                               api_signals, api_macro, api_macro_history)
from webapp.api.records import (api_decisions, api_trades,  # noqa: E402,F401
                                api_risk_events, api_news)
from webapp.api.content import (api_reports, api_report, api_logs,  # noqa: E402
                                api_sessions, api_session, api_backtest, api_doc)
from webapp.api.gate import api_pending, api_confirm, api_reject  # noqa: E402
from webapp.api.workflow import api_workflow  # noqa: E402
from webapp.api.groups import api_concepts, api_dynamic_pools  # noqa: E402
from webapp.api.data_status import api_data_status, api_health  # noqa: E402,F401


GET_ROUTES = {
    "/api/overview": lambda c, qs: api_overview(c, qs),
    "/api/equity_curve": lambda c, qs: api_equity_curve(c, qs),
    "/api/candles": lambda c, qs: api_candles(c, qs),
    "/api/signals": lambda c, qs: api_signals(c, qs),
    "/api/decisions": lambda c, qs: api_decisions(c, qs),
    "/api/trades": lambda c, qs: api_trades(c, qs),
    "/api/risk_events": lambda c, qs: api_risk_events(c, qs),
    "/api/news": lambda c, qs: api_news(c, qs),
    "/api/macro": lambda c, qs: api_macro(c, qs),
    "/api/macro_history": lambda c, qs: api_macro_history(c, qs),
    "/api/health": lambda c, qs: api_health(c, qs),
    "/api/reports": lambda c, qs: api_reports(qs),
    "/api/report": lambda c, qs: api_report(qs),
    "/api/logs": lambda c, qs: api_logs(qs),
    "/api/pending": lambda c, qs: api_pending(qs),
    "/api/sessions": lambda c, qs: api_sessions(qs),
    "/api/session": lambda c, qs: api_session(qs),
    "/api/backtest": lambda c, qs: api_backtest(qs),
    "/api/workflow": lambda c, qs: api_workflow(c, qs),
    "/api/doc": lambda c, qs: api_doc(qs),
    "/api/concepts": lambda c, qs: api_concepts(c, qs),
    "/api/dynamic_pools": lambda c, qs: api_dynamic_pools(c, qs),
    "/api/data_status": lambda c, qs: api_data_status(c, qs),
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AgSickleDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 输出
    def _send_bytes(self, status: int, body: bytes, ctype: str,
                    cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # /api/* 维持 no-store（实时数据）；静态资源按文件性质拆分（_serve_static）
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _err(self, status: int, msg: str) -> None:
        self._send_json({"error": msg}, status)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s %s\n" % (datetime.now().strftime("%H:%M:%S"),
                                           self.address_string(), fmt % args))

    # ---- 静态资源
    def _serve_static(self, path: str) -> None:
        rel = unquote(path).lstrip("/")
        if rel == "":
            rel = "index.html"
        target = safe_join(STATIC_DIR, rel)
        if target is None or not target.is_file():
            self._err(404, "静态资源不存在: %s" % rel)
            return
        suffix = target.suffix.lower()
        ctype = MIME_OVERRIDE.get(suffix) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        # 缓存拆分（Phase 5）：vite 产物 /assets/* 自带 content-hash → 一年 immutable；
        # index.html → no-cache 协商缓存（ETag 304，改版即刻生效）；API 维持 no-store
        if rel.startswith("assets/"):
            cache_control = "public, max-age=31536000, immutable"
        else:
            cache_control = "no-cache"
        etag = '"%x-%x"' % (int(target.stat().st_mtime), target.stat().st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("ETag", etag)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 路由
    _ALLOWED_HOSTS = ("127.0.0.1", "localhost")

    def _host_ok(self) -> bool:
        """Host 白名单：GET/POST 统一校验，防 DNS rebinding 读走组合数据。"""
        host = (self.headers.get("Host") or "").lower()
        return any(host == h or host.startswith(h + ":") for h in self._ALLOWED_HOSTS)

    def do_GET(self) -> None:  # noqa: N802
        try:
            if not self._host_ok():
                self._err(403, "仅允许本地 Host 访问（got Host=%r）"
                          % (self.headers.get("Host"),))
                return
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            route = GET_ROUTES.get(parts.path)
            if route is not None:
                conn = get_conn()
                try:
                    self._send_json(route(conn, qs))
                finally:
                    conn.close()
                return
            if parts.path.startswith("/api/"):
                self._err(404, "未知接口 %s" % parts.path)
                return
            self._serve_static(parts.path)
        except FileNotFoundError as e:
            self._err(404, str(e))
        except (ValueError,) as e:
            self._err(400, str(e))
        except Exception:
            traceback.print_exc()
            self._err(500, "服务器内部错误: %s" % traceback.format_exc(limit=1).strip())

    def do_POST(self) -> None:  # noqa: N802
        try:
            parts = urlsplit(self.path)
            if parts.path not in ("/api/confirm", "/api/reject"):
                self._err(404, "未知写接口 %s" % parts.path)
                return
            # 写操作来源防护（审查 P1-1）：防 CSRF / DNS-rebinding 触发下单动作
            if not self._host_ok():
                self._err(403, "写接口仅允许本地 Host 访问（got Host=%r）"
                          % (self.headers.get("Host"),))
                return
            origin = self.headers.get("Origin")
            if origin:
                # 精确 hostname 比对（此前 startswith 前缀校验可被
                # http://127.0.0.1.evil.com 绕过）
                try:
                    oh = (urlsplit(origin).hostname or "").lower()
                except ValueError:
                    oh = ""
                if oh not in self._ALLOWED_HOSTS:
                    self._err(403, "跨源写请求已拒绝（Origin=%r）" % origin)
                    return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._err(415, "Content-Type 须为 application/json（got %r）" % ctype)
                return
            raw_len = int(self.headers.get("Content-Length") or 0)
            if raw_len > (1 << 20):
                self._err(413, "请求体超过 1MB 上限")
                self.close_connection = True   # 审查P3-2：不消费超限body，直接断开防请求混淆
                return
            length = raw_len
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("body 必须为 JSON 对象")
            except (ValueError, UnicodeDecodeError) as e:
                self._err(400, "请求体解析失败: %s" % e)
                return
            if parts.path == "/api/confirm":
                self._send_json(api_confirm(body))
            else:
                self._send_json(api_reject(body))
        except subprocess.TimeoutExpired:
            self._err(504, "runner.py 执行超时（300s）")
        except FileNotFoundError as e:
            self._err(404, str(e))
        except (ValueError,) as e:
            self._err(400, str(e))
        except Exception:
            traceback.print_exc()
            self._err(500, "服务器内部错误: %s" % traceback.format_exc(limit=1).strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="A股镰刀手 · AI交易员看板（本地仪表盘）")
    ap.add_argument("--port", type=int, default=8317, help="监听端口（默认 8317）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 56)
    print("  A股镰刀手 · AI交易员看板 已启动")
    print("  访问地址: %s" % url)
    print("  数据库  : %s" % db_file())
    print("  静态目录: %s" % STATIC_DIR)
    print("  按 Ctrl+C 停止")
    print("=" * 56)
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
