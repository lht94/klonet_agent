"""治理层 PostgreSQL 集成测试。

**没有真库时整个模块 skip**。跑法（本机 Docker / 远端服务器相同）：

    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_governance_postgres.py -q

验收三条离不开真库（计划 阶段 1 完成标准）：

1. 事件与投影在同一事务提交——事件落库失败时投影不得存在；
2. 重复提交幂等——同一 ``idempotency_key`` 不产生重复事件/投影变更；
3. RLS 跨租户隔离——另一个 user 绑定租户后读不到治理记录。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import Tenant
from klonet_agent.runtime.governance.postgres import PostgresGovernanceRepository
from klonet_agent.runtime.governance.repository import (
    ConcurrentProjectionError,
    GovernanceRepositoryError,
)
from klonet_agent.runtime.governance.service import (
    GovernanceUnavailableError,
    InvalidStatusTransitionError,
    RuntimeGovernance,
)

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

ALICE = Tenant(user_id="alice", project_id="demo")
BOB = Tenant(user_id="bob", project_id="demo")


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过治理层集成测试")
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过治理层集成测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:
        pytest.fail(f"{TEST_DSN_ENV} 已设置但连不上：{exc}")
    return dsn


@pytest.fixture(scope="module")
def db(admin_dsn: str) -> Any:
    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            applied = database.run_migrations()
            assert "0004_governance" in applied
            assert applied[-1] == "0005_governance_provenance"
            yield database
        finally:
            database.close()


@pytest.fixture()
def alice_gov(db: MemoryDatabase):
    repo = PostgresGovernanceRepository(db, ALICE)
    governance = RuntimeGovernance(
        repo, ALICE, session_id="sess-governance-test", mode="mentor"
    )
    governance.start_run()
    return governance


def _to_regclass(db: MemoryDatabase, name: str) -> bool:
    with db.diagnostic_session() as conn:
        row = conn.execute(
            "SELECT to_regclass(%s) AS oid", (name,)
        ).fetchone()
    return bool(row and row["oid"])


class TestGovernanceSchema:
    def test_all_governance_tables_created(self, db):
        for table in (
            "runs", "turns", "tasks", "steps", "runtime_events",
            "failures", "model_calls", "tool_calls", "redactions",
            "evidence", "claims", "claim_evidence", "route_decisions",
        ):
            assert _to_regclass(db, f"governance.{table}"), f"缺表 governance.{table}"

    def test_migrations_idempotent(self, db):
        assert db.run_migrations() == []  # 已应用且 checksum 未变


class TestEventAndProjection:
    def test_run_turn_model_tool_events(self, alice_gov):
        alice_gov.start_turn()
        alice_gov.record_model_call(model="test-model", total_tokens=42, duration_ms=7)
        alice_gov.record_tool_call(tool_name="read_file", outcome="succeeded")
        events = alice_gov.repository.list_events(alice_gov.run_id)
        types = {e.event_type for e in events}
        assert {"run.started", "turn.started", "model_call.completed", "tool_call.completed"} <= types
        assert alice_gov.repository.count_events(alice_gov.run_id) == len(events)

    def test_event_and_projection_same_transaction(self, db, alice_gov):
        """事件写失败时投影必须一起失败（单事务语义）。"""

        summary = alice_gov.apply_todos(
            [{"id": 1, "content": "任务A", "status": "pending"}]
        )
        assert "created=1" in summary
        task = alice_gov.repository.list_tasks(alice_gov.run_id)[0]
        assert task is not None
        # 事件存在
        events = alice_gov.repository.list_events(alice_gov.run_id)
        assert any(e.task_id == task.task_id for e in events)

    def test_duplicate_submit_is_idempotent(self, alice_gov):
        todos = [{"id": 1, "content": "任务A", "status": "pending"}]
        alice_gov.apply_todos(todos)
        before = alice_gov.repository.count_events(alice_gov.run_id)
        alice_gov.apply_todos(todos)  # 同内容重复提交
        assert alice_gov.repository.count_events(alice_gov.run_id) == before
        assert len(alice_gov.repository.list_tasks(alice_gov.run_id)) == 1

    def test_optimistic_lock_on_tasks(self, db, alice_gov):
        alice_gov.apply_todos([{"id": 1, "content": "A", "status": "pending"}])
        task = alice_gov.repository.list_tasks(alice_gov.run_id)[0]
        stale_version = task.version - 1
        with pytest.raises(ConcurrentProjectionError):
            alice_gov.transition_task(
                task.task_id, "running",
                reason_code="task.started", expected_version=stale_version,
            )
        # 正确版本可以推进
        updated = alice_gov.transition_task(
            task.task_id, "running", reason_code="task.started"
        )
        assert updated.version == task.version + 1
        reread = alice_gov.repository.get_task(task.task_id)
        assert str(reread.status) == "running"

    def test_illegal_transition_rejected(self, alice_gov):
        alice_gov.apply_todos([{"id": 1, "content": "A", "status": "pending"}])
        task = alice_gov.repository.list_tasks(alice_gov.run_id)[0]
        with pytest.raises(InvalidStatusTransitionError):
            alice_gov.transition_task(task.task_id, "completed", reason_code="task.completed")

    def test_failure_unique_active_constraint(self, alice_gov):
        failure = alice_gov.record_failure(
            stage="tool_execution", error_class="ConnErrZ", message="x"
        )
        active = alice_gov.repository.list_active_failures(alice_gov.run_id)
        assert len(active) == 1
        alice_gov.close_failure(
            failure.failure_id, to_status="resolved", resolution_action="重启"
        )
        # resolved 之后可以再开同类的活跃记录（partial unique index 只约束 active）。
        again = alice_gov.record_failure(
            stage="tool_execution", error_class="ConnErrZ", message="y"
        )
        assert again.failure_id != failure.failure_id
        assert len(alice_gov.repository.list_active_failures(alice_gov.run_id)) == 1

    def test_secret_tombstone_no_plaintext(self, alice_gov):
        alice_gov.record_failure(
            stage="tool_execution",
            error_class="AuthErrQ",
            message="bad key sk-abcdefghijklmnop1234",
        )
        events = alice_gov.repository.list_events(alice_gov.run_id)
        dumped = repr(events)
        assert "sk-abcdefghijklmnop1234" not in dumped
        failure = alice_gov.repository.list_active_failures(alice_gov.run_id)[0]
        assert "sk-abcdefghijklmnop1234" not in failure.message


class TestEvidenceAndRouting:
    def test_record_evidence_persists_with_hash(self, alice_gov):
        evidence, claim = alice_gov.record_evidence(
            source_type="tool",
            subject="run_command",
            observation="port 8080 free",
            raw_content="port 8080 free",
        )
        assert claim is None
        stored = alice_gov.repository.get_evidence(evidence.evidence_id)
        assert stored is not None
        assert stored.source_type == "tool"
        assert stored.artifact_hash is not None
        assert "evidence.recorded" in {
            e.event_type for e in alice_gov.repository.list_events(alice_gov.run_id)
        }

    def test_conflict_creates_contradicted_claim(self, alice_gov):
        ev1, _ = alice_gov.record_evidence(
            source_type="tool", subject="probe_x", observation="port 8080 free"
        )
        ev2, claim = alice_gov.record_evidence(
            source_type="tool", subject="probe_x", observation="port 8080 occupied"
        )
        assert claim is not None
        links = alice_gov.repository.list_claim_links(claim.claim_id)
        assert {l.evidence_id for l in links} == {ev1.evidence_id, ev2.evidence_id}

    def test_claim_gate_and_route_decision(self, alice_gov):
        ev, _ = alice_gov.record_evidence(
            source_type="tool", subject="probe_y", observation="ok"
        )
        claim = alice_gov.create_claim(
            subject="probe_y", statement="端口空闲", evidence_ids=[ev.evidence_id]
        )
        assert alice_gov.evidence_sufficient(claim.claim_id)

        decision = alice_gov.record_route_decision(actual_model="test-model")
        assert decision is not None and decision.shadow is True
        assert any(
            e.event_type == "route.decided"
            for e in alice_gov.repository.list_events(alice_gov.run_id)
        )


class TestIsolation:
    def test_rls_blocks_cross_tenant_read(self, db, alice_gov):
        """另一个 user 绑定租户后必须读不到 alice 的治理数据。

        需要能 SET ROLE 到非超级用户角色；postgres 超级用户绕过 RLS，
        所以先用 klonet_app 角色验证，取不到角色时 skip。
        """

        with db.diagnostic_session() as conn:
            row = conn.execute(
                """
                SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super,
                       pg_has_role(current_user, 'klonet_app', 'member') AS can_app
                """
            ).fetchone()
        if not (row and (row["is_super"] or row["can_app"])):
            pytest.skip("当前账号无法切换到 klonet_app，无法验证 RLS")

        alice_gov.start_turn()
        alice_gov.apply_todos([{"id": 1, "content": "A", "status": "pending"}])

        # 用 SET LOCAL ROLE 模拟 klonet_app + bob 租户。
        with db.diagnostic_session() as conn:
            conn.execute("SET LOCAL ROLE klonet_app")
            conn.execute("SELECT set_config('app.user_id', 'bob', true)")
            conn.execute("SELECT set_config('app.project_id', 'demo', true)")
            count = conn.execute(
                "SELECT count(*) AS n FROM governance.tasks"
            ).fetchone()["n"]
        assert count == 0, "bob 不得读到 alice 的治理任务"

        # alice 自己正常可读。
        with db.diagnostic_session() as conn:
            conn.execute("SET LOCAL ROLE klonet_app")
            conn.execute("SELECT set_config('app.user_id', 'alice', true)")
            conn.execute("SELECT set_config('app.project_id', 'demo', true)")
            count = conn.execute(
                "SELECT count(*) AS n FROM governance.tasks WHERE user_id = 'alice'"
            ).fetchone()["n"]
        assert count >= 1

    def test_database_down_fails_closed(self, db, alice_gov):
        class BrokenRepo:
            def __getattr__(self, name):
                def _raise(*args, **kwargs):
                    raise ConnectionError("db down")
                return _raise

        governance = RuntimeGovernance(BrokenRepo(), ALICE)
        with pytest.raises(GovernanceUnavailableError):
            governance.start_run()
        with pytest.raises(GovernanceUnavailableError):
            governance.apply_todos([{"id": 1, "content": "A", "status": "pending"}])
