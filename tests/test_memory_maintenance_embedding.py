"""EmbeddingOutboxJob 测试（04 计划 §6.1 / 阶段 3）。

分两部分：

* **离线**：cursor 编解码、租户排序去重、dry-run 不写库、无嵌入凭据时的降级。
* **真库**（没有 DSN 时整块 skip）：写入 outbox 后 Job 最终生成向量；
  删除记忆不会复活；版本被替代后新旧都补齐；两个 worker 并发消费不重复；
  临时错误有界重试；永久错误直接 abandoned；coverage 与最老 pending 指标。

跑法::

    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_maintenance_embedding.py -q
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    MemoryRecord,
    MemorySource,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
)
from klonet_agent.memory.embedding_worker import (
    EmbeddingContentError,
    EmbeddingRunStats,
)
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.jobs.embedding import (
    EmbeddingOutboxJob,
    decode_tenant_cursor,
    encode_tenant_cursor,
)
from klonet_agent.memory.postgres import EMBEDDING_DIMENSIONS, PostgresMemoryRepository
from klonet_agent.memory.repository import (
    DEFAULT_EMBEDDING_PROFILE_ID,
    EmbeddingOutboxStats,
    NewRecordCommand,
    NewVersionCommand,
)

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


def _unit_vector(index: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[index % EMBEDDING_DIMENSIONS] = 1.0
    return tuple(values)


class _FixedEmbedder:
    """返回固定向量；可选在调用时执行副作用（用于制造竞态）。"""

    def __init__(self, index: int = 0, *, on_call: Any = None):
        self.index = index
        self.on_call = on_call
        self.calls = 0

    def __call__(self, text: str) -> tuple[float, ...]:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(text)
        return _unit_vector(self.index)


class _FlakyEmbedder:
    """前 ``fail_times`` 次抛错，之后成功。``permanent=True`` 时错误带永久标记。"""

    def __init__(self, fail_times: int, *, permanent: bool = False):
        self.fail_times = fail_times
        self.permanent = permanent
        self.calls = 0

    def __call__(self, text: str) -> tuple[float, ...]:
        self.calls += 1
        if self.calls <= self.fail_times:
            if self.permanent:
                raise EmbeddingContentError("permanent embedding failure")
            raise RuntimeError("transient embedding failure")
        return _unit_vector(0)


def _context(*, batch_limit: int = 50, dry_run: bool = False, deadline_in: float = 60.0) -> JobContext:
    now = datetime.now(timezone.utc)
    return JobContext(
        job_name="embedding_outbox",
        run_id=str(uuid4()),
        worker_id="test-worker",
        started_at=now,
        deadline=now + timedelta(seconds=deadline_in),
        batch_limit=batch_limit,
        dry_run=dry_run,
    )


# --------------------------------------------------------------------------- #
# 离线
# --------------------------------------------------------------------------- #


def test_job_name_is_canonical() -> None:
    assert EmbeddingOutboxJob.name == "embedding_outbox"


def test_cursor_round_trip() -> None:
    encoded = encode_tenant_cursor(Tenant(user_id="alice", project_id="demo"))
    assert json.loads(encoded) == ["alice", "demo"]
    assert decode_tenant_cursor(encoded) == ("alice", "demo")


def test_cursor_round_trip_without_project() -> None:
    encoded = encode_tenant_cursor(Tenant(user_id="bob", project_id=None))
    assert decode_tenant_cursor(encoded) == ("bob", None)


def test_cursor_garbage_decodes_to_none() -> None:
    assert decode_tenant_cursor(None) is None
    assert decode_tenant_cursor("") is None
    assert decode_tenant_cursor("{not json") is None
    assert decode_tenant_cursor("[1]") is None
    assert decode_tenant_cursor('["", "p"]') is None
    # 形态不认识时**不猜位置**——从头上重扫，绝不跳过一批租户。
    assert decode_tenant_cursor('{"after": "alice"}') is None


def test_job_without_embedder_returns_zero_counts() -> None:
    job = EmbeddingOutboxJob(database=None, embedder=None)  # type: ignore[arg-type]
    result = job.run(_context(), None)
    assert isinstance(result, JobResult)
    assert result.scanned == 0 and result.changed == 0 and result.failed == 0
    assert any("embedder_unavailable" in line for line in result.details)


def test_job_sorts_and_dedupes_tenants() -> None:
    """cursor 的正确性依赖顺序稳定；重复给出同一租户只应处理一次。"""

    seen: list[tuple[str, str | None]] = []

    class _RecordingWorker:
        def __init__(self, tenant):
            self._tenant = tenant

        def run(self, *, limit, deadline, should_stop):  # noqa: ANN001
            seen.append((self._tenant.user_id, self._tenant.project_id))
            return EmbeddingRunStats()

        def stats(self):
            return EmbeddingOutboxStats()

    def _provider():
        return [
            Tenant(user_id="bob", project_id="demo"),
            Tenant(user_id="alice", project_id="demo"),
            Tenant(user_id="alice", project_id=None),
            Tenant(user_id="alice", project_id="demo"),  # 重复
        ]

    job = EmbeddingOutboxJob(
        database=None,  # type: ignore[arg-type]
        embedder=_FixedEmbedder(),
        tenants_provider=_provider,
    )
    job._repository_factory = lambda tenant: tenant  # type: ignore[assignment]
    job._worker_factory = (  # type: ignore[assignment]
        lambda repository, embedder, **kwargs: _RecordingWorker(repository)
    )
    job.run(_context(), None)
    assert seen == [("alice", None), ("alice", "demo"), ("bob", "demo")]


def test_dry_run_does_not_claim_or_write() -> None:
    class _CountingWorker:
        def __init__(self):
            self.ran = 0
            self.snapshots = 0

        def run(self, **kwargs):  # noqa: ANN003
            self.ran += 1
            return EmbeddingRunStats()

        def stats(self):
            self.snapshots += 1
            return EmbeddingOutboxStats(pending=3)

    worker = _CountingWorker()
    job = EmbeddingOutboxJob(
        database=None,  # type: ignore[arg-type]
        embedder=_FixedEmbedder(),
        tenants_provider=lambda: [Tenant(user_id="alice", project_id="demo")],
    )
    job._repository_factory = lambda tenant: None  # type: ignore[assignment]
    job._worker_factory = lambda repository, embedder, **kwargs: worker  # type: ignore[assignment]
    result = job.run(_context(dry_run=True), None)
    assert worker.ran == 0
    assert worker.snapshots == 1
    assert result.scanned == 3
    assert result.changed == 0
    assert "coverage=" in result.details[0]


def test_config_batch_size_is_honoured() -> None:
    """Job 的 batch_size 来自 MaintenanceConfig（工厂装配路径）。"""

    config = MaintenanceConfig(embedding_batch_size=7)
    assert config.job_batch_size("embedding_outbox") == 7


# --------------------------------------------------------------------------- #
# 真库
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过 EmbeddingOutboxJob 真库测试。"
            "（04 计划阶段 3 的验收标准全部需要真库）"
        )
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过 EmbeddingOutboxJob 真库测试")
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
    """每个测试用独立租户，避免模块级夹具里的数据互相干扰。"""

    return Tenant(user_id=f"emb_{uuid4().hex[:8]}", project_id="demo")


def _repo(database: MemoryDatabase, tenant: Tenant) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _add_record(
    repo: PostgresMemoryRepository, tenant: Tenant, content: str
) -> MemoryRecord:
    return repo.add_record(
        NewRecordCommand(
            user_id=tenant.user_id,
            project_id=tenant.project_id,
            memory_type=MemoryType.FACT,
            scope=Scope.PROJECT,
            subject_key=f"fact:project:demo:{uuid4().hex[:8]}",
            content=content,
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


def _seed(repo: PostgresMemoryRepository, tenant: Tenant, content: str) -> str:
    """写入一条记忆（会自动 enqueue outbox 行），返回 active version id。"""

    record = _add_record(repo, tenant, content)
    assert record.active_version_id is not None
    return str(record.active_version_id)


def _job(database: MemoryDatabase, embedder: Any, **kwargs) -> EmbeddingOutboxJob:
    return EmbeddingOutboxJob(
        database=database,
        embedder=embedder,
        embedding_model="test-model",
        embedding_version="test-model",
        **kwargs,
    )


def _has_embedding(database: MemoryDatabase, version_id: str) -> bool:
    with database.diagnostic_session() as conn:
        row = conn.execute(
            "SELECT embedding IS NOT NULL AS has_vec FROM memory_versions WHERE id = %s",
            (UUID(version_id),),
        ).fetchone()
    return bool(row and row["has_vec"])


def _outbox_row(database: MemoryDatabase, version_id: str) -> Any:
    with database.diagnostic_session() as conn:
        return conn.execute(
            """
            SELECT status, attempt_count, next_attempt_at
              FROM memory_embedding_outbox
             WHERE memory_version_id = %s AND embedding_profile_id = %s
            """,
            (UUID(version_id), DEFAULT_EMBEDDING_PROFILE_ID),
        ).fetchone()


def test_job_embeds_pending_outbox_and_reports_coverage(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    version_ids = [_seed(repo, tenant, f"待补向量的记忆 {i}") for i in range(3)]

    job = _job(db, _FixedEmbedder(), tenants_provider=lambda: [tenant])
    result = job.run(_context(), None)

    assert result.scanned >= 3
    assert result.changed == 3
    assert result.failed == 0
    assert result.next_cursor is None
    assert "coverage=1.0000" in result.details[0]

    for version_id in version_ids:
        assert _has_embedding(db, version_id) is True
        assert _outbox_row(db, version_id)["status"] == "completed"


def test_deleted_record_is_not_reembedded(db: MemoryDatabase) -> None:
    """删除之后：向量清空、outbox 删除、Job 再跑也不会补回来。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    keep_version = _seed(repo, tenant, "保留的记忆")
    drop_record = _add_record(repo, tenant, "将被删除的记忆")
    drop_version = str(drop_record.active_version_id)
    repo.delete_memory(str(drop_record.id))

    assert _outbox_row(db, drop_version) is None, "delete_memory 必须删掉 outbox 行"

    job = _job(db, _FixedEmbedder(), tenants_provider=lambda: [tenant])
    result = job.run(_context(), None)

    assert _has_embedding(db, drop_version) is False, "已删除记忆不得被重新嵌入"
    assert _has_embedding(db, keep_version) is True
    assert result.failed == 0


def test_delete_between_claim_and_write_back_is_skipped(db: MemoryDatabase) -> None:
    """竞态：领取之后、写回之前被删除 → 跳过（不是失败，也不复活）。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    record = _add_record(repo, tenant, "竞态记忆")
    version_id = str(record.active_version_id)

    def _delete_on_embed(_text: str) -> None:
        # 在"生成向量"这一步里删除记录——模拟用户正好在 worker 干活时删除。
        repo.delete_memory(str(record.id))

    job = _job(
        db,
        _FixedEmbedder(on_call=_delete_on_embed),
        tenants_provider=lambda: [tenant],
    )
    result = job.run(_context(), None)

    assert result.failed == 0, "删除不是失败，不该计进 failed"
    assert _has_embedding(db, version_id) is False
    assert _outbox_row(db, version_id) is None


def test_superseded_version_is_still_embedded(db: MemoryDatabase) -> None:
    """版本被替代后，新旧版本都应有向量（as_of 视图要能召回旧值）。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    record = _add_record(repo, tenant, "旧版本正文")
    old_version = str(record.active_version_id)

    new_version = repo.add_version(
        NewVersionCommand(
            memory_id=str(record.id),
            content="新版本正文",
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
    assert str(new_version.id) != old_version

    job = _job(db, _FixedEmbedder(), tenants_provider=lambda: [tenant])
    result = job.run(_context(), None)

    assert result.failed == 0
    assert _has_embedding(db, old_version) is True
    assert _has_embedding(db, str(new_version.id)) is True


def test_two_workers_do_not_double_process(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    version_ids = [_seed(repo, tenant, f"并发消费记忆 {i}") for i in range(10)]

    db_a = MemoryDatabase(db.config.dsn, min_size=1, max_size=4)
    db_a.open()
    db_b = MemoryDatabase(db.config.dsn, min_size=1, max_size=4)
    db_b.open()
    try:
        job_a = _job(db_a, _FixedEmbedder(0), tenants_provider=lambda: [tenant], batch_size=3)
        job_b = _job(db_b, _FixedEmbedder(1), tenants_provider=lambda: [tenant], batch_size=3)
        results: list[JobResult] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def _run(job: EmbeddingOutboxJob) -> None:
            barrier.wait()
            result = job.run(_context(batch_limit=50), None)
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=_run, args=(job_a,)),
            threading.Thread(target=_run, args=(job_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        db_a.close()
        db_b.close()

    total_changed = sum(r.changed for r in results)
    assert total_changed == 10, f"10 条任务应恰好写回 10 次，实际 {total_changed}"
    for version_id in version_ids:
        assert _has_embedding(db, version_id) is True
        assert _outbox_row(db, version_id)["status"] == "completed"


def test_transient_error_retries_then_succeeds(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    version_id = _seed(repo, tenant, "临时失败后重试")

    embedder = _FlakyEmbedder(fail_times=1)
    job = _job(db, embedder, tenants_provider=lambda: [tenant])

    first = job.run(_context(), None)
    row = _outbox_row(db, version_id)
    assert first.failed >= 1
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] is not None
    assert _has_embedding(db, version_id) is False

    # 把退避时间拨到过去，模拟"退避到点了"。
    with db.diagnostic_session() as conn:
        conn.execute(
            "UPDATE memory_embedding_outbox "
            "   SET next_attempt_at = now() - interval '1 minute' "
            " WHERE memory_version_id = %s",
            (UUID(version_id),),
        )

    second = job.run(_context(), None)
    assert second.changed >= 1
    assert _has_embedding(db, version_id) is True
    assert _outbox_row(db, version_id)["status"] == "completed"


def test_permanent_error_is_abandoned_without_retry(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    version_id = _seed(repo, tenant, "永久失败的记忆")

    embedder = _FlakyEmbedder(fail_times=99, permanent=True)
    job = _job(db, embedder, tenants_provider=lambda: [tenant])
    result = job.run(_context(), None)

    assert result.failed >= 1
    row = _outbox_row(db, version_id)
    assert row["status"] == "failed", "永久失败必须直接进终态"
    assert row["attempt_count"] == 1, "永久失败不该反复重试"
    assert row["next_attempt_at"] is None
    assert embedder.calls == 1


def test_expired_deadline_stops_before_claiming(db: MemoryDatabase) -> None:
    """deadline 已过期时不应再领取任何任务。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    version_id = _seed(repo, tenant, "不该被处理的记忆")

    job = _job(db, _FixedEmbedder(), tenants_provider=lambda: [tenant])
    result = job.run(_context(deadline_in=-1.0), None)

    assert result.changed == 0
    assert _has_embedding(db, version_id) is False


def test_oldest_pending_metric_reflects_backlog(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    _seed(repo, tenant, "积压的记忆 A")
    _seed(repo, tenant, "积压的记忆 B")

    embedder = _FlakyEmbedder(fail_times=99, permanent=False)
    job = _job(db, embedder, tenants_provider=lambda: [tenant], batch_size=1)
    result = job.run(_context(batch_limit=1), None)

    assert "oldest_pending_seconds=" in result.details[0]
    # 有积压时 coverage 不应是 1.0
    assert "coverage=1.0000" not in result.details[0]


def test_cursor_advances_across_tenants(db: MemoryDatabase) -> None:
    """两个租户 + 第一轮预算极小 → 第一轮只跑一个，第二轮接着跑另一个。"""

    tenants = [_tenant(), _tenant()]
    versions: dict[tuple[str, str | None], str] = {}
    for tenant in tenants:
        repo = _repo(db, tenant)
        versions[(tenant.user_id, tenant.project_id)] = _seed(repo, tenant, f"{tenant.user_id} 的记忆")

    ordered = sorted(tenants, key=lambda t: (t.user_id, t.project_id or ""))

    # 第一轮：让 _clock 第二次被调用时立刻"超时"，逼 Job 在第一个租户后停下。
    start = datetime.now(timezone.utc)
    counter = {"n": 0}

    def _clock() -> datetime:
        counter["n"] += 1
        return start if counter["n"] <= 1 else start + timedelta(seconds=999)

    job = _job(db, _FixedEmbedder(), tenants_provider=lambda: list(tenants))
    job._clock = _clock  # type: ignore[assignment]
    first = job.run(_context(), None)
    assert first.next_cursor is not None, "预算用尽时应留下 cursor"
    assert first.changed == 1, "第一轮只应跑完第一个租户"

    # 第二轮：正常预算，从 cursor 之后接着跑，应清空 cursor。
    job2 = _job(db, _FixedEmbedder(), tenants_provider=lambda: list(tenants))
    second = job2.run(_context(), first.next_cursor)
    assert second.next_cursor is None

    for tenant in ordered:
        assert _has_embedding(db, versions[(tenant.user_id, tenant.project_id)]) is True
