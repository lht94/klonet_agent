"""Bind V4 semantic changes to the existing audited execution capabilities."""

from __future__ import annotations

from typing import Any

from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4


class V4BindingError(ValueError):
    """A semantic change could not be represented by an executable capability."""


class V4ChangeBinder:
    """Narrow adapter around the shared Action/Shell capability binder."""

    def __init__(self, capability_binder: Any) -> None:
        self.capability_binder = capability_binder

    def bind(
        self,
        plan: ChangePlanV4,
        *,
        grounded_context: Any | None = None,
    ) -> ChangePlanV4:
        legacy = PrivilegedPlan(
            plan_id=plan.plan_id,
            goal=plan.goal,
            risk=plan.risk,
            resources=list(plan.resources),
            assumptions=list(plan.assumptions),
            steps=[
                PrivilegedStep(
                    step_id=step.step_id,
                    title=step.title,
                    objective=step.objective,
                    reason=step.reason,
                    evidence_refs=list(step.evidence_refs),
                    depends_on=list(step.depends_on),
                    risk=step.risk,
                    expected_changes=list(step.expected_changes),
                    postconditions=list(step.postconditions),
                )
                for step in plan.steps
            ],
        )
        try:
            bound = self.capability_binder.prepare_plan(
                legacy,
                grounded_context=grounded_context,
            )
        except ExecutionBindingError as exc:
            raise V4BindingError(str(exc)) from exc
        by_id = {step.step_id: step for step in bound.steps}
        for change in plan.steps:
            implementation = by_id[change.step_id]
            self._validate_step_bindings(implementation)
            change.execution_binding = implementation.execution_binding
            change.implementation_plan = implementation.implementation_plan
            change.risk = implementation.risk
        plan.risk = bound.risk
        plan.status = "awaiting_confirmation"
        plan.authorized_hash = ""
        return plan

    def _validate_step_bindings(self, step: PrivilegedStep) -> None:
        candidates = (
            step.implementation_plan.steps
            if step.implementation_plan is not None
            else [step]
        )
        for candidate in candidates:
            binding = candidate.execution_binding
            if binding is None:
                raise V4BindingError(
                    "missing execution binding for %s" % candidate.step_id
                )
            if binding.kind not in {"registered_action", "shell_artifact"}:
                raise V4BindingError(
                    "%s is not a V4 execution capability: %s"
                    % (binding.kind, candidate.step_id)
                )
