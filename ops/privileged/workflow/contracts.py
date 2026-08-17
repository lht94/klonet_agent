"""High-level contracts for the staged Ops-Privilege workflow."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
import re

from klonet_agent.ops.privileged.contracts import (
    ExecutionBinding,
    ExecutionEvidence,
    ImplementationPlan,
    PlanResource,
    RISK_LEVELS,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@dataclass(frozen=True)
class ProbeRequest:
    probe: str
    args: dict[str, Any]
    purpose: str

    def __post_init__(self) -> None:
        if not str(self.probe or "").strip():
            raise ValueError("probe is required")
        if not isinstance(self.args, dict):
            raise ValueError("probe args must be an object")

    @property
    def cache_key(self) -> str:
        payload = {"probe": self.probe.strip(), "args": self.args}
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def normalize_probe_request(
    probe: str,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Route source/config paths to the bounded operational file reader."""

    normalized_probe = str(probe or "").strip()
    normalized_args = dict(args)
    path = str(normalized_args.get("path") or "").strip()
    if normalized_probe == "logs" and Path(path).suffix.lower() in {
        ".py", ".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml",
    }:
        normalized_probe = "ops_file"
        normalized_args.setdefault("view", "head")
        normalized_args.setdefault("max_chars", 20000)
    return normalized_probe, normalized_args


def normalize_instance_alias(value: str) -> str:
    """Normalize display aliases without weakening project-root identity."""

    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return normalized[6:] if normalized.startswith("klonet") else normalized


@dataclass(frozen=True)
class ResolvedPlatformIdentity:
    project_root: str
    primary_alias: str
    aliases: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not Path(self.project_root).is_absolute():
            raise ValueError("resolved platform root must be absolute")
        if not self.primary_alias or not self.evidence_refs:
            raise ValueError("resolved platform identity requires alias and evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "primary_alias": self.primary_alias,
            "aliases": list(self.aliases),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    request: ProbeRequest
    output: str
    status: str = "available"
    collected_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_probe(
        cls,
        request: ProbeRequest,
        output: str,
        *,
        status: str = "available",
    ) -> "EvidenceRecord":
        return cls(
            evidence_id="ev-" + request.cache_key[:16],
            request=request,
            output=str(output or ""),
            status=status,
        )


@dataclass
class EvidenceBundle:
    goal: str
    records: list[EvidenceRecord] = field(default_factory=list)
    budget_exhausted: bool = False
    blocked_reason: str = ""

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        existing = next(
            (
                item
                for item in self.records
                if item.request.cache_key == record.request.cache_key
            ),
            None,
        )
        if existing is not None:
            return existing
        self.records.append(record)
        return record

    @property
    def evidence_ids(self) -> set[str]:
        return {item.evidence_id for item in self.records}

    @property
    def knowledge_records(self) -> list[EvidenceRecord]:
        return [
            item for item in self.records
            if item.request.probe == "klonet_knowledge"
        ]


@dataclass(frozen=True)
class RuntimeComponentSpec:
    name: str
    category: str = "application"
    managed: bool = True
    default_restart: bool = True
    screen_suffix: str = ""
    command_argv: tuple[str, ...] = ()
    preflight_argv: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    health_checks: tuple[dict[str, Any], ...] = ()
    start_after: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", self.name):
            raise ValueError("invalid runtime component name")
        if self.category not in {"application", "shared_dependency"}:
            raise ValueError("invalid runtime component category")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", self.screen_suffix,
        ):
            raise ValueError("invalid runtime component screen suffix")
        if any(not 1 <= int(port) <= 65535 for port in self.ports):
            raise ValueError("invalid runtime component port")

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, evidence_refs: tuple[str, ...] = (),
    ) -> "RuntimeComponentSpec":
        return cls(
            name=str(data.get("name") or ""),
            category=str(data.get("category") or "application"),
            managed=bool(data.get("managed", True)),
            default_restart=bool(data.get("default_restart", True)),
            screen_suffix=str(data.get("screen_suffix") or data.get("name") or ""),
            command_argv=tuple(str(item) for item in data.get("command_argv") or []),
            preflight_argv=tuple(str(item) for item in data.get("preflight_argv") or []),
            ports=tuple(int(item) for item in data.get("ports") or []),
            health_checks=tuple(
                dict(item) for item in data.get("health_checks") or []
                if isinstance(item, dict)
            ),
            start_after=tuple(str(item) for item in data.get("start_after") or []),
            evidence_refs=evidence_refs,
        )


@dataclass(frozen=True)
class EvidenceClaim:
    text: str
    evidence_refs: list[str]

    def __post_init__(self) -> None:
        if not str(self.text or "").strip():
            raise ValueError("evidence claim text is required")
        if not self.evidence_refs:
            raise ValueError("evidence claim requires evidence_refs")


DIAGNOSIS_STATUSES = {
    "not_applicable",
    "incomplete",
    "symptom_confirmed",
    "cause_confirmed",
    "no_failure_confirmed",
}


@dataclass(frozen=True)
class DiagnosisAssessment:
    """Structured diagnostic progress; completion is never inferred from prose."""

    status: str = "not_applicable"
    symptom: str = ""
    failure_point: str = ""
    root_cause: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in DIAGNOSIS_STATUSES:
            raise ValueError("invalid diagnosis status: %s" % self.status)
        if self.status == "symptom_confirmed" and not self.symptom.strip():
            raise ValueError("symptom_confirmed requires symptom")
        if self.status == "cause_confirmed":
            if not self.symptom.strip():
                raise ValueError("cause_confirmed requires symptom")
            if not self.failure_point.strip():
                raise ValueError("cause_confirmed requires failure_point")
            if not self.root_cause.strip():
                raise ValueError("cause_confirmed requires root_cause")
        if self.status in {
            "symptom_confirmed", "cause_confirmed", "no_failure_confirmed",
        } and not self.evidence_refs:
            raise ValueError("confirmed diagnosis requires evidence_refs")


@dataclass
class EvidenceConclusion:
    confirmed_facts: list[EvidenceClaim] = field(default_factory=list)
    uncertainties: list[EvidenceClaim] = field(default_factory=list)
    missing_decisions: list[str] = field(default_factory=list)
    diagnosis: DiagnosisAssessment = field(default_factory=DiagnosisAssessment)

    def validate_against(self, bundle: EvidenceBundle) -> None:
        known = bundle.evidence_ids
        for claim in [*self.confirmed_facts, *self.uncertainties]:
            unknown = [ref for ref in claim.evidence_refs if ref not in known]
            if unknown:
                raise ValueError(
                    "unknown evidence reference: %s" % ", ".join(unknown)
                )
        unknown_diagnosis_refs = [
            ref for ref in self.diagnosis.evidence_refs if ref not in known
        ]
        if unknown_diagnosis_refs:
            raise ValueError(
                "unknown diagnosis evidence reference: %s"
                % ", ".join(unknown_diagnosis_refs)
            )


GOAL_OUTCOME_STATUSES = {
    "achieved",
    "need_evidence",
    "need_execution",
    "need_replan",
    "needs_user_decision",
    "blocked",
}


@dataclass(frozen=True)
class GoalOutcome:
    """The sole goal-level completion and next-transition contract."""

    status: str
    reason: str = ""
    evidence_requests: list[ProbeRequest] = field(default_factory=list)
    user_question: str = ""
    failed_criteria: list[str] = field(default_factory=list)
    next_objective: str = ""
    plan: ChangePlan | None = None
    candidate_plan: ChangePlan | None = None
    missing_decisions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in GOAL_OUTCOME_STATUSES:
            raise ValueError("invalid goal outcome status: %s" % self.status)
        if self.status == "need_evidence" and not self.evidence_requests:
            raise ValueError("need_evidence requires evidence_requests")
        if self.status == "needs_user_decision" and not self.user_question:
            raise ValueError("needs_user_decision requires user_question")
        if self.status == "need_execution" and self.plan is None:
            raise ValueError("need_execution requires plan")


class DiscoveryBudgetExceeded(RuntimeError):
    """A bounded Discovery workflow exhausted its safe request budget."""


@dataclass
class DiscoveryBudget:
    max_rounds: int = 3
    max_per_round: int = 4
    max_total_probes: int = 10
    rounds_used: int = 0
    seen_keys: set[str] = field(default_factory=set)

    def register_round(self, requests: list[ProbeRequest]) -> list[ProbeRequest]:
        if self.rounds_used >= self.max_rounds:
            raise DiscoveryBudgetExceeded("discovery round budget exhausted")
        unique: list[ProbeRequest] = []
        round_keys: set[str] = set()
        for request in requests:
            key = request.cache_key
            if key in self.seen_keys or key in round_keys:
                continue
            round_keys.add(key)
            unique.append(request)
        if len(unique) > self.max_per_round:
            raise DiscoveryBudgetExceeded("per-round probe budget exceeded")
        if len(self.seen_keys) + len(unique) > self.max_total_probes:
            raise DiscoveryBudgetExceeded("total probe budget exceeded")
        self.rounds_used += 1
        self.seen_keys.update(round_keys)
        return unique


@dataclass
class ChangeStep:
    step_id: str
    title: str
    objective: str
    risk: str
    expected_changes: list[str]
    postconditions: list[dict[str, Any]]
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    execution_binding: ExecutionBinding | None = None
    implementation_plan: ImplementationPlan | None = None
    status: str = "pending"
    observation: str = ""
    execution_attempts: int = 0
    evidence: ExecutionEvidence | None = None

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError("invalid risk: %s" % self.risk)
        if self.risk == "readonly":
            raise ValueError("ChangeStep cannot be readonly")
        if not self.expected_changes:
            raise ValueError("ChangeStep requires expected_changes")
        if not self.postconditions:
            raise ValueError("ChangeStep requires postconditions")
        if self.execution_binding is not None and self.implementation_plan is not None:
            raise ValueError(
                "ChangeStep cannot have both a direct binding and implementation plan"
            )

    def executable_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "objective": self.objective,
            "reason": self.reason,
            "evidence_refs": self.evidence_refs,
            "depends_on": self.depends_on,
            "risk": self.risk,
            "expected_changes": self.expected_changes,
            "postconditions": self.postconditions,
            "execution_binding": self.execution_binding.executable_dict()
            if self.execution_binding is not None
            else None,
            "implementation_plan": self.implementation_plan.executable_dict()
            if self.implementation_plan is not None
            else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.executable_dict(),
            "execution_binding": self.execution_binding.to_dict()
            if self.execution_binding is not None
            else None,
            "implementation_plan": self.implementation_plan.to_dict()
            if self.implementation_plan is not None
            else None,
            "status": self.status,
            "observation": self.observation,
            "execution_attempts": self.execution_attempts,
            "evidence": self.evidence.to_dict() if self.evidence is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangeStep":
        values = dict(data)
        values["execution_binding"] = ExecutionBinding.from_dict(
            values.get("execution_binding")
        )
        values["implementation_plan"] = ImplementationPlan.from_dict(
            values.get("implementation_plan")
        )
        values["evidence"] = ExecutionEvidence.from_dict(values.get("evidence"))
        return cls(**values)


CHANGE_PLAN_STATUSES = {
    "draft",
    "awaiting_confirmation",
    "approved",
    "executing",
    "verifying",
    "completed",
    "paused",
    "blocked",
    "failed",
    "aborted",
}


@dataclass(frozen=True)
class RecoveryOption:
    option_id: str
    label: str
    description: str
    action: str
    recommended: bool = False
    requires_new_approval: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", self.option_id):
            raise ValueError("invalid recovery option id")
        if self.action not in {
            "component_restart", "collect_more_evidence", "retry_planning",
            "provide_direction", "cancel",
        }:
            raise ValueError("invalid recovery option action")
        if not self.label.strip() or not self.description.strip():
            raise ValueError("recovery option requires label and description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "description": self.description,
            "action": self.action,
            "recommended": self.recommended,
            "requires_new_approval": self.requires_new_approval,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecoveryOption":
        return cls(
            option_id=str(data.get("option_id") or ""),
            label=str(data.get("label") or ""),
            description=str(data.get("description") or ""),
            action=str(data.get("action") or ""),
            recommended=bool(data.get("recommended", False)),
            requires_new_approval=bool(data.get("requires_new_approval", True)),
        )


@dataclass
class FailureRecord:
    failure_id: str
    stage: str
    category: str
    summary: str
    technical_reason: str
    environment_changed: str = "false"
    failed_step: str = ""
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    attempted_recoveries: list[str] = field(default_factory=list)
    automatic_recovery_exhausted: bool = True
    options: list[RecoveryOption] = field(default_factory=list)
    selected_option_id: str = ""
    created_at: str = field(default_factory=_utc_now)
    goal: str = ""
    plan_id: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"failure-[A-Za-z0-9_-]{1,64}", self.failure_id):
            raise ValueError("invalid failure id")
        if self.stage not in {
            "discovery", "synthesis", "planning", "binding", "execution",
            "verification",
        }:
            raise ValueError("invalid failure stage")
        if self.environment_changed not in {"true", "false", "unknown"}:
            raise ValueError("invalid environment_changed state")
        if not self.summary.strip() or not self.technical_reason.strip():
            raise ValueError("failure outcome requires reasons")
        if not self.options:
            raise ValueError("failure outcome requires recovery options")

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "stage": self.stage,
            "category": self.category,
            "summary": self.summary,
            "technical_reason": self.technical_reason,
            "environment_changed": self.environment_changed,
            "failed_step": self.failed_step,
            "completed_steps": list(self.completed_steps),
            "skipped_steps": list(self.skipped_steps),
            "failed_checks": list(self.failed_checks),
            "attempted_recoveries": list(self.attempted_recoveries),
            "automatic_recovery_exhausted": self.automatic_recovery_exhausted,
            "options": [item.to_dict() for item in self.options],
            "selected_option_id": self.selected_option_id,
            "created_at": self.created_at,
            "goal": self.goal,
            "plan_id": self.plan_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FailureRecord | None":
        if not isinstance(data, dict):
            return None
        return cls(
            failure_id=str(data.get("failure_id") or ""),
            stage=str(data.get("stage") or ""),
            category=str(data.get("category") or ""),
            summary=str(data.get("summary") or ""),
            technical_reason=str(data.get("technical_reason") or ""),
            environment_changed=str(data.get("environment_changed") or "false"),
            failed_step=str(data.get("failed_step") or ""),
            completed_steps=[str(item) for item in data.get("completed_steps") or []],
            skipped_steps=[str(item) for item in data.get("skipped_steps") or []],
            failed_checks=[str(item) for item in data.get("failed_checks") or []],
            attempted_recoveries=[
                str(item) for item in data.get("attempted_recoveries") or []
            ],
            automatic_recovery_exhausted=bool(
                data.get("automatic_recovery_exhausted", True)
            ),
            options=[
                RecoveryOption.from_dict(item) for item in data.get("options") or []
                if isinstance(item, dict)
            ],
            selected_option_id=str(data.get("selected_option_id") or ""),
            created_at=str(data.get("created_at") or _utc_now()),
            goal=str(data.get("goal") or ""),
            plan_id=str(data.get("plan_id") or ""),
        )


@dataclass
class ChangePlan:
    plan_id: str
    goal: str
    risk: str
    steps: list[ChangeStep]
    resources: list[PlanResource] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    schema_version: int = 4
    status: str = "draft"
    authorized_hash: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    failure: FailureRecord | None = None

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("priv-ops-"):
            raise ValueError("change plan id must start with priv-ops-")
        if self.schema_version != 4:
            raise ValueError("change plan schema_version must be 4")
        if self.risk not in RISK_LEVELS or self.risk == "readonly":
            raise ValueError("change plan requires mutating risk")
        if not self.steps:
            raise ValueError("change plan requires steps")
        if self.status not in CHANGE_PLAN_STATUSES:
            raise ValueError("invalid change plan status: %s" % self.status)

    @classmethod
    def new(
        cls,
        *,
        goal: str,
        risk: str,
        steps: list[ChangeStep],
        resources: list[PlanResource] | None = None,
        assumptions: list[str] | None = None,
    ) -> "ChangePlan":
        return cls(
            plan_id="priv-ops-" + uuid.uuid4().hex[:10],
            goal=goal,
            risk=risk,
            steps=steps,
            resources=list(resources or []),
            assumptions=list(assumptions or []),
        )

    @property
    def content_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "risk": self.risk,
            "resources": [item.to_dict() for item in self.resources],
            "assumptions": self.assumptions,
            "steps": [item.executable_dict() for item in self.steps],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def is_authorized(self) -> bool:
        return bool(self.authorized_hash) and self.authorized_hash == self.content_hash

    def authorize(self) -> None:
        self.authorized_hash = self.content_hash
        self.status = "approved"
        self.updated_at = _utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "goal": self.goal,
            "risk": self.risk,
            "steps": [item.to_dict() for item in self.steps],
            "resources": [item.to_dict() for item in self.resources],
            "assumptions": self.assumptions,
            "status": self.status,
            "authorized_hash": self.authorized_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangePlan":
        return cls(
            plan_id=str(data.get("plan_id") or ""),
            goal=str(data.get("goal") or ""),
            risk=str(data.get("risk") or ""),
            steps=[
                ChangeStep.from_dict(item)
                for item in data.get("steps", [])
                if isinstance(item, dict)
            ],
            resources=[
                PlanResource.from_dict(item)
                for item in data.get("resources", [])
                if isinstance(item, dict)
            ],
            assumptions=[str(item) for item in data.get("assumptions", [])],
            schema_version=int(data.get("schema_version") or 4),
            status=str(data.get("status") or "draft"),
            authorized_hash=str(data.get("authorized_hash") or ""),
            created_at=str(data.get("created_at") or _utc_now()),
            updated_at=str(data.get("updated_at") or _utc_now()),
            failure=FailureRecord.from_dict(data.get("failure")),
        )
