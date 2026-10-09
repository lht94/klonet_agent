"""ExpirationJob —— 把"``valid_to`` 已过但状态还是 active"的记录归档（04 计划 §6.2）。

**这是防御性清理**，不是常规过期路径：正常写入会走 ``mark_expired``，状态与
``valid_to`` 一起改。出现分歧意味着有历史遗留直接改过 ``valid_to``，它们会一直
占着 "active" 的名字，让审计数字对不上。

三个刻意的取舍：

1. **按租户轮转，且必须包含 shared_ops**。``shared_ops`` 的放行在数据库层挂在
   **角色**（``TO klonet_ops``）而不是租户字段上；普通租户上下文永远扫不到它。
   所以 Job 默认用 ``include_shared_ops=True`` 枚举租户——漏掉这一项意味着共享
   运维记忆的过期记录永远不归档，而指标上看不出任何异常。
2. **每个 tick 只跑一个"批"**，进度靠 ``memory_maintenance_jobs.cursor`` 断点。
   一次 tick 里把所有租户跑完会拖过 lease 甚至 grace period。
3. **不自己传 keyset cursor**。keyset 分页（``(valid_to, memory_id)``）在
   repository 层实现并被 Admin 使用；但**归档本身天然可续**——已归档的行不再满足
   ``status='active'``，所以下一轮从该租户头部重新列出的就是"下一批"。
   这比维护一层嵌套 cursor 更不容易错（也避免"inner cursor 与 tenant cursor
   一起失效"的复合 bug 面）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.admin import MemoryAdmin
from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.domain import Tenant
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.tenants import (
    default_tenants_provider,
    normalise_tenants,
    sweep_tenants,
)

__all__ = ["ExpirationJob", "build_job"]


class ExpirationJob:
    """归档过期记忆；实现 ``MaintenanceJob``。"""

    name = "expiration"

    def __init__(
        self,
        *,
        database: MemoryDatabase,
        tenants_provider: Callable[[], Sequence[Tenant]] | None = None,
        admin_factory: Callable[[Tenant], MemoryAdmin] | None = None,
        batch_size: int = 500,
        outbox_defer_seconds: int = 3600,
        max_tenants_per_run: int = 50,
        max_seconds_per_run: float = 60.0,
        include_shared_ops: bool = True,
        clock: Callable[[], datetime] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        if max_tenants_per_run <= 0:
            raise ValueError("max_tenants_per_run 必须为正整数")
        if max_seconds_per_run <= 0:
            raise ValueError("max_seconds_per_run 必须为正数")
        self._database = database
        self._admin_factory = admin_factory
        self._batch_size = int(batch_size)
        self._outbox_defer_seconds = int(outbox_defer_seconds)
        self._max_tenants = int(max_tenants_per_run)
        self._max_seconds = float(max_seconds_per_run)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._should_stop = should_stop
        self._tenants_provider = tenants_provider or default_tenants_provider(
            database, include_shared_ops=include_shared_ops
        )

    # ------------------------------------------------------------- 依赖 --

    def _list_tenants(self) -> list[Tenant]:
        return normalise_tenants(self._tenants_provider())

    def _admin_for(self, tenant: Tenant) -> MemoryAdmin:
        if self._admin_factory is not None:
            return self._admin_factory(tenant)
        from klonet_agent.memory.postgres import PostgresMemoryRepository

        return MemoryAdmin(PostgresMemoryRepository(self._database, tenant))

    # ------------------------------------------------------------- 运行 --

    def run(self, context: JobContext, cursor: str | None) -> JobResult:
        tenants = self._list_tenants()
        if not tenants:
            return JobResult(details=("no_tenants：没有可调度的租户",))

        budget_end = self._clock().timestamp() + self._max_seconds
        if context.deadline is not None:
            budget_end = min(budget_end, context.deadline.timestamp())
        batch_end = datetime.fromtimestamp(budget_end, tz=timezone.utc)
        moment = self._clock()

        def _handler(tenant: Tenant) -> tuple[int, int, int, str | None] | None:
            admin = self._admin_for(tenant)
            if context.dry_run:
                candidates = admin.expired_preview(
                    now=moment, batch_size=self._batch_size
                )
                return (
                    candidates,
                    0,
                    0,
                    f"{_label(tenant)}: dry_run candidates={candidates}",
                )
            report = admin.archive_expired_report(
                now=moment,
                batch_size=self._batch_size,
                max_batches=1,
                outbox_defer_seconds=self._outbox_defer_seconds,
            )
            note = None
            if report.archived or report.outbox_deferred:
                note = (
                    f"{_label(tenant)}: archived={report.archived} "
                    f"outbox_deferred={report.outbox_deferred}"
                )
            return (report.archived, report.archived, 0, note)

        outcome = sweep_tenants(
            tenants,
            cursor,
            handler=_handler,
            clock=self._clock,
            batch_deadline=batch_end,
            should_stop=self._should_stop,
            max_tenants=self._max_tenants,
        )

        summary = (
            f"tenants={outcome.tenants_done} scanned={outcome.scanned} "
            f"archived={outcome.changed} failed={outcome.failed}"
        )
        details = (summary, *outcome.notes[:8])
        return JobResult(
            scanned=outcome.scanned,
            changed=outcome.changed,
            proposed=0,
            failed=outcome.failed,
            next_cursor=outcome.next_cursor,
            details=details,
        )


def _label(tenant: Tenant) -> str:
    return f"{tenant.user_id}/{tenant.project_id or '-'}"


def build_job(
    *, database: MemoryDatabase, config: MaintenanceConfig
) -> ExpirationJob:
    return ExpirationJob(
        database=database,
        batch_size=config.expiration_batch_size,
    )
