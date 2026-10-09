"""EmbeddingOutboxJob —— 把 embedding 积压真正清空（04 计划 §6.1 / 阶段 3）。

**它包装而不是重写** ``memory/embedding_worker.py``：那个类已经有租约、SKIP
LOCKED、指数退避、永久失败判定，全部是被 02 阶段 4 验证过的语义。这里的
增量只有三件：

1. **按租户调度**——``EmbeddingWorker`` 与 ``MemoryRepository`` 都是租户作用域
   的（outbox 的 RLS 沿 ``memory_versions → memory_records`` 回溯到租户），
   所以一个"能扫全库"的守护进程在设计上不存在。Job 负责枚举租户并逐个跑。
2. **把单次执行压进 deadline**——Job 运行在一个已经领了租约的 tick 里，
   必须在下一次 claim 之前交回，否则租约过期、别的 worker 会把同一批
   重新领走。
3. **产出标准 ``JobResult``**——让 Worker 主循环（``service.py``）能把
   这一批的 scanned/changed/failed 写进 ``memory_maintenance_runs``。

cursor 的语义：按 ``(user_id, project_id)`` 稳定排序后，cursor 记录**最后
一个完整跑完的租户**。全部跑完则写 ``None``——下一轮从头上重扫一遍是廉价
的（没有待办时 ``claim_pending_embeddings`` 直接返回空），但能保证不会因为
"总是从上次位置往后"而永久漏掉排在前面的租户。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.domain import Tenant
from klonet_agent.memory.embedding_worker import EmbeddingWorker
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.tenants import (
    decode_tenant_cursor,
    encode_tenant_cursor,
    normalise_tenants,
    tenant_key,
    tenants_after_cursor,
)
from klonet_agent.memory.repository import (
    DEFAULT_EMBEDDING_PROFILE_ID,
    EMBEDDING_DIMENSIONS,
    EmbeddingOutboxStats,
)

__all__ = [
    "EmbeddingOutboxJob",
    "build_job",
    "decode_tenant_cursor",
    "encode_tenant_cursor",
]


def _tenant_key(tenant: Tenant) -> tuple[str, str]:
    """稳定排序键（与 ``maintenance/tenants.tenant_key`` 同一口径）。"""

    return tenant_key(tenant)


class EmbeddingOutboxJob:
    """按租户消费 embedding outbox；实现 ``MaintenanceJob``。"""

    name = "embedding_outbox"

    def __init__(
        self,
        *,
        database: MemoryDatabase,
        embedder: Any | None,
        tenants_provider: Callable[[], Sequence[Tenant]] | None = None,
        repository_factory: Callable[[Tenant], Any] | None = None,
        worker_factory: Callable[..., EmbeddingWorker] | None = None,
        batch_size: int = 20,
        max_attempts: int = 5,
        lease_seconds: float = 300.0,
        max_seconds_per_run: float = 20.0,
        max_tenants_per_run: int = 50,
        profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID,
        embedding_model: str | None = None,
        embedding_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须为正整数")
        if max_seconds_per_run <= 0:
            raise ValueError("max_seconds_per_run 必须为正数")
        if max_tenants_per_run <= 0:
            raise ValueError("max_tenants_per_run 必须为正整数")
        self._database = database
        self._embedder = embedder
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._should_stop = should_stop
        self._batch_size = int(batch_size)
        self._max_attempts = int(max_attempts)
        self._lease_seconds = float(lease_seconds)
        self._max_seconds = float(max_seconds_per_run)
        self._max_tenants = int(max_tenants_per_run)
        self._profile_id = str(profile_id or "").strip() or DEFAULT_EMBEDDING_PROFILE_ID
        self._embedding_model = embedding_model
        self._embedding_version = embedding_version
        self._tenants_provider = tenants_provider
        self._repository_factory = repository_factory
        self._worker_factory = worker_factory

    # ------------------------------------------------------------- 依赖 --

    def _list_tenants(self) -> list[Tenant]:
        if self._tenants_provider is not None:
            tenants: Iterable[Tenant] = self._tenants_provider()
        else:
            from klonet_agent.memory.postgres import list_active_tenants

            tenants = list_active_tenants(self._database, limit=self._max_tenants * 4)
        # 去重 + 稳定排序：cursor 的正确性依赖顺序稳定。
        return normalise_tenants(tenants)[: self._max_tenants]

    def _repository_for(self, tenant: Tenant) -> Any:
        if self._repository_factory is not None:
            return self._repository_factory(tenant)
        from klonet_agent.memory.postgres import PostgresMemoryRepository

        return PostgresMemoryRepository(self._database, tenant)

    def _worker(self, repository: Any) -> EmbeddingWorker:
        if self._worker_factory is not None:
            return self._worker_factory(
                repository,
                self._embedder,
                batch_size=self._batch_size,
                max_attempts=self._max_attempts,
                lease_seconds=self._lease_seconds,
                profile_id=self._profile_id,
                model=self._embedding_model,
                model_version=self._embedding_version,
            )
        return EmbeddingWorker(
            repository,
            self._embedder,
            profile_id=self._profile_id,
            batch_size=self._batch_size,
            max_attempts=self._max_attempts,
            lease_seconds=self._lease_seconds,
            expected_dimensions=EMBEDDING_DIMENSIONS,
            model=self._embedding_model,
            model_version=self._embedding_version,
        )

    # ------------------------------------------------------------- 运行 --

    def run(self, context: JobContext, cursor: str | None) -> JobResult:
        if self._embedder is None:
            # 没有嵌入凭据：不是失败，是"这个部署没开语义通道"。返回 0
            # 计数而不是抛异常，让 Worker 保持健康、console 上看得见原因。
            return JobResult(details=("embedder_unavailable：未配置嵌入凭据，跳过",))

        tenants = self._list_tenants()
        if not tenants:
            return JobResult(details=("no_tenants：没有可调度的租户",))

        tenants = tenants_after_cursor(self._list_tenants(), cursor)
        if not tenants:
            # cursor 已经走到底：下一轮从头来。
            return JobResult(details=("cursor_exhausted：本轮无剩余租户",))

        budget_end = self._clock().timestamp() + self._max_seconds
        hard_deadline = context.deadline
        if hard_deadline is not None:
            budget_end = min(budget_end, hard_deadline.timestamp())
        batch_end = datetime.fromtimestamp(budget_end, tz=timezone.utc)

        scanned = changed = failed = 0
        skipped = 0
        notes: list[str] = []
        last_done: Tenant | None = None
        all_done = True
        aggregate = _StatsAccumulator()

        for index, tenant in enumerate(tenants):
            worker = self._worker(self._repository_for(tenant))
            if context.dry_run:
                stats = worker.stats()
                aggregate.add_snapshot(stats)
                scanned += int(stats.outstanding)
                last_done = tenant
                continue
            run_stats = worker.run(
                limit=context.batch_limit,
                deadline=batch_end,
                should_stop=self._should_stop,
            )
            scanned += run_stats.claimed
            changed += run_stats.embedded
            failed += run_stats.failed
            skipped += run_stats.skipped
            try:
                aggregate.add_snapshot(worker.stats())
            except Exception:  # noqa: BLE001 - 指标读不到不影响主流程
                pass
            if run_stats.claimed > 0:
                notes.append(
                    f"{_tenant_label(tenant)}: claimed={run_stats.claimed} "
                    f"embedded={run_stats.embedded} retried={run_stats.retried} "
                    f"abandoned={run_stats.abandoned} skipped={run_stats.skipped}"
                )
            if self._time_is_up(batch_end) and index < len(tenants) - 1:
                # 还有租户没轮上：cursor 停在刚刚跑完的这个。
                all_done = False
                last_done = tenant
                break
            last_done = tenant

        next_cursor = None if all_done else (
            encode_tenant_cursor(last_done) if last_done is not None else None
        )

        summary = (
            f"tenants={len(tenants)} scanned={scanned} changed={changed} "
            f"failed={failed} skipped={skipped} "
            f"coverage={aggregate.coverage():.4f}"
        )
        oldest = aggregate.oldest_pending_age_seconds(now=self._clock())
        if oldest is not None:
            summary += f" oldest_pending_seconds={oldest:.0f}"
        details = (summary, *notes[:8])
        return JobResult(
            scanned=scanned,
            changed=changed,
            proposed=0,
            failed=failed,
            next_cursor=next_cursor,
            details=tuple(details),
        )

    def _time_is_up(self, batch_end: datetime) -> bool:
        if self._should_stop is not None and self._should_stop():
            return True
        return self._clock() >= batch_end


def _tenant_label(tenant: Tenant) -> str:
    return f"{tenant.user_id}/{tenant.project_id or '-'}"


class _StatsAccumulator:
    """把逐租户的 ``EmbeddingOutboxStats`` 汇成一个总览。

    coverage 不能简单取平均：租户之间条数差异很大，平均会让"小租户都完成了、
    大租户全积压"看起来正常。这里按条数加权。
    """

    def __init__(self) -> None:
        self._pending = 0
        self._processing = 0
        self._completed = 0
        self._failed = 0
        self._oldest: datetime | None = None

    def add_snapshot(self, stats: EmbeddingOutboxStats) -> None:
        self._pending += int(stats.pending)
        self._processing += int(stats.processing)
        self._completed += int(stats.completed)
        self._failed += int(stats.failed)
        oldest = stats.oldest_pending_at
        if isinstance(oldest, datetime):
            if self._oldest is None or oldest < self._oldest:
                self._oldest = oldest

    def coverage(self) -> float:
        total = self._pending + self._processing + self._completed + self._failed
        if total == 0:
            return 1.0
        return self._completed / total

    def oldest_pending_age_seconds(self, *, now: datetime) -> float | None:
        if self._oldest is None:
            return None
        return max(0.0, (now - self._oldest).total_seconds())


def build_job(*, database: MemoryDatabase, config: MaintenanceConfig) -> EmbeddingOutboxJob | None:
    """CLI 用的工厂：按配置装配 Job。

    嵌入凭据缺失时返回 ``None``——让 ``_build_jobs`` 跳过注册。这比注册一个
    永远返回 "embedder_unavailable" 的 Job 更好：``status`` 里的 ``jobs``
    列表就能直接说明"这个部署没开语义通道"。
    """

    from klonet_agent.llm.embeddings import build_default_embedding_provider
    from klonet_agent.config import DEFAULT_EMBEDDING_MODEL

    embedder = build_default_embedding_provider()
    if embedder is None:
        return None
    return EmbeddingOutboxJob(
        database=database,
        embedder=embedder,
        batch_size=config.embedding_batch_size,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        embedding_version=DEFAULT_EMBEDDING_MODEL,
    )
