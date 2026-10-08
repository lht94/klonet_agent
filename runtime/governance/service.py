"""RuntimeGovernance：治理层的事务边界与状态转换服务。

计划 §3.3 的不变量在这里集中执行：

1. 事件与投影同事务（由 repository 实现保证）；
2. 状态改变必须留事件——所有写路径都先构造 ``RuntimeEvent`` 再落投影；
3. 活跃失败只能被「解决事件 + 验证证据」关闭；
4. secret 先被隐私网关拒绝，再谈持久化；
5. 数据库不可用时的降级规则（计划 阶段 1）：telemetry（模型/工具调用的
   观察记录）允许缓冲重试；**任务状态改变与失败关闭必须 fail closed**，
   直接抛 :class:`GovernanceUnavailableError`，绝不允许"只改内存对象"。
"""

from __future__ import annotations

from typing import Any, Iterable

from klonet_agent.memory.domain import Tenant
from klonet_agent.runtime.governance.models import (
    enum_text,
    ActorType,
    FailureLessonCandidate,
    FailureRecord,
    FailureStatus,
    InvalidStatusTransitionError,
    ModelCallRecord,
    PrivacyClass,
    REASON_FAILURE_DISMISSED,
    REASON_FAILURE_OPENED,
    REASON_FAILURE_RESOLVED,
    REASON_FAILURE_VERIFIED,
    REASON_MODEL_CALL_COMPLETED,
    REASON_MODEL_CALL_FAILED,
    REASON_RUN_STARTED,
    REASON_TASK_BLOCKED,
    REASON_TASK_CANCELLED,
    REASON_TASK_COMPLETED,
    REASON_TASK_CREATED,
    REASON_TASK_STARTED,
    REASON_TOOL_CALL_COMPLETED,
    REASON_TOOL_CALL_FAILED,
    REASON_TURN_STARTED,
    RunRecord,
    RuntimeEvent,
    TaskRecord,
    TaskStatus,
    ToolCallRecord,
    ToolOutcome,
    ensure_task_transition,
    new_id,
    utcnow,
)
from klonet_agent.runtime.governance.privacy import (
    PrivacyGateway,
    SecretRejectedError,
    default_gateway,
)
from klonet_agent.runtime.governance.repository import (
    ConcurrentProjectionError,
    GovernanceRepository,
    GovernanceRepositoryError,
)

# todos 状态 → 任务状态机的映射（AgentSession.VALID_STATUS 的兼容视图）。
_TODO_STATUS_TO_TASK = {
    "pending": TaskStatus.PENDING,
    "in_progress": TaskStatus.RUNNING,
    "completed": TaskStatus.COMPLETED,
    "waiting_user": TaskStatus.BLOCKED,
    "blocked": TaskStatus.BLOCKED,
}


class GovernanceUnavailableError(RuntimeError):
    """治理存储不可用。状态改变类操作在此异常面前必须 fail closed。"""


class RuntimeGovernance:
    """一次 Agent 运行的治理边界。

    生命周期：``start_run``（进程启动一次）→ ``start_turn``（每轮用户输入）
    → 模型/工具/任务/失败记录 → 下一个 turn。
    """

    def __init__(
        self,
        repository: GovernanceRepository,
        tenant: Tenant,
        *,
        privacy: PrivacyGateway | None = None,
        exporters: Iterable = (),
        session_id: str | None = None,
        mode: str | None = None,
        telemetry_buffer_limit: int = 200,
    ):
        self.repository = repository
        self.tenant = tenant
        self.privacy = privacy or default_gateway()
        self.exporters = list(exporters)
        self.session_id = session_id
        self.mode = mode
        self._telemetry_buffer: list[tuple[Any, RuntimeEvent]] = []
        self._telemetry_buffer_limit = telemetry_buffer_limit
        self._run: RunRecord | None = None
        self._turn_id: str | None = None
        self._lesson_candidates: list[FailureLessonCandidate] = []

    # ------------------------------------------------------------ 生命周期 --
    @property
    def run_id(self) -> str | None:
        return self._run.run_id if self._run else None

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    @property
    def lesson_candidates(self) -> list[FailureLessonCandidate]:
        return list(self._lesson_candidates)

    def start_run(self, *, run_id: str | None = None) -> RunRecord:
        """开始（或接续）一次运行。幂等：同一 run_id 不会重复落库。"""

        run = RunRecord(
            run_id=run_id or new_id(),
            user_id=self.tenant.user_id,
            project_id=self.tenant.project_id,
            session_id=self.session_id,
            mode=self.mode,
        )
        try:
            self.repository.begin_run(run)
        except Exception as exc:
            raise GovernanceUnavailableError(
                f"治理层无法登记运行（fail closed）: {exc}"
            ) from exc
        self._run = run
        event = self._event(
            event_type="run.started",
            reason_code=REASON_RUN_STARTED,
            payload={"mode": self.mode, "session_id": self.session_id},
            idempotency_key=f"run:{run.run_id}",
        )
        # run 事件属于治理链路的第一步：失败即 fail closed，绝不允许
        # "登记了运行却没有事件"的半套状态存在。
        try:
            self.repository.append_event(event)
        except Exception as exc:
            self._run = None
            raise GovernanceUnavailableError(
                f"治理层无法写入运行事件（fail closed）: {exc}"
            ) from exc
        self._export(event)
        return run

    def start_turn(self) -> str:
        """开始新一轮用户输入处理，返回 turn_id。"""

        if self._run is None:
            self.start_run()
        assert self._run is not None
        self._turn_id = new_id()
        event = self._event(
            event_type="turn.started",
            reason_code=REASON_TURN_STARTED,
            payload={},
            turn_id=self._turn_id,
        )
        self._persist_telemetry(event, None)
        return self._turn_id

    # ------------------------------------------------------------ 模型调用 --
    def record_model_call(
        self,
        *,
        model: str,
        total_tokens: int = 0,
        duration_ms: int = 0,
        succeeded: bool = True,
        error_class: str | None = None,
    ) -> None:
        """记录一次模型调用（telemetry 级：不可用时缓冲，不阻断主链路）。"""

        if self._run is None:
            return
        record = ModelCallRecord(
            call_id=new_id(),
            run_id=self._run.run_id,
            user_id=self.tenant.user_id,
            project_id=self.tenant.project_id,
            model=model,
            outcome="succeeded" if succeeded else "failed",
            total_tokens=max(0, int(total_tokens)),
            duration_ms=max(0, int(duration_ms)),
            turn_id=self._turn_id,
            error_class=error_class,
        )
        event = self._event(
            event_type="model_call.completed"
            if succeeded
            else "model_call.failed",
            reason_code=REASON_MODEL_CALL_COMPLETED
            if succeeded
            else REASON_MODEL_CALL_FAILED,
            payload={
                "model": model,
                "total_tokens": record.total_tokens,
                "duration_ms": record.duration_ms,
                "outcome": record.outcome,
                "error_class": error_class,
            },
            turn_id=self._turn_id,
            idempotency_key=f"model_call:{record.call_id}",
        )
        self._persist_telemetry(event, lambda: self.repository.record_model_call(record, event))

    # ------------------------------------------------------------ 工具调用 --
    def record_tool_call(
        self,
        *,
        tool_name: str,
        duration_ms: int = 0,
        outcome: ToolOutcome | str = ToolOutcome.SUCCEEDED,
        args_preview: dict | None = None,
        error_class: str | None = None,
    ) -> None:
        """记录一次工具调用。``outcome_unknown`` 必须显式给出。"""

        if self._run is None:
            return
        record = ToolCallRecord(
            call_id=new_id(),
            run_id=self._run.run_id,
            user_id=self.tenant.user_id,
            project_id=self.tenant.project_id,
            tool_name=tool_name,
            outcome=outcome,
            duration_ms=max(0, int(duration_ms)),
            args_preview=self._redacted_preview(args_preview),
            turn_id=self._turn_id,
            error_class=error_class,
        )
        succeeded = enum_text(outcome) == enum_text(ToolOutcome.SUCCEEDED)
        event = self._event(
            event_type="tool_call.completed" if succeeded else "tool_call.failed",
            reason_code=REASON_TOOL_CALL_COMPLETED if succeeded else REASON_TOOL_CALL_FAILED,
            payload={
                "tool_name": tool_name,
                "outcome": enum_text(outcome),
                "duration_ms": record.duration_ms,
                "error_class": error_class,
            },
            turn_id=self._turn_id,
            idempotency_key=f"tool_call:{record.call_id}",
        )
        self._persist_telemetry(event, lambda: self.repository.record_tool_call(record, event))

    def _redacted_preview(self, preview: dict | None) -> dict:
        """参数预览脱敏：投影与事件都不允许出现明文秘密。"""

        safe: dict = {}
        for key, value in dict(preview or {}).items():
            if isinstance(value, str):
                sanitized, _ = self.privacy.redact(value)
                safe[str(key)] = sanitized[:300]
            elif isinstance(value, (int, float, bool)) or value is None:
                safe[str(key)] = value
            else:
                safe[str(key)] = f"<{type(value).__name__}>"
        return safe

    # ------------------------------------------------------------ 任务状态 --
    def apply_todos(self, todos: list[dict]) -> str:
        """把 ``AgentSession.todos`` 镜像为权威任务投影（fail closed）。

        返回给调用方一段摘要。任何投影失败（存储不可用、非法状态转换、
        乐观锁冲突）都会抛异常——调用方不得在异常后继续修改内存 todos。
        """

        if self._run is None:
            self.start_run()
        assert self._run is not None
        created = 0
        for ordinal, todo in enumerate(todos, start=1):
            content = str(todo.get("content") or "").strip()
            if not content:
                continue
            todo_status = _TODO_STATUS_TO_TASK.get(
                str(todo.get("status", "pending")), TaskStatus.PENDING
            )
            key = f"{self._run.run_id}:todo:{todo.get('id', ordinal)}"
            existing = self._find_task_by_key(key)
            if existing is None:
                safe_content, _ = self.privacy.redact(content)
                task = TaskRecord(
                    task_id=new_id(),
                    run_id=self._run.run_id,
                    user_id=self.tenant.user_id,
                    project_id=self.tenant.project_id,
                    content=safe_content,
                    status=todo_status,
                    ordinal=ordinal,
                    idempotency_key=key,
                    blocked_reason="waiting_user"
                    if str(todo.get("status")) == "waiting_user"
                    else None,
                )
                event = self._event(
                    event_type="task.created",
                    reason_code=REASON_TASK_CREATED,
                    payload={"content": safe_content, "status": enum_text(todo_status)},
                    task_id=task.task_id,
                    idempotency_key=f"{key}:create",
                )
                self._persist_state(event, task=task)
                created += 1
                continue

            # 已存在：按状态机走转换。非法转换直接抛错。
            current = TaskStatus(existing.status)
            if current == todo_status:
                continue
            if current is TaskStatus.PENDING and todo_status is TaskStatus.RUNNING:
                reason = REASON_TASK_STARTED
            elif todo_status is TaskStatus.BLOCKED:
                reason = REASON_TASK_BLOCKED
            elif todo_status is TaskStatus.COMPLETED:
                reason = REASON_TASK_COMPLETED
            elif todo_status is TaskStatus.CANCELLED:
                reason = REASON_TASK_CANCELLED
            else:
                reason = REASON_TASK_STARTED
            self.transition_task(
                existing.task_id,
                todo_status,
                reason_code=reason,
                expected_version=existing.version,
            )
        return f"governance tasks synced: total={len(todos)}, created={created}"

    def transition_task(
        self,
        task_id: str,
        to_status: TaskStatus | str,
        *,
        reason_code: str,
        blocked_reason: str | None = None,
        expected_version: int | None = None,
    ) -> TaskRecord:
        """一次受约束的任务状态转换（事件 + 投影同事务，乐观锁）。"""

        if self._run is None:
            raise GovernanceUnavailableError("治理层尚未开始运行（start_run）")
        current = self._get_task(task_id)
        if current is None:
            raise GovernanceRepositoryError(f"任务不存在: {task_id}")
        ensure_task_transition(current.status, to_status)
        updated = TaskRecord(
            task_id=current.task_id,
            run_id=current.run_id,
            user_id=current.user_id,
            project_id=current.project_id,
            content=current.content,
            status=to_status,
            priority=current.priority,
            ordinal=current.ordinal,
            blocked_reason=blocked_reason or current.blocked_reason,
            version=current.version,
            idempotency_key=current.idempotency_key,
        )
        event = self._event(
            event_type="task.status_changed",
            reason_code=reason_code,
            payload={
                "from_status": enum_text(current.status),
                "to_status": enum_text(to_status),
                "reason_code": reason_code,
            },
            task_id=task_id,
        )
        self._persist_state(
            event,
            task=updated,
            expect_task_version=expected_version
            if expected_version is not None
            else current.version,
        )
        return updated

    # ---------------------------------------------------------------- 失败 --
    def record_failure(
        self,
        *,
        stage: str,
        error_class: str,
        message: str = "",
        retryable: bool = False,
        task_id: str | None = None,
        step_id: str | None = None,
    ) -> FailureRecord:
        """打开（或累计）一个活跃失败记录。"""

        if self._run is None:
            self.start_run()
        assert self._run is not None
        scope = step_id or task_id or "run"
        base_key = f"{self._run.run_id}:failure:{scope}:{error_class}"
        # 投影字段也必须过网关：账本有 tombstone 兜底，但 failures 表里
        # 不能留明文（计划 §4.6：一处执行，全链路生效）。
        safe_message, _ = self.privacy.redact(message)

        # 同一作用域同类失败已活跃 → 复用记录并累计尝试次数（计划 §4.2：
        # "同一步骤同类失败只存在一个活跃记录"）。
        active = self._find_active_failure(error_class, task_id, step_id)
        if active is not None:
            active.attempt_count = int(active.attempt_count) + 1
            event = self._event(
                event_type="failure.reopened",
                reason_code=REASON_FAILURE_OPENED,
                payload={
                    "stage": stage,
                    "error_class": error_class,
                    "attempt_count": active.attempt_count,
                    "message_preview": message[:300],
                },
                task_id=task_id,
                step_id=step_id,
                turn_id=self._turn_id,
                idempotency_key=f"{base_key}:reopen:{active.failure_id}:{active.attempt_count}",
            )
            self._persist_state(event, failure=active)
            return active

        failure_id = new_id()
        failure = FailureRecord(
            failure_id=failure_id,
            run_id=self._run.run_id,
            user_id=self.tenant.user_id,
            project_id=self.tenant.project_id,
            stage=stage,
            error_class=error_class,
            message=safe_message,
            retryable=retryable,
            task_id=task_id,
            step_id=step_id,
            turn_id=self._turn_id,
            idempotency_key=f"{base_key}:{failure_id}",
        )
        event = self._event(
            event_type="failure.opened",
            reason_code=REASON_FAILURE_OPENED,
            payload={
                "stage": stage,
                "error_class": error_class,
                "retryable": retryable,
                "message_preview": message[:300],
            },
            task_id=task_id,
            step_id=step_id,
            turn_id=self._turn_id,
            idempotency_key=f"{base_key}:open:{failure_id}",
        )
        self._persist_state(event, failure=failure)
        return failure

    def close_failure(
        self,
        failure_id: str,
        *,
        to_status: FailureStatus | str,
        resolution_action: str | None = None,
        verification_evidence_ids: list[str] | None = None,
        reason: str | None = None,
    ) -> FailureRecord:
        """关闭一个失败：resolved 需要解决动作；verified 还需要验证证据。"""

        current = self._get_failure(failure_id)
        if current is None:
            raise GovernanceRepositoryError(f"失败记录不存在: {failure_id}")
        target = FailureStatus(to_status)
        evidence = list(verification_evidence_ids or [])
        if target is FailureStatus.VERIFIED and not evidence:
            raise InvalidStatusTransitionError(
                "失败", enum_text(current.status),
                "verified 需要至少一条验证证据（计划 §3.3-4）",
            )
        if target is FailureStatus.RESOLVED and not (resolution_action or "").strip():
            raise InvalidStatusTransitionError(
                "失败", enum_text(current.status),
                "resolved 需要明确的解决动作",
            )
        closed = FailureRecord(
            failure_id=current.failure_id,
            run_id=current.run_id,
            user_id=current.user_id,
            project_id=current.project_id,
            stage=current.stage,
            error_class=current.error_class,
            message=current.message,
            retryable=current.retryable,
            status=target,
            attempt_count=current.attempt_count,
            task_id=current.task_id,
            step_id=current.step_id,
            turn_id=current.turn_id,
            root_cause_hypothesis=current.root_cause_hypothesis,
            resolution_action=resolution_action or current.resolution_action,
            verification_evidence_ids=evidence or current.verification_evidence_ids,
            idempotency_key=current.idempotency_key,
        )
        reason_code = {
            FailureStatus.RESOLVED: REASON_FAILURE_RESOLVED,
            FailureStatus.VERIFIED: REASON_FAILURE_VERIFIED,
            FailureStatus.DISMISSED: REASON_FAILURE_DISMISSED,
        }.get(target, REASON_FAILURE_OPENED)
        event = self._event(
            event_type="failure.closed",
            reason_code=reason_code,
            payload={
                "to_status": enum_text(target),
                "reason_code": reason_code,
                "evidence_count": len(closed.verification_evidence_ids),
            },
            task_id=current.task_id,
            step_id=current.step_id,
            turn_id=current.turn_id,
        )
        self._persist_state(event, failure=closed)
        if target is FailureStatus.VERIFIED:
            self._lesson_candidates.append(
                FailureLessonCandidate(
                    candidate_id=new_id(),
                    failure_id=failure_id,
                    lesson=self._lesson_text(closed),
                    error_class=closed.error_class,
                    scope_user_id=closed.user_id,
                    scope_project_id=closed.project_id,
                )
            )
        return closed

    @staticmethod
    def _lesson_text(failure: FailureRecord) -> str:
        action = failure.resolution_action or "（未记录解决动作）"
        return (
            f"错误类别 {failure.error_class} 在阶段 {failure.stage} 已验证修复：{action}。"
            f"根因假设：{failure.root_cause_hypothesis or '未记录'}。"
        )

    # ---------------------------------------------------------------- 内部 --
    def _event(
        self,
        *,
        event_type: str,
        reason_code: str | None,
        payload: dict,
        turn_id: str | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            event_id=new_id(),
            event_type=event_type,
            run_id=self._run.run_id if self._run else "unattached",
            actor_type=ActorType.SYSTEM,
            actor_id="klonet-agent",
            payload={**payload, "reason_code": reason_code},
            reason_code=reason_code,
            tenant_user_id=self.tenant.user_id,
            project_id=self.tenant.project_id,
            session_id=self.session_id,
            turn_id=turn_id or self._turn_id,
            task_id=task_id,
            step_id=step_id,
            idempotency_key=idempotency_key,
            privacy_class=PrivacyClass.INTERNAL,
            occurred_at=utcnow(),
        )

    def _privacy_filtered(self, event: RuntimeEvent):
        """过隐私网关；secret 命中时降级为不含原文的 tombstone 事件。"""

        try:
            sanitized, records = self.privacy.admit_payload(dict(event.payload))
        except SecretRejectedError:
            tombstone = {
                "reason_code": event.reason_code,
                "rejected": "secret_detected",
                "privacy_rules_version": self.privacy.rules_version,
            }
            event.payload = tombstone
            event.privacy_class = PrivacyClass.SENSITIVE
            return event, []
        event.payload = sanitized
        return event, records

    def _persist_state(
        self,
        event: RuntimeEvent,
        *,
        task: TaskRecord | None = None,
        step=None,
        failure: FailureRecord | None = None,
        expect_task_version: int | None = None,
        expect_step_version: int | None = None,
    ) -> None:
        """状态改变类写入：fail closed，任何存储故障都向上抛。"""

        event, redactions = self._privacy_filtered(event)
        try:
            self.repository.append_event(
                event,
                task=task,
                step=step,
                failure=failure,
                expect_task_version=expect_task_version,
                expect_step_version=expect_step_version,
            )
            if redactions:
                self.repository.append_redactions(event.event_id, redactions)
        except (InvalidStatusTransitionError, ConcurrentProjectionError):
            raise
        except Exception as exc:
            raise GovernanceUnavailableError(
                f"治理存储不可用，状态改变被拒绝（fail closed）: {exc}"
            ) from exc
        self._export(event)

    def _persist_telemetry(self, event: RuntimeEvent, writer) -> None:
        """telemetry 级写入：不可用时缓冲，恢复后按幂等键补投。"""

        event, redactions = self._privacy_filtered(event)

        def _write() -> None:
            ok = writer() if writer else self.repository.append_event(event)
            if ok and redactions:
                self.repository.append_redactions(event.event_id, redactions)

        try:
            _write()
        except Exception:
            self._buffer(event, _write)
        self._export(event)

    def _buffer(self, event: RuntimeEvent, replay) -> None:
        if len(self._telemetry_buffer) >= self._telemetry_buffer_limit:
            self._telemetry_buffer.pop(0)
        self._telemetry_buffer.append((event, replay))

    def flush_telemetry(self) -> int:
        """尝试补投缓冲的 telemetry 事件，返回成功条数。"""

        remaining: list[tuple[Any, Any]] = []
        flushed = 0
        for event, replay in self._telemetry_buffer:
            try:
                replay()
                flushed += 1
            except Exception:
                remaining.append((event, replay))
        self._telemetry_buffer = remaining
        return flushed

    def _export(self, event: RuntimeEvent) -> None:
        for exporter in self.exporters:
            try:
                exporter.export(event)
            except Exception:
                # 导出永不反向影响权威链路（计划 §4.4：JSONL 只是导出）。
                pass

    # ------------------------------------------------------------ 查询转发 --
    def _get_task(self, task_id: str) -> TaskRecord | None:
        try:
            return self.repository.get_task(task_id)
        except Exception as exc:
            raise GovernanceUnavailableError(f"治理存储读取失败: {exc}") from exc

    def _get_failure(self, failure_id: str) -> FailureRecord | None:
        try:
            return self.repository.get_failure(failure_id)
        except Exception as exc:
            raise GovernanceUnavailableError(f"治理存储读取失败: {exc}") from exc

    def _find_active_failure(
        self, error_class: str, task_id: str | None, step_id: str | None
    ) -> FailureRecord | None:
        try:
            candidates = self.repository.list_active_failures(self._run.run_id)
        except Exception as exc:
            raise GovernanceUnavailableError(f"治理存储读取失败: {exc}") from exc
        for item in candidates:
            if item.error_class != error_class:
                continue
            if (item.task_id or None) != (task_id or None):
                continue
            if (item.step_id or None) != (step_id or None):
                continue
            return item
        return None

    def _find_task_by_key(self, key: str) -> TaskRecord | None:
        try:
            return self.repository.find_task_by_idempotency_key(key)
        except Exception as exc:
            raise GovernanceUnavailableError(f"治理存储读取失败: {exc}") from exc
