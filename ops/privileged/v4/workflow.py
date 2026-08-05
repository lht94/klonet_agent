"""Confirmation-gated mutation workflow for Ops-Privilege V4."""

from __future__ import annotations

import re
from typing import Any, Iterable

from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.ops.privileged.v4.coordinator import V4WorkflowResult
from klonet_agent.ops.privileged.v4.binding import V4BindingError
from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, ChangeStepV4


class V4MutationWorkflow:
    def __init__(
        self,
        *,
        planner: Any,
        binder: Any,
        store: Any,
        executor: Any,
        verifier: Any,
        discovery: Any | None = None,
        synthesis: Any | None = None,
        max_replanning_rounds: int = 2,
    ) -> None:
        self.planner = planner
        self.binder = binder
        self.store = store
        self.executor = executor
        self.verifier = verifier
        self.discovery = discovery
        self.synthesis = synthesis
        self.max_replanning_rounds = max(0, int(max_replanning_rounds))

    def submit(
        self,
        goal: str,
        *,
        evidence_bundle: Any,
        evidence_conclusion: Any,
        conversation_context: str = "",
    ) -> V4WorkflowResult:
        outcome = self.planner.plan(goal, evidence_bundle, evidence_conclusion)
        replanning_rounds = 0
        while outcome.status == "need_evidence":
            if replanning_rounds >= self.max_replanning_rounds:
                return V4WorkflowResult(
                    True,
                    "blocked",
                    "Planner-to-Discovery evidence budget exhausted.",
                    evidence=evidence_bundle,
                )
            if self.discovery is None or self.synthesis is None:
                return V4WorkflowResult(
                    True,
                    "blocked",
                    "Planner requested evidence but Discovery is unavailable.",
                    evidence=evidence_bundle,
                )
            evidence_bundle = self.discovery.collect_requests(
                outcome.probe_requests,
                evidence_bundle,
            )
            evidence_conclusion = self.synthesis.synthesize(goal, evidence_bundle)
            replanning_rounds += 1
            outcome = self.planner.plan(goal, evidence_bundle, evidence_conclusion)
        if outcome.status != "ready" or outcome.plan is None:
            return V4WorkflowResult(
                True,
                outcome.status,
                outcome.reason or "change planning did not produce an executable plan",
                evidence=evidence_bundle,
            )
        self.store.save(outcome.plan)
        try:
            plan = self.binder.bind(outcome.plan)
        except V4BindingError as exc:
            outcome = self.planner.plan(
                goal,
                evidence_bundle,
                evidence_conclusion,
                binding_feedback=str(exc),
            )
            if outcome.status != "ready" or outcome.plan is None:
                return V4WorkflowResult(
                    True,
                    "blocked",
                    outcome.reason or "binding replan did not produce a ready plan",
                    evidence=evidence_bundle,
                )
            self.store.save(outcome.plan)
            try:
                plan = self.binder.bind(outcome.plan)
            except V4BindingError as final_error:
                outcome.plan.status = "blocked"
                self.store.save(outcome.plan)
                return V4WorkflowResult(
                    True,
                    "blocked",
                    "V4 binding replan budget exhausted: %s" % final_error,
                    plan=outcome.plan,
                    evidence=evidence_bundle,
                )
        plan.status = "awaiting_confirmation"
        plan.authorized_hash = ""
        self.store.save(plan)
        return V4WorkflowResult(
            True,
            "awaiting_confirmation",
            self._confirmation_message(plan),
            plan=plan,
            evidence=evidence_bundle,
        )

    def confirm(self, plan_id: str, content_hash: str) -> V4WorkflowResult:
        plan = self.store.load(plan_id)
        if plan.status != "awaiting_confirmation" or content_hash != plan.content_hash:
            return V4WorkflowResult(
                True,
                "confirmation_rejected",
                "V4 confirmation hash or plan state does not match; nothing executed.",
                plan=plan,
            )
        self._approve_shell_artifacts(plan)
        plan.authorize()
        self.store.save(plan)
        return self._execute(plan)

    def handle_control(self, text: str) -> V4WorkflowResult | None:
        match = re.fullmatch(
            r"confirm-priv-v4\s+(priv-v4-[A-Za-z0-9_-]{1,64})\s+([0-9a-f]{64})",
            str(text or "").strip(),
        )
        if match is None:
            return None
        return self.confirm(match.group(1), match.group(2))

    def _execute(self, plan: ChangePlanV4) -> V4WorkflowResult:
        if not plan.is_authorized:
            return V4WorkflowResult(
                True, "confirmation_rejected", "plan is not exactly authorized", plan=plan
            )
        verification_plan = self._verification_plan(plan)
        for change in plan.steps:
            for step in self._execution_steps(change):
                if step.status in {"completed", "skipped"}:
                    continue
                if step.status == "execution_unknown":
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return V4WorkflowResult(True, "paused", step.observation, plan=plan)
                plan.status = "executing"
                change.status = "running"
                step.status = "running"
                step.execution_attempts += 1
                if change.implementation_plan is None:
                    change.execution_attempts = step.execution_attempts
                self.store.save(plan)
                step.evidence = self.executor.execute(step)
                if change.implementation_plan is None:
                    change.evidence = step.evidence
                    change.execution_attempts = step.execution_attempts
                step.status = "execution_unknown" if step.evidence.timed_out else "verifying"
                plan.status = "verifying"
                self.store.save(plan)
                if step.status == "execution_unknown":
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return V4WorkflowResult(True, "paused", "execution outcome unknown", plan=plan)
                try:
                    decision = self.verifier.verify_step(verification_plan, step)
                except Exception as exc:
                    step.status = "paused"
                    step.observation = "Verifier or Checker failed safely: %s" % exc
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return V4WorkflowResult(
                        True,
                        "paused",
                        step.observation,
                        plan=plan,
                    )
                if decision.status != "passed":
                    step.status = "paused"
                    step.observation = str(getattr(decision, "reason", "verification failed"))
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return V4WorkflowResult(
                        True,
                        "paused",
                        step.observation,
                        plan=plan,
                        verification=decision,
                    )
                step.status = "completed"
                step.observation = str(getattr(decision, "reason", "passed"))
                self.store.save(plan)
            if change.implementation_plan is not None:
                semantic_evidence = change.implementation_plan.steps[-1].evidence
                semantic_step = PrivilegedStep(
                    step_id=change.step_id,
                    title=change.title,
                    objective=change.objective,
                    reason=change.reason,
                    risk=change.risk,
                    expected_changes=list(change.expected_changes),
                    postconditions=list(change.postconditions),
                    status="verifying",
                    evidence=semantic_evidence,
                )
                try:
                    semantic_decision = self.verifier.verify_step(
                        verification_plan,
                        semantic_step,
                    )
                except Exception as exc:
                    change.status = plan.status = "paused"
                    change.observation = (
                        "Semantic Verifier or Checker failed safely: %s" % exc
                    )
                    self.store.save(plan)
                    return V4WorkflowResult(
                        True,
                        "paused",
                        change.observation,
                        plan=plan,
                    )
                if semantic_decision.status != "passed":
                    change.status = plan.status = "paused"
                    change.observation = str(
                        getattr(
                            semantic_decision,
                            "reason",
                            "semantic verification failed",
                        )
                    )
                    self.store.save(plan)
                    return V4WorkflowResult(
                        True,
                        "paused",
                        change.observation,
                        plan=plan,
                        verification=semantic_decision,
                    )
                change.implementation_plan.status = "completed"
            change.status = "completed"
            change.observation = "all executable changes passed verification"
            self.store.save(plan)
        plan.status = "completed"
        self.store.save(plan)
        return V4WorkflowResult(True, "completed", "V4 change plan completed.", plan=plan)

    @staticmethod
    def _execution_steps(change: ChangeStepV4) -> Iterable[PrivilegedStep]:
        if change.implementation_plan is not None:
            return change.implementation_plan.steps
        return [
            PrivilegedStep(
                step_id=change.step_id,
                title=change.title,
                objective=change.objective,
                reason=change.reason,
                evidence_refs=list(change.evidence_refs),
                depends_on=list(change.depends_on),
                risk=change.risk,
                expected_changes=list(change.expected_changes),
                postconditions=list(change.postconditions),
                execution_binding=change.execution_binding,
                status=change.status,
                observation=change.observation,
                execution_attempts=change.execution_attempts,
            )
        ]

    @staticmethod
    def _verification_plan(plan: ChangePlanV4) -> PrivilegedPlan:
        steps = []
        for change in plan.steps:
            steps.extend(V4MutationWorkflow._execution_steps(change))
        return PrivilegedPlan(
            plan_id=plan.plan_id,
            goal=plan.goal,
            risk=plan.risk,
            steps=steps,
            resources=list(plan.resources),
            assumptions=list(plan.assumptions),
            status="approved",
            verification_level="full",
        )

    @staticmethod
    def _approve_shell_artifacts(plan: ChangePlanV4) -> None:
        for change in plan.steps:
            for step in V4MutationWorkflow._execution_steps(change):
                binding = step.execution_binding
                if binding is None or binding.shell_artifact is None:
                    continue
                artifact = binding.shell_artifact
                artifact.approved_contract_hash = artifact.contract_hash
                artifact.status = "approved"

    @staticmethod
    def _confirmation_message(plan: ChangePlanV4) -> str:
        lines = [
            "V4 change plan %s" % plan.plan_id,
            "Goal: %s" % plan.goal,
            "Risk: %s" % plan.risk,
        ]
        if plan.resources:
            lines.append("Frozen resources:")
            for resource in plan.resources:
                lines.append(
                    "- %s (%s/%s): %s"
                    % (resource.name, resource.kind, resource.status, resource.value)
                )
        lines.append("Changes:")
        for change in plan.steps:
            lines.append(
                "- %s: %s — %s"
                % (change.step_id, change.title, change.objective)
            )
            lines.append("  expected: %s" % "; ".join(change.expected_changes))
            lines.append(
                "  verify: %s"
                % ", ".join(
                    str(item.get("checker") or "unknown")
                    for item in change.postconditions
                )
            )
            for step in V4MutationWorkflow._execution_steps(change):
                binding = step.execution_binding
                if binding is None:
                    lines.append("  binding: missing")
                    continue
                if binding.kind == "registered_action":
                    lines.append(
                        "  binding: registered_action: %s args=%s"
                        % (binding.action, binding.args)
                    )
                    continue
                artifact = binding.shell_artifact
                lines.append(
                    "  binding: shell_artifact: %s sha256=%s"
                    % (
                        artifact.artifact_id if artifact is not None else "missing",
                        artifact.sha256 if artifact is not None else "missing",
                    )
                )
                if artifact is not None:
                    lines.extend(
                        [
                            "  script:",
                            "```bash",
                            artifact.script,
                            "```",
                        ]
                    )
        lines.extend(
            [
                "Exact plan hash: %s" % plan.content_hash,
                "Confirm this exact V4 change plan with:",
                "confirm-priv-v4 %s %s" % (plan.plan_id, plan.content_hash),
            ]
        )
        return "\n".join(lines)
