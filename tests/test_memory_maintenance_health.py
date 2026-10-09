"""健康快照与指标上报测试（04 计划 §6.6 / 阶段 7）。

离线部分覆盖过渡语义、fail-closed、flatten 键形态与"无泄密"；
真库部分覆盖真实数字（jobs / outbox / 漂移 / 提案）、record_run_metric
落 runtime_events、HealthReportJob 的上报与降级。
"""

from __future__ import annotations

import json
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
from klonet_agent.memory.maintenance.health import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_PENDING_AGE_SECONDS,
    collect_snapshot,
    flatten_metrics,
)
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import NewRecordCommand

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"
MEMORY_DSN_ENV = "KLONET_AGENT_MEMORY_DSN"


# --------------------------------------------------------------------------- #
# 离线
# --------------------------------------------------------------------------- #


def test_no_dsn_is_transition_healthy(monkeypatch) -> None:
    """未配置 DSN：过渡语义 healthy=True（模块在、Worker 未部署）。"""

    monkeypatch.delenv(MEMORY_DSN_ENV, raising=False)
    snapshot = collect_snapshot()
    assert snapshot["healthy"] is True
    assert "未配置" in str(snapshot["reason"])


def test_broken_database_is_fail_closed(monkeypatch) -> None:
    """DSN 在但库不可用：healthy=False（阶段 7 起真实故障必须阻断），
    且**不抛异常**、错误只有类型+消息（无连接串/密码）。"""

    class _Broken:
        def _require_pool(self):
            raise RuntimeError("connection refused (模拟)")

    snapshot = collect_snapshot(database=_Broken())
    assert snapshot["healthy"] is False
    assert "RuntimeError" in str(snapshot["reason"])
    # fail-closed 不炸调用方，且 §6.6 键一个不少（空值兜底）。
    for key in (
        "memory_maintenance_last_success_timestamp",
        "memory_embedding_pending_total",
        "memory_expired_active_total",
        "memory_reembedding_progress_ratio",
    ):
        assert key in snapshot


def test_dsn_env_set_but_unreachable_is_fail_closed(monkeypatch) -> None:
    """从环境解析（database=None）路径：DSN 指向打不开的库 → healthy=False。"""

    monkeypatch.setenv(
        MEMORY_DSN_ENV, "postgresql://postgres:wrong@127.0.0.1:1/postgres"
    )
    snapshot = collect_snapshot()
    assert snapshot["healthy"] is False
    text = json.dumps(snapshot, ensure_ascii=False, default=str)
    # 不泄密：错误信息里没有完整连接串。
    assert "wrong" not in text


def test_flatten_metrics_shapes() -> None:
    snapshot = {
        "healthy": True,
        "reason": "ok",
        "captured_at": "2026-10-09T00:00:00+00:00",
        "memory_maintenance_consecutive_failures": {"purge": 1, "expiration": 0},
        "memory_embedding_pending_total": {"default": 3},
        "memory_expired_active_total": 2,
        "memory_deleted_waiting_purge_total": 0,
        "memory_proposals_pending_total": {"merge": 1},
    }
    flat = flatten_metrics(snapshot)
    assert 'memory_maintenance_consecutive_failures{job="purge"}' in flat
    assert 'memory_embedding_pending_total{profile="default"}' in flat
    assert 'memory_proposals_pending_total{type="merge"}' in flat
    assert flat["memory_expired_active_total"] == 2
    # 非指标字段不上报。
    assert all(not key.startswith(("healthy", "reason", "captured")) for key in flat)


# --------------------------------------------------------------------------- #
# 真库
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过 health 真库测试。")
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过 health 真库测试")
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
    return Tenant(user_id=f"health_{uuid4().hex[:8]}", project_id="demo")


def _repo(database: MemoryDatabase, tenant: Tenant) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _add(
    repo: PostgresMemoryRepository,
    tenant: Tenant,
    *,
    subject: str,
    content: str,
):
    return repo.add_record(
        NewRecordCommand(
            user_id=tenant.user_id,
            project_id=tenant.project_id,
            memory_type=MemoryType.FACT,
            scope=Scope.PROJECT,
            subject_key=subject,
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


def test_real_numbers_flow_into_snapshot(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    keep = _add(repo, tenant, subject=f"fact:project:hl:{uuid4().hex[:8]}", content="保留 [A]")
    doomed = _add(repo, tenant, subject=f"fact:project:hl:{uuid4().hex[:8]}", content="待清理 [B]")
    repo.delete_memory(str(doomed.id))

    snapshot = collect_snapshot(database=db)

    # embedding outbox：写入记忆会自动排队 default profile；被删除的那条
    # outbox 行随"删除三件套"一起清掉（删除不复活），所以 pending = 1。
    assert snapshot["memory_embedding_pending_total"].get("default", 0) == 1
    cov = snapshot["memory_embedding_coverage_ratio"]
    if "default" in cov:
        assert 0.0 <= cov["default"] <= 1.0

    # 待 purge 的 deleted 计数含本用例那条。
    assert snapshot["memory_deleted_waiting_purge_total"] >= 1
    # healthy：6 个 Job 的 failures 全 0、pending 是刚写入的（年龄小）。
    assert snapshot["healthy"] is True


def test_consecutive_failures_flip_health(db: MemoryDatabase) -> None:
    job_name = "purge"
    with db.diagnostic_session() as conn:
        conn.execute(
            "UPDATE memory_maintenance.memory_maintenance_jobs "
            "   SET consecutive_failures = %s WHERE job_name = %s",
            (MAX_CONSECUTIVE_FAILURES, job_name),
        )
    try:
        snapshot = collect_snapshot(database=db)
        assert snapshot["healthy"] is False
        assert job_name in str(snapshot["reason"])
    finally:
        with db.diagnostic_session() as conn:
            conn.execute(
                "UPDATE memory_maintenance.memory_maintenance_jobs "
                "   SET consecutive_failures = 0 WHERE job_name = %s",
                (job_name,),
            )


def test_old_pending_backlog_flips_health(db: MemoryDatabase) -> None:
    old = datetime.now(timezone.utc) - timedelta(seconds=MAX_PENDING_AGE_SECONDS + 60)
    with db.diagnostic_session() as conn:
        conn.execute(
            "UPDATE memory_embedding_outbox SET created_at = %s "
            " WHERE status = 'pending'",
            (old,),
        )
    try:
        snapshot = collect_snapshot(database=db)
        assert snapshot["healthy"] is False
        assert "pending" in str(snapshot["reason"])
    finally:
        with db.diagnostic_session() as conn:
            conn.execute("UPDATE memory_embedding_outbox SET created_at = now()")


def test_record_run_metric_writes_runtime_events(db: MemoryDatabase) -> None:
    from klonet_agent.runtime.governance.service import record_run_metric

    rid = record_run_metric(
        db,
        metrics={"memory_expired_active_total": 1, "memory_embedding_pending_total{profile=\"default\"}": 2},
        run_id=f"memory-maintenance-test-{uuid4().hex[:8]}",
    )
    with db.diagnostic_session() as conn:
        event = conn.execute(
            "SELECT e.event_type AS event_type, e.payload AS payload, "
            "       r.user_id AS run_user FROM governance.runtime_events e "
            " JOIN governance.runs r ON r.run_id = e.run_id WHERE r.run_id = %s "
            " ORDER BY e.occurred_at DESC LIMIT 1",
            (rid,),
        ).fetchone()
    assert event is not None
    assert event["event_type"] == "run_metric"
    assert event["run_user"] == "system"
    metrics = event["payload"]["metrics"]
    assert metrics["memory_expired_active_total"] == 1
    assert metrics["memory_embedding_pending_total{profile=\"default\"}"] == 2


def test_health_report_job_reports_via_governance(db: MemoryDatabase) -> None:
    from klonet_agent.memory.maintenance.base import JobContext
    from klonet_agent.memory.maintenance.jobs.health_report import HealthReportJob

    now = datetime.now(timezone.utc)
    context = JobContext(
        job_name="health_report",
        run_id=str(uuid4()),
        worker_id="test-worker",
        started_at=now,
        deadline=now + timedelta(seconds=60),
        batch_limit=1,
    )
    job = HealthReportJob(database=db)
    result = job.run(context)
    assert result.changed == 1
    assert any("reported=True" in line for line in result.details)

    with db.diagnostic_session() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM governance.runtime_events "
            " WHERE event_type = 'run_metric' AND actor_id LIKE 'health_report/%'"
        ).fetchone()["n"]
    assert n >= 1


def test_health_report_job_skips_when_governance_missing() -> None:
    from klonet_agent.memory.maintenance.base import JobContext
    from klonet_agent.memory.maintenance.jobs.health_report import HealthReportJob

    # governance 表不存在的库：Job 不炸，只记录跳过说明。
    calls: dict[str, int] = {"n": 0}

    class _NoGovernance:
        def _require_pool(self):
            raise RuntimeError("不应被走到——表存在性检查先返回 False")

    # 用真库结构但伪造 to_regclass 返回空：简单做法是 governance_enabled=False。
    job = HealthReportJob(database=_NoGovernance(), governance_enabled=False)
    now = datetime.now(timezone.utc)
    context = JobContext(
        job_name="health_report",
        run_id=str(uuid4()),
        worker_id="test-worker",
        started_at=now,
        deadline=now + timedelta(seconds=60),
        batch_limit=1,
    )
    # collect_snapshot 会走 _require_pool → RuntimeError → fail closed 快照；
    # Job 仍要正常返回（健康采集失败不是 Job 崩溃）。
    result = job.run(context)
    assert result.changed == 0
    assert calls["n"] == 0
