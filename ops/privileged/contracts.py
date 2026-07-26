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

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError("invalid verification status: %s" % self.status)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> VerificationDecision | None:
        return cls(**data) if data else None


@dataclass
class PrivilegedStep:
    step_id: str
    title: str
    command: str
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

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError("invalid risk: %s" % self.risk)
        if self.status not in STEP_STATUSES:
            raise ValueError("invalid step status: %s" % self.status)
        self.timeout = max(1, min(int(self.timeout), 3600))

    def executable_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "command": self.command,
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
    schema_version: int = 1
    status: str = "draft"
    verification_level: str = "none"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    authorized_hash: str = ""
    verification: VerificationDecision | None = None

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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrivilegedPlan:
        values = dict(data)
        values.pop("content_hash", None)
        values["steps"] = [
            PrivilegedStep.from_dict(item) for item in values.get("steps", [])
        ]
        values["verification"] = VerificationDecision.from_dict(
            values.get("verification")
        )
        return cls(**values)
