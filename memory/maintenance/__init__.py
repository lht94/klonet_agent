"""记忆生命周期 Worker 模块（04 计划阶段 1 起逐步填充）。

阶段 0 时此包只暴露 ``health`` 子模块；阶段 1 起新增 ``base`` 协议层与
``repository`` 仓库层；阶段 2 起新增 ``service`` 主循环与 ``cli`` 命令行
入口。后续阶段（3–8）会陆续加 ``jobs`` / ``reembedding`` / ``proposals``。

不允许主链路（``agent.py`` / ``orchestrator.py`` / ``session.py`` / ``agents/`` /
``app/``） import 本包——见 ``scripts/check_maintenance_isolation.py`` 与
``tests/test_maintenance_isolation.py``（阶段 2 的 CI 检查）。
"""

from __future__ import annotations

__all__ = ["base", "cli", "health", "repository", "service"]