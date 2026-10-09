"""ReembeddingJob 与 embedding 迁移状态机测试（04 计划 §6.5 / 阶段 6）。

离线部分覆盖纯函数与契约（cursor 解码、数据类、Job 配置校验、
无迁移短路）；真库部分覆盖 0008 状态机触发器、显式生命周期、
outbox 播种排除条件、双 profile 写回与检索路由、门禁与原子切换。

跑法::

    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_reembedding.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    MemorySource,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
)
from klonet_agent.memory.maintenance.base import JobContext
from klonet_agent.memory.maintenance.jobs.reembedding import (
    ReembeddingJob,
    ReembeddingWorker,
    _decode_cursor,
    build_job,
)
from klonet_agent.memory.maintenance.reembedding import (
    DEFAULT_MIN_COVERAGE,
    DEFAULT_MIN_RECALL,
    EmbeddingMigration,
    EmbeddingMigrationError,
    EmbeddingMigrationGateError,
    EmbeddingMigrationManager,
    EmbeddingMigrationStateError,
    ValidationReport,
)
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import NewRecordCommand

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


# --------------------------------------------------------------------------- #
# 离线：纯函数与契约
# --------------------------------------------------------------------------- #


def _context(*, batch_limit: int = 50) -> JobContext:
    now = datetime.now(timezone.utc)
    return JobContext(
        job_name="reembedding",
        run_id=str(uuid4()),
        worker_id="test-worker",
        started_at=now,
        deadline=now + timedelta(seconds=60),
        batch_limit=batch_limit,
    )


def test_decode_cursor_valid_and_garbage() -> None:
    import json

    cursor = json.dumps({"profile_id": "p2", "cursor": '["u1", "p1"]'})
    assert _decode_cursor(cursor) == ("u1", "p1")
    assert _decode_cursor(json.dumps({"profile_id": "p2", "cursor": None})) is None
    # 解析不认识一律 None（从头重扫），绝不"猜位置"跳过租户。
    assert _decode_cursor("not-json") is None
    assert _decode_cursor(None) is None
    assert _decode_cursor(json.dumps(["wrong", "shape"])) is None


def test_migration_live_property() -> None:
    def _migration(status: str) -> EmbeddingMigration:
        return EmbeddingMigration(
            migration_id="m1",
            status=status,
            source_profile_id="default",
            target_profile_id="p2",
            target_model="m",
            target_model_version="1",
            target_dimensions=1024,
            target_table="memory_embeddings",
        )

    assert _migration("backfilling").live
    assert _migration("ready").live
    assert not _migration("retired").live
    assert not _migration("cancelled").live


def test_validation_report_to_json_roundtrip() -> None:
    report = ValidationReport(
        migration_id="m1",
        coverage=0.995,
        eligible=200,
        completed=199,
        recall_overlap=0.83,
        probes=5,
        k=10,
        latency_ms={"source": 12.345, "target": 13.456},
        min_coverage=DEFAULT_MIN_COVERAGE,
        min_recall=DEFAULT_MIN_RECALL,
        passed=True,
    )
    payload = report.to_json()
    assert payload["coverage"] == 0.995
    assert payload["latency_ms"] == {"source": 12.35, "target": 13.46}
    assert payload["passed"] is True


def test_job_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        ReembeddingJob(
            database=None,
            embedder=lambda text: (1.0,),
            embedding_model="m",
            embedding_version="1",
            batch_size=0,
        )
    with pytest.raises(ValueError):
        ReembeddingJob(
            database=None,
            embedder=lambda text: (1.0,),
            embedding_model="m",
            embedding_version="1",
            max_tenants=0,
        )


def test_build_job_returns_none_without_credentials(monkeypatch) -> None:
    import klonet_agent.llm.embeddings as embeddings

    monkeypatch.setattr(embeddings, "build_default_embedding_provider", lambda: None)
    from klonet_agent.config import MaintenanceConfig

    assert build_job(database=None, config=MaintenanceConfig()) is None


def test_job_short_circuits_without_migration() -> None:
    class _StubManager:
        def get_live(self):
            return None

    job = ReembeddingJob(
        database=None,
        embedder=lambda text: (1.0,),
        embedding_model="m",
        embedding_version="1",
        manager=_StubManager(),
    )
    result = job.run(_context(), None)
    assert result.scanned == 0
    assert any("no_active_migration" in line for line in result.details)


def test_worker_is_embedding_worker_with_profile_writeback() -> None:
    from klonet_agent.memory.embedding_worker import EmbeddingWorker

    assert issubclass(ReembeddingWorker, EmbeddingWorker)
    # 覆盖点必须是写回钩子：基类写 default 单列，子类写多 profile 表。
    assert ReembeddingWorker._write_back is not EmbeddingWorker._write_back


# --------------------------------------------------------------------------- #
# 真库部分
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过 reembedding 真库测试。")
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过 reembedding 真库测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:
        pytest.fail(f"{TEST_DSN_ENV} 已设置但连不上：{exc}")
    return dsn


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    return _admin_dsn_or_skip()


#: 当前模块临时库的 DSN（``db`` fixture 写入；离线时为空）。
#: 清场 fixture **不能**直接依赖 ``db``——autouse 会把 skip 传染给全部
#: 离线用例（真踩过：18 项全 skip）。
_CURRENT_DSN: list[str] = []


@pytest.fixture(scope="module")
def db(admin_dsn: str):
    with temporary_database(admin_dsn) as dsn:
        _CURRENT_DSN.clear()
        _CURRENT_DSN.append(dsn)
        database = MemoryDatabase(dsn, min_size=1, max_size=6)
        database.open()
        try:
            database.run_migrations()
            yield database
        finally:
            database.close()
        _CURRENT_DSN.clear()


@pytest.fixture(autouse=True)
def _no_leftover_live_migrations():
    """任何一条测试失败都不能把活迁移留给后面的用例。

    账本的 partial unique index 同时只允许一个非终态迁移——这是产品约束，
    代价是测试之间的级联：第一个失败会挡住后面所有 create。这里在每个
    用例结束后强制清场（retired 的不动，只清活的）。
    """

    yield
    if not _CURRENT_DSN:
        return
    database = MemoryDatabase(_CURRENT_DSN[0], min_size=1, max_size=2)
    database.open()
    try:
        manager = EmbeddingMigrationManager(database)
        for migration in manager.list():
            if migration.live:
                try:
                    manager.cancel(migration.migration_id)
                except Exception:  # noqa: BLE001 - 清场失败让下个用例暴露
                    pass
    finally:
        database.close()


def _tenant() -> Tenant:
    return Tenant(user_id=f"reemb_{uuid4().hex[:8]}", project_id="demo")


def _repo(database: MemoryDatabase, tenant: Tenant) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _add(
    repo: PostgresMemoryRepository,
    tenant: Tenant,
    *,
    subject: str,
    content: str,
    confidence: float = 0.6,
):
    return repo.add_record(
        NewRecordCommand(
            user_id=tenant.user_id,
            project_id=tenant.project_id,
            memory_type=MemoryType.FACT,
            scope=Scope.PROJECT,
            subject_key=subject,
            content=content,
            confidence=confidence,
            sources=(
                MemorySource(
                    source_type=SourceType.USER_STATEMENT,
                    source_id=f"rows-{uuid4().hex[:8]}",
                    observed_at=datetime.now(timezone.utc),
                    source_excerpt="测试证据",
                ),
            ),
        )
    )


_UNIT = tuple([1.0] + [0.0] * 1023)
_ORTH = tuple([0.0, 1.0] + [0.0] * 1022)


def _fake_embedder(content: str) -> tuple[float, ...]:
    # 正文里带 [A] 的给 _UNIT，[B] 的给 _ORTH——让两条向量可分。
    return _ORTH if "[B]" in content else _UNIT


def _job(database: MemoryDatabase, **overrides: Any) -> ReembeddingJob:
    params: dict[str, Any] = {
        "database": database,
        "embedder": _fake_embedder,
        "embedding_model": "test-model",
        "embedding_version": "1",
        "batch_size": 10,
    }
    params.update(overrides)
    return ReembeddingJob(**params)


# --------------------------------------------------------------------------- #
# 0008 落地与状态机
# --------------------------------------------------------------------------- #


def test_migration_tables_and_singleton_seeded(db: MemoryDatabase) -> None:
    with db.diagnostic_session() as conn:
        tables = {
            row["relname"]
            for row in conn.execute(
                "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'memory_maintenance' AND c.relkind = 'r'"
            ).fetchall()
        }
        assert "embedding_migrations" in tables
        assert "embedding_active_profile" in tables
        row = conn.execute(
            "SELECT profile_id, model, dimensions FROM memory_maintenance.embedding_active_profile WHERE id = 1"
        ).fetchone()
        assert (row["profile_id"], row["dimensions"]) == ("default", 1024)


def test_illegal_state_transition_rejected_by_trigger(db: MemoryDatabase) -> None:
    import psycopg

    # 触发器用 ERRCODE '2F002'（与 0003/0007 同一语义）；psycopg 按 SQLSTATE
    # 映射异常类（ModifyingSqlDataNotPermitted），所以按码查而不是猜类名。
    guard_error = psycopg.errors.lookup("2F002")

    manager = EmbeddingMigrationManager(db)
    manager.create(
        migration_id=f"m-{uuid4().hex[:8]}",
        target_profile_id=f"p-{uuid4().hex[:6]}",
        target_model="m",
        target_model_version="1",
    )
    live = manager.get_live()
    assert live is not None
    with pytest.raises(guard_error):
        with db.diagnostic_session() as conn:
            conn.execute(
                "UPDATE memory_maintenance.embedding_migrations "
                "   SET status = 'retired' WHERE migration_id = %s",
                (live.migration_id,),
            )
    manager.cancel(live.migration_id)


def test_create_rejects_same_profile_and_missing_table(db: MemoryDatabase) -> None:
    manager = EmbeddingMigrationManager(db)
    with pytest.raises(EmbeddingMigrationError):
        manager.create(
            migration_id=f"m-{uuid4().hex[:8]}",
            target_profile_id="default",
            target_model="m",
            target_model_version="1",
        )
    with pytest.raises(EmbeddingMigrationError):
        manager.create(
            migration_id=f"m-{uuid4().hex[:8]}",
            target_profile_id="p-x",
            target_model="m",
            target_model_version="1",
            target_table="no_such_table",
        )
    with pytest.raises(EmbeddingMigrationError):
        manager.create(
            migration_id=f"m-{uuid4().hex[:8]}",
            target_profile_id="p-x",
            target_model="m",
            target_model_version="1",
            target_dimensions=1536,
            target_table="memory_embeddings",
        )


def test_create_rejects_second_live_migration(db: MemoryDatabase) -> None:
    manager = EmbeddingMigrationManager(db)
    first = manager.create(
        migration_id=f"m-{uuid4().hex[:8]}",
        target_profile_id=f"p-{uuid4().hex[:6]}",
        target_model="m",
        target_model_version="1",
    )
    with pytest.raises(EmbeddingMigrationStateError):
        manager.create(
            migration_id=f"m-{uuid4().hex[:8]}",
            target_profile_id=f"q-{uuid4().hex[:6]}",
            target_model="m",
            target_model_version="1",
        )
    manager.cancel(first.migration_id)


# --------------------------------------------------------------------------- #
# 生命周期 + outbox 播种
# --------------------------------------------------------------------------- #


def _seed_two_records(database: MemoryDatabase, tenant: Tenant):
    repo = _repo(database, tenant)
    a = _add(repo, tenant, subject=f"fact:project:reemb:{uuid4().hex[:8]}", content="后端使用 [A] Python 3.11")
    b = _add(repo, tenant, subject=f"fact:project:reemb:{uuid4().hex[:8]}", content="后端使用 [B] React 18")
    return repo, a, b


def test_start_seeds_outbox_excluding_deleted_and_expired(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, a, b = _seed_two_records(db, tenant)
    doomed = _add(repo, tenant, subject=f"fact:project:reemb:{uuid4().hex[:8]}", content="将被删除 [A]")
    repo.delete_memory(str(doomed.id))
    # 过期（valid_to 已封口但记录仍 active）的版本也不该入队。
    expired = _add(repo, tenant, subject=f"fact:project:reemb:{uuid4().hex[:8]}", content="已失效 [A]")
    with db.diagnostic_session() as conn:
        conn.execute(
            "UPDATE memory_versions SET valid_to = now() WHERE memory_id = %s",
            (expired.id,),
        )

    manager = EmbeddingMigrationManager(db)
    target = f"p-{uuid4().hex[:6]}"
    migration = manager.create(
        migration_id=f"m-{uuid4().hex[:8]}",
        target_profile_id=target,
        target_model="test-model",
        target_model_version="1",
    )
    started = manager.start(migration.migration_id)
    assert started.status == "backfilling"

    with db.diagnostic_session() as conn:
        queued = conn.execute(
            "SELECT count(*) AS n FROM memory_embedding_outbox WHERE embedding_profile_id = %s",
            (target,),
        ).fetchone()["n"]
    assert queued == 2  # deleted 与 valid_to 封口的都被排除

    with db.diagnostic_session() as conn:
        rows = conn.execute(
            "SELECT r.status AS s FROM memory_records r WHERE r.id = %s",
            (expired.id,),
        ).fetchall()
    assert rows and rows[0]["s"] == "active"  # 只封版本，记录仍 active

    manager.cancel(migration.migration_id)


def test_pause_requires_backfilling(db: MemoryDatabase) -> None:
    manager = EmbeddingMigrationManager(db)
    migration = manager.create(
        migration_id=f"m-{uuid4().hex[:8]}",
        target_profile_id=f"p-{uuid4().hex[:6]}",
        target_model="m",
        target_model_version="1",
    )
    with pytest.raises(EmbeddingMigrationStateError):
        manager.pause(migration.migration_id)  # planned → paused 非法
    started = manager.start(migration.migration_id)
    assert started.status == "backfilling"
    paused = manager.pause(migration.migration_id)
    assert paused.status == "paused"
    resumed = manager.resume(migration.migration_id)
    assert resumed.status == "backfilling"
    cancelled = manager.cancel(migration.migration_id)
    assert cancelled.status == "cancelled"
    # cancel 撤掉未完成的 outbox 行。
    with db.diagnostic_session() as conn:
        queued = conn.execute(
            "SELECT count(*) AS n FROM memory_embedding_outbox "
            " WHERE embedding_profile_id = %s AND status IN ('pending','processing')",
            (migration.target_profile_id,),
        ).fetchone()["n"]
    assert queued == 0
    with pytest.raises(EmbeddingMigrationStateError):
        manager.cancel(migration.migration_id)  # 终态不能再操作


# --------------------------------------------------------------------------- #
# 回填 + 双 profile 检索
# --------------------------------------------------------------------------- #


def test_backfill_writes_profile_table_and_search_routes(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, a, b = _seed_two_records(db, tenant)
    manager = EmbeddingMigrationManager(db)
    target = f"p-{uuid4().hex[:6]}"
    migration = manager.create(
        migration_id=f"m-{uuid4().hex[:8]}",
        target_profile_id=target,
        target_model="test-model",
        target_model_version="1",
    )
    started = manager.start(migration.migration_id)
    assert started.status == "backfilling"
    started = manager.start(migration.migration_id)
    assert started.status == "backfilling"
    job = _job(db, manager=manager)
    result = job.run(_context(), None)
    # coverage/eligible 是**全库全局**的（迁移语义如此），module 级共享库里
    # 先前用例的租户也会被播种+回填，所以断言"至少包含本租户的 2 条"。
    assert result.changed >= 2
    assert result.failed == 0

    # 向量落在多 profile 表，default 单列不被触碰。
    with db.diagnostic_session() as conn:
        profile_rows = conn.execute(
            "SELECT embedding_model FROM memory_embeddings "
            " WHERE embedding_profile_id = %s AND memory_version_id = ANY(%s)",
            (target, [a.active_version.id, b.active_version.id]),
        ).fetchall()
        legacy = conn.execute(
            "SELECT embedding IS NULL AS empty FROM memory_versions WHERE id = ANY(%s)",
            ([a.active_version.id, b.active_version.id],),
        ).fetchall()
    assert len(profile_rows) == 2
    assert all(row["embedding_model"] == "test-model" for row in profile_rows)
    assert all(row["empty"] for row in legacy)  # default 单列仍是空

    # 迁移已被 Job 自动推进到 validating（队列清空）。
    assert manager.get(migration.migration_id).status == "validating"

    # 双 profile 检索路由：target profile 走语义通道命中；default 无向量退化全文。
    query = SimpleNamespace(
        text="Python", limit=5, scopes=None, memory_types=None,
        min_confidence=0.0, include_shared_ops=False, as_of=None,
    )
    target_hits = repo.search(query, query_embedding=_UNIT, embedding_profile_id=target)
    assert target_hits, "target profile 必须能语义召回"
    assert any(str(hit.record.id) == str(a.id) for hit in target_hits)
    default_hits = repo.search(query, query_embedding=_UNIT)
    # default 单列从未被写：语义通道必须为空（只可能全文命中）。
    assert all(hit.semantic_rank is None for hit in default_hits)

    manager.cancel(migration.migration_id)  # validating 可取消，清干净现场


def test_set_profile_embedding_contract(db: MemoryDatabase) -> None:
    from klonet_agent.memory.domain import MemoryDomainError
    from klonet_agent.memory.repository import RecordNotFoundError

    tenant = _tenant()
    repo, a, _ = _seed_two_records(db, tenant)
    vid = str(a.active_version.id)

    with pytest.raises(MemoryDomainError):
        repo.set_profile_embedding(vid, _UNIT, embedding_profile_id="default",
                                   embedding_model="m", embedding_version="1")
    with pytest.raises(RecordNotFoundError):
        repo.set_profile_embedding(str(uuid4()), _UNIT, embedding_profile_id="p-z",
                                   embedding_model="m", embedding_version="1")

    assert repo.set_profile_embedding(vid, _UNIT, embedding_profile_id="p-z",
                                      embedding_model="m", embedding_version="1") is True
    # 幂等 upsert（同模型覆盖）。
    assert repo.set_profile_embedding(vid, _UNIT, embedding_profile_id="p-z",
                                      embedding_model="m", embedding_version="1") is True

    # 删除 → 清理并跳过（False），向量一并消失。
    repo.delete_memory(str(a.id))
    assert repo.set_profile_embedding(vid, _UNIT, embedding_profile_id="p-z",
                                      embedding_model="m", embedding_version="1") is False
    with db.diagnostic_session() as conn:
        left = conn.execute(
            "SELECT count(*) AS n FROM memory_embeddings WHERE memory_version_id = %s",
            (vid,),
        ).fetchone()["n"]
    assert left == 0


# --------------------------------------------------------------------------- #
# 门禁与原子切换
# --------------------------------------------------------------------------- #


def _hit(memory_id: str) -> SimpleNamespace:
    return SimpleNamespace(record=SimpleNamespace(id=memory_id))


def test_validate_gate_and_atomic_promote_rollback(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, a, b = _seed_two_records(db, tenant)
    manager = EmbeddingMigrationManager(db)
    target = f"p-{uuid4().hex[:6]}"
    migration = manager.create(
        migration_id=f"m-{uuid4().hex[:8]}",
        target_profile_id=target,
        target_model="test-model",
        target_model_version="1",
    )
    manager.start(migration.migration_id)
    job = _job(db, manager=manager)
    job.run(_context(), None)
    assert manager.get(migration.migration_id).status == "validating"

    # 未 validate 就 promote → 门禁拒绝。
    with pytest.raises(EmbeddingMigrationGateError):
        manager.promote(migration.migration_id)

    # coverage 不足（eligible 里故意再补一个未完成的）→ 不通过。
    extra = _add(repo, tenant, subject=f"fact:project:reemb:{uuid4().hex[:8]}", content="新写入 [A]")
    manager.seed_backfill(migration.migration_id)

    def _search_all(text: str, profile: str):
        # 两个 profile 返回相同命中（高 overlap），但 coverage 是独立判据。
        return [_hit(str(a.id)), _hit(str(b.id))]

    report = manager.validate(
        migration.migration_id, search_fn=_search_all, probes=["Python", "React"]
    )
    assert not report.passed
    assert any("coverage" in reason for reason in report.reasons)
    assert manager.get(migration.migration_id).status == "validating"

    # 把缺口补上 → 通过 → ready → promote 原子切换。
    # coverage 是全库全局的：用 Job 本身排空所有租户的积压（真实路径），
    # 只补本租户永远凑不齐门槛。
    job.run(_context(), None)
    report = manager.validate(
        migration.migration_id, search_fn=_search_all, probes=["Python", "React"]
    )
    assert report.passed
    assert report.coverage == 1.0
    ready = manager.get(migration.migration_id)
    assert ready.status == "ready"

    promoted = manager.promote(migration.migration_id)
    assert promoted.status == "active"
    active = manager.active_profile()
    assert active["profile_id"] == target
    assert active["model"] == "test-model"

    # promote 后检索默认路由到新 profile（不传 profile 参数）。
    query = SimpleNamespace(
        text="Python", limit=5, scopes=None, memory_types=None,
        min_confidence=0.0, include_shared_ops=False, as_of=None,
    )
    hits = repo.search(query, query_embedding=_UNIT)
    assert any(str(hit.record.id) == str(a.id) for hit in hits)

    # 回滚：一个操作切回 default。
    rolled = manager.rollback(migration.migration_id)
    assert rolled.status == "retired"
    assert manager.active_profile()["profile_id"] == "default"
    with pytest.raises(EmbeddingMigrationStateError):
        manager.rollback(migration.migration_id)  # 终态不能再回滚


def test_validate_requires_validating_status(db: MemoryDatabase) -> None:
    manager = EmbeddingMigrationManager(db)
    migration = manager.create(
        migration_id=f"m-{uuid4().hex[:8]}",
        target_profile_id=f"p-{uuid4().hex[:6]}",
        target_model="m",
        target_model_version="1",
    )
    with pytest.raises(EmbeddingMigrationStateError):
        manager.validate(
            migration.migration_id,
            search_fn=lambda text, profile: [],
            probes=["x"],
        )
    manager.cancel(migration.migration_id)


def test_rerunning_0008_is_noop(db: MemoryDatabase) -> None:
    with db.diagnostic_session() as conn:
        before = conn.execute(
            "SELECT count(*) AS n FROM memory_maintenance.embedding_migrations"
        ).fetchone()["n"]
    applied = db.run_migrations()
    assert "0008_embedding_migrations" not in applied  # 已应用的不重跑
    with db.diagnostic_session() as conn:
        after = conn.execute(
            "SELECT count(*) AS n FROM memory_maintenance.embedding_migrations"
        ).fetchone()["n"]
    assert after == before
