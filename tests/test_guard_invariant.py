"""门卫不变量守护测试（C-ARC-5，施工方案 §2.T1）：把「PaperBroker.buy/sell 是
trade 表唯一成交入口」从约定固化为静态 AST 测试。

不变量（CONSTRAINTS.md §3.1）：所有成交必须经 runner 的风控链——生产代码中
`.buy(conn, ...)` / `.sell(conn, ...)` 调用点只允许两处：
- execution/runner.py::_execute       （confirm 闸门之后：buy+sell）
- execution/runner.py::_do_kill_locked（规则5 授权的唯一无 confirm 清仓卖出，仅
  sell；豁免依据 = CONSTRAINTS §3.3 优先级链「kill（强平）」最高级；W-D7/P2-21
  锁下沉自 _do_kill 改名，外层 _do_kill 只负责可重入执行锁）

扫描口径：AST 遍历全部 *.py，收集 `Call(func=Attribute(attr in ("buy","sell")))`
且**第一个位置实参名为 conn** 的节点 → (相对路径, 所在函数名)。
首参 conn 是 PaperBroker.buy/sell 的签名锚（execution/paper.py），普通对象方法
（如 sqlite conn.execute）不会误中。

白名单豁免类：tests/** 下的调用点按类豁免——它们在 :memory:/AGSICKLE_DB 沙箱里
单测 broker 本身（C-TEST-3/4 隔离纪律），不触生产库；生产代码（tests/ 之外）
新增任何调用点都会让本测试变红，必须显式修订白名单并过 code review。

残余风险（声明）：`getattr(broker, "buy")` 动态派发 AST 拦不住（全库无此风格，
接受）；本测试只守「代码文本」不变量，运行时绕行（自写 sqlite INSERT trade）
不在此射程——那是 repo.insert_trade 收敛层 + readback 回读的防线职责。

直跑：python3 tests/test_guard_invariant.py（兼容 pytest 收集）
"""
import ast
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# 生产代码允许的成交调用点（文件为 posix 相对路径，函数为所在 def 名）
# _do_kill → _do_kill_locked：Sprint4 W-D7/P2-21 锁下沉改名（同一 kill 路径——
# 外层 _do_kill 只加可重入执行锁，成交调用留在 _locked 实现内）
ALLOWED_POINTS = {
    ("execution/runner.py", "_execute"),
    ("execution/runner.py", "_do_kill_locked"),
}

# 不扫描的目录（运行时产物/第三方/本地工具区）
EXCLUDE_DIRS = {".venv", "venv", "node_modules", "logs", ".git", "__pycache__",
                ".zcode", ".lark-draft", "dist", "build", "coverage"}


def _iter_py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        yield p, rel


def _enclosing_func_name(tree: ast.AST, node: ast.Call) -> str:
    """节点所在最内层函数名；模块级调用返回 ""（视作可疑点，必进不了白名单）。"""
    best = ""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.lineno <= node.lineno <= getattr(n, "end_lineno", n.lineno):
                best = n.name  # walk 无序，取行号覆盖的最内层：继续遍历以窄化
    return best


def scan_broker_call_points(root: Path) -> set:
    """收集 root 树内 `.buy(conn,…)/.sell(conn,…)` 调用点 {(relpath, func_name)}。"""
    out = set()
    for p, rel in _iter_py_files(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue  # 非源码/坏文件不拦（守护测试自身不可成为流程故障点）
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in ("buy", "sell")):
                continue
            if not node.args:
                continue
            a0 = node.args[0]
            if isinstance(a0, ast.Name) and a0.id == "conn":
                out.add((rel.as_posix(), _enclosing_func_name(tree, node)))
    return out


def _production_violations(points: set) -> set:
    """生产类调用点（tests/ 之外）落在白名单之外的即为违例。"""
    return {pt for pt in points if not pt[0].startswith("tests/")} - ALLOWED_POINTS


# ---------------- 测试 ----------------

def test_production_call_points_within_whitelist():
    """全库现状：生产类成交调用点 ⊆ 白名单（证明「runner 3 处」断言持续成立）。"""
    points = scan_broker_call_points(BASE)
    viol = _production_violations(points)
    assert not viol, "生产代码出现白名单外的 broker 成交调用点：%s" % sorted(viol)


def test_whitelist_points_actually_scanned():
    """反向断言：白名单两处必须被扫描器实际找到——防扫描器自身坏掉后集合为空、
    上一条测试空转通过（扫描器 rot 守护）。"""
    points = scan_broker_call_points(BASE)
    missing = ALLOWED_POINTS - points
    assert not missing, "白名单调用点未被扫描器发现（扫描器或代码结构变了）：%s" % sorted(missing)


def test_injected_call_point_turns_guard_red():
    """注入验证：tmp 目录写一个含 x.buy(conn,…) 的 .py，扫描器必须报红
    （不改生产源码，模拟「第 4 处调用点」）。"""
    with tempfile.TemporaryDirectory(prefix="guard_inject_") as td:
        rogue = Path(td) / "rogue.py"
        rogue.write_text(
            "def sneak(conn):\n"
            "    return broker.buy(conn, '000001', 'x', 10.0, 100)\n",
            encoding="utf-8")
        points = scan_broker_call_points(Path(td))
        assert points == {("rogue.py", "sneak")}, points
        viol = _production_violations(points)
        assert viol == {("rogue.py", "sneak")}, "注入点必须被判为违例"


def test_benign_attr_calls_not_matched():
    """误伤验证：attr 名撞车但首参非 conn（如 engine.buy(x)）不收集。"""
    with tempfile.TemporaryDirectory(prefix="guard_benign_") as td:
        f = Path(td) / "benign.py"
        f.write_text(
            "class Cart:\n"
            "    def buy(self, item): ...\n"
            "cart = Cart()\n"
            "cart.buy('apple')\n",
            encoding="utf-8")
        assert scan_broker_call_points(Path(td)) == set()


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
            import traceback
            traceback.print_exc()
    print("\n%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
