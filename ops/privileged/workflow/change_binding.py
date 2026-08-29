"""Bind semantic changes to the existing audited execution capabilities."""

from __future__ import annotations

import copy
import inspect
from typing import Any, Callable

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
        checkpoint: Callable[[ChangePlan], None] | None = None,
    ) -> ChangePlan:
        resolutions = list(
            getattr(grounded_context, "facts", {}).get(
                "evidence_resolutions", []
            )
            if grounded_context is not None else []
        )
        invalidated_facts: list[str] = []
        affected_steps: set[str] = set()
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                continue
            contradicted = {
                str(item) for item in resolution.get(
                    "contradicted_fact_ids", []
                )
            }
            if not contradicted:
                continue
            invalidated_facts.extend(sorted(contradicted))
            affected_steps.update(
                str(item) for item in resolution.get("affected_steps", [])
                if str(item)
            )
            expected_values = {
                item.get("expected")
                for item in resolution.get("requirements", [])
                if isinstance(item, dict)
                and str(item.get("fact_id") or "") in contradicted
                and not isinstance(item.get("expected"), (dict, list, set))
            }
            for resource in plan.resources:
                if resource.value not in expected_values:
                    continue
                affected_steps.update(
                    str(consumer).split(".", 1)[0]
                    for consumer in resource.consumers
                    if str(consumer).split(".", 1)[0]
                )
        if invalidated_facts:
            ordered_steps = [
                step.step_id for step in plan.steps
                if step.step_id in affected_steps
            ]
            raise ChangeBindingError(
                "frozen evidence was contradicted before binding: %s"
                % ",".join(sorted(set(invalidated_facts))),
                category="binding_evidence_invalidated",
                replan_recommended=True,
                failed_criteria=[
                    "contradicted frozen fact=%s" % fact_id
                    for fact_id in sorted(set(invalidated_facts))
                ],
                replan_context={
                    "step_id": ordered_steps[0] if ordered_steps else "",
                    "affected_steps": ordered_steps,
                    "invalidated_fact_ids": sorted(set(invalidated_facts)),
                },
            )
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
                    execution_binding=copy.deepcopy(step.execution_binding),
                    implementation_plan=copy.deepcopy(step.implementation_plan),
                )
                for step in plan.steps
            ],
        )
        by_change_id = {step.step_id: step for step in plan.steps}

        def save_checkpoint(
            partial: PrivilegedPlan,
            semantic_step_id: str,
            atomic_step_index: int,
        ) -> None:
            partial_step = next(
                (item for item in partial.steps if item.step_id == semantic_step_id),
                None,
            )
            target = by_change_id.get(semantic_step_id)
            if partial_step is not None and target is not None:
                target.execution_binding = copy.deepcopy(
                    partial_step.execution_binding
                )
                target.implementation_plan = copy.deepcopy(
                    partial_step.implementation_plan
                )
                target.risk = partial_step.risk
            plan.binding_cursor = {
                "phase": "binding",
                "semantic_step_id": semantic_step_id,
                "atomic_step_index": int(atomic_step_index),
            }
            plan.status = "draft"
            plan.authorized_hash = ""
            if checkpoint is not None:
                checkpoint(plan)

        try:
            prepare = self.capability_binder.prepare_plan
            parameters = inspect.signature(prepare).parameters
            kwargs: dict[str, Any] = {"grounded_context": grounded_context}
            if "checkpoint" in parameters:
                kwargs["checkpoint"] = save_checkpoint
            bound = prepare(legacy, **kwargs)
        except ExecutionBindingError as exc:
            raise ChangeBindingError(
                str(exc),
                category=exc.category,
                replan_recommended=exc.replan_recommended,
                failed_criteria=exc.failed_criteria,
                missing_decisions=exc.missing_decisions,
                replan_context=exc.replan_context,
            ) from exc
        except Exception as exc:
            name = type(exc).__name__
            transient = any(marker in name.lower() for marker in (
                "timeout", "connection", "ratelimit", "serviceunavailable",
                "apierror",
            ))
            cursor = dict(plan.binding_cursor)
            raise ChangeBindingError(
                "%s: %s" % (name, str(exc)[:1000]),
                category=(
                    "binding_provider_transient"
                    if transient else "binding_internal_failure"
                ),
                replan_recommended=False,
                failed_criteria=["binding step did not produce an executable contract"],
                replan_context={
                    "resume_binding": bool(cursor),
                    "binding_cursor": cursor,
                    "exception_type": name,
                },
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
        plan.binding_cursor = {}
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
