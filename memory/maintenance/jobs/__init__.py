"""具体维护 Job 实现（04 计划阶段 3 起填充）。

模块约定：``memory.maintenance.jobs.<canonical_name>`` 暴露 ``JOB`` 对象
（实现 ``MaintenanceJob``）。``memory/maintenance/cli.py::_build_jobs`` 按
``KNOWN_JOB_NAMES`` 逐个 try-import，实现了就自动注册、没实现就跳过——
不写死名字，避免"实现了但忘了注册"。

阶段 3：``embedding.py``（EmbeddingOutboxJob）
阶段 4：``expiration.py`` / ``purge.py``
阶段 5：``consolidation.py``
阶段 6：``reembedding.py``
阶段 7：``health_report.py``
"""

from __future__ import annotations

__all__: list[str] = []
