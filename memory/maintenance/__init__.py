"""记忆生命周期 Worker 模块占位（04 计划阶段 0 落地 + 阶段 1 起逐步填充）。

阶段 0 时此包只暴露 ``health`` 子模块（已在 ``health.py`` 完整给出），其他子模块
（``base`` / ``repository`` / ``service`` / ``cli`` / ``jobs`` / ``reembedding`` /
``proposals``）是阶段 1–7 的工作。

不允许主链路（``agent.py`` / ``orchestrator.py`` / ``session.py`` / ``agents/`` /
``app/``） import 本包——见 ``scripts/check_maintenance_isolation.py`` 与
``tests/test_maintenance_isolation.py``（阶段 2 的 CI 检查）。
"""

from __future__ import annotations

__all__ = ["health"]