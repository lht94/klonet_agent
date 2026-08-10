"""High-level contracts for the staged Ops-Privilege V4 workflow."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

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


@dataclass(frozen=True)
class EvidenceClaim:
    text: str
    evidence_refs: list[str]

    def __post_init__(self) -> None:
        if not str(self.text or "").strip():
            raise ValueError("evidence claim text is required")
        if not self.evidence_refs:
            raise ValueError("evidence claim requires evidence_refs")


@dataclass
class EvidenceConclusion:
    confirmed_facts: list[EvidenceClaim] = field(default_factory=list)
    uncertainties: list[EvidenceClaim] = field(default_factory=list)
    missing_decisions: list[str] = field(default_factory=list)

    def validate_against(self, bundle: EvidenceBundle) -> None:
        known = bundle.evidence_ids
        for claim in [*self.confirmed_facts, *self.uncertainties]:
            unknown = [ref for ref in claim.evidence_refs if ref not in known]
            if unknown:
                raise ValueError(
                    "unknown evidence reference: %s" % ", ".join(unknown)
                )


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
class ChangeStepV4:
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
            raise ValueError("ChangeStepV4 cannot be readonly")
        if not self.expected_changes:
            raise ValueError("ChangeStepV4 requires expected_changes")
        if not self.postconditions:
            raise ValueError("ChangeStepV4 requires postconditions")
        if self.execution_binding is not None and self.implementation_plan is not None:
            raise ValueError(
                "ChangeStepV4 cannot have both a direct binding and implementation plan"
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
    def from_dict(cls, data: dict[str, Any]) -> "ChangeStepV4":
        values = dict(data)
        values["execution_binding"] = ExecutionBinding.from_dict(
            values.get("execution_binding")
        )
        values["implementation_plan"] = ImplementationPlan.from_dict(
            values.get("implementation_plan")
        )
        values["evidence"] = ExecutionEvidence.from_dict(values.get("evidence"))
        return cls(**values)


V4_PLAN_STATUSES = {
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


@dataclass
class ChangePlanV4:
    plan_id: str
    goal: str
    risk: str
    steps: list[ChangeStepV4]
    resources: list[PlanResource] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    schema_version: int = 4
    status: str = "draft"
    authorized_hash: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("priv-v4-"):
            raise ValueError("V4 plan id must start with priv-v4-")
        if self.schema_version != 4:
            raise ValueError("V4 plan schema_version must be 4")
        if self.risk not in RISK_LEVELS or self.risk == "readonly":
            raise ValueError("V4 change plan requires mutating risk")
        if not self.steps:
            raise ValueError("V4 change plan requires steps")
        if self.status not in V4_PLAN_STATUSES:
            raise ValueError("invalid V4 plan status: %s" % self.status)

    @classmethod
    def new(
        cls,
        *,
        goal: str,
        risk: str,
        steps: list[ChangeStepV4],
        resources: list[PlanResource] | None = None,
        assumptions: list[str] | None = None,
    ) -> "ChangePlanV4":
        return cls(
            plan_id="priv-v4-" + uuid.uuid4().hex[:10],
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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangePlanV4":
        return cls(
            plan_id=str(data.get("plan_id") or ""),
            goal=str(data.get("goal") or ""),
            risk=str(data.get("risk") or ""),
            steps=[
                ChangeStepV4.from_dict(item)
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
        )
