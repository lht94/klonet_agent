"""PurgeJob 测试（04 计划 §6.3 / 阶段 4）。

离线覆盖保留期策略与 dry-run 语义；真库覆盖"未到期一条都不删""到期才删"
"级联干净""删完不可召回""tombstone 先写""expired/superseded 不碰"。

跑法::

    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_maintenance_purge.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.admin import MemoryAdmin, PurgeReport, RetentionPolicy
from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    MemorySource,
    MemoryStatus,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
)
from klonet_agent.memory.maintenance.base import JobContext
from klonet_agent.memory.maintenance.jobs.purge import PurgeJob, retention_policy_from_env
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import NewRecordCommand

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


def _context(*, batch_limit: int = 50, dry_run: bool = False) -> JobContext:
    now = datetime.now(timezone.utc)
    return JobContext(
        job_name="purge",
        run_id=str(uuid4()),
        worker_id="test-worker",
        started_at=now,
        deadline=now + timedelta(seconds=60),
        batch_limit=batch_limit,
        dry_run=dry_run,
    )


class _RecordingTracer:
    """收集 ``record_privileged_event``，用于验证 tombstone 先写。"""

    def __init__(self):
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record_privileged_event(self, **kwargs: Any) -> None:
        self.events.append((kwargs.get("event", ""), kwargs.get("payload") or {}))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


# --------------------------------------------------------------------------- #
# 离线：保留期策略
# --------------------------------------------------------------------------- #


def test_job_name_is_canonical() -> None:
    assert PurgeJob.name == "purge"


def test_retention_default_is_thirty_days() -> None:
    policy = retention_policy_from_env({})
    assert policy.soft_delete_retention_days == 30


def test_retention_reads_env() -> None:
    policy = retention_policy_from_env({"KLONET_AGENT_MEMORY_RETENTION_DAYS": "7"})
    assert policy.soft_delete_retention_days == 7


def test_retention_invalid_value_falls_back_to_default() -> None:
    """取值非法退回缺省而不是拒绝启动：这个 Job 的价值是"保守地什么都不删"。"""

    policy = retention_policy_from_env({"KLONET_AGENT_MEMORY_RETENTION_DAYS": "abc"})
    assert policy.soft_delete_retention_days == 30


def test_compliance_floor_clamps_shorter_retention() -> None:
    """配了比合规下限还短的保留期时，按**下限**执行（本仓库唯一一处钳制）。"""

    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    policy = RetentionPolicy(soft_delete_retention_days=1, compliance_floor_days=7)
    assert policy.purge_cutoff(now=now) == now - timedelta(days=7)


def test_retention_never_negative() -> None:
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    policy = RetentionPolicy(soft_delete_retention_days=-5)
    assert policy.purge_cutoff(now=now) == now


def test_job_rejects_non_positive_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        PurgeJob(database=None, batch_size=0)  # type: ignore[arg-type]


def test_dry_run_does_not_delete() -> None:
    calls: list[dict[str, Any]] = []

    class _Admin:
        def purge_report(self, **kwargs: Any) -> PurgeReport:
            from klonet_agent.memory.repository import DeletedBacklog

            calls.append(kwargs)
            return PurgeReport(
                dry_run=True,
                removed=3,
                backlog=DeletedBacklog(count=3, estimated_bytes=120),
            )

    job = PurgeJob(
        database=None,  # type: ignore[arg-type]
        tenants_provider=lambda: [Tenant(user_id="alice", project_id="demo")],
        admin_factory=lambda tenant: _Admin(),  # type: ignore[arg-type,return-value]
    )
    result = job.run(_context(dry_run=True), None)
    assert calls and calls[0]["dry_run"] is True
    assert result.changed == 0, "dry-run 不该报告任何实际删除"
    assert result.scanned == 3
    assert any("dry_run would_remove=3" in line for line in result.details)


def test_config_batch_size_is_honoured() -> None:
    assert MaintenanceConfig(purge_batch_size=77).job_batch_size("purge") == 77


# --------------------------------------------------------------------------- #
# 真库
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过 PurgeJob 真库测试。")
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过 PurgeJob 真库测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:
        pytest.fail(f"{TEST_DSN_ENV} 已设置但连不上：{exc}")
    return dsn


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    return _admin_dsn_or_skip()


@pytest.fixture(scope="module")
def db(admin_dsn: str):
    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=6)
        database.open()
        try:
            database.run_migrations()
            yield database
        finally:
            database.close()


def _tenant() -> Tenant:
    return Tenant(user_id=f"purge_{uuid4().hex[:8]}", project_id="demo")


def _repo(database: MemoryDatabase, tenant: Tenant) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _add(repo: PostgresMemoryRepository, tenant: Tenant, content: str, **kwargs: Any):
    return repo.add_record(
        NewRecordCommand(
            user_id=tenant.user_id,
            project_id=tenant.project_id,
            memory_type=MemoryType.FACT,
            scope=Scope.PROJECT,
            subject_key=f"fact:project:purge:{uuid4().hex[:8]}",
            content=content,
            sources=(
                MemorySource(
                    source_type=SourceType.USER_STATEMENT,
                    source_id=f"rows-{uuid4().hex[:8]}",
                    observed_at=datetime.now(timezone.utc),
                    source_excerpt="测试证据",
                ),
            ),
            **kwargs,
        )
    )


def _age_deleted(db: MemoryDatabase, memory_id: str, *, days: int) -> None:
    """把 ``updated_at`` 往前拨，模拟"已经过了保留期"。"""

    with db.diagnostic_session() as conn:
        conn.execute(
            "UPDATE memory_records SET updated_at = now() - (%s || ' days')::interval "
            " WHERE id = %s",
            (str(int(days)), UUID(memory_id)),
        )


def _exists(db: MemoryDatabase, memory_id: str) -> bool:
    with db.diagnostic_session() as conn:
        row = conn.execute(
            "SELECT 1 AS ok FROM memory_records WHERE id = %s", (UUID(memory_id),)
        ).fetchone()
    return row is not None


def _version_rows(db: MemoryDatabase, memory_id: str) -> int:
    with db.diagnostic_session() as conn:
        return int(
            conn.execute(
                "SELECT count(*) AS n FROM memory_versions WHERE memory_id = %s",
                (UUID(memory_id),),
            ).fetchone()["n"]
        )


def _safe(text: str) -> str:
    """避免 Windows 控制台的 GBK 编码问题（保持纯 ASCII）。"""

    return "".join(ch if ord(ch) < 128 else "_" for ch in text)


def test_within_retention_is_never_purged(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    record = _add(repo, tenant, "还在保留期内的删除记录")
    repo.delete_memory(str(record.id))
    _age_deleted(db, str(record.id), days=1)

    removed = MemoryAdmin(repo).purge(
        policy=RetentionPolicy(soft_delete_retention_days=30)
    )
    assert removed == 0, "未到保留期一条都不能删"
    assert _exists(db, str(record.id)) is True


def test_expired_and_superseded_are_not_purged(db: MemoryDatabase) -> None:
    """purge 只处理显式 deleted；expired / superseded 必须原地不动。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    base = datetime.now(timezone.utc)

    expired_record = _add(
        repo, tenant, "过期记录不应被 purge", valid_from=base - timedelta(days=2)
    )
    with db.diagnostic_session() as conn:
        # valid_to 必须严格晚于 valid_from（CHECK 会拦），所以先把种子记录的
        # valid_from 拉早，再写一个仍然早于 now 的 valid_to。
        conn.execute(
            "UPDATE memory_versions SET valid_to = %s WHERE memory_id = %s",
            (base - timedelta(hours=1), UUID(str(expired_record.id))),
        )
    repo.mark_expired(str(expired_record.id), base)

    superseded_record = _add(repo, tenant, "被替代的记录不应被 purge")
    from klonet_agent.memory.repository import NewVersionCommand

    repo.add_version(
        NewVersionCommand(
            memory_id=str(superseded_record.id),
            content="替代后的正文",
            sources=(
                MemorySource(
                    source_type=SourceType.USER_STATEMENT,
                    source_id=f"rows-{uuid4().hex[:8]}",
                    observed_at=base,
                    source_excerpt="测试证据",
                ),
            ),
        )
    )
    _age_deleted(db, str(expired_record.id), days=90)
    _age_deleted(db, str(superseded_record.id), days=90)

    removed = MemoryAdmin(repo).purge(
        policy=RetentionPolicy(soft_delete_retention_days=30)
    )
    assert removed == 0
    assert _exists(db, str(expired_record.id)) is True
    assert _exists(db, str(superseded_record.id)) is True


def test_beyond_retention_is_purged_and_leaves_no_trace(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    record = _add(repo, tenant, "Python 部署端口 中文 待清理")
    version_id = str(record.active_version_id)
    repo.delete_memory(str(record.id))
    _age_deleted(db, str(record.id), days=90)

    # 删除时 outbox 与向量就该清了；这里确认一下前置状态。
    assert MemoryAdmin(repo).recall_check(str(record.id), text="Python 部署端口") is False

    removed = MemoryAdmin(repo).purge(
        policy=RetentionPolicy(soft_delete_retention_days=30)
    )
    assert removed == 1
    assert _exists(db, str(record.id)) is False, "记录必须被物理删除"
    assert _version_rows(db, str(record.id)) == 0, "版本必须随外键级联删除"
    with db.diagnostic_session() as conn:
        orphan_sources = conn.execute(
            "SELECT count(*) AS n FROM memory_sources WHERE memory_version_id = %s",
            (UUID(version_id),),
        ).fetchone()["n"]
    assert int(orphan_sources) == 0, "不能留下孤立来源"
    assert MemoryAdmin(repo).recall_check(str(record.id), text="Python 部署端口") is False


def test_dry_run_reports_without_deleting(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    record = _add(repo, tenant, "dry-run 目标")
    repo.delete_memory(str(record.id))
    _age_deleted(db, str(record.id), days=90)

    report = MemoryAdmin(repo).purge_report(
        policy=RetentionPolicy(soft_delete_retention_days=30), dry_run=True
    )
    assert report.dry_run is True
    assert report.removed == 1
    assert report.backlog is not None
    assert report.backlog.oldest_deleted_at is not None
    assert report.backlog.estimated_bytes > 0
    assert report.backlog.memory_ids == (str(record.id),)
    assert _exists(db, str(record.id)) is True, "dry-run 绝不能真删"


def test_tombstone_is_written_before_delete(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    record = _add(repo, tenant, "tombstone 目标")
    repo.delete_memory(str(record.id))
    _age_deleted(db, str(record.id), days=90)

    tracer = _RecordingTracer()
    admin = MemoryAdmin(repo, tracer=tracer)
    admin.purge(policy=RetentionPolicy(soft_delete_retention_days=30))

    names = tracer.names()
    assert "memory_purge_tombstone" in names
    assert "memory_purged" in names
    assert names.index("memory_purge_tombstone") < names.index("memory_purged")
    # tombstone 绝不含正文。
    payload = dict(tracer.events)[  # type: ignore[arg-type]
        "memory_purge_tombstone"
    ]
    assert "tombstone 目标" not in str(payload)


def test_multi_batch_loop_clears_backlog(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    for index in range(5):
        record = _add(repo, tenant, f"多批次清理 {index}")
        repo.delete_memory(str(record.id))
        _age_deleted(db, str(record.id), days=90)

    report = MemoryAdmin(repo).purge_report(
        policy=RetentionPolicy(soft_delete_retention_days=30),
        batch_size=2,
    )
    assert report.removed == 5
    assert report.batches >= 3, "单批 2 条、共 5 条，至少要跑三批"
    assert MemoryAdmin(repo).purge_report(
        policy=RetentionPolicy(soft_delete_retention_days=30)
    ).removed == 0


def test_purge_job_dry_run_then_real(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    record = _add(repo, tenant, _safe("purge job 目标"))
    repo.delete_memory(str(record.id))
    _age_deleted(db, str(record.id), days=90)

    job = PurgeJob(
        database=db,
        tenants_provider=lambda: [tenant],
        policy=RetentionPolicy(soft_delete_retention_days=30),
        batch_size=50,
    )

    dry = job.run(_context(dry_run=True), None)
    assert dry.changed == 0
    assert _exists(db, str(record.id)) is True
    assert any("dry_run" in line for line in dry.details)

    real = job.run(_context(), None)
    assert real.changed == 1
    assert _exists(db, str(record.id)) is False


def test_purge_job_isolated_across_tenants(db: MemoryDatabase) -> None:
    alice = _tenant()
    bob = _tenant()
    alice_repo = _repo(db, alice)
    bob_repo = _repo(db, bob)
    alice_record = _add(alice_repo, alice, "alice 待清理")
    bob_record = _add(bob_repo, bob, "bob 待清理")
    for tenant_repo, record in ((alice_repo, alice_record), (bob_repo, bob_record)):
        tenant_repo.delete_memory(str(record.id))
        _age_deleted(db, str(record.id), days=90)

    job = PurgeJob(
        database=db,
        tenants_provider=lambda: [alice],
        policy=RetentionPolicy(soft_delete_retention_days=30),
        batch_size=50,
    )
    result = job.run(_context(), None)
    assert result.changed == 1
    assert _exists(db, str(alice_record.id)) is False
    assert _exists(db, str(bob_record.id)) is True
