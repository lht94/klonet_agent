"""具体维护 Job 实现（04 计划阶段 3 起填充）。

**模块约定**：``memory.maintenance.jobs.<module>`` 暴露其中之一：

* ``build_job(*, database, config)`` 工厂，返回 Job 或 ``None``
  （需要数据库/凭据的 Job；``None`` = "这个部署不具备运行条件"）；
* 或 ``JOB`` 对象（无依赖的 Job）。

``memory/maintenance/cli.py::_build_jobs`` 按 ``JOB_MODULES`` 逐个 try-import，
实现了就自动注册、没实现就跳过——不写死"哪些已实现"。

**为什么需要 ``JOB_MODULES`` 这张表**：Job 的**规范名**（写进
``memory_maintenance_jobs.job_name``，是外部契约）不必等于**模块文件名**。
最初 ``_build_jobs`` 直接用规范名拼模块名，于是 ``embedding_outbox`` 去找
``jobs/embedding_outbox.py``——而文件叫 ``jobs/embedding.py``，Job 静默地
没被注册。这类"命名约定不一致"的 bug 不会让任何单测变红，只会让 ``status``
里的 ``jobs`` 列表悄悄少一项。所以映射必须显式写下来。

阶段 3：``embedding.py``（EmbeddingOutboxJob）
阶段 4：``expiration.py`` / ``purge.py``
阶段 5：``consolidation.py``
阶段 6：``reembedding.py``
阶段 7：``health_report.py``
"""

from __future__ import annotations

# 规范 Job 名 → 模块文件 basename。未列出的名字默认模块名与规范名相同。
JOB_MODULES: dict[str, str] = {
    "embedding_outbox": "embedding",
    "expiration": "expiration",
    "purge": "purge",
    "consolidation": "consolidation",
    "reembedding": "reembedding",
    "health_report": "health_report",
}

__all__ = ["JOB_MODULES"]
