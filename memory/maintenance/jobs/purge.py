"""PurgeJob —— 物理清理超过保留期的逻辑删除记录（04 计划 §6.3）。

与旧 ``MemoryAdmin.purge()``（单次 ``purge_deleted`` 调用）的差别，全部写进
``MemoryAdmin.purge_report``：多批次、tombstone 先写、删完抽查召回。

这个 Job 只负责三件部署层的事：

1. **按租户轮转 + 断点**——与 ExpirationJob 同一套 ``sweep_tenants``；
2. **保留期从部署配置读**，而不是从调度周期推。``KLONET_AGENT_MEMORY_RETENTION_DAYS``
   缺省 30 天；配了更短的会被 ``RetentionPolicy.compliance_floor_days`` 顶回去
   （本仓库唯一一处钳制，方向保守）。
3. **dry-run 透传**——``once --job purge --dry-run`` 输出"会删多少 / 最老删除时间 /
   预计释放字节"，不写库。真实删除不可逆，运维必须先看得见这个数字。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.admin import MemoryAdmin, RetentionPolicy
from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.domain import Tenant
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.tenants import (
    default_tenants_provider,
    normalise_tenants,
    sweep_tenants,
)

__all__ = ["PurgeJob", "build_job", "retention_policy_from_env"]

_RETENTION_DAYS_ENV = "KLONET_AGENT_MEMORY_RETENTION_DAYS"
_DEFAULT_RETENTION_DAYS = 30


def retention_policy_from_env(env: Any | None = None) -> RetentionPolicy:
    """从部署配置读保留期。

    **刻意不接受"调度周期"当参数**：``purge_interval_seconds=86400`` 是"多久跑
    一次"，不是"保留多久"。把两者混起来会写出"每天跑一次、保留 1 天"这种
    看起来自洽、实际把用户数据当天删光的配置。

    取值非法时退回缺省 30 天而不是拒绝启动：这个 Job 的价值是"保守地什么都不删"，
    为它让整个 Worker 起不来不划算（对比 ``MaintenanceConfig`` 的严格拒绝——
    那里的非法值会让服务以错误方式运行，这里只会让它更保守）。
    """

    source = os.environ if env is None else env
    raw = str(source.get(_RETENTION_DAYS_ENV, "") or "").strip()
    days = _DEFAULT_RETENTION_DAYS
    if raw:
        try:
            days = max(0, int(raw))
        except ValueError:
            days = _DEFAULT_RETENTION_DAYS
    return RetentionPolicy(soft_delete_retention_days=days)


class PurgeJob:
    """物理清理已过保留期的逻辑删除记录；实现 ``MaintenanceJob``。"""

    name = "purge"

    def __init__(
        self,
        *,
        database: MemoryDatabase,
        tenants_provider: Callable[[], Sequence[Tenant]] | None = None,
        admin_factory: Callable[[Tenant], MemoryAdmin] | None = None,
        policy: RetentionPolicy | None = None,
        batch_size: int = 1000,
        recall_sample: int = 3,
        max_tenants_per_run: int = 50,
        max_seconds_per_run: float = 60.0,
        include_shared_ops: bool = False,
        clock: Callable[[], datetime] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        if max_tenants_per_run <= 0:
            raise ValueError("max_tenants_per_run 必须为正整数")
        if max_seconds_per_run <= 0:
            raise ValueError("max_seconds_per_run 必须为正数")
        if recall_sample < 0:
            raise ValueError("recall_sample 不能为负数")
        self._database = database
        self._admin_factory = admin_factory
        self._policy = policy or retention_policy_from_env()
        self._batch_size = int(batch_size)
        self._recall_sample = int(recall_sample)
        self._max_tenants = int(max_tenants_per_run)
        self._max_seconds = float(max_seconds_per_run)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._should_stop = should_stop
        # 默认**不**扫 shared_ops：共享运维记忆的保留期通常由运维单独控制
        # （它们是团队资产，不该跟着某个用户的删除策略走）。
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

        leaks: list[str] = []
        stopped_early = False

        def _handler(tenant: Tenant) -> tuple[int, int, int, str | None] | None:
            nonlocal leaks, stopped_early
            admin = self._admin_for(tenant)
            report = admin.purge_report(
                policy=self._policy,
                now=self._clock(),
                dry_run=context.dry_run,
                batch_size=self._batch_size,
                max_batches=1,
                recall_sample=self._recall_sample,
            )
            if report.recall_leaks:
                leaks.extend(report.recall_leaks)
                stopped_early = True
            if context.dry_run:
                backlog = report.backlog
                note = (
                    f"{_label(tenant)}: dry_run would_remove={report.removed} "
                    f"oldest={backlog.oldest_deleted_at.isoformat() if backlog and backlog.oldest_deleted_at else '-'} "
                    f"freed_bytes≈{backlog.estimated_bytes if backlog else 0}"
                )
                return (report.removed, 0, 0, note)
            note = None
            if report.removed:
                note = f"{_label(tenant)}: purged={report.removed}"
            return (report.removed, report.removed, 0, note)

        def _should_stop() -> bool:
            # 抽查发现"删了还能召回"时必须立刻停后续租户——继续删只会扩大污染面。
            # 注意这里是**闭包**而不是在参数位置求值的表达式：`stopped_early`
            # 会在迭代过程中被 `_handler` 改写。
            if stopped_early:
                return True
            return bool(self._should_stop and self._should_stop())

        outcome = sweep_tenants(
            tenants,
            cursor,
            handler=_handler,
            clock=self._clock,
            batch_deadline=batch_end,
            should_stop=_should_stop,
            max_tenants=self._max_tenants,
        )

        summary = (
            f"tenants={outcome.tenants_done} scanned={outcome.scanned} "
            f"purged={outcome.changed} stopped_early={stopped_early}"
            f"{' dry_run' if context.dry_run else ''}"
        )
        details = [summary, *outcome.notes[:8]]
        if leaks:
            details.append(f"recall_leaks={len(leaks)}")
        return JobResult(
            scanned=outcome.scanned,
            changed=outcome.changed,
            proposed=0,
            failed=0,
            next_cursor=outcome.next_cursor,
            details=tuple(details),
        )


def _label(tenant: Tenant) -> str:
    return f"{tenant.user_id}/{tenant.project_id or '-'}"


def build_job(*, database: MemoryDatabase, config: MaintenanceConfig) -> PurgeJob:
    return PurgeJob(
        database=database,
        batch_size=config.purge_batch_size,
    )
