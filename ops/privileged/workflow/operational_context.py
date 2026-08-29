"""Session-scoped operational goal and evidence persistence."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from klonet_agent.ops.privileged.workflow.contracts import (
    EvidenceSubject,
    EvidenceBundle,
    EvidenceRecord,
    FactObservation,
    ProbeRequest,
    _utc_now,
)
from klonet_agent.tools.environment import redact_sensitive_text


DYNAMIC_PROBES = frozenset({
    "running_platforms", "platform_health", "ports", "port_owner",
    "process", "process_detail", "process_tree", "process_logs",
    "screen", "screen_session", "http_endpoint", "tcp_connection",
    "readonly_command",
})


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned or "default"


@dataclass
class OperationalContextSnapshot:
    resolved_goal: str = ""
    base_goal: str = ""
    decision_history: list[str] = field(default_factory=list)
    pending_goal_revision: str = ""
    pending_goal_relation: str = ""
    active_plan_id: str = ""
    workflow_intent: str = ""
    goal_kind: str = ""
    operation: str = "none"
    scope: str = "none"
    components: list[str] = field(default_factory=list)
    output_locale: str = "zh-CN"
    target_roots: list[str] = field(default_factory=list)
    evidence: EvidenceBundle = field(default_factory=lambda: EvidenceBundle(goal=""))
    updated_at: str = field(default_factory=_utc_now)

    def reusable_evidence(self, goal: str) -> EvidenceBundle:
        """Return static evidence only; volatile runtime facts must be refreshed."""

        bundle = EvidenceBundle(goal=goal)
        for record in self.evidence.records:
            if record.request.probe in DYNAMIC_PROBES:
                continue
            bundle.add(record)
        return bundle

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved_goal": self.resolved_goal,
            "base_goal": self.base_goal,
            "decision_history": list(self.decision_history),
            "pending_goal_revision": self.pending_goal_revision,
            "pending_goal_relation": self.pending_goal_relation,
            "active_plan_id": self.active_plan_id,
            "workflow_intent": self.workflow_intent,
            "goal_kind": self.goal_kind,
            "operation": self.operation,
            "scope": self.scope,
            "components": list(self.components),
            "output_locale": self.output_locale,
            "target_roots": list(self.target_roots),
            "updated_at": self.updated_at,
            "evidence": {
                "goal": self.evidence.goal,
                "records": [
                    {
                        "evidence_id": item.evidence_id,
                        "probe": item.request.probe,
                        "args": item.request.args,
                        "purpose": item.request.purpose,
                        "required_facts": [
                            fact.to_dict() for fact in item.request.required_facts
                        ],
                        "freshness": item.request.freshness,
                        "gap_id": item.request.gap_id,
                        "affected_steps": list(item.request.affected_steps),
                        "subject": (
                            item.request.subject.to_dict()
                            if item.request.subject is not None else None
                        ),
                        "scope": list(item.request.scope),
                        "exclusions": list(item.request.exclusions),
                        "output": redact_sensitive_text(item.output),
                        "status": item.status,
                        "collected_at": item.collected_at,
                        "observations": [
                            observation.to_dict()
                            for observation in item.observations
                        ],
                    }
                    for item in self.evidence.records
                ],
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationalContextSnapshot":
        evidence_data = data.get("evidence") if isinstance(data, dict) else {}
        evidence_data = evidence_data if isinstance(evidence_data, dict) else {}
        bundle = EvidenceBundle(goal=str(evidence_data.get("goal") or ""))
        for raw in evidence_data.get("records") or []:
            if not isinstance(raw, dict):
                continue
            request = ProbeRequest(
                str(raw.get("probe") or ""),
                dict(raw.get("args") or {}),
                str(raw.get("purpose") or ""),
                tuple(raw.get("required_facts") or []),
                str(raw.get("freshness") or "cached"),
                str(raw.get("gap_id") or ""),
                tuple(str(item) for item in raw.get("affected_steps") or []),
                EvidenceSubject.from_value(raw.get("subject")),
                tuple(str(item) for item in raw.get("scope") or []),
                tuple(str(item) for item in raw.get("exclusions") or []),
            )
            bundle.add(EvidenceRecord(
                evidence_id=str(raw.get("evidence_id") or "ev-" + request.cache_key[:16]),
                request=request,
                output=str(raw.get("output") or ""),
                status=str(raw.get("status") or "available"),
                collected_at=str(raw.get("collected_at") or _utc_now()),
                observations=tuple(
                    FactObservation.from_dict(item)
                    for item in raw.get("observations") or []
                    if isinstance(item, dict)
                ),
            ))
        return cls(
            resolved_goal=str(data.get("resolved_goal") or ""),
            base_goal=str(data.get("base_goal") or data.get("resolved_goal") or ""),
            decision_history=[str(item) for item in data.get("decision_history") or []],
            pending_goal_revision=str(data.get("pending_goal_revision") or ""),
            pending_goal_relation=str(data.get("pending_goal_relation") or ""),
            active_plan_id=str(data.get("active_plan_id") or ""),
            workflow_intent=str(data.get("workflow_intent") or ""),
            goal_kind=str(data.get("goal_kind") or ""),
            operation=str(data.get("operation") or "none"),
            scope=str(data.get("scope") or "none"),
            components=[str(item) for item in data.get("components") or []],
            output_locale=str(data.get("output_locale") or "zh-CN"),
            target_roots=[str(item) for item in data.get("target_roots") or []],
            evidence=bundle,
            updated_at=str(data.get("updated_at") or _utc_now()),
        )


class OperationalContextStore:
    def __init__(self, memory_root: Path, *, user_id: str, project_id: str) -> None:
        self.path = (
            Path(memory_root) / "sessions" / _safe_component(user_id)
            / _safe_component(project_id) / "privileged_operational_context.json"
        )

    def load(self) -> OperationalContextSnapshot | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return OperationalContextSnapshot.from_dict(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save(self, snapshot: OperationalContextSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        snapshot.updated_at = _utc_now()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(self.path))
