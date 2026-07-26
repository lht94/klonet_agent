"""独立、证据驱动且受确定性规则约束的 Verifier。"""

from __future__ import annotations

import json
from typing import Any

from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
from klonet_agent.ops.privileged.contracts import (
    PrivilegedPlan,
    PrivilegedStep,
    VerificationDecision,
)
from klonet_agent.ops.privileged.planner import _parse_json_object


VERIFIER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Verifier.
Judge only from the supplied goal, execution evidence, and deterministic check
results. Return one JSON object with status, goal_achieved, reason, and
next_action. A zero exit code alone is not proof of the requested state.
You have no tools and cannot execute or retry commands.
""".strip()


class PrivilegedVerifierAgent:
    def __init__(
        self,
        llm: Any | None,
        registry: DefaultCheckerRegistry | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry or DefaultCheckerRegistry()

    def verify_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision:
        deterministic = self._deterministic_gate(step)
        model_decision = self._model_decision(plan, step)
        if deterministic is not None:
            return deterministic
        if model_decision is not None:
            model_decision.verification_level = plan.verification_level
            if model_decision.status == "passed" and not model_decision.goal_achieved:
                model_decision.status = "inconclusive"
            return model_decision
        return VerificationDecision(
            status="passed",
            goal_achieved=True,
            verification_level=plan.verification_level,
            reason="all deterministic checks passed",
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

    def _model_decision(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision | None:
        if self.llm is None:
            return None
        payload = {
            "goal": plan.goal,
            "step": step.executable_dict(),
            "execution_evidence": step.evidence.to_dict() if step.evidence else None,
            "check_results": [item.to_dict() for item in step.checks],
        }
        messages = [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            response = self.llm.complete(messages=messages, tools=None)
            data = _parse_json_object(response.choices[0].message.content or "")
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return VerificationDecision(
                status="inconclusive",
                goal_achieved=False,
                reason="verifier returned invalid structured output",
                next_action="review deterministic evidence",
            )
        status = str(data.get("status") or "inconclusive")
        if status not in {"passed", "failed", "inconclusive", "replan_required", "blocked"}:
            status = "inconclusive"
        return VerificationDecision(
            status=status,
            goal_achieved=bool(data.get("goal_achieved")),
            reason=str(data.get("reason") or "").strip(),
            next_action=str(data.get("next_action") or "").strip(),
        )
