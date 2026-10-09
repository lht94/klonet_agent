"""治理层的 PostgreSQL 实现。

与 ``memory/postgres.py`` 同一套基础设施（``MemoryDatabase`` 连接池 +
租户事务），但职责严格分离：治理对象放在独立 ``governance`` schema，
绝不复用记忆表。核心不变量（计划 §3.2）：

    INSERT runtime_event 与 UPSERT 当前投影必须发生在**同一个事务**里；
    事件幂等命中时投影也不得重复应用。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.domain import Tenant
from klonet_agent.runtime.governance.models import (
    event_row as _event_row,
    enum_text,
    FailureRecord,
    ModelCallRecord,
    RunRecord,
    RuntimeEvent,
    StepRecord,
    TaskRecord,
    ToolCallRecord,
    utcnow,
)
from klonet_agent.runtime.governance.evidence_postgres import (
    GovernanceEvidenceMixin,
)
from klonet_agent.runtime.governance.repository import (
    ConcurrentProjectionError,
    GovernanceRepositoryError,
)

_PROJECTION_TABLES = ("tasks", "steps", "failures")


def _uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise GovernanceRepositoryError(f"非法 UUID 字段: {value}") from exc


def _jsonb(value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False, default=str)


class PostgresGovernanceRepository(GovernanceEvidenceMixin):
    """governance schema 的 PostgreSQL 仓储。

    复用 ``MemoryDatabase`` 的连接池与租户绑定：治理与记忆共享一个数据库
    进程、一套 RLS 变量，但 schema 与表完全分开。
    """

    def __init__(self, database: MemoryDatabase, tenant: Tenant) -> None:
        self.database = database
        self.tenant = tenant

    def with_tenant(self, tenant: Tenant) -> "PostgresGovernanceRepository":
        return PostgresGovernanceRepository(self.database, tenant)

    # ---------------------------------------------------------------- 写入 --
    def begin_run(self, run: RunRecord) -> None:
        with self.database.tenant_session(self.tenant) as conn:
            conn.execute(
                """
                INSERT INTO governance.runs
                    (run_id, user_id, project_id, session_id, mode,
                     schema_version, started_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    run.run_id,
                    run.user_id,
                    run.project_id,
                    run.session_id,
                    run.mode,
                    run.schema_version,
                    run.started_at,
                ),
            )

    def append_event(
        self,
        event: RuntimeEvent,
        *,
        task: TaskRecord | None = None,
        step: StepRecord | None = None,
        failure: FailureRecord | None = None,
        expect_task_version: int | None = None,
        expect_step_version: int | None = None,
        close_failure_status: str | None = None,
    ) -> bool:
        row = _event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            inserted = conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, task_id, step_id,
                     parent_event_id, idempotency_key, actor_type, actor_id,
                     payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(task_id)s, %(step_id)s,
                        %(parent_event_id)s, %(idempotency_key)s,
                        %(actor_type)s, %(actor_id)s, %(payload)s,
                        %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                row,
            ).fetchone()
            if inserted is None:
                # 幂等命中：事件已存在，投影不得重复应用。
                return False

            if task is not None:
                self._upsert_task(conn, task, expect_task_version)
            if step is not None:
                self._upsert_step(conn, step, expect_step_version)
            if failure is not None:
                self._upsert_failure(conn, failure, close_failure_status)
        return True

    def record_model_call(self, record: ModelCallRecord, event: RuntimeEvent) -> bool:
        row = _event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            inserted = conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, idempotency_key, actor_type,
                     actor_id, payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(idempotency_key)s,
                        %(actor_type)s, %(actor_id)s, %(payload)s,
                        %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                row,
            ).fetchone()
            if inserted is None:
                return False
            conn.execute(
                """
                INSERT INTO governance.model_calls
                    (call_id, run_id, user_id, project_id, model, outcome,
                     total_tokens, duration_ms, turn_id, error_class, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (call_id) DO NOTHING
                """,
                (
                    record.call_id,
                    record.run_id,
                    record.user_id,
                    record.project_id,
                    record.model,
                    enum_text(record.outcome),
                    record.total_tokens,
                    record.duration_ms,
                    record.turn_id,
                    record.error_class,
                    record.occurred_at,
                ),
            )
        return True

    def record_tool_call(self, record: ToolCallRecord, event: RuntimeEvent) -> bool:
        row = _event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            inserted = conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, idempotency_key, actor_type,
                     actor_id, payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(idempotency_key)s,
                        %(actor_type)s, %(actor_id)s, %(payload)s,
                        %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                row,
            ).fetchone()
            if inserted is None:
                return False
            conn.execute(
                """
                INSERT INTO governance.tool_calls
                    (call_id, run_id, user_id, project_id, tool_name, outcome,
                     duration_ms, args_preview, turn_id, error_class, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (call_id) DO NOTHING
                """,
                (
                    record.call_id,
                    record.run_id,
                    record.user_id,
                    record.project_id,
                    record.tool_name,
                    enum_text(record.outcome),
                    record.duration_ms,
                    _jsonb(record.args_preview),
                    record.turn_id,
                    record.error_class,
                    record.occurred_at,
                ),
            )
        return True

    def append_redactions(self, event_id: str, records: list) -> None:
        if not records:
            return
        with self.database.tenant_session(self.tenant) as conn:
            for item in records:
                conn.execute(
                    """
                    INSERT INTO governance.redactions
                        (event_id, user_id, project_id, rule_id, category,
                         content_hash, privacy_class)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        _uuid(event_id),
                        self.tenant.user_id,
                        self.tenant.project_id,
                        item.rule_id,
                        item.category,
                        item.content_hash,
                        enum_text(item.privacy_class),
                    ),
                )

    def _ensure_turn_row(self, conn: Any, event: RuntimeEvent) -> None:
        """保证 ``turn_id`` 的外键存在：turns 行与事件同事务补齐。

        ``start_turn`` 是 telemetry 级操作，可能在数据库不可用时被缓冲；
        这里按幂等键补齐 turns 行，保证事件外键永远可满足。
        """

        if not event.turn_id:
            return
        conn.execute(
            """
            INSERT INTO governance.turns
                (turn_id, run_id, user_id, project_id, session_id, started_at)
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (turn_id) DO NOTHING
            """,
            (
                event.turn_id,
                event.run_id,
                self.tenant.user_id,
                self.tenant.project_id,
                event.session_id,
                event.occurred_at,
            ),
        )

    # -------------------------------------------------------------- 投影 --
    def _upsert_task(
        self, conn: Any, task: TaskRecord, expect_version: int | None
    ) -> None:
        params = (
            task.task_id,
            task.run_id,
            task.user_id,
            task.project_id,
            task.content,
            enum_text(task.status),
            task.priority,
            task.ordinal,
            task.blocked_reason,
            task.idempotency_key,
        )
        updated = conn.execute(
            """
            UPDATE governance.tasks
               SET content = %s,
                   status = %s,
                   priority = %s,
                   ordinal = %s,
                   blocked_reason = %s,
                   version = version + 1,
                   updated_at = now()
             WHERE task_id = %s
               AND (%s::int IS NULL OR version = %s::int)
            RETURNING version
            """,
            params[4:5] + params[5:9] + (task.task_id, expect_version, expect_version),
        ).fetchone()
        if updated is not None:
            task.version = int(updated["version"])
            return
        try:
            inserted = conn.execute(
                """
                INSERT INTO governance.tasks
                    (task_id, run_id, user_id, project_id, content, status,
                     priority, ordinal, blocked_reason, version, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO NOTHING
                RETURNING version
                """,
                params[:9] + (task.version,) + params[9:],
            ).fetchone()
        except Exception as exc:
            raise self._classify_conflict(exc, "task", task.task_id, expect_version) from exc
        if inserted is None:
            # 并发插入抢先用掉了：走一次带版本校验的更新。
            refreshed = conn.execute(
                """
                UPDATE governance.tasks
                   SET content = %s, status = %s, priority = %s, ordinal = %s,
                       blocked_reason = %s, version = version + 1, updated_at = now()
                 WHERE task_id = %s
                   AND (%s::int IS NULL OR version = %s::int)
                RETURNING version
                """,
                params[4:5] + params[5:9] + (task.task_id, expect_version, expect_version),
            ).fetchone()
            if refreshed is None:
                raise ConcurrentProjectionError("task", task.task_id, expect_version or -1)
            task.version = int(refreshed["version"])
            return
        task.version = int(inserted["version"])

    def _upsert_step(
        self, conn: Any, step: StepRecord, expect_version: int | None
    ) -> None:
        updated = conn.execute(
            """
            UPDATE governance.steps
               SET content = %s, status = %s, reason_code = %s,
                   version = version + 1, updated_at = now()
             WHERE step_id = %s
               AND (%s::int IS NULL OR version = %s::int)
            RETURNING version
            """,
            (
                step.content,
                enum_text(step.status),
                step.reason_code,
                step.step_id,
                expect_version,
                expect_version,
            ),
        ).fetchone()
        if updated is not None:
            step.version = int(updated["version"])
            return
        try:
            inserted = conn.execute(
                """
                INSERT INTO governance.steps
                    (step_id, task_id, run_id, user_id, project_id, content,
                     status, reason_code, version, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (step_id) DO NOTHING
                RETURNING version
                """,
                (
                    step.step_id,
                    step.task_id,
                    step.run_id,
                    step.user_id,
                    step.project_id,
                    step.content,
                    enum_text(step.status),
                    step.reason_code,
                    step.version,
                    step.idempotency_key,
                ),
            ).fetchone()
        except Exception as exc:
            raise self._classify_conflict(exc, "step", step.step_id, expect_version) from exc
        if inserted is None:
            raise ConcurrentProjectionError("step", step.step_id, expect_version or -1)
        step.version = int(inserted["version"])

    def _upsert_failure(
        self, conn: Any, failure: FailureRecord, close_status: str | None
    ) -> None:
        status = enum_text(close_status or failure.status)
        conn.execute(
            """
            INSERT INTO governance.failures
                (failure_id, run_id, user_id, project_id, stage, error_class,
                 message, retryable, status, attempt_count, task_id, step_id,
                 turn_id, root_cause_hypothesis, resolution_action,
                 verification_evidence_ids, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s)
            ON CONFLICT (failure_id) DO UPDATE
                 SET status = EXCLUDED.status,
                     attempt_count = EXCLUDED.attempt_count,
                     resolution_action = EXCLUDED.resolution_action,
                     root_cause_hypothesis = EXCLUDED.root_cause_hypothesis,
                     verification_evidence_ids = EXCLUDED.verification_evidence_ids,
                     updated_at = now()
            """,
            (
                failure.failure_id,
                failure.run_id,
                failure.user_id,
                failure.project_id,
                failure.stage,
                failure.error_class,
                failure.message,
                failure.retryable,
                status,
                failure.attempt_count,
                failure.task_id,
                failure.step_id,
                failure.turn_id,
                failure.root_cause_hypothesis,
                failure.resolution_action,
                _jsonb(failure.verification_evidence_ids),
                failure.idempotency_key,
            ),
        )
        failure.status = status

    @staticmethod
    def _classify_conflict(
        exc: Exception, kind: str, record_id: str, expect_version: int | None
    ) -> Exception:
        message = str(exc)
        if "failures_active" in message or "duplicate key" in message:
            return GovernanceRepositoryError(
                f"{kind} {record_id} 违反唯一约束（活跃记录已存在或幂等键冲突）: {exc}"
            )
        return exc

    # ---------------------------------------------------------------- 查询 --
    def get_task(self, task_id: str) -> TaskRecord | None:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM governance.tasks WHERE task_id = %s", (task_id,)
            ).fetchone()
        return self._task_from_row(row) if row else None

    def find_task_by_idempotency_key(self, key: str) -> TaskRecord | None:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM governance.tasks WHERE idempotency_key = %s", (key,)
            ).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(self, run_id: str) -> list[TaskRecord]:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM governance.tasks WHERE run_id = %s ORDER BY ordinal, created_at",
                (run_id,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_failure(self, failure_id: str) -> FailureRecord | None:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM governance.failures WHERE failure_id = %s",
                (failure_id,),
            ).fetchone()
        return self._failure_from_row(row) if row else None

    def list_active_failures(self, run_id: str) -> list[FailureRecord]:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM governance.failures WHERE run_id = %s AND status = 'active'",
                (run_id,),
            ).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def list_events(self, run_id: str, limit: int = 500) -> list[RuntimeEvent]:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM governance.runtime_events
                 WHERE run_id = %s
                 ORDER BY occurred_at, recorded_at
                 LIMIT %s
                """,
                (run_id, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def count_events(self, run_id: str) -> int:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM governance.runtime_events WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- 行映射 --
    @staticmethod
    def _task_from_row(row: Any) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            run_id=row["run_id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            content=row["content"],
            status=row["status"],
            priority=row["priority"],
            ordinal=row["ordinal"],
            blocked_reason=row["blocked_reason"],
            version=row["version"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _failure_from_row(row: Any) -> FailureRecord:
        return FailureRecord(
            failure_id=row["failure_id"],
            run_id=row["run_id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            stage=row["stage"],
            error_class=row["error_class"],
            message=row["message"],
            retryable=row["retryable"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            task_id=row["task_id"],
            step_id=row["step_id"],
            turn_id=row["turn_id"],
            root_cause_hypothesis=row["root_cause_hypothesis"],
            resolution_action=row["resolution_action"],
            verification_evidence_ids=list(row["verification_evidence_ids"] or []),
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: Any) -> RuntimeEvent:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return RuntimeEvent(
            event_id=str(row["event_id"]),
            event_type=row["event_type"],
            run_id=row["run_id"],
            actor_type=row["actor_type"],
            actor_id=row["actor_id"],
            payload=payload,
            reason_code=payload.get("reason_code") if isinstance(payload, dict) else None,
            tenant_user_id=row["user_id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            task_id=row["task_id"],
            step_id=row["step_id"],
            idempotency_key=row["idempotency_key"],
            privacy_class=row["privacy_class"],
            occurred_at=row["occurred_at"],
            schema_version=row["schema_version"],
        )
