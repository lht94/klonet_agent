"""治理层存储协议与内存实现。

协议刻意收窄（计划 §9「过度平台化」风险）：只提供本次运行真正需要的
写入与查询入口，每个写方法在实现里必须是**单事务**——事件与投影同生
同灭（计划 §3.2）。内存实现供单元测试、降级缓冲和本地无库部署使用，
它不是第二个权威源：生产权威永远是 PostgreSQL。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from klonet_agent.runtime.governance.models import (
    FailureRecord,
    ModelCallRecord,
    RunRecord,
    RuntimeEvent,
    StepRecord,
    TaskRecord,
    ToolCallRecord,
)
from klonet_agent.runtime.governance.provenance import (
    ClaimEvidenceLink,
    ClaimRecord,
    EvidenceRecord,
)
from klonet_agent.runtime.governance.routing_policy import RouteDecision


class GovernanceRepositoryError(RuntimeError):
    """治理存储层错误的基类。"""


class DuplicateEventError(GovernanceRepositoryError):
    """同一 ``event_id`` / ``idempotency_key`` 的事件已经存在（幂等命中）。"""

    def __init__(self, event_id: str):
        self.event_id = event_id
        super().__init__(f"事件已存在: {event_id}")


class ConcurrentProjectionError(GovernanceRepositoryError):
    """乐观锁冲突：另一个 worker 已经更新了该投影。"""

    def __init__(self, kind: str, record_id: str, expected_version: int):
        self.kind = kind
        self.record_id = record_id
        self.expected_version = expected_version
        super().__init__(
            f"{kind} {record_id} 乐观锁冲突（期望 version={expected_version}）"
        )


@runtime_checkable
class GovernanceRepository(Protocol):
    """治理层存储协议。所有实现必须保证「事件+投影」同事务。"""

    def begin_run(self, run: RunRecord) -> None: ...

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
        """追加一条事件并（可选）在同一事务里更新投影。

        返回 ``True`` 表示事件为新写入；幂等命中（重复提交）返回 ``False``
        且**不得**重复应用投影。
        """
        ...

    def record_model_call(self, record: ModelCallRecord, event: RuntimeEvent) -> bool: ...

    def record_tool_call(self, record: ToolCallRecord, event: RuntimeEvent) -> bool: ...

    def append_redactions(self, event_id: str, records: list) -> None: ...

    # ---------------------------------------------------------------- 查询 --
    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def find_task_by_idempotency_key(self, key: str) -> TaskRecord | None: ...

    def list_tasks(self, run_id: str) -> list[TaskRecord]: ...

    def get_failure(self, failure_id: str) -> FailureRecord | None: ...

    def list_active_failures(self, run_id: str) -> list[FailureRecord]: ...

    def list_events(self, run_id: str, limit: int = 500) -> list[RuntimeEvent]: ...

    def count_events(self, run_id: str) -> int: ...

    # -------------------------------------------------- 阶段 4/5：证据与路由 --
    def add_evidence(self, evidence: EvidenceRecord, event: RuntimeEvent) -> bool: ...

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None: ...

    def find_evidence_by_subject(self, subject: str) -> list[EvidenceRecord]: ...

    def mark_evidence_stale(self, evidence_id: str, event: RuntimeEvent) -> None: ...

    def add_claim(self, claim: ClaimRecord, event: RuntimeEvent) -> bool: ...

    def link_claim_evidence(
        self, link: ClaimEvidenceLink, event: RuntimeEvent
    ) -> bool: ...

    def list_claim_links(self, claim_id: str) -> list[ClaimEvidenceLink]: ...

    def list_claims(self, run_id: str) -> list[ClaimRecord]: ...

    def record_route_decision(self, decision: RouteDecision, event: RuntimeEvent) -> bool: ...



class InMemoryGovernanceRepository:
    """线程不安全的内存实现：单元测试与降级缓冲用。

    数据结构直接保存 DTO 实例（含版本号），语义与 PostgreSQL 实现严格
    对齐：幂等、乐观锁、单事务（内存里没有真事务，但"失败时不落任何
    一半状态"的语义用先校验后提交来保证）。
    """

    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self._events: dict[str, RuntimeEvent] = {}
        self._idempotency: dict[str, str] = {}
        self.tasks: dict[str, TaskRecord] = {}
        self.steps: dict[str, StepRecord] = {}
        self.failures: dict[str, FailureRecord] = {}
        self.model_calls: list[ModelCallRecord] = []
        self.tool_calls: list[ToolCallRecord] = []
        self.redactions: dict[str, list] = {}
        self.evidence: dict[str, EvidenceRecord] = {}
        self.claims: dict[str, ClaimRecord] = {}
        self.claim_links: list[ClaimEvidenceLink] = []
        self.route_decisions: list[RouteDecision] = []

    # ---------------------------------------------------------------- 写入 --
    def begin_run(self, run: RunRecord) -> None:
        self.runs.setdefault(run.run_id, run)

    def _event_slot(self, event: RuntimeEvent) -> str | None:
        if event.idempotency_key and event.idempotency_key in self._idempotency:
            return self._idempotency[event.idempotency_key]
        if event.event_id in self._events:
            return event.event_id
        return None

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
        existing = self._event_slot(event)
        if existing is not None:
            return False

        # 先做全部校验（乐观锁、引用存在性），再统一提交——模拟单事务。
        if task is not None:
            current = self.tasks.get(task.task_id)
            if expect_task_version is not None and current is not None:
                if current.version != expect_task_version:
                    raise ConcurrentProjectionError("task", task.task_id, expect_task_version)
        if step is not None:
            current_step = self.steps.get(step.step_id)
            if expect_step_version is not None and current_step is not None:
                if current_step.version != expect_step_version:
                    raise ConcurrentProjectionError("step", step.step_id, expect_step_version)

        self._events[event.event_id] = event
        if event.idempotency_key:
            self._idempotency[event.idempotency_key] = event.event_id

        if task is not None:
            self.tasks[task.task_id] = task
        if step is not None:
            self.steps[step.step_id] = step
        if failure is not None:
            self.failures[failure.failure_id] = failure
        if close_failure_status is not None and failure is not None:
            failure.status = close_failure_status
        return True

    def record_model_call(self, record: ModelCallRecord, event: RuntimeEvent) -> bool:
        if self._event_slot(event) is not None:
            return False
        self._events[event.event_id] = event
        if event.idempotency_key:
            self._idempotency[event.idempotency_key] = event.event_id
        self.model_calls.append(record)
        return True

    def record_tool_call(self, record: ToolCallRecord, event: RuntimeEvent) -> bool:
        if self._event_slot(event) is not None:
            return False
        self._events[event.event_id] = event
        if event.idempotency_key:
            self._idempotency[event.idempotency_key] = event.event_id
        self.tool_calls.append(record)
        return True

    def append_redactions(self, event_id: str, records: list) -> None:
        self.redactions.setdefault(event_id, []).extend(records)

    # ---------------------------------------------------------------- 查询 --
    def get_task(self, task_id: str) -> TaskRecord | None:
        return self.tasks.get(task_id)

    def find_task_by_idempotency_key(self, key: str) -> TaskRecord | None:
        for task in self.tasks.values():
            if task.idempotency_key == key:
                return task
        return None

    def list_tasks(self, run_id: str) -> list[TaskRecord]:
        return [t for t in self.tasks.values() if t.run_id == run_id]

    def get_failure(self, failure_id: str) -> FailureRecord | None:
        return self.failures.get(failure_id)

    def list_active_failures(self, run_id: str) -> list[FailureRecord]:
        return [
            f
            for f in self.failures.values()
            if f.run_id == run_id and str(getattr(f.status, "value", f.status)) == "active"
        ]

    def list_events(self, run_id: str, limit: int = 500) -> list[RuntimeEvent]:
        events = [e for e in self._events.values() if e.run_id == run_id]
        events.sort(key=lambda e: e.occurred_at)
        return events[:limit]

    def count_events(self, run_id: str) -> int:
        return sum(1 for e in self._events.values() if e.run_id == run_id)

    # ------------------------------------------------- 阶段 4/5：证据与路由 --
    def add_evidence(self, evidence: EvidenceRecord, event: RuntimeEvent) -> bool:
        if self._event_slot(event) is not None:
            return False
        self._events[event.event_id] = event
        if event.idempotency_key:
            self._idempotency[event.idempotency_key] = event.event_id
        self.evidence[evidence.evidence_id] = evidence
        return True

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        return self.evidence.get(evidence_id)

    def find_evidence_by_subject(self, subject: str) -> list[EvidenceRecord]:
        return [e for e in self.evidence.values() if e.subject == subject]

    def mark_evidence_stale(self, evidence_id: str, event: RuntimeEvent) -> None:
        item = self.evidence.get(evidence_id)
        if item is not None:
            item.freshness = "stale"
        if self._event_slot(event) is None:
            self._events[event.event_id] = event
            if event.idempotency_key:
                self._idempotency[event.idempotency_key] = event.event_id

    def add_claim(self, claim: ClaimRecord, event: RuntimeEvent) -> bool:
        if self._event_slot(event) is not None:
            return False
        self._events[event.event_id] = event
        if event.idempotency_key:
            self._idempotency[event.idempotency_key] = event.event_id
        self.claims[claim.claim_id] = claim
        return True

    def link_claim_evidence(self, link: ClaimEvidenceLink, event: RuntimeEvent) -> bool:
        if self._event_slot(event) is not None:
            return False
        self._events[event.event_id] = event
        if event.idempotency_key:
            self._idempotency[event.idempotency_key] = event.event_id
        self.claim_links.append(link)
        return True

    def list_claim_links(self, claim_id: str) -> list[ClaimEvidenceLink]:
        return [l for l in self.claim_links if l.claim_id == claim_id]

    def list_claims(self, run_id: str) -> list[ClaimRecord]:
        return [c for c in self.claims.values() if c.run_id == run_id]

    def record_route_decision(self, decision: RouteDecision, event: RuntimeEvent) -> bool:
        if self._event_slot(event) is not None:
            return False
        self._events[event.event_id] = event
        if event.idempotency_key:
            self._idempotency[event.idempotency_key] = event.event_id
        self.route_decisions.append(decision)
        return True

    # ---------------------------------------------------------- 测试辅助 --
    def all_events(self) -> list[RuntimeEvent]:
        return list(self._events.values())
