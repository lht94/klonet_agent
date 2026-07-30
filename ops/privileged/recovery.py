"""Evidence-driven recovery analysis for failed privileged plans."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.tools.environment import redact_sensitive_text


RECOVERY_ANALYZER_PROMPT = """
You are the Klonet privileged-operation recovery analyst.
Analyze one failed step without proposing shell commands. Return one JSON object:
{
  "summary": "concise Chinese explanation of what is known",
  "hypotheses": ["ranked cause hypothesis"],
  "confirmed_cause": "cause only when directly supported, otherwise empty",
  "probes": [
    {"probe": "one allowed probe name", "args": {}, "purpose": "why needed"}
  ],
  "required_capability": "what a safe repair action must be able to do"
}
Use at most 4 probes. Probe arguments must come from supplied evidence. Do not
invent paths, services, sessions, ports or process IDs. Never request mutation.
Allowed probes are supplied by the caller.
""".strip()


RECOVERY_CONCLUSION_PROMPT = """
You are the Klonet recovery evidence reviewer. Given the original failure,
initial hypotheses and new read-only evidence, return one JSON object:
{
  "summary": "clear Chinese root-cause explanation for the user",
  "confirmed_cause": "best evidence-backed cause, or empty",
  "remaining_uncertainty": ["facts still unknown"],
  "required_capability": "what an executable repair must change",
  "planning_guidance": "constraints the repair planner must follow"
}
Do not invent facts or commands.
""".strip()


RECOVERY_REVIEW_PROMPT = """
You review whether a proposed registered-action repair plan actually addresses
the diagnosed failure. Return one JSON object:
{
  "covers_cause": true,
  "explanation": "concise Chinese reason",
  "missing_capability": ""
}
Set covers_cause=false when the plan merely repeats the failed action, relies on
an unsupported capability, or does not address the confirmed cause. Do not judge
authorization; deterministic code handles that.
""".strip()


@dataclass
class RecoveryAnalysis:
    summary: str = ""
    hypotheses: list[str] = field(default_factory=list)
    confirmed_cause: str = ""
    probes: list[dict[str, Any]] = field(default_factory=list)
    required_capability: str = ""


@dataclass
class RecoveryConclusion:
    summary: str = ""
    confirmed_cause: str = ""
    remaining_uncertainty: list[str] = field(default_factory=list)
    required_capability: str = ""
    planning_guidance: str = ""


@dataclass
class RecoveryPlanReview:
    covers_cause: bool
    explanation: str = ""
    missing_capability: str = ""


class PrivilegedRecoveryAgent:
    """Analyze, request safe probes, and critique a repair plan."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def analyze(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        *,
        probe_catalog: str,
    ) -> RecoveryAnalysis:
        data = self._json_call(
            RECOVERY_ANALYZER_PROMPT,
            {
                "goal": plan.goal,
                "failed_step": _safe_step(step),
                "probe_catalog": probe_catalog,
            },
        )
        probes = data.get("probes") if isinstance(data.get("probes"), list) else []
        return RecoveryAnalysis(
            summary=_text(data.get("summary"), 500),
            hypotheses=_text_list(data.get("hypotheses"), 6),
            confirmed_cause=_text(data.get("confirmed_cause"), 800),
            probes=[item for item in probes[:4] if isinstance(item, dict)],
            required_capability=_text(data.get("required_capability"), 800),
        )

    def conclude(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        analysis: RecoveryAnalysis,
        diagnostic_evidence: str,
    ) -> RecoveryConclusion:
        data = self._json_call(
            RECOVERY_CONCLUSION_PROMPT,
            {
                "goal": plan.goal,
                "failed_step": _safe_step(step),
                "initial_analysis": analysis.__dict__,
                "diagnostic_evidence": redact_sensitive_text(
                    diagnostic_evidence
                )[:18000],
            },
        )
        return RecoveryConclusion(
            summary=_text(data.get("summary"), 1000),
            confirmed_cause=_text(data.get("confirmed_cause"), 1000),
            remaining_uncertainty=_text_list(
                data.get("remaining_uncertainty"),
                8,
            ),
            required_capability=_text(data.get("required_capability"), 1000),
            planning_guidance=_text(data.get("planning_guidance"), 1200),
        )

    def review_plan(
        self,
        failed_step: PrivilegedStep,
        conclusion: RecoveryConclusion,
        replacement: PrivilegedPlan,
    ) -> RecoveryPlanReview:
        data = self._json_call(
            RECOVERY_REVIEW_PROMPT,
            {
                "failed_step": _safe_step(failed_step),
                "diagnosis": conclusion.__dict__,
                "proposed_plan": {
                    "goal": replacement.goal,
                    "steps": [
                        {
                            "title": step.title,
                            "action": step.action,
                            "args": step.args,
                            "risk": step.risk,
                        }
                        for step in replacement.steps
                    ],
                },
            },
        )
        return RecoveryPlanReview(
            covers_cause=data.get("covers_cause") is True,
            explanation=_text(data.get("explanation"), 800),
            missing_capability=_text(data.get("missing_capability"), 800),
        )

    def _json_call(self, system_prompt: str, payload: dict[str, Any]) -> dict:
        safe_payload = redact_sensitive_text(
            json.dumps(payload, ensure_ascii=False)
        )[:24000]
        try:
            response = self.llm.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": safe_payload},
                ],
                tools=None,
                reasoning_effort="low",
                temperature=0,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
        except TypeError:
            response = self.llm.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": safe_payload},
                ],
                tools=None,
            )
        choices = getattr(response, "choices", None) or []
        content = getattr(
            getattr(choices[0], "message", None),
            "content",
            "",
        ) if choices else ""
        data = json.loads(str(content or ""))
        if not isinstance(data, dict):
            raise ValueError("recovery model did not return an object")
        return data


def _safe_step(step: PrivilegedStep) -> dict[str, Any]:
    evidence = step.evidence
    return {
        "title": step.title,
        "action": step.action,
        "args": step.args,
        "risk": step.risk,
        "status": step.status,
        "return_code": evidence.return_code if evidence else None,
        "timed_out": evidence.timed_out if evidence else False,
        "environment_changed": evidence.environment_changed if evidence else False,
        "evidence": redact_sensitive_text(
            (evidence.stderr or evidence.stdout) if evidence else ""
        )[:10000],
        "checks": [item.to_dict() for item in step.checks],
    }


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _text_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, 500) for item in value[:limit] if _text(item, 500)]
