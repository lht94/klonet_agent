"""Mutation-only Change Planner for Ops-Privilege V4."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from klonet_agent.ops.privileged.contracts import PlanResource, RISK_LEVELS
from klonet_agent.ops.privileged.v4.contracts import (
    ChangePlanV4,
    ChangeStepV4,
    EvidenceBundle,
    EvidenceConclusion,
    ProbeRequest,
)
from klonet_agent.ops.privileged.v4.discovery import parse_json_object


CHANGE_PLANNER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege V4 Change Planner.
Plan only real host state changes. Discovery, inspection, evidence aggregation,
summaries, reports, answers and verification are separate workflow phases and
must never appear as changes. Do not select Action names or emit commands.

Return one JSON object with status `need_evidence`, `ready`, or `blocked`.
For need_evidence return at most four registered read-only probe_requests.
For blocked return reason and missing_decisions.
For ready return goal, assumptions, frozen/deferred resources and `changes`.
Every change needs step_id, title, objective, reason, evidence_refs, depends_on,
risk, non-empty expected_changes, and non-empty structured postconditions.
Risk cannot be readonly. Evidence references must be supplied evidence IDs.
""".strip()


@dataclass
class V4PlanningOutcome:
    status: str
    plan: ChangePlanV4 | None = None
    probe_requests: list[ProbeRequest] = field(default_factory=list)
    reason: str = ""
    missing_decisions: list[str] = field(default_factory=list)


class V4ChangePlannerAgent:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def plan(
        self,
        goal: str,
        bundle: EvidenceBundle,
        conclusion: EvidenceConclusion,
        *,
        binding_feedback: str = "",
    ) -> V4PlanningOutcome:
        messages = [
            {"role": "system", "content": CHANGE_PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Goal:\n%s\n\nEvidence conclusion:\n%s\n\nEvidence records:\n%s"
                % (
                    goal,
                    self._conclusion_json(conclusion),
                    self._evidence_json(bundle),
                ),
            },
        ]
        if binding_feedback:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous semantic plan could not be bound to an audited "
                        "Action or Shell capability. Replan once without changing the "
                        "goal or inventing evidence. Binding feedback: %s"
                        % binding_feedback
                    ),
                }
            )
        last_error: Exception | None = None
        content = ""
        for attempt in range(2):
            try:
                response = self.llm.complete(
                    messages=messages,
                    tools=None,
                    reasoning_effort="low",
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                content = response.choices[0].message.content or ""
                return self._outcome(parse_json_object(content), goal, bundle)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": "Repair the Change Planner JSON. Error: %s" % exc,
                        }
                    )
        raise ValueError(str(last_error or "invalid Change Planner output"))

    def _outcome(
        self,
        data: dict[str, Any],
        goal: str,
        bundle: EvidenceBundle,
    ) -> V4PlanningOutcome:
        status = str(data.get("status") or "").strip().lower()
        if status == "need_evidence":
            return V4PlanningOutcome(
                status=status,
                probe_requests=self._probe_requests(data.get("probe_requests")),
            )
        if status == "blocked":
            missing = data.get("missing_decisions")
            return V4PlanningOutcome(
                status=status,
                reason=str(data.get("reason") or "planning blocked"),
                missing_decisions=[str(item) for item in missing]
                if isinstance(missing, list)
                else [],
            )
        if status != "ready":
            raise ValueError("planner status must be need_evidence, ready, or blocked")
        resources = [
            PlanResource.from_dict(item)
            for item in data.get("resources", [])
            if isinstance(item, dict)
        ]
        steps = self._steps(data.get("changes"), bundle)
        risk = max(steps, key=lambda item: RISK_LEVELS.index(item.risk)).risk
        assumptions = data.get("assumptions")
        return V4PlanningOutcome(
            status="ready",
            plan=ChangePlanV4.new(
                goal=str(data.get("goal") or goal),
                risk=risk,
                steps=steps,
                resources=resources,
                assumptions=[str(item) for item in assumptions]
                if isinstance(assumptions, list)
                else [],
            ),
        )

    @staticmethod
    def _steps(value: Any, bundle: EvidenceBundle) -> list[ChangeStepV4]:
        if not isinstance(value, list) or not value:
            raise ValueError("ready Change Planner output requires changes")
        known = bundle.evidence_ids
        steps = []
        known_step_ids = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("change must be an object")
            refs = [str(ref) for ref in item.get("evidence_refs", [])]
            unknown = [ref for ref in refs if ref not in known]
            if unknown:
                raise ValueError("unknown evidence reference: %s" % ", ".join(unknown))
            dependencies = [str(dep) for dep in item.get("depends_on", [])]
            if any(dep not in known_step_ids for dep in dependencies):
                raise ValueError("change dependency must reference an earlier step")
            step = ChangeStepV4(
                step_id=str(item.get("step_id") or ""),
                title=str(item.get("title") or ""),
                objective=str(item.get("objective") or ""),
                reason=str(item.get("reason") or ""),
                evidence_refs=refs,
                depends_on=dependencies,
                risk=str(item.get("risk") or ""),
                expected_changes=[str(change) for change in item.get("expected_changes", [])],
                postconditions=[
                    dict(check)
                    for check in item.get("postconditions", [])
                    if isinstance(check, dict)
                ],
            )
            if not step.step_id or step.step_id in known_step_ids:
                raise ValueError("change step_id must be unique and non-empty")
            known_step_ids.add(step.step_id)
            steps.append(step)
        return steps

    @staticmethod
    def _probe_requests(value: Any) -> list[ProbeRequest]:
        if not isinstance(value, list):
            return []
        return [
            ProbeRequest(
                str(item.get("probe") or ""),
                item.get("args") if isinstance(item.get("args"), dict) else {},
                str(item.get("purpose") or "resolve planning evidence gap"),
            )
            for item in value
            if isinstance(item, dict)
        ]

    @staticmethod
    def _evidence_json(bundle: EvidenceBundle) -> str:
        return json.dumps(
            [
                {
                    "evidence_id": item.evidence_id,
                    "probe": item.request.probe,
                    "status": item.status,
                    "output": item.output[:7000],
                }
                for item in bundle.records
            ],
            ensure_ascii=False,
        )

    @staticmethod
    def _conclusion_json(conclusion: EvidenceConclusion) -> str:
        return json.dumps(
            {
                "confirmed_facts": [
                    {"text": item.text, "evidence_refs": item.evidence_refs}
                    for item in conclusion.confirmed_facts
                ],
                "uncertainties": [
                    {"text": item.text, "evidence_refs": item.evidence_refs}
                    for item in conclusion.uncertainties
                ],
                "missing_decisions": conclusion.missing_decisions,
            },
            ensure_ascii=False,
        )
