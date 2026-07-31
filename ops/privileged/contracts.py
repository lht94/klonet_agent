"""高权限工作流使用的持久化数据契约。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


PLAN_STATUSES = {
    "draft",
    "awaiting_confirmation",
    "approved",
    "executing",
    "verifying",
    "completed",
    "partially_completed",
    "paused",
    "blocked",
    "failed",
    "aborted",
}
STEP_STATUSES = {
    "pending",
    "awaiting_confirmation",
    "approved",
    "running",
    "executed",
    "verifying",
    "completed",
    "paused",
    "blocked",
    "failed",
    "execution_unknown",
    "skipped",
}
RISK_LEVELS = ("readonly", "low", "medium", "high", "destructive")
CHECK_STATUSES = {"passed", "failed", "unavailable"}
VERIFICATION_STATUSES = {
    "passed",
    "failed",
    "inconclusive",
    "replan_required",
    "blocked",
}
EXECUTION_BINDING_KINDS = {
    "registered_action",
    "shell_artifact",
    "verification_only",
    "legacy_command",
}
SHELL_ARTIFACT_STATUSES = {
    "draft",
    "awaiting_confirmation",
    "approved",
    "executed",
    "failed",
    "expired",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ExecutionEvidence:
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    started_at: str = ""
    finished_at: str = ""
    timed_out: bool = False
    environment_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExecutionEvidence | None:
        return cls(**data) if data else None


@dataclass
class CheckResult:
    checker: str
    status: str
    expected: str = ""
    observed: str = ""
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise ValueError("invalid check status: %s" % self.status)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckResult:
        return cls(**data)


@dataclass
class VerificationDecision:
    status: str
    goal_achieved: bool = False
    verification_level: str = "none"
    failures: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    reason: str = ""
    next_action: str = ""
    confirmed_facts: list[str] = field(default_factory=list)
    failed_criteria: list[str] = field(default_factory=list)
    reflection: str = ""
    recommended_next_focus: str = ""
    probe_history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError("invalid verification status: %s" % self.status)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> VerificationDecision | None:
        return cls(**data) if data else None


@dataclass
class ShellArtifact:
    artifact_id: str
    script: str
    sha256: str
    interpreter: str = "/bin/bash"
    cwd: str = ""
    run_as: str = ""
    env_allowlist: dict[str, str] = field(default_factory=dict)
    timeout: int = 120
    environment_fingerprint: str = ""
    declared_changes: list[str] = field(default_factory=list)
    rollback: str = ""
    single_use_nonce: str = ""
    expires_at: str = ""
    status: str = "awaiting_confirmation"
    executed_at: str = ""
    approved_contract_hash: str = ""

    def __post_init__(self) -> None:
        if self.status not in SHELL_ARTIFACT_STATUSES:
            raise ValueError("invalid shell artifact status: %s" % self.status)
        self.timeout = max(1, min(int(self.timeout), 600))

    @property
    def contract_hash(self) -> str:
        payload = {
            "artifact_id": self.artifact_id,
            "script": self.script,
            "sha256": self.sha256,
            "interpreter": self.interpreter,
            "cwd": self.cwd,
            "run_as": self.run_as,
            "env_allowlist": self.env_allowlist,
            "timeout": self.timeout,
            "environment_fingerprint": self.environment_fingerprint,
            "declared_changes": self.declared_changes,
            "rollback": self.rollback,
            "single_use_nonce": self.single_use_nonce,
            "expires_at": self.expires_at,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["contract_hash"] = self.contract_hash
        return data

    def executable_dict(self) -> dict[str, Any]:
        # The environment fingerprint is pinned at exact step confirmation,
        # after any earlier confirmed Actions have completed. It therefore
        # belongs to the Shell contract, not the earlier plan authorization.
        return {
            "artifact_id": self.artifact_id,
            "script": self.script,
            "sha256": self.sha256,
            "interpreter": self.interpreter,
            "cwd": self.cwd,
            "run_as": self.run_as,
            "env_allowlist": self.env_allowlist,
            "timeout": self.timeout,
            "declared_changes": self.declared_changes,
            "rollback": self.rollback,
            "single_use_nonce": self.single_use_nonce,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ShellArtifact | None:
        if not data:
            return None
        values = dict(data)
        values.pop("contract_hash", None)
        return cls(**values)


@dataclass
class ExecutionBinding:
    kind: str
    risk: str
    approval_scope: str = "plan"
    action: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    shell_artifact: ShellArtifact | None = None
    resolved_from_evidence: list[str] = field(default_factory=list)
    preconditions: list[dict[str, Any]] = field(default_factory=list)
    postconditions: list[dict[str, Any]] = field(default_factory=list)
    binding_reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EXECUTION_BINDING_KINDS:
            raise ValueError("invalid execution binding kind: %s" % self.kind)
        if self.risk not in RISK_LEVELS:
            raise ValueError("invalid execution binding risk: %s" % self.risk)
        if self.kind == "registered_action" and not self.action:
            raise ValueError("registered action binding requires action")
        if self.kind == "shell_artifact" and self.shell_artifact is None:
            raise ValueError("shell binding requires artifact")
        if self.kind == "verification_only":
            if self.action or self.shell_artifact is not None:
                raise ValueError(
                    "verification-only binding cannot execute an implementation"
                )
            if not self.postconditions:
                raise ValueError("verification-only binding requires postconditions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "risk": self.risk,
            "approval_scope": self.approval_scope,
            "action": self.action,
            "args": self.args,
            "shell_artifact": (
                self.shell_artifact.to_dict()
                if self.shell_artifact is not None
                else None
            ),
            "resolved_from_evidence": self.resolved_from_evidence,
            "preconditions": self.preconditions,
            "postconditions": self.postconditions,
            "binding_reason": self.binding_reason,
        }

    def executable_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "risk": self.risk,
            "approval_scope": self.approval_scope,
            "action": self.action,
            "args": self.args,
            "shell_artifact": (
                self.shell_artifact.executable_dict()
                if self.shell_artifact is not None
                else None
            ),
            "resolved_from_evidence": self.resolved_from_evidence,
            "preconditions": self.preconditions,
            "postconditions": self.postconditions,
            "binding_reason": self.binding_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExecutionBinding | None:
        if not data:
            return None
        values = dict(data)
        values["shell_artifact"] = ShellArtifact.from_dict(
            values.get("shell_artifact")
        )
        return cls(**values)


@dataclass
class FailurePacket:
    original_goal: str
    failed_step: dict[str, Any]
    execution_binding: dict[str, Any]
    execution_evidence: dict[str, Any]
    verification: dict[str, Any]
    environment_changes: list[str] = field(default_factory=list)
    completed_steps: list[dict[str, Any]] = field(default_factory=list)
    remaining_steps: list[dict[str, Any]] = field(default_factory=list)
    reflection: str = ""
    environment_fingerprint: str = ""
    failure_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailurePacket:
        return cls(**data)


@dataclass
class PrivilegedStep:
    step_id: str
    title: str
    objective: str = ""
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    execution_binding: ExecutionBinding | None = None
    # v2 compatibility fields. New Planner output never writes these directly.
    command: str = ""
    action: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str = ""
    risk: str = "medium"
    approval_scope: str = "plan"
    timeout: int = 120
    expected_changes: list[str] = field(default_factory=list)
    preconditions: list[dict[str, Any]] = field(default_factory=list)
    postconditions: list[dict[str, Any]] = field(default_factory=list)
    rollback: str = ""
    status: str = "pending"
    evidence: ExecutionEvidence | None = None
    checks: list[CheckResult] = field(default_factory=list)
    observation: str = ""
    execution_attempts: int = 0
    implementation_rebind_attempts: int = 0

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError("invalid risk: %s" % self.risk)
        if self.status not in STEP_STATUSES:
            raise ValueError("invalid step status: %s" % self.status)
        self.timeout = max(1, min(int(self.timeout), 3600))

    def executable_dict(self) -> dict[str, Any]:
        if self.execution_binding is not None:
            return {
                "step_id": self.step_id,
                "title": self.title,
                "objective": self.objective,
                "reason": self.reason,
                "evidence_refs": self.evidence_refs,
                "depends_on": self.depends_on,
                "success_criteria": self.success_criteria,
                "execution_binding": self.execution_binding.executable_dict(),
                "timeout": self.timeout,
                "expected_changes": self.expected_changes,
                "rollback": self.rollback,
            }
        return {
            "step_id": self.step_id,
            "title": self.title,
            "command": self.command,
            "action": self.action,
            "args": self.args,
            "cwd": self.cwd,
            "risk": self.risk,
            "approval_scope": self.approval_scope,
            "timeout": self.timeout,
            "expected_changes": self.expected_changes,
            "preconditions": self.preconditions,
            "postconditions": self.postconditions,
            "rollback": self.rollback,
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrivilegedStep:
        values = dict(data)
        values["evidence"] = ExecutionEvidence.from_dict(values.get("evidence"))
        values["execution_binding"] = ExecutionBinding.from_dict(
            values.get("execution_binding")
        )
        values["checks"] = [
            CheckResult.from_dict(item) for item in values.get("checks", [])
        ]
        return cls(**values)


@dataclass
class PrivilegedPlan:
    plan_id: str
    goal: str
    risk: str
    steps: list[PrivilegedStep]
    schema_version: int = 3
    status: str = "draft"
    verification_level: str = "none"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    authorized_hash: str = ""
    verification: VerificationDecision | None = None
    grounding: dict[str, Any] = field(default_factory=dict)
    recovery_history: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    probe_history: list[dict[str, Any]] = field(default_factory=list)
    failure_packets: list[FailurePacket] = field(default_factory=list)
    replan_attempts: int = 0

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError("invalid risk: %s" % self.risk)
        if self.status not in PLAN_STATUSES:
            raise ValueError("invalid plan status: %s" % self.status)

    @property
    def content_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "risk": self.risk,
            "assumptions": self.assumptions,
            "steps": [step.executable_dict() for step in self.steps],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def is_authorized(self) -> bool:
        return bool(self.authorized_hash) and self.authorized_hash == self.content_hash

    def authorize(self) -> None:
        self.authorized_hash = self.content_hash
        self.status = "approved"
        self.updated_at = utc_now()

    def replace_steps(self, steps: list[PrivilegedStep]) -> None:
        self.steps = list(steps)
        self.authorized_hash = ""
        self.status = "awaiting_confirmation"
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "goal": self.goal,
            "risk": self.risk,
            "status": self.status,
            "verification_level": self.verification_level,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "authorized_hash": self.authorized_hash,
            "content_hash": self.content_hash,
            "steps": [step.to_dict() for step in self.steps],
            "verification": self.verification.to_dict() if self.verification else None,
            "grounding": self.grounding,
            "recovery_history": self.recovery_history,
            "assumptions": self.assumptions,
            "probe_history": self.probe_history,
            "failure_packets": [
                item.to_dict() for item in self.failure_packets
            ],
            "replan_attempts": self.replan_attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrivilegedPlan:
        values = dict(data)
        values.pop("content_hash", None)
        original_schema = int(values.get("schema_version") or 1)
        values["steps"] = [
            PrivilegedStep.from_dict(item) for item in values.get("steps", [])
        ]
        values["verification"] = VerificationDecision.from_dict(
            values.get("verification")
        )
        values["failure_packets"] = [
            FailurePacket.from_dict(item)
            for item in values.get("failure_packets", [])
            if isinstance(item, dict)
        ]
        if original_schema < 3:
            _migrate_legacy_plan_values(values, original_schema)
        return cls(**values)


def _migrate_legacy_plan_values(
    values: dict[str, Any],
    original_schema: int,
) -> None:
    """Make v1/v2 plans inspectable without trusting old authorization."""

    for step in values.get("steps", []):
        if not step.objective:
            step.objective = step.title
        if not step.success_criteria:
            step.success_criteria = [
                str(item.get("checker") or "")
                for item in step.postconditions
                if isinstance(item, dict) and item.get("checker")
            ]
        if step.execution_binding is None:
            if step.action:
                step.execution_binding = ExecutionBinding(
                    kind="registered_action",
                    action=step.action,
                    args=dict(step.args),
                    risk=step.risk,
                    approval_scope=step.approval_scope,
                    preconditions=list(step.preconditions),
                    postconditions=list(step.postconditions),
                    binding_reason="migrated from schema v%s" % original_schema,
                )
            elif step.command:
                step.execution_binding = ExecutionBinding(
                    kind="legacy_command",
                    risk=max(
                        (step.risk, "high"),
                        key=RISK_LEVELS.index,
                    ),
                    approval_scope="step",
                    binding_reason=(
                        "legacy raw command is inspectable but never executable"
                    ),
                )
                step.status = "blocked"
                step.observation = (
                    "旧版原始命令计划已迁移为只读审计记录，不能执行；"
                    "请重新生成 Agentic V3 计划。"
                )
    values["schema_version"] = 3
    grounding = dict(values.get("grounding") or {})
    grounding["migrated_from_schema"] = original_schema
    values["grounding"] = grounding
    if values.get("status") not in {"completed", "aborted"}:
        values["authorized_hash"] = ""
        if values.get("status") not in {"paused", "blocked", "failed"}:
            values["status"] = "awaiting_confirmation"
