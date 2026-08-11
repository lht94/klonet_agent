"""Session-scoped operational goal and evidence persistence."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from klonet_agent.ops.privileged.workflow.contracts import (
    EvidenceBundle,
    EvidenceRecord,
    ProbeRequest,
    _utc_now,
)
from klonet_agent.tools.environment import redact_sensitive_text


DYNAMIC_PROBES = frozenset({
    "running_platforms", "platform_health", "ports", "port_owner",
    "process", "process_detail", "process_tree", "process_logs",
    "screen", "screen_session", "http_endpoint", "tcp_connection",
})


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned or "default"


@dataclass
class OperationalContextSnapshot:
    resolved_goal: str = ""
    output_locale: str = "zh-CN"
    target_roots: list[str] = field(default_factory=list)
    phase: str = "diagnosing"
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
            "output_locale": self.output_locale,
            "target_roots": list(self.target_roots),
            "phase": self.phase,
            "updated_at": self.updated_at,
            "evidence": {
                "goal": self.evidence.goal,
                "records": [
                    {
                        "evidence_id": item.evidence_id,
                        "probe": item.request.probe,
                        "args": item.request.args,
                        "purpose": item.request.purpose,
                        "output": redact_sensitive_text(item.output),
                        "status": item.status,
                        "collected_at": item.collected_at,
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
            )
            bundle.add(EvidenceRecord(
                evidence_id=str(raw.get("evidence_id") or "ev-" + request.cache_key[:16]),
                request=request,
                output=str(raw.get("output") or ""),
                status=str(raw.get("status") or "available"),
                collected_at=str(raw.get("collected_at") or _utc_now()),
            ))
        return cls(
            resolved_goal=str(data.get("resolved_goal") or ""),
            output_locale=str(data.get("output_locale") or "zh-CN"),
            target_roots=[str(item) for item in data.get("target_roots") or []],
            phase=str(data.get("phase") or "diagnosing"),
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
