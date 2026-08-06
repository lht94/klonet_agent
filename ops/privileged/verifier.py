"""独立、证据驱动且受确定性规则约束的 Verifier。"""

from __future__ import annotations

from typing import Any
import json

from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
from klonet_agent.ops.privileged.contracts import (
    PrivilegedPlan,
    PrivilegedStep,
    VerificationDecision,
)
from klonet_agent.ops.privileged.action_contracts import _parse_json_object
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES


VERIFIER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Verifier Agent.
Judge whether one semantic step achieved its observable success criteria.
Deterministic failures and non-zero/unknown execution outcomes are hard lower
bounds: you may explain them but never change them to passed.

Return one JSON object. If more evidence is materially required, use:
{"status":"need_evidence","probe_requests":[
 {"probe":"registered read-only probe","args":{},"purpose":"..."}
]}
Otherwise use:
{
 "status":"passed|failed|inconclusive",
 "summary":"concise Chinese result",
 "confirmed_facts":[],
 "failed_criteria":[],
 "missing_evidence":[],
 "reflection":"what the execution result teaches the next Planner",
 "recommended_next_focus":"what evidence or state to address next"
}
Never request mutation, invent facts, or expose secrets.
""".strip()

MAX_VERIFICATION_PROBE_ROUNDS = 2


class PrivilegedVerifierAgent:
    def __init__(
        self,
        llm: Any | None,
        registry: DefaultCheckerRegistry | None = None,
        probe_runner: Any | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry or DefaultCheckerRegistry()
        self.probe_runner = probe_runner

    def verify_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision:
        deterministic = self._deterministic_gate(step)
        if deterministic is not None:
            if self.llm is None:
                return deterministic
            return self._agent_review(
                plan,
                step,
                deterministic_floor=deterministic,
            )
        if self._has_strong_deterministic_success(step):
            return VerificationDecision(
                status="passed",
                goal_achieved=True,
                verification_level=plan.verification_level,
                reason="all deterministic state checks passed",
                confirmed_facts=[
                    "%s=%s" % (item.checker, item.observed)
                    for item in step.checks
                ],
            )
        if self.llm is not None:
            return self._agent_review(
                plan,
                step,
                deterministic_floor=None,
            )
        return VerificationDecision(
            status="passed",
            goal_achieved=True,
            verification_level=plan.verification_level,
            reason=(
                "execution completed and all configured deterministic checks passed"
            ),
        )

    def _agent_review(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        *,
        deterministic_floor: VerificationDecision | None,
    ) -> VerificationDecision:
        payload = {
            "goal": plan.goal,
            "semantic_step": {
                "step_id": step.step_id,
                "objective": step.objective or step.title,
                "reason": step.reason,
                "success_criteria": step.success_criteria,
                "expected_effects": step.expected_changes,
            },
            "execution_binding": (
                step.execution_binding.to_dict()
                if step.execution_binding is not None
                else None
            ),
            "execution_evidence": (
                step.evidence.to_dict() if step.evidence is not None else None
            ),
            "deterministic_checks": [
                item.to_dict() for item in step.checks
            ],
            "deterministic_floor": (
                deterministic_floor.to_dict()
                if deterministic_floor is not None
                else None
            ),
            "registered_probe_catalog": DEFAULT_READONLY_PROBES.render(),
        }
        messages = [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                )[:24000],
            },
        ]
        probe_round = 0
        probe_history = []
        for _attempt in range(4):
            try:
                data = self._json_call(messages)
            except Exception:
                decision = deterministic_floor or VerificationDecision(
                    status="inconclusive",
                    goal_achieved=False,
                    verification_level=plan.verification_level,
                    missing_evidence=["Verifier Agent response unavailable"],
                    reason=(
                        "deterministic checks were insufficient and the"
                        " Verifier Agent did not return a valid decision"
                    ),
                )
                decision.probe_history = list(probe_history)
                return decision
            status = str(data.get("status") or "").strip().lower()
            if status == "need_evidence":
                if (
                    self.probe_runner is None
                    or probe_round >= MAX_VERIFICATION_PROBE_ROUNDS
                ):
                    decision = deterministic_floor or VerificationDecision(
                        status="inconclusive",
                        goal_achieved=False,
                        verification_level=plan.verification_level,
                        missing_evidence=[
                            "Verifier requested unavailable additional evidence"
                        ],
                        reason="verification evidence remains insufficient",
                    )
                    decision.probe_history = list(probe_history)
                    return decision
                requests = self._probe_requests(
                    data.get("probe_requests")
                )
                evidence = self.probe_runner(requests)
                probe_round += 1
                probe_history.append(
                    {
                        "round": probe_round,
                        "requests": requests,
                        "evidence": str(evidence)[:16000],
                    }
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(data, ensure_ascii=False),
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Additional read-only verification evidence:\n%s"
                            % str(evidence)[:16000]
                        ),
                    }
                )
                continue
            if status not in {"passed", "failed", "inconclusive"}:
                raise ValueError("invalid Verifier Agent status")
            if (
                deterministic_floor is not None
                and deterministic_floor.status in {"failed", "blocked"}
            ):
                status = deterministic_floor.status
            return VerificationDecision(
                status=status,
                goal_achieved=status == "passed",
                verification_level=plan.verification_level,
                failures=(
                    list(deterministic_floor.failures)
                    if deterministic_floor is not None
                    else []
                ),
                missing_evidence=_strings(data.get("missing_evidence")),
                reason=str(data.get("summary") or "").strip()[:1200],
                next_action=str(
                    data.get("recommended_next_focus") or ""
                ).strip()[:1000],
                confirmed_facts=_strings(data.get("confirmed_facts")),
                failed_criteria=_strings(data.get("failed_criteria")),
                reflection=str(data.get("reflection") or "").strip()[:2000],
                recommended_next_focus=str(
                    data.get("recommended_next_focus") or ""
                ).strip()[:1000],
                probe_history=probe_history,
            )
        return deterministic_floor or VerificationDecision(
            status="inconclusive",
            goal_achieved=False,
            verification_level=plan.verification_level,
            reason="Verifier Agent did not reach a conclusion",
            probe_history=probe_history,
        )

    def _json_call(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            response = self.llm.complete(
                messages=messages,
                tools=None,
                reasoning_effort="medium",
                temperature=0,
                response_format={"type": "json_object"},
            )
        except TypeError:
            response = self.llm.complete(messages=messages, tools=None)
        content = response.choices[0].message.content or ""
        return _parse_json_object(content)

    @staticmethod
    def _probe_requests(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValueError("Verifier need_evidence requires probes")
        known = {
            spec.name for spec in DEFAULT_READONLY_PROBES.describe()
        }
        result = []
        for item in value[:3]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("probe") or "").strip()
            if name not in known:
                raise ValueError("Verifier probe not registered=%s" % name)
            args = item.get("args")
            result.append(
                {
                    "probe": name,
                    "args": args if isinstance(args, dict) else {},
                    "purpose": str(item.get("purpose") or "").strip()[:500],
                }
            )
        if not result:
            raise ValueError("Verifier probes are empty")
        return result

    @staticmethod
    def _has_strong_deterministic_success(step: PrivilegedStep) -> bool:
        """A real state checker is stronger than an LLM interpretation."""

        required_state_checks = [
            result
            for result in step.checks
            if result.checker != "exit_code_zero"
        ]
        return bool(required_state_checks) and all(
            result.status == "passed" for result in required_state_checks
        )

    def verify_deterministic_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision:
        """Verify a read-only action from execution evidence and Checkers only."""

        deterministic = self._deterministic_gate(step)
        if deterministic is not None:
            return deterministic
        return VerificationDecision(
            status="passed",
            goal_achieved=True,
            verification_level=plan.verification_level,
            reason="all deterministic checks passed",
        )

    def verify_recovered_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision:
        """只检查重启后的当前状态，不依赖也不伪造原执行返回码。"""

        del plan
        usable = [
            specification
            for specification in step.postconditions
            if specification.get("checker") != "exit_code_zero"
        ]
        if not usable:
            return VerificationDecision(
                status="blocked",
                goal_achieved=False,
                missing_evidence=["postcondition independent of exit code"],
                reason="interrupted step has no current-state checker",
                next_action="inspect current state; do not auto-reexecute",
            )
        step.checks = [self.registry.run(item, evidence=None) for item in usable]
        failures = [item.checker for item in step.checks if item.status == "failed"]
        unavailable = [
            item.checker for item in step.checks if item.status == "unavailable"
        ]
        if failures:
            return VerificationDecision(
                status="failed",
                goal_achieved=False,
                failures=failures,
                reason="current state does not satisfy recovered postconditions",
                next_action="create a new plan; do not replay the interrupted step",
            )
        if unavailable:
            return VerificationDecision(
                status="inconclusive",
                goal_achieved=False,
                missing_evidence=unavailable,
                reason="current-state checker unavailable",
                next_action="inspect current state; do not auto-reexecute",
            )
        return VerificationDecision(
            status="passed",
            goal_achieved=True,
            verification_level="recovered-current-state",
            reason="current state satisfies all independent postconditions",
        )

    def _deterministic_gate(
        self,
        step: PrivilegedStep,
    ) -> VerificationDecision | None:
        evidence = step.evidence
        if evidence is None:
            return VerificationDecision(
                status="blocked",
                goal_achieved=False,
                failures=["missing execution evidence"],
                reason="step has no execution evidence",
                next_action="inspect current state; do not auto-reexecute",
            )
        if evidence.timed_out or evidence.return_code is None:
            return VerificationDecision(
                status="blocked",
                goal_achieved=False,
                failures=["execution outcome unknown"],
                reason="execution timed out or was interrupted",
                next_action="inspect current state; do not auto-reexecute",
            )

        step.checks = [
            self.registry.run(specification, evidence=evidence)
            for specification in step.postconditions
        ]
        failures = []
        if evidence.return_code != 0:
            failures.append("return_code=%s" % evidence.return_code)
        failures.extend(
            result.checker for result in step.checks if result.status == "failed"
        )
        unavailable = [
            result.checker for result in step.checks if result.status == "unavailable"
        ]
        if failures:
            return VerificationDecision(
                status="failed",
                goal_achieved=False,
                failures=failures,
                reason="execution or required checks failed",
                next_action="diagnose evidence and create a revised plan",
            )
        if unavailable:
            return VerificationDecision(
                status="inconclusive",
                goal_achieved=False,
                missing_evidence=unavailable,
                reason="required checker unavailable",
                next_action="install or select a deterministic checker",
            )
        return None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()[:1000]
        for item in value[:20]
        if str(item).strip()
    ]
