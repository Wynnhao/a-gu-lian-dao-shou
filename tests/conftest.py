"""pytest 全局夹具：把「生产文件读写点」统一隔离到临时沙箱。

背景（2026-09-17 审查补丁批 Fix D）：tests/test_pipeline_full.py 的 teardown_module
会清掉所有「模块导入时不存在」的 AGSICKLE_* 环境变量（整文件隔离设计），连带清掉
其它测试文件在 import 期设置的沙箱目录（test_risk_engine / test_signals 的
AGSICKLE_SIGNAL_EVAL_DIR）。被清后：
- 规则20（因子拥挤熔断）与截面打分降权改读**生产**
  logs/signal_eval/factor_crowding.json——2026-09-17 生产 crowded=true，
  直接令 12 个风控用例 + 2 个信号用例批量失败；
- 更严重的是 compute_all 会把合成拥挤状态**写回生产目录**，污染真实风控状态
  （与 P0-4 的 risk_event 泄漏同类）。

此夹具对每个用例注入全新空沙箱目录（缺文件 → crowded=False），per-test 独立，
既防读污染也防写污染。各测试文件内显式 `_sandbox_signal_eval_dir()` 的用例仍然
有效（它们各自覆盖并恢复）。
"""
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_signal_eval_dir(monkeypatch):
    monkeypatch.setenv("AGSICKLE_SIGNAL_EVAL_DIR",
                       tempfile.mkdtemp(prefix="agsickle_signal_eval_ct_"))
