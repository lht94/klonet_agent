"""运行治理的 DTO、枚举、状态机与 reason code。

计划 §3.1/§3.3 的落地约定：

1. 所有权威对象携带 ``run_id``/``turn_id`` 等运行身份与所有权字段
   （tenant 维度即 ``user_id``/``project_id``，与记忆系统的租户模型一致）；
2. 状态改变必须以 ``RuntimeEvent`` 的形式留痕，内存对象只是投影；
3. 同一业务事件重复提交必须幂等（``event_id`` / ``idempotency_key`` 唯一）；
4. ``schema_version`` 支持事件协议演进，不就地改写旧事件。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# 事件协议版本。字段只增不改语义；不兼容变更时递增并配 upcaster。
SCHEMA_VERSION = 1


def new_id() -> str:
    """生成一个运行对象的稳定 ID（uuid4 十六进制串）。"""

    return uuid.uuid4().hex


def utcnow() -> datetime:
    """统一的时钟入口，方便测试注入与审计对齐。"""

    return datetime.now(timezone.utc)


def enum_text(value) -> str:
    """枚举 → 存储文本。

    注意 ``str(TaskStatus.RUNNING)`` 在 Python 3.11+ 返回的是限定名
    ``TaskStatus.RUNNING`` 而不是 ``running``；凡是写进 SQL / JSON 的
    枚举字段必须走这里。
    """

    return value.value if isinstance(value, Enum) else str(value)


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class PrivacyClass(str, Enum):
    """计划 §4.6 的四级数据分类。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class TaskStatus(str, Enum):
    """计划 §4.1 的任务状态机。

    ``waiting_user`` 在旧版 todos 里存在；这里归入 ``blocked`` 并用
    ``blocked_reason`` 区分，状态机本身保持最小。
    """

    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    """计划 §4.1 的步骤状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class FailureStatus(str, Enum):
    """计划 §4.2 的失败生命周期。

    活跃失败只能被「解决事件 + 验证证据」关闭；``dismissed`` 表示明确忽略
    （例如环境噪声），同样必须给出理由，不允许被新文本静默覆盖。
    """

    ACTIVE = "active"
    RESOLVED = "resolved"
    VERIFIED = "verified"
    DISMISSED = "dismissed"


class ToolOutcome(str, Enum):
    """计划 §5.5 的工具调用结果分类，为安全重试提供依据。

    ``outcome_unknown`` 是独立状态：有副作用的调用结果不明时禁止自动重试。
    """

    NOT_STARTED = "not_started"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ActorType(str, Enum):

    SYSTEM = "system"
    USER = "user"
    MODEL = "model"
    TOOL = "tool"


# --------------------------------------------------------------------------- #
# 状态机转换表
# --------------------------------------------------------------------------- #

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.BLOCKED, TaskStatus.COMPLETED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    # 终态不允许再转换；completed/cancelled 的重开视为新任务。
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.RUNNING, StepStatus.SKIPPED}),
    StepStatus.RUNNING: frozenset(
        {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED}
    ),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
}

FAILURE_TRANSITIONS: dict[FailureStatus, frozenset[FailureStatus]] = {
    FailureStatus.ACTIVE: frozenset(
        {FailureStatus.RESOLVED, FailureStatus.VERIFIED, FailureStatus.DISMISSED}
    ),
    # resolved → verified 需要「解决动作 + 验证证据」，由服务层校验。
    FailureStatus.RESOLVED: frozenset({FailureStatus.VERIFIED, FailureStatus.DISMISSED}),
    FailureStatus.VERIFIED: frozenset(),
    FailureStatus.DISMISSED: frozenset(),
}


class GovernanceDomainError(ValueError):
    """治理层领域规则违反的基类。"""


class InvalidStatusTransitionError(GovernanceDomainError):
    """非法状态转换：必须被拒绝并记录，不允许静默接受。"""

    def __init__(self, kind: str, from_status: str, to_status: str):
        self.kind = kind
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"非法{kind}状态转换: {from_status} -> {to_status}"
        )


def ensure_transition(
    enum_cls: type[Enum],
    table: dict[Enum, frozenset],
    kind: str,
    from_status: Enum | str,
    to_status: Enum | str,
) -> None:
    """校验一次状态转换是否合法，非法即抛错。

    字符串入参先归一到枚举：非法取值在这里抛 ``ValueError``，而不是被
    静默当成一个"永远不允许"的目标状态。
    """

    current = from_status if isinstance(from_status, enum_cls) else enum_cls(str(from_status))
    target = to_status if isinstance(to_status, enum_cls) else enum_cls(str(to_status))
    allowed = table.get(current, frozenset())
    if target not in allowed:
        raise InvalidStatusTransitionError(kind, str(from_status), str(to_status))


def ensure_task_transition(from_status: TaskStatus | str, to_status: TaskStatus | str) -> None:
    ensure_transition(TaskStatus, TASK_TRANSITIONS, "任务", from_status, to_status)


def ensure_step_transition(from_status: StepStatus | str, to_status: StepStatus | str) -> None:
    ensure_transition(StepStatus, STEP_TRANSITIONS, "步骤", from_status, to_status)


def ensure_failure_transition(
    from_status: FailureStatus | str, to_status: FailureStatus | str
) -> None:
    ensure_transition(FailureStatus, FAILURE_TRANSITIONS, "失败", from_status, to_status)


# --------------------------------------------------------------------------- #
# Reason code（计划 §4.5/§阶段 0：可机器读的决策原因）
# --------------------------------------------------------------------------- #

REASON_TASK_CREATED = "task.created"
REASON_TASK_STARTED = "task.started"
REASON_TASK_BLOCKED = "task.blocked"
REASON_TASK_COMPLETED = "task.completed"
REASON_TASK_CANCELLED = "task.cancelled"
REASON_STEP_STARTED = "step.started"
REASON_STEP_SUCCEEDED = "step.succeeded"
REASON_STEP_FAILED = "step.failed"
REASON_STEP_SKIPPED = "step.skipped"
REASON_FAILURE_OPENED = "failure.opened"
REASON_FAILURE_RESOLVED = "failure.resolved"
REASON_FAILURE_VERIFIED = "failure.verified"
REASON_FAILURE_DISMISSED = "failure.dismissed"
REASON_MODEL_CALL_COMPLETED = "model_call.completed"
REASON_MODEL_CALL_FAILED = "model_call.failed"
REASON_TOOL_CALL_COMPLETED = "tool_call.completed"
REASON_TOOL_CALL_FAILED = "tool_call.failed"
REASON_TURN_STARTED = "turn.started"
REASON_RUN_STARTED = "run.started"
REASON_EVIDENCE_RECORDED = "evidence.recorded"
REASON_EVIDENCE_STALE = "evidence.stale"
REASON_CLAIM_CREATED = "claim.created"
REASON_CLAIM_LINKED = "claim.linked"
REASON_ROUTE_DECIDED = "route.decided"


# --------------------------------------------------------------------------- #
# 记录 DTO
# --------------------------------------------------------------------------- #


@dataclass
class RunRecord:
    """一次端到端执行（通常是一个 Agent 进程的生命周期）。"""

    run_id: str
    user_id: str
    project_id: str | None
    session_id: str | None = None
    mode: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION


@dataclass
class RuntimeEvent:
    """追加写入的事实账本条目（计划 §5.1）。"""

    event_id: str
    event_type: str
    run_id: str
    actor_type: ActorType | str
    payload: dict[str, Any]
    reason_code: str | None = None
    tenant_user_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    parent_event_id: str | None = None
    idempotency_key: str | None = None
    privacy_class: PrivacyClass | str = PrivacyClass.INTERNAL
    occurred_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION
    actor_id: str | None = None


@dataclass
class TaskRecord:
    """持久化任务（``AgentSession.todos`` 的权威化对应物）。"""

    task_id: str
    run_id: str
    user_id: str
    project_id: str | None
    content: str
    status: TaskStatus | str = TaskStatus.PENDING
    priority: int = 0
    ordinal: int = 0
    blocked_reason: str | None = None
    version: int = 1
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class StepRecord:
    """可执行步骤。输出不落大文本，只关联 evidence/artifact（后续阶段）。"""

    step_id: str
    task_id: str
    run_id: str
    user_id: str
    project_id: str | None
    content: str
    status: StepStatus | str = StepStatus.PENDING
    reason_code: str | None = None
    version: int = 1
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class FailureRecord:
    """通用失败记录（计划 §4.2/§5.3）。

    ``root_cause_hypothesis`` 只是待验证假设；只有 verified 的失败才允许
    生成 ``FailureLessonCandidate`` 进入记忆管线。
    """

    failure_id: str
    run_id: str
    user_id: str
    project_id: str | None
    stage: str
    error_class: str
    message: str = ""
    retryable: bool = False
    status: FailureStatus | str = FailureStatus.ACTIVE
    attempt_count: int = 1
    task_id: str | None = None
    step_id: str | None = None
    turn_id: str | None = None
    root_cause_hypothesis: str | None = None
    resolution_action: str | None = None
    verification_evidence_ids: list[str] = field(default_factory=list)
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class FailureLessonCandidate:
    """从 verified 失败提炼的可复用经验候选（不直接进记忆）。"""

    candidate_id: str
    failure_id: str
    lesson: str
    error_class: str
    scope_user_id: str
    scope_project_id: str | None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ModelCallRecord:
    """模型调用投影：只存公开可解释信息，不存消息正文与思维链。"""

    call_id: str
    run_id: str
    user_id: str
    project_id: str | None
    model: str
    outcome: str
    total_tokens: int = 0
    duration_ms: int = 0
    turn_id: str | None = None
    error_class: str | None = None
    occurred_at: datetime = field(default_factory=utcnow)


@dataclass
class ToolCallRecord:
    """工具调用投影。``outcome_unknown`` 阻断自动重试（计划 §3.3-8）。"""

    call_id: str
    run_id: str
    user_id: str
    project_id: str | None
    tool_name: str
    outcome: ToolOutcome | str
    duration_ms: int = 0
    args_preview: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    error_class: str | None = None
    occurred_at: datetime = field(default_factory=utcnow)


@dataclass
class RedactionRecord:
    """脱敏留痕：只记规则、类别与内容哈希，绝不保存秘密原文（计划 §4.6）。"""

    rule_id: str
    category: str
    content_hash: str
    privacy_class: PrivacyClass | str = PrivacyClass.SENSITIVE
    created_at: datetime = field(default_factory=utcnow)
