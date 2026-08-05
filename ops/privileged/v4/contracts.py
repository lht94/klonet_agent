"""High-level contracts for the staged Ops-Privilege V4 workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from klonet_agent.ops.privileged.contracts import RISK_LEVELS


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

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError("invalid risk: %s" % self.risk)
        if self.risk == "readonly":
            raise ValueError("ChangeStepV4 cannot be readonly")
        if not self.expected_changes:
            raise ValueError("ChangeStepV4 requires expected_changes")
        if not self.postconditions:
            raise ValueError("ChangeStepV4 requires postconditions")
