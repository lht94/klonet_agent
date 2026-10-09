"""记忆维护调度仓库测试（04 计划 §5.3 / 阶段 1 完成标准）。

分两部分：

* **离线（不依赖 DSN）** — 验证 ``MaintenanceJob`` Protocol、``JobContext`` /
  ``JobResult`` 字段约束、``_coerce_*`` 静态方法。
* **真库** — 验证 schema 正确（迁移落地后 ``memory_maintenance_jobs`` /
  ``memory_maintenance_runs`` 表存在）、``claim_due_job`` 在并发下
  互斥、``heartbeat`` 续租、``complete`` / ``fail`` 推进 cursor 与
  ``consecutive_failures``、重复 migration 无副作用、租约过期可被
  重新领取、cursor 不在失败时推进。

跑法::

    KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres" \\
        python -m pytest tests/test_memory_maintenance_repository.py -q
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from klonet_agent.config import PROJECT_ROOT
from klonet_agent.memory.database import (
    MemoryDatabase,
    load_migration_files,
    temporary_database,
)
from klonet_agent.memory.maintenance.base import (
    JobContext,
    JobResult,
    MaintenanceJob,
)
from klonet_agent.memory.maintenance.repository import (
    ClaimRace,
    JobClaim,
    MaintenanceRepository,
)

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


# --------------------------------------------------------------------------- #
# 离线测试：JobContext / JobResult 字段约束
# --------------------------------------------------------------------------- #


def test_job_context_rejects_empty_fields() -> None:
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=1)
    with pytest.raises(ValueError, match="job_name"):
        JobContext(
            job_name="",
            run_id="run-1",
            worker_id="w-1",
            started_at=now,
            deadline=deadline,
            batch_limit=10,
        )
    with pytest.raises(ValueError, match="run_id"):
        JobContext(
            job_name="j",
            run_id="",
            worker_id="w-1",
            started_at=now,
            deadline=deadline,
            batch_limit=10,
        )
    with pytest.raises(ValueError, match="worker_id"):
        JobContext(
            job_name="j",
            run_id="r",
            worker_id="",
            started_at=now,
            deadline=deadline,
            batch_limit=10,
        )


def test_job_context_rejects_non_positive_batch_limit() -> None:
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=1)
    with pytest.raises(ValueError, match="batch_limit"):
        JobContext(
            job_name="j",
            run_id="r",
            worker_id="w",
            started_at=now,
            deadline=deadline,
            batch_limit=0,
        )
    with pytest.raises(ValueError, match="batch_limit"):
        JobContext(
            job_name="j",
            run_id="r",
            worker_id="w",
            started_at=now,
            deadline=deadline,
            batch_limit=-5,
        )


def test_job_result_rejects_negative_counters() -> None:
    with pytest.raises(ValueError, match="scanned"):
        JobResult(scanned=-1)
    with pytest.raises(ValueError, match="changed"):
        JobResult(changed=-1)
    with pytest.raises(ValueError, match="proposed"):
        JobResult(proposed=-1)
    with pytest.raises(ValueError, match="failed"):
        JobResult(failed=-1)


def test_job_result_defaults_to_zero() -> None:
    r = JobResult()
    assert r.scanned == 0
    assert r.changed == 0
    assert r.proposed == 0
    assert r.failed == 0
    assert r.next_cursor is None
    assert r.details == ()


def test_maintenance_job_protocol_accepts_runtime_checkable() -> None:
    """``@runtime_checkable`` 必须能 ``isinstance`` 判定；这是 scheduler 注册 Job
    时的契约前提（详见阶段 2 的 service.py）。"""

    class _Noop:
        name = "noop"

        def run(self, context: JobContext, cursor: str | None) -> JobResult:
            return JobResult()

    assert isinstance(_Noop(), MaintenanceJob)


# --------------------------------------------------------------------------- #
# 真库测试：DSN 门控
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过维护仓库真库测试。"
            "（04 计划阶段 1：claim_due_job 的并发互斥只有真库能验证）"
        )
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过维护仓库真库测试")
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
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            applied = database.run_migrations()
            # 0006 是阶段 1 新增的迁移；本测试文件前置于阶段 1 落地
            # 是写不进来的——所以**强制要求**该迁移已就位。
            assert "0006_memory_maintenance" in applied, (
                f"0006_memory_maintenance 未在 applied_migrations 中：{applied}"
            )
            yield database
        finally:
            database.close()


@pytest.fixture
def repo(db: MemoryDatabase) -> MaintenanceRepository:
    return MaintenanceRepository(db)


@pytest.fixture
def isolated_job(db: MemoryDatabase) -> str:
    """每个真库测试都从一个干净状态开始：

    * 把所有 ``next_run_at`` 推远到 2099，让 ``claim_due_job`` 暂时拿不到。
    * 删除所有非本测试用的 job，让并发测试不互相干扰。

    返回一个本测试用的 job_name（``embedding_outbox`` 已经在 0006 里
    INSERT 一行 enabled=false，把它的 ``enabled`` 切到 true + 拉回 now 即可）。
    """

    with db.diagnostic_session() as conn:
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_jobs
            SET next_run_at = '2099-01-01T00:00:00+00:00',
                lease_owner = NULL,
                lease_expires_at = NULL
            """
        )
        conn.execute(
            """
            DELETE FROM memory_maintenance.memory_maintenance_runs
            """
        )
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_jobs
            SET enabled = true,
                next_run_at = now(),
                schedule_seconds = 10
            WHERE job_name = 'embedding_outbox'
            """
        )
    return "embedding_outbox"


# --------------------------------------------------------------------------- #
# 迁移层面的不变量
# --------------------------------------------------------------------------- #


def test_maintenance_migration_creates_expected_tables(db: MemoryDatabase) -> None:
    with db.diagnostic_session() as conn:
        tables = conn.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'memory_maintenance'
            ORDER BY table_name
            """
        ).fetchall()
        names = [row["table_name"] for row in tables]
    assert "memory_maintenance_jobs" in names
    assert "memory_maintenance_runs" in names


def test_maintenance_migration_inserts_default_jobs(db: MemoryDatabase) -> None:
    """0006 的握手 INSERT 应该已经有 6 个 job 名字（全部 enabled=false）。"""

    with db.diagnostic_session() as conn:
        rows = conn.execute(
            """
            SELECT job_name, enabled, schedule_seconds FROM
                memory_maintenance.memory_maintenance_jobs
            ORDER BY job_name
            """
        ).fetchall()
    names = {row["job_name"] for row in rows}
    assert names == {
        "embedding_outbox",
        "expiration",
        "purge",
        "consolidation",
        "reembedding",
        "health_report",
    }
    # 默认全部 disabled（Worker 阶段 8 才开启）。
    assert all(row["enabled"] is False for row in rows)
    # 默认周期不为零即可。
    assert all(int(row["schedule_seconds"]) > 0 for row in rows)


def test_rerunning_maintenance_migration_is_noop(db: MemoryDatabase) -> None:
    """schema_migrations 校验重复执行 0006 无副作用。"""

    with db.diagnostic_session() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_maintenance.memory_maintenance_jobs"
        ).fetchone()["n"]
    # 走迁移执行器的"再跑一次"路径——0006 已经在 schema_migrations 里，
    # 所以这次不会被执行；``applied`` 返回空列表是正确的。
    applied = db.run_migrations()
    assert applied == []
    with db.diagnostic_session() as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_maintenance.memory_maintenance_jobs"
        ).fetchone()["n"]
    assert after == before


# --------------------------------------------------------------------------- #
# claim_due_job：原子领取
# --------------------------------------------------------------------------- #


def test_claim_returns_none_when_no_due_job(repo: MaintenanceRepository, db: MemoryDatabase) -> None:
    """所有 job 都被 fixture 推到 2099，应当无 due。"""

    with db.diagnostic_session() as conn:
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_jobs
            SET next_run_at = '2099-01-01T00:00:00+00:00',
                lease_owner = NULL, lease_expires_at = NULL
            """
        )
    assert repo.claim_due_job(worker_id="w-1", lease_seconds=60.0) is None


def test_claim_succeeds_and_writes_running_run(
    repo: MaintenanceRepository, isolated_job: str, db: MemoryDatabase
) -> None:
    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    assert claim.job_name == isolated_job
    assert claim.worker_id == "w-1"
    assert isinstance(claim.started_at, datetime)
    assert claim.deadline > claim.started_at
    assert claim.cursor is None

    with db.diagnostic_session() as conn:
        run_row = conn.execute(
            """
            SELECT run_id, status, worker_id FROM
                memory_maintenance.memory_maintenance_runs
            WHERE run_id = %s
            """,
            (claim.run_id,),
        ).fetchone()
        assert run_row is not None
        assert run_row["status"] == "running"
        assert run_row["worker_id"] == "w-1"

        lease = conn.execute(
            """
            SELECT lease_owner, lease_expires_at > now() AS still_valid FROM
                memory_maintenance.memory_maintenance_jobs
            WHERE job_name = %s
            """,
            (isolated_job,),
        ).fetchone()
        assert lease["lease_owner"] == "w-1"
        assert lease["still_valid"] is True


def test_concurrent_claim_only_one_succeeds(repo: MaintenanceRepository, isolated_job: str) -> None:
    """两个 worker 同时领 due job，只有一个成功——这是 04 计划阶段 1 完成标准第一条。"""

    barrier = threading.Barrier(2)
    results: list[JobClaim | None | Exception] = []
    results_lock = threading.Lock()

    def _worker(worker_id: str) -> None:
        # 复制 repository 各自走自己的事务（pool 多连接）。
        barrier.wait()
        try:
            claim = repo.claim_due_job(worker_id=worker_id, lease_seconds=60.0)
            with results_lock:
                results.append(claim)
        except Exception as exc:  # pragma: no cover - 调试时偶尔出现
            with results_lock:
                results.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(f"w-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    successes = [r for r in results if isinstance(r, JobClaim)]
    assert len(successes) == 1, f"应只有一个 worker 拿到 claim：{results}"
    assert successes[0].job_name == isolated_job


# --------------------------------------------------------------------------- #
# heartbeat：续租
# --------------------------------------------------------------------------- #


def test_heartbeat_extends_lease(
    repo: MaintenanceRepository, isolated_job: str, db: MemoryDatabase
) -> None:
    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=30.0)
    assert claim is not None
    # 把 lease 缩短到过去，模拟"租约已快过期"——
    # 这里只验证 heartbeat 真正把 lease 推后。
    with db.diagnostic_session() as conn:
        before = conn.execute(
            "SELECT lease_expires_at FROM memory_maintenance.memory_maintenance_jobs "
            "WHERE job_name = %s",
            (isolated_job,),
        ).fetchone()["lease_expires_at"]
    assert repo.heartbeat(job_name=isolated_job, worker_id="w-1", lease_seconds=300.0) is True
    with db.diagnostic_session() as conn:
        after = conn.execute(
            "SELECT lease_expires_at FROM memory_maintenance.memory_maintenance_jobs "
            "WHERE job_name = %s",
            (isolated_job,),
        ).fetchone()["lease_expires_at"]
    assert after > before


def test_heartbeat_rejects_wrong_owner(
    repo: MaintenanceRepository, isolated_job: str
) -> None:
    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    # 别的 worker 续租 → False
    assert repo.heartbeat(job_name=isolated_job, worker_id="w-2", lease_seconds=60.0) is False


def test_heartbeat_rejects_expired_lease(
    repo: MaintenanceRepository, isolated_job: str, db: MemoryDatabase
) -> None:
    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    # 手动把 lease 推到过去
    with db.diagnostic_session() as conn:
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_jobs
            SET lease_expires_at = now() - interval '1 minute'
            WHERE job_name = %s
            """,
            (isolated_job,),
        )
    assert repo.heartbeat(job_name=isolated_job, worker_id="w-1", lease_seconds=60.0) is False


# --------------------------------------------------------------------------- #
# complete / fail：收尾
# --------------------------------------------------------------------------- #


def test_complete_advances_cursor_and_resets_failures(
    repo: MaintenanceRepository, isolated_job: str, db: MemoryDatabase
) -> None:
    # 先累加一次失败，让 consecutive_failures > 0
    with db.diagnostic_session() as conn:
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_jobs
            SET consecutive_failures = 3
            WHERE job_name = %s
            """,
            (isolated_job,),
        )

    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    result = JobResult(scanned=10, changed=2, proposed=0, failed=0,
                       next_cursor='{"profile":"default","after":"v-1"}',
                       details=("batch=10", "after=v-1"))
    repo.complete(
        job_name=isolated_job, run_id=claim.run_id, result=result, next_run_seconds=10.0
    )

    with db.diagnostic_session() as conn:
        run_row = conn.execute(
            "SELECT status, finished_at, scanned, changed, details FROM "
            "memory_maintenance.memory_maintenance_runs WHERE run_id = %s",
            (claim.run_id,),
        ).fetchone()
        assert run_row["status"] == "succeeded"
        assert run_row["finished_at"] is not None
        assert run_row["scanned"] == 10
        assert run_row["changed"] == 2
        assert "batch=10" in run_row["details"]["lines"][0]

        job_row = conn.execute(
            "SELECT cursor, consecutive_failures, lease_owner, last_succeeded_at, "
            "next_run_at > now() AS next_in_future FROM "
            "memory_maintenance.memory_maintenance_jobs WHERE job_name = %s",
            (isolated_job,),
        ).fetchone()
        assert job_row["consecutive_failures"] == 0
        assert job_row["lease_owner"] is None
        assert job_row["last_succeeded_at"] is not None
        assert job_row["next_in_future"] is True
        # cursor 形态是 JSON
        cursor_obj = job_row["cursor"]
        if isinstance(cursor_obj, str):
            cursor_obj = json.loads(cursor_obj)
        assert cursor_obj["after"] == "v-1"


def test_complete_rejects_non_running_run(
    repo: MaintenanceRepository, isolated_job: str
) -> None:
    """同一 run_id 二次 complete 应抛错——防止"幽灵成功"覆盖真实状态。"""

    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    result = JobResult(scanned=1, changed=1, next_cursor="x")
    repo.complete(
        job_name=isolated_job, run_id=claim.run_id, result=result, next_run_seconds=10.0
    )
    with pytest.raises(RuntimeError, match="不是 running 状态"):
        repo.complete(
            job_name=isolated_job, run_id=claim.run_id, result=result, next_run_seconds=10.0
        )


def test_fail_increments_consecutive_failures_and_keeps_cursor(
    repo: MaintenanceRepository, isolated_job: str, db: MemoryDatabase
) -> None:
    # 先给个旧 cursor
    with db.diagnostic_session() as conn:
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_jobs
            SET cursor = %s::jsonb
            WHERE job_name = %s
            """,
            ('{"after":"old"}', isolated_job),
        )

    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    repo.fail(
        job_name=isolated_job,
        run_id=claim.run_id,
        error_code="embedding_timeout",
        error_summary="provider API timeout 30s",
        next_run_seconds=10.0,
    )

    with db.diagnostic_session() as conn:
        run_row = conn.execute(
            "SELECT status, error_code, error_summary FROM "
            "memory_maintenance.memory_maintenance_runs WHERE run_id = %s",
            (claim.run_id,),
        ).fetchone()
        assert run_row["status"] == "failed"
        assert run_row["error_code"] == "embedding_timeout"
        assert "timeout" in run_row["error_summary"]

        job_row = conn.execute(
            "SELECT cursor, consecutive_failures FROM "
            "memory_maintenance.memory_maintenance_jobs WHERE job_name = %s",
            (isolated_job,),
        ).fetchone()
        assert job_row["consecutive_failures"] == 1
        # 失败不推进 cursor：旧 cursor 仍在
        cursor_obj = job_row["cursor"]
        if isinstance(cursor_obj, str):
            cursor_obj = json.loads(cursor_obj)
        assert cursor_obj["after"] == "old"


def test_fail_rejects_empty_error_code(
    repo: MaintenanceRepository, isolated_job: str
) -> None:
    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    with pytest.raises(ValueError, match="error_code"):
        repo.fail(
            job_name=isolated_job, run_id=claim.run_id,
            error_code="", error_summary="x", next_run_seconds=10.0,
        )


def test_fail_rejects_empty_error_summary(
    repo: MaintenanceRepository, isolated_job: str
) -> None:
    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    with pytest.raises(ValueError, match="error_summary"):
        repo.fail(
            job_name=isolated_job, run_id=claim.run_id,
            error_code="x", error_summary="", next_run_seconds=10.0,
        )


def test_fail_truncates_overlong_summary(
    repo: MaintenanceRepository, isolated_job: str, db: MemoryDatabase
) -> None:
    claim = repo.claim_due_job(worker_id="w-1", lease_seconds=60.0)
    assert claim is not None
    long_summary = "x" * 5000
    repo.fail(
        job_name=isolated_job, run_id=claim.run_id,
        error_code="boom", error_summary=long_summary, next_run_seconds=10.0,
    )
    with db.diagnostic_session() as conn:
        row = conn.execute(
            "SELECT length(error_summary) AS n FROM "
            "memory_maintenance.memory_maintenance_runs WHERE run_id = %s",
            (claim.run_id,),
        ).fetchone()
    assert row["n"] == 1000


# --------------------------------------------------------------------------- #
# 租约过期与重新领取
# --------------------------------------------------------------------------- #


def test_expired_lease_allows_reclaim(
    repo: MaintenanceRepository, isolated_job: str, db: MemoryDatabase
) -> None:
    """worker 崩溃后 lease_expires_at 过期，另一个 worker 应能领走。"""

    claim = repo.claim_due_job(worker_id="w-dead", lease_seconds=60.0)
    assert claim is not None
    # 把 lease 推到过去 + last_started_at 推远，让 next_run_at 自然小于 now。
    with db.diagnostic_session() as conn:
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_jobs
            SET lease_expires_at = now() - interval '1 minute',
                next_run_at = now() - interval '1 minute'
            WHERE job_name = %s
            """,
            (isolated_job,),
        )
    # 旧的 runs 状态还是 running——partial unique index 会挡住新 claim 除非
    # 旧 runs 已被 fail/abandoned 标记。这里走与阶段 3 一致的"abandoned"
    # 逻辑（仍由 PG 兜底，repository 显式承认并转 ClaimRace）：
    # 简单起见，把旧 runs 显式 abandoned 以模拟清理路径。
    with db.diagnostic_session() as conn:
        conn.execute(
            """
            UPDATE memory_maintenance.memory_maintenance_runs
            SET status = 'abandoned', finished_at = now(),
                error_code = 'lease_expired',
                error_summary = 'worker crashed; lease reaped'
            WHERE run_id = %s
            """,
            (claim.run_id,),
        )

    new_claim = repo.claim_due_job(worker_id="w-rescue", lease_seconds=60.0)
    assert new_claim is not None
    assert new_claim.run_id != claim.run_id
    assert new_claim.worker_id == "w-rescue"


# --------------------------------------------------------------------------- #
# 内部辅助方法
# --------------------------------------------------------------------------- #


def test_coerce_cursor_handles_dict_and_string() -> None:
    """cursor 形态由 Job 决定，repository 只稳住成字符串。"""

    assert MaintenanceRepository._coerce_cursor(None) is None
    assert MaintenanceRepository._coerce_cursor("plain") == "plain"
    assert MaintenanceRepository._coerce_cursor('{"a":1}') == '{"a":1}'
    assert MaintenanceRepository._coerce_cursor({"a": 1}) == '{"a": 1}'


def test_dump_cursor_preserves_json_or_wraps_string() -> None:
    """完整 JSON 透传；非 JSON 字符串被包成 JSON 字符串。"""

    assert MaintenanceRepository._dump_cursor(None) == "null"
    assert MaintenanceRepository._dump_cursor("") == "null"
    assert MaintenanceRepository._dump_cursor('{"a":1}') == '{"a":1}'
    # 非 JSON 字符串（极少见）会被转成 "字符串"
    assert json.loads(MaintenanceRepository._dump_cursor("hello")) == "hello"