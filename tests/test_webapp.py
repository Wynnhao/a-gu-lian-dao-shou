"""webapp/server.py 冒烟测试（此前唯一写入口零测试——审查 P2-1）。

线程内起 ThreadingHTTPServer + 临时 SQLite 库 + urllib 实请求，覆盖：
- 静态首页 200 / 未知接口 404 / 路径穿越 404；
- Host 白名单：DNS rebinding Host → 403（GET 也拦，此前只有 POST 校验）；
- Origin 精确 hostname 校验：http://127.0.0.1.evil.com 前缀绕过 → 403；
- Content-Type 强制 / 超大请求体 413；
- report 不存在 → 404（此前一律 400）。

直跑：python3 tests/test_webapp.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


class Server:
    """测试专用服务器实例：临时库 + 随机端口。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="agsickle_webtest_")
        self.db_path = str(Path(self.tmp) / "market.db")
        conn = sqlite3.connect(self.db_path)
        from data.fetcher import DDL
        conn.executescript(DDL)
        conn.execute("INSERT INTO portfolio_state (date, cash, market_value, total,"
                     " drawdown, kill_switch, note) VALUES ('2026-09-11',900000,"
                     "100000,1000000,0,0,'test')")
        conn.commit()
        conn.close()
        import importlib
        import webapp.server as srv_mod
        self.srv_mod = importlib.reload(srv_mod)  # 保证拿到最新模块状态
        self.srv_mod.CONFIG_PATH = Path(self.tmp) / "config.json"
        self.srv_mod.CONFIG_PATH.write_text(json.dumps({"db_path": self.db_path}),
                                            encoding="utf-8")
        self.srv_mod._CONFIG_CACHE = {"mtime": None, "cfg": {}}
        self.srv_mod.LOGS_DIR = Path(self.tmp) / "logs"
        self.srv_mod.ORDERS_DIR = Path(self.tmp) / "orders"
        self.srv_mod.SESSION_DIR = Path(self.tmp) / "session"
        self.httpd = self.srv_mod.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      self.srv_mod.DashboardHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path: str, host: str = "127.0.0.1"):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     headers={"Host": host})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, path: str, body: dict, origin: str = None,
             ctype: str = "application/json", host: str = "127.0.0.1",
             big: bool = False):
        raw = json.dumps(body).encode() if not big else b"x" * (2 << 20)
        headers = {"Host": host, "Content-Type": ctype}
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=raw, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (ConnectionResetError, BrokenPipeError, OSError):
            # 413 场景：服务端不消费超限 body 直接断连（审查P3-2 设计行为），
            # 客户端表现为连接重置
            return 0, b""


SRV = None


@test
def test_get_routes_and_static():
    st, body = SRV.get("/")
    assert st == 200 and b"<html" in body[:400].lower(), (st, body[:80])
    st, _ = SRV.get("/api/unknown_endpoint")
    assert st == 404, st
    st, _ = SRV.get("/../../etc/passwd")
    assert st == 404, st
    st, body = SRV.get("/api/overview")
    assert st == 200 and json.loads(body)["total"] == 1000000.0, (st, body[:200])


@test
def test_get_host_whitelist():
    """DNS rebinding：恶意 Host 的 GET 也应 403（此前只有 POST 校验）。"""
    st, _ = SRV.get("/api/overview", host="evil.example.com")
    assert st == 403, st


@test
def test_post_write_protections():
    # Origin 前缀绕过：127.0.0.1.evil.com 必须被精确 hostname 校验拦下
    st, _ = SRV.post("/api/confirm", {"decision_id": 1},
                     origin="http://127.0.0.1.evil.com")
    assert st == 403, st
    st, _ = SRV.post("/api/confirm", {"decision_id": 1}, host="evil.com")
    assert st == 403, st
    st, _ = SRV.post("/api/confirm", {"decision_id": 1}, ctype="text/plain")
    assert st == 415, st
    st, _ = SRV.post("/api/confirm", {"decision_id": 1}, big=True)
    assert st in (413, 0), st   # 0=服务端按设计断连（不消费超限body）


@test
def test_report_missing_is_404():
    st, _ = SRV.get("/api/report?file=not_exist_report.md")
    assert st == 404, st


@test
def test_data_status_endpoint():
    """数据状态总览：空库也应 200，且各分节键齐全（新鲜度/源/体检/会话）。"""
    st, body = SRV.get("/api/data_status")
    assert st == 200, (st, body[:200])
    d = json.loads(body)
    for k in ("generated_at", "latest_bar_date", "watchlist_total", "watchlist_lagging",
              "indexes", "pools", "signal", "decision", "sources_30d",
              "recent_fails", "audit", "session", "quotes_audit"):
        assert k in d, k
    assert d["audit"].get("total") == 0          # 空库体检应为 0 问题
    assert [p["pool"] for p in d["pools"]] == ["movers", "hot_theme", "hot_stock"]


def _setup():
    global SRV
    SRV = Server()


def _teardown():
    if SRV:
        SRV.stop()


# 本文件为直跑专用设计（Server 初始化在 main() 内，pytest 直接收集会 NoneType）：
# 对已注册的测试函数关闭 pytest 收集，直跑（python3 tests/test_webapp.py）不受影响
for _fn in _TESTS:
    _fn.__test__ = False


def main() -> int:
    failed = 0
    _setup()
    try:
        for fn in _TESTS:
            try:
                fn()
                print("PASS %s" % fn.__name__)
            except Exception:  # noqa: BLE001
                failed += 1
                print("FAIL %s" % fn.__name__)
                traceback.print_exc()
    finally:
        _teardown()
    print("%d/%d tests passed" % (len(_TESTS) - failed, len(_TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
