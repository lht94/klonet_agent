"""ExpirationJob 测试（04 计划 §6.2 / 阶段 4）。

离线部分覆盖租户轮转与 dry-run；真库部分覆盖 keyset 分页、条件更新、
跨租户硬过滤、outbox 联动，以及**必须显式验证的 shared_ops 一项**。

跑法::

    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_maintenance_expiration.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.admin import ExpiredArchiveReport, MemoryAdmin, RetentionPolicy
from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    MemorySource,
    MemoryStatus,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
)
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.jobs.expiration import ExpirationJob
from klonet_agent.memory.maintenance.tenants import (
    SHARED_OPS_TENANT,
    TenantSweepOutcome,
    normalise_tenants,
    sweep_tenants,
    tenants_after_cursor,
)
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import NewRecordCommand

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


def _context(*, batch_limit: int = 50, dry_run: bool = False) -> JobContext:
    now = datetime.now(timezone.utc)
    return JobContext(
        job_name="expiration",
        run_id=str(uuid4()),
        worker_id="test-worker",
        started_at=now,
        deadline=now + timedelta(seconds=60),
        batch_limit=batch_limit,
        dry_run=dry_run,
    )


class _RecordingAdmin:
    """替身：记录调用参数，返回预设报告。"""

    def __init__(self, report: ExpiredArchiveReport | None = None, preview: int = 0):
        self.report = report or ExpiredArchiveReport(exhausted=True)
        self.preview = preview
        self.archive_calls: list[dict[str, Any]] = []
        self.preview_calls: list[dict[str, Any]] = []

    def archive_expired_report(self, **kwargs: Any) -> ExpiredArchiveReport:
        self.archive_calls.append(kwargs)
        return self.report

    def expired_preview(self, **kwargs: Any) -> int:
        self.preview_calls.append(kwargs)
        return self.preview


# --------------------------------------------------------------------------- #
# 离线：租户轮转
# --------------------------------------------------------------------------- #


def test_job_name_is_canonical() -> None:
    assert ExpirationJob.name == "expiration"


def test_normalise_tenants_is_stable_and_deduped() -> None:
    tenants = normalise_tenants(
        [
            Tenant(user_id="bob", project_id="demo"),
            Tenant(user_id="alice", project_id="demo"),
            Tenant(user_id="alice", project_id=None),
            Tenant(user_id="alice", project_id="demo"),
        ]
    )
    assert [(t.user_id, t.project_id) for t in tenants] == [
        ("alice", None),
        ("alice", "demo"),
        ("bob", "demo"),
    ]


def test_tenants_after_cursor_skips_processed() -> None:
    tenants = normalise_tenants(
        [Tenant(user_id="alice"), Tenant(user_id="bob"), Tenant(user_id="carol")]
    )
    from klonet_agent.memory.maintenance.tenants import encode_tenant_cursor

    remaining = tenants_after_cursor(tenants, encode_tenant_cursor(tenants[0]))
    assert [t.user_id for t in remaining] == ["bob", "carol"]
    # cursor 认不出来时保留全部（而不是跳过任何租户）。
    assert tenants_after_cursor(tenants, "garbage") == tenants


def test_sweep_stops_at_deadline_and_keeps_cursor() -> None:
    """预算用尽时**当前租户已跑完**再停，并把 cursor 停在它身上。"""

    tenants = [Tenant(user_id=f"u{i}") for i in range(5)]
    seen: list[str] = []
    now = datetime.now(timezone.utc)
    ticks = {"n": 0}

    def _clock() -> datetime:
        ticks["n"] += 1
        # 第一次调用用来判定"预算是否已经用完"：返回 now 之外的时刻逼出 break。
        return now + timedelta(hours=1)

    def _handler(tenant: Tenant):
        seen.append(tenant.user_id)
        return (1, 1, 0, None)

    outcome = sweep_tenants(
        tenants,
        None,
        handler=_handler,
        clock=_clock,
        batch_deadline=now + timedelta(seconds=30),
    )
    assert isinstance(outcome, TenantSweepOutcome)
    assert seen == ["u0"], "第一个租户跑完后发现超时，应立即停"
    assert outcome.next_cursor is not None
    from klonet_agent.memory.maintenance.tenants import decode_tenant_cursor

    assert decode_tenant_cursor(outcome.next_cursor) == ("u0", None)
    assert outcome.changed == 1


def test_sweep_resets_cursor_when_all_done() -> None:
    tenants = [Tenant(user_id="u0"), Tenant(user_id="u1")]
    outcome = sweep_tenants(
        tenants,
        None,
        handler=lambda tenant: (0, 0, 0, None),
        clock=lambda: datetime.now(timezone.utc),
        batch_deadline=None,
    )
    assert outcome.next_cursor is None
    assert outcome.tenants_done == 2


def test_dry_run_does_not_archive() -> None:
    admin = _RecordingAdmin(preview=7)
    job = ExpirationJob(
        database=None,  # type: ignore[arg-type]
        tenants_provider=lambda: [Tenant(user_id="alice", project_id="demo")],
        admin_factory=lambda tenant: admin,  # type: ignore[arg-type,return-value]
    )
    result = job.run(_context(dry_run=True), None)
    assert admin.archive_calls == [], "dry-run 绝不能调归档"
    assert admin.preview_calls and admin.preview_calls[0]["batch_size"] == 500
    assert result.scanned == 7
    assert result.changed == 0
    assert "dry_run candidates=7" in result.details[1]


def test_archive_calls_admin_once_per_tenant() -> None:
    admins: dict[str, _RecordingAdmin] = {}

    def _factory(tenant: Tenant) -> Any:
        admin = _RecordingAdmin(ExpiredArchiveReport(archived=2, batches=1))
        admins[tenant.user_id] = admin
        return admin

    job = ExpirationJob(
        database=None,  # type: ignore[arg-type]
        tenants_provider=lambda: [
            Tenant(user_id="alice", project_id="demo"),
            Tenant(user_id="bob", project_id="demo"),
        ],
        admin_factory=_factory,
    )
    result = job.run(_context(), None)
    assert set(admins) == {"alice", "bob"}
    assert all(len(admin.archive_calls) == 1 for admin in admins.values())
    assert all(
        admin.archive_calls[0]["max_batches"] == 1 for admin in admins.values()
    ), "每个 tick 每个租户只跑一批，进度靠 cursor"
    assert result.changed == 4


def test_no_tenants_returns_note() -> None:
    job = ExpirationJob(
        database=None,  # type: ignore[arg-type]
        tenants_provider=lambda: [],
    )
    result = job.run(_context(), None)
    assert result.scanned == 0
    assert any("no_tenants" in line for line in result.details)


def test_config_batch_size_is_honoured() -> None:
    assert MaintenanceConfig(expiration_batch_size=123).job_batch_size("expiration") == 123


# --------------------------------------------------------------------------- #
# 真库
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过 ExpirationJob 真库测试。")
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过 ExpirationJob 真库测试")
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


def _tenant(*, shared: bool = False) -> Tenant:
    if shared:
        return SHARED_OPS_TENANT
    return Tenant(user_id=f"exp_{uuid4().hex[:8]}", project_id="demo")


def _repo(database: MemoryDatabase, tenant: Tenant) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _add(repo: PostgresMemoryRepository, tenant: Tenant, content: str, **kwargs: Any):
    return repo.add_record(
        NewRecordCommand(
            user_id=tenant.user_id,
            project_id=tenant.project_id,
            memory_type=MemoryType.FACT,
            scope=Scope.SHARED_OPS if tenant is SHARED_OPS_TENANT else Scope.PROJECT,
            subject_key=f"fact:exp:{uuid4().hex[:8]}",
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


def _make_drift(
    db: MemoryDatabase, memory_id: str, *, hours_ago: int = 2
) -> None:
    """制造"``valid_to`` 已过、状态仍是 active"的漂移（直接改 SQL）。

    这正是 ExpirationJob 要清理的历史遗留形态：走 ``mark_expired`` 不会产生它。
    种子记录给了更早的 ``valid_from``，所以 ``valid_to > valid_from`` 的 CHECK 仍然满足。
    """

    with db.diagnostic_session() as conn:
        conn.execute(
            "UPDATE memory_versions SET valid_to = now() - (%s || ' hours')::interval "
            " WHERE memory_id = %s AND valid_to IS NULL",
            (str(int(hours_ago)), UUID(memory_id)),
        )


def _status_of(db: MemoryDatabase, memory_id: str) -> str:
    with db.diagnostic_session() as conn:
        row = conn.execute(
            "SELECT status FROM memory_records WHERE id = %s", (UUID(memory_id),)
        ).fetchone()
    return str(row["status"])


def _candidates(db: MemoryDatabase, repo: PostgresMemoryRepository) -> list[str]:
    page = repo.list_expired_candidates(
        cutoff=datetime.now(timezone.utc), cursor=None, batch_size=100
    )
    return [item.memory_id for item in page.items]


def test_archives_drifted_record_and_leaves_healthy_alone(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    healthy = _add(repo, tenant, "健康的记忆：Python 部署端口 8080")
    drifted = _add(
        repo,
        tenant,
        "漂移的记忆：Python 部署端口 9090",
        valid_from=datetime.now(timezone.utc) - timedelta(days=3),
    )
    _make_drift(db, str(drifted.id))

    assert _candidates(db, repo) == [str(drifted.id)]

    admin = MemoryAdmin(repo)
    report = admin.archive_expired_report(now=datetime.now(timezone.utc), max_batches=5)

    assert report.archived == 1
    assert _status_of(db, str(drifted.id)) == MemoryStatus.EXPIRED.value
    assert _status_of(db, str(healthy.id)) == MemoryStatus.ACTIVE.value
    assert _candidates(db, repo) == [], "归档之后不该再有候选"


def test_archive_is_idempotent(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    drifted = _add(
        repo,
        tenant,
        "幂等记忆",
        valid_from=datetime.now(timezone.utc) - timedelta(days=3),
    )
    _make_drift(db, str(drifted.id))
    admin = MemoryAdmin(repo)
    now = datetime.now(timezone.utc)
    assert admin.archive_expired_report(now=now, max_batches=5).archived == 1
    second = admin.archive_expired_report(now=now, max_batches=5)
    assert second.archived == 0, "已归档的记录不会再次被归档"


def test_keyset_paging_does_not_skip_rows(db: MemoryDatabase) -> None:
    """候选多于一批时，cursor 分页必须逐条覆盖，不能漏。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    ids = set()
    base = datetime.now(timezone.utc)
    for index in range(5):
        record = _add(
            repo,
            tenant,
            f"分页记忆 {index}",
            valid_from=base - timedelta(days=10 + index),
        )
        # 让 valid_to 各不相同且都早于 now，顺序可控。
        with db.diagnostic_session() as conn:
            conn.execute(
                "UPDATE memory_versions SET valid_to = %s WHERE memory_id = %s",
                (base - timedelta(hours=10 - index), UUID(str(record.id))),
            )
        ids.add(str(record.id))

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = repo.list_expired_candidates(
            cutoff=base, cursor=cursor, batch_size=2
        )
        assert len(page.items) <= 2
        seen.extend(item.memory_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert set(seen) == ids
    assert len(seen) == len(set(seen)), "分页不该重复返回同一条"
    # 顺序是 keyset 定义的 (valid_to, memory_id)，必须单调。
    candidates = repo.list_expired_candidates(cutoff=base, cursor=None, batch_size=100)
    valid_tos = [item.valid_to for item in candidates.items]
    assert valid_tos == sorted(valid_tos)


def test_archive_defers_pending_outbox(db: MemoryDatabase) -> None:
    """归档必须在**同一事务**里把未闭环的 embedding 任务推后。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    drifted = _add(
        repo,
        tenant,
        "带 outbox 的漂移记忆",
        valid_from=datetime.now(timezone.utc) - timedelta(days=3),
    )
    _make_drift(db, str(drifted.id))

    def _outbox_retry_at() -> Any:
        with db.diagnostic_session() as conn:
            return conn.execute(
                "SELECT next_attempt_at FROM memory_embedding_outbox "
                " WHERE memory_version_id = %s",
                (UUID(str(drifted.active_version_id)),),
            ).fetchone()["next_attempt_at"]

    before = _outbox_retry_at()
    now = datetime.now(timezone.utc)
    report = MemoryAdmin(repo).archive_expired_report(
        now=now, max_batches=5, outbox_defer_seconds=7200
    )
    assert report.archived == 1
    assert report.outbox_deferred == 1
    after = _outbox_retry_at()
    assert after is not None and after > (before or now - timedelta(days=1))
    assert after >= now + timedelta(seconds=7000), "必须推到 defer 窗口之后"


def test_cross_tenant_isolation(db: MemoryDatabase) -> None:
    """跑 alice 的 Job 绝不能归档 bob 的过期记录。"""

    alice = _tenant()
    bob = _tenant()
    alice_repo = _repo(db, alice)
    bob_repo = _repo(db, bob)
    alice_drift = _add(
        alice_repo, alice, "alice 漂移", valid_from=datetime.now(timezone.utc) - timedelta(days=3)
    )
    bob_drift = _add(
        bob_repo, bob, "bob 漂移", valid_from=datetime.now(timezone.utc) - timedelta(days=3)
    )
    _make_drift(db, str(alice_drift.id))
    _make_drift(db, str(bob_drift.id))

    job = ExpirationJob(
        database=db, tenants_provider=lambda: [alice], batch_size=100
    )
    result = job.run(_context(batch_limit=100), None)

    assert result.changed == 1
    assert _status_of(db, str(alice_drift.id)) == MemoryStatus.EXPIRED.value
    assert _status_of(db, str(bob_drift.id)) == MemoryStatus.ACTIVE.value
    assert _candidates(db, bob_repo) == [str(bob_drift.id)]


def test_shared_ops_requires_explicit_inclusion(db: MemoryDatabase) -> None:
    """shared_ops 的过期归档必须显式包含它——这正是 02 阶段 1 把放行挂在角色上
    带来的后果：普通租户上下文永远扫不到它。

    两半都要断言：

    1. 只给普通租户时，shared_ops 的漂移记录**不会**被归档（证明"扫不到"）；
    2. 把 ``SHARED_OPS_TENANT`` 显式加进租户清单后，它被归档（证明"加了就能"）。
    """

    shared_repo = _repo(db, SHARED_OPS_TENANT)
    drifted = _add(
        shared_repo,
        SHARED_OPS_TENANT,
        "共享运维漂移记忆",
        valid_from=datetime.now(timezone.utc) - timedelta(days=3),
    )
    _make_drift(db, str(drifted.id))

    ordinary = _tenant()
    ordinary_repo = _repo(db, ordinary)
    _add(ordinary_repo, ordinary, "普通租户记忆")

    # 1) 只给普通租户 → 扫不到 shared_ops。
    job_without = ExpirationJob(
        database=db, tenants_provider=lambda: [ordinary], batch_size=100
    )
    job_without.run(_context(batch_limit=100), None)
    assert _status_of(db, str(drifted.id)) == MemoryStatus.ACTIVE.value

    # 2) 显式带上 SHARED_OPS_TENANT → 归档成功。
    job_with = ExpirationJob(
        database=db, tenants_provider=lambda: [ordinary, SHARED_OPS_TENANT], batch_size=100
    )
    result = job_with.run(_context(batch_limit=100), None)
    assert result.changed == 1
    assert _status_of(db, str(drifted.id)) == MemoryStatus.EXPIRED.value


def test_default_tenants_provider_includes_shared_ops(db: MemoryDatabase) -> None:
    """``include_shared_ops=True`` 时默认枚举器必须给出 shared 租户。

    这条比"显式传 SHARED_OPS_TENANT"更强：它验证的是 **Job 的默认配置**就有
    shared——否则运维必须记得手工加，而"忘了加"在指标上完全看不出来。
    """

    from klonet_agent.memory.maintenance.tenants import default_tenants_provider

    ordinary = _tenant()
    _add(_repo(db, ordinary), ordinary, "让库里有普通租户")

    provider = default_tenants_provider(db, include_shared_ops=True)
    keys = {(t.user_id, t.project_id) for t in provider()}
    assert ("shared", None) in keys

    without = default_tenants_provider(db, include_shared_ops=False)
    assert ("shared", None) not in {(t.user_id, t.project_id) for t in without()}


def test_expired_record_stays_recallable_via_as_of(db: MemoryDatabase) -> None:
    """归档 ≠ 删除：历史版本必须仍然能被 as_of 召回（计划 §6.5）。"""

    from klonet_agent.memory.domain import MemoryQuery

    tenant = _tenant()
    repo = _repo(db, tenant)
    base = datetime.now(timezone.utc)
    record = _add(
        repo, tenant, "Python 部署端口记录", valid_from=base - timedelta(days=10)
    )
    with db.diagnostic_session() as conn:
        conn.execute(
            "UPDATE memory_versions SET valid_to = %s WHERE memory_id = %s",
            (base - timedelta(days=1), UUID(str(record.id))),
        )

    MemoryAdmin(repo).archive_expired_report(now=base, max_batches=5)
    assert _status_of(db, str(record.id)) == MemoryStatus.EXPIRED.value

    hits = repo.search(MemoryQuery(text="Python 部署端口", as_of=base - timedelta(days=5)))
    assert any(hit.record.id == str(record.id) for hit in hits), (
        "归档只是终止有效期，as_of 仍应看到当时的版本"
    )
