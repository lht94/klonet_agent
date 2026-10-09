"""ReembeddingJob —— 按 profile 回填向量（04 计划 §6.5 / 阶段 6）。

与 :class:`EmbeddingOutboxJob` 的分工：

- **EmbeddingOutboxJob** 是常驻清理：消费 default profile 的 outbox 积压，
  永远不知道"迁移"的存在。
- **ReembeddingJob** 只在有**显式创建的迁移**（账本 status='backfilling'）
  时工作：为 target profile 补种 outbox 行、驱动租户级回填、推进 cursor；
  outbox 清空后自动把迁移推进到 ``validating``。之后是否 promote 由
  ``EmbeddingMigrationManager.validate / promote`` 的门禁**显式**决定——
  Job 绝不自动切换 active profile。

回填复用 :class:`EmbeddingWorker`（租约 / SKIP LOCKED / 退避 / 永久失败
判定全是 02 阶段 4 验证过的语义），只覆盖写回钩子：target profile 的
向量落 ``public.memory_embeddings``（多 profile 表），不碰 default 的单列。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.domain import Tenant
from klonet_agent.memory.embedding_worker import EmbeddingWorker
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.reembedding import (
    EmbeddingMigrationManager,
    EmbeddingMigrationStateError,
)
from klonet_agent.memory.maintenance.tenants import (
    encode_tenant_cursor,
    normalise_tenants,
    tenant_key,
    tenants_after_cursor,
)

__all__ = [
    "ReembeddingJob",
    "ReembeddingWorker",
    "build_job",
]


class ReembeddingWorker(EmbeddingWorker):
    """:class:`EmbeddingWorker` 的 target-profile 变体：只换写回目标。"""

    def _write_back(self, item: Any, vector: tuple[float, ...]) -> bool:
        return self._repository.set_profile_embedding(
            item.version_id,
            vector,
            embedding_profile_id=self._profile_id,
            embedding_model=self._model,
            embedding_version=self._model_version,
        )


class ReembeddingJob:
    """按租户回填 target profile 的向量；实现 ``MaintenanceJob``。"""

    name = "reembedding"

    def __init__(
        self,
        *,
        database: MemoryDatabase,
        embedder: Any,
        embedding_model: str,
        embedding_version: str,
        batch_size: int = 50,
        max_tenants: int = 50,
        tenants_provider: Callable[[], Sequence[Tenant]] | None = None,
        manager: EmbeddingMigrationManager | None = None,
        worker_factory: Callable[..., EmbeddingWorker] | None = None,
        should_stop: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        if max_tenants <= 0:
            raise ValueError("max_tenants 必须为正整数")
        self._database = database
        self._embedder = embedder
        self._model = embedding_model
        self._model_version = embedding_version
        self._batch_size = int(batch_size)
        self._max_tenants = int(max_tenants)
        self._tenants_provider = tenants_provider
        self._manager = manager or EmbeddingMigrationManager(database)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._should_stop = should_stop
        self._worker_factory = worker_factory or self._default_worker

    # ------------------------------------------------- MaintenanceJob ----

    def run(self, context: JobContext, cursor: str | None) -> JobResult:
        migration = self._manager.get_live()
        if migration is None or migration.status != "backfilling":
            # 没有显式迁移（或不在回填态）就没事可做——"只显式迁移才启用"。
            return JobResult(details=("no_active_migration：无回填中的迁移",))

        target_profile = migration.target_profile_id
        batch_end = self._batch_end(context)
        stopped_early = False

        # 1) 补种：迁移开始后新写入的 active 版本也要进队列（幂等）。
        try:
            self._manager.seed_backfill(migration.migration_id)
        except Exception:  # noqa: BLE001 - 补种失败不阻塞本轮消费
            pass

        # 2) 按租户消费 target profile 的 outbox（cursor 断点续跑）。
        tenants = normalise_tenants(self._list_tenants())
        if cursor:
            start_key = _decode_cursor(cursor)
            if start_key is not None:
                tenants = [t for t in tenants if tenant_key(t) > start_key]
        tenants = tenants[: self._max_tenants]
        if not tenants:
            return JobResult(
                next_cursor=cursor,
                details=("cursor_exhausted：本轮无剩余租户",),
            )

        scanned = changed = failed = skipped = 0
        last_tenant: str | None = None
        for tenant in tenants:
            if self._clock() >= batch_end or self._stopped():
                stopped_early = True
                break
            worker = self._worker_factory(tenant=tenant, profile_id=target_profile)
            stats = worker.run(
                limit=context.batch_limit,
                deadline=batch_end,
                should_stop=self._stopped,
            )
            scanned += stats.claimed
            changed += stats.embedded
            failed += stats.failed
            skipped += stats.skipped
            last_tenant = encode_tenant_cursor(tenant)

        next_cursor = last_tenant if stopped_early else None
        self._save_progress(
            migration.migration_id, next_cursor, target_profile, scanned, changed, failed
        )

        # 3) 跑完且队列清空 → validating；promote 仍由门禁显式决定。
        completed, eligible = self._manager.backfill_progress(migration.migration_id)
        advanced = False
        if not stopped_early and eligible > 0 and completed >= eligible:
            try:
                self._manager.mark_validating(migration.migration_id)
                advanced = True
            except EmbeddingMigrationStateError:
                pass  # 并发下已被推进；账本状态以 DB 为准

        coverage = (completed / eligible) if eligible else 1.0
        summary = (
            f"migration={migration.migration_id} scanned={scanned} changed={changed} "
            f"failed={failed} skipped={skipped} coverage={coverage:.3f}"
            + (" → validating" if advanced else "")
        )
        return JobResult(
            scanned=scanned,
            changed=changed,
            proposed=0,
            failed=failed,
            next_cursor=next_cursor,
            details=(summary,),
        )

    # ------------------------------------------------------------ 内部 ----

    def _stopped(self) -> bool:
        return self._should_stop is not None and self._should_stop()

    def _batch_end(self, context: JobContext) -> datetime:
        """单次执行预算：宁早勿晚交回租约（留 20% 余量，上限 10 分钟）。"""

        remaining = (context.deadline - self._clock()).total_seconds()
        budget = min(max(1.0, remaining * 0.8), 600.0)
        return self._clock() + budget

    def _list_tenants(self) -> list[Tenant]:
        if self._tenants_provider is not None:
            return list(self._tenants_provider())
        from klonet_agent.memory.postgres import list_active_tenants

        return list(list_active_tenants(self._database, limit=self._max_tenants * 4))

    def _default_worker(self, *, tenant: Tenant, profile_id: str) -> EmbeddingWorker:
        from klonet_agent.memory.postgres import PostgresMemoryRepository

        repository = PostgresMemoryRepository(self._database, tenant)
        return ReembeddingWorker(
            repository,
            self._embedder,
            profile_id=profile_id,
            batch_size=self._batch_size,
            model=self._model,
            model_version=self._model_version,
        )

    def _save_progress(
        self,
        migration_id: str,
        cursor: str | None,
        profile_id: str,
        scanned: int,
        changed: int,
        failed: int,
    ) -> None:
        """cursor 形态 §6.5：``(profile_id, 断点)``；断点 = 最后完整跑完的租户，
        全部跑完写 null（下一轮从头重扫很廉价，且不会漏排在前面的租户）。"""

        import json

        from klonet_agent.memory.maintenance.reembedding import _json

        cursor_payload = {"profile_id": profile_id, "cursor": cursor}
        run_payload = {
            "last_run": {
                "scanned": scanned,
                "changed": changed,
                "failed": failed,
                "at": self._clock().isoformat(),
            }
        }
        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET cursor = %s::jsonb,
                           stats = stats || %s::jsonb
                     WHERE migration_id = %s
                    """,
                    [json.dumps(cursor_payload), _json(run_payload), migration_id],
                )


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    """迁移 cursor JSON ``{"profile_id":..., "cursor": "<tenant json>|null"}``
    → 排序键；解析不认识一律 None（从头重扫，绝不猜位置跳过租户）。"""

    import json

    if not cursor:
        return None
    try:
        payload = json.loads(cursor)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    inner = payload.get("cursor")
    if not inner:
        return None
    from klonet_agent.memory.maintenance.tenants import decode_tenant_cursor

    return decode_tenant_cursor(str(inner))


def build_job(
    *, database: MemoryDatabase, config: MaintenanceConfig
) -> ReembeddingJob | None:
    """CLI 工厂。无嵌入凭据返回 ``None``（与 EmbeddingOutboxJob 同一约定）。

    Reembedding 只在显式迁移时才有工作量，但注册本身不受限——``status``
    里能看到它，账本建好迁移后 ``run`` 自然开始干活。
    """

    from klonet_agent.llm.embeddings import build_default_embedding_provider
    from klonet_agent.config import DEFAULT_EMBEDDING_MODEL

    embedder = build_default_embedding_provider()
    if embedder is None:
        return None
    return ReembeddingJob(
        database=database,
        embedder=embedder,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        embedding_version=DEFAULT_EMBEDDING_MODEL,
        batch_size=config.embedding_batch_size,
    )
