"""``memory_cutover.py --check`` 与 Worker 健康门禁的集成测试（04 计划阶段 7）。

§11.9 硬门禁：``--check`` 必须真的调 ``collect_snapshot()``，且
``healthy == false`` 时非零退出。缺这个集成 = 门禁不存在 = §11 自动判失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import memory_cutover as cli  # noqa: E402 - scripts 目录导入

from klonet_agent.memory.maintenance import health as health_module  # noqa: E402


@pytest.fixture()
def with_test_dsn(monkeypatch):
    monkeypatch.setenv("KLONET_AGENT_TEST_PG_DSN", "postgresql://placeholder:5432/postgres")


def test_check_calls_collect_snapshot_and_blocks_when_unhealthy(
    monkeypatch, with_test_dsn
) -> None:
    """healthy=False → --check 退出码 1，且绝不再往下跑评测。"""

    calls: dict[str, int] = {"n": 0}

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return {"healthy": False, "reason": "连续失败 ≥ 3 的 Job：purge"}

    monkeypatch.setattr(health_module, "collect_snapshot", _spy)
    monkeypatch.setattr(
        cli,
        "evaluate_memory_eval",
        lambda: pytest.fail("健康门禁应先拦住，不应跑到评测"),
    )
    code = cli.main(["--check"])
    assert code == cli.EXIT_EVAL_FAILED
    assert calls["n"] == 1, "--check 必须恰好调用一次 collect_snapshot"


def test_check_passes_when_healthy(monkeypatch, with_test_dsn) -> None:
    monkeypatch.setattr(
        health_module,
        "collect_snapshot",
        lambda *a, **k: {"healthy": True, "reason": "Worker 健康"},
    )
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (True, "达标"))
    code = cli.main(["--check"])
    assert code == cli.EXIT_OK


def test_health_snapshot_exception_degrades_to_pass(monkeypatch, with_test_dsn) -> None:
    """快照采集抛异常：cutover 路径不能炸——降级为过渡态放行。"""

    def _raise(*args, **kwargs):
        raise ImportError("模拟：worker 快照采集失败")

    monkeypatch.setattr(health_module, "collect_snapshot", _raise)
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (True, "达标"))
    code = cli.main(["--check"])
    assert code == cli.EXIT_OK


def test_real_snapshot_with_no_memory_dsn_is_transition_healthy(monkeypatch) -> None:
    """不 mock 的接线验证：真实 collect_snapshot 在无 DSN 时放行。"""

    monkeypatch.delenv("KLONET_AGENT_MEMORY_DSN", raising=False)
    healthy, detail = cli.evaluate_worker_health()
    assert healthy is True
    assert "过渡" in detail or "未配置" in detail
