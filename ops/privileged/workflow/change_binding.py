"""Bind semantic changes to the existing audited execution capabilities."""

from __future__ import annotations

from typing import Any

from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
from klonet_agent.ops.privileged.workflow.contracts import ChangePlan


class ChangeBindingError(ValueError):
    """A semantic change could not be represented by an executable capability."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "semantic_binding",
        replan_recommended: bool = True,
        failed_criteria: list[str] | None = None,
        missing_decisions: list[str] | None = None,
        replan_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.replan_recommended = replan_recommended
        self.failed_criteria = list(failed_criteria or [])
        self.missing_decisions = list(missing_decisions or [])
        self.replan_context = dict(replan_context or {})


class ChangeBinder:
    """Narrow adapter around the shared Action/Shell capability binder."""

    def __init__(self, capability_binder: Any) -> None:
        self.capability_binder = capability_binder

    def bind(
        self,
        plan: ChangePlan,
        *,
        grounded_context: Any | None = None,
    ) -> ChangePlan:
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
            raise ChangeBindingError(
                str(exc),
                category=exc.category,
                replan_recommended=exc.replan_recommended,
                failed_criteria=exc.failed_criteria,
                missing_decisions=exc.missing_decisions,
                replan_context=exc.replan_context,
            ) from exc
        by_id = {step.step_id: step for step in bound.steps}
        for change in plan.steps:
            implementation = by_id[change.step_id]
            self._lift_hierarchical_verification(change, implementation)
            self._validate_step_bindings(implementation)
            change.execution_binding = implementation.execution_binding
            change.implementation_plan = implementation.implementation_plan
            change.risk = implementation.risk
        plan.risk = bound.risk
        plan.status = "awaiting_confirmation"
        plan.authorized_hash = ""
        return plan

    @staticmethod
    def _lift_hierarchical_verification(change: Any, step: PrivilegedStep) -> None:
        implementation = step.implementation_plan
        if implementation is None:
            return
        verification_steps = [
            item
            for item in implementation.steps
            if item.execution_binding is not None
            and item.execution_binding.kind == "verification_only"
        ]
        if not verification_steps:
            return
        verification_ids = {item.step_id for item in verification_steps}
        executable = [
            item for item in implementation.steps if item.step_id not in verification_ids
        ]
        if not executable:
            raise ChangeBindingError(
                "hierarchical change has no Action or Shell implementation"
            )
        by_id = {item.step_id: item for item in verification_steps}
        precondition_ids = {
            dependency
            for item in executable
            for dependency in item.depends_on
            if dependency in verification_ids
        }

        def expand(dependency: str, seen: set[str] | None = None) -> list[str]:
            if dependency not in verification_ids:
                return [dependency]
            seen = set(seen or ())
            if dependency in seen:
                return []
            seen.add(dependency)
            result = []
            for predecessor in by_id[dependency].depends_on:
                for expanded in expand(predecessor, seen):
                    if expanded not in result:
                        result.append(expanded)
            return result

        for item in executable:
            rewired = []
            for dependency in item.depends_on:
                for expanded in expand(dependency):
                    if expanded != item.step_id and expanded not in rewired:
                        rewired.append(expanded)
            item.depends_on = rewired
        for verification in verification_steps:
            if verification.step_id in precondition_ids:
                continue
            binding = verification.execution_binding
            for postcondition in binding.postconditions:
                if postcondition not in change.postconditions:
                    change.postconditions.append(dict(postcondition))
        implementation.steps = executable

    def _validate_step_bindings(self, step: PrivilegedStep) -> None:
        candidates = (
            step.implementation_plan.steps
            if step.implementation_plan is not None
            else [step]
        )
        for candidate in candidates:
            binding = candidate.execution_binding
            if binding is None:
                raise ChangeBindingError(
                    "missing execution binding for %s" % candidate.step_id
                )
            if binding.kind not in {"registered_action", "shell_artifact"}:
                raise ChangeBindingError(
                    "%s is not a workflow execution capability: %s"
                    % (binding.kind, candidate.step_id)
                )
