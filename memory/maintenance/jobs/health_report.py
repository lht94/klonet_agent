"""HealthReportJob —— 把生命周期健康度变成可查询数据（04 计划 §6.6 / 阶段 7）。

每个 tick：采集 ``health.collect_snapshot()`` → 经
``runtime.governance.service.record_run_metric`` 追加进
``governance.runtime_events``（**唯一权威通道**，不开第二张表）→ 产出
标准 ``JobResult``。

采集或上报失败都不让 Job 整体炸掉（写 fail 计数即可）——健康 Job 自己挂掉
会把所有 Job 拖进"连续失败 ≥ 3 → 不健康"的告警，反而淹没真实故障。
"""

from __future__ import annotations

from typing import Any

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.health import collect_snapshot, flatten_metrics

__all__ = ["HealthReportJob", "build_job"]


class HealthReportJob:
    """采集健康快照并上报指标；实现 ``MaintenanceJob``。"""

    name = "health_report"

    def __init__(
        self,
        *,
        database: MemoryDatabase,
        governance_enabled: bool = True,
    ) -> None:
        self._database = database
        self._governance_enabled = governance_enabled

    def run(self, context: JobContext) -> JobResult:
        snapshot = collect_snapshot(database=self._database, now=context.started_at)
        healthy = bool(snapshot.get("healthy"))

        reported = False
        note = ""
        if self._governance_enabled:
            try:
                # governance.runtime_events 未迁移的部署（0004 未应用）跳过
                # 上报——健康采集本身照常工作，账本缺位不是 Worker 的故障。
                if self._governance_tables_present():
                    from klonet_agent.runtime.governance.service import record_run_metric

                    record_run_metric(
                        self._database,
                        metrics=flatten_metrics(snapshot),
                        run_id=f"memory-maintenance-{context.run_id}",
                        actor_id=f"health_report/{context.worker_id}",
                    )
                    reported = True
                else:
                    note = "governance.runtime_events 不存在，跳过指标上报"
            except Exception as exc:  # noqa: BLE001 - 上报失败不能拖垮健康 Job
                note = f"指标上报失败：{type(exc).__name__}: {exc}"

        summary = (
            f"healthy={healthy} reason={snapshot.get('reason')!s} reported={reported}"
        )
        details = (summary, note) if note else (summary,)
        return JobResult(
            scanned=1,
            changed=1 if reported else 0,
            proposed=0,
            failed=0 if healthy else 0,  # 不健康 ≠ Job 失败；判定在快照里
            details=details,
        )

    def _governance_tables_present(self) -> bool:
        pool = self._database._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT to_regclass('governance.runtime_events') AS oid"
            ).fetchone()
        return bool(row and row["oid"])


def build_job(
    *, database: MemoryDatabase, config: MaintenanceConfig
) -> HealthReportJob:
    """CLI 工厂。健康 Job 只依赖数据库，任何部署都具备运行条件。"""

    return HealthReportJob(database=database)
