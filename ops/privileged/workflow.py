"""Ops-Privilege Supervisor：审批、执行、验证与恢复的唯一协调入口。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from klonet_agent.ops.privileged.checkers import ensure_postconditions
from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy
from klonet_agent.ops.privileged.store import PrivilegedPlanStore


COMMAND_PATTERN = re.compile(
    r"^(?:list-priv|"
    r"(?:show-priv|confirm-priv|resume-priv|abort-priv)\s+\S+|"
    r"confirm-priv-step\s+\S+\s+\S+)$"
)


@dataclass
class WorkflowResult:
    kind: str
    message: str
    plan: PrivilegedPlan | None = None


class PrivilegedOpsWorkflow:
    """主 Agent 之外的权限边界；只有此类能够调用命令执行器。"""

    def __init__(
        self,
        *,
        planner: Any,
        executor: Any,
        verifier: Any,
        store: PrivilegedPlanStore,
        event_sink: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.store = store
        self.event_sink = event_sink

    @staticmethod
    def is_control_command(text: str) -> bool:
        return bool(COMMAND_PATTERN.fullmatch(" ".join((text or "").split())))

    def submit(
        self,
        goal: str,
        *,
        environment_context: str = "",
    ) -> WorkflowResult:
        plan = self.planner.plan(goal, environment_context=environment_context)
        self._event("privileged_plan_created", plan)
        if plan.risk == "readonly":
            return self._execute_plan(
                plan,
                persist=False,
                deterministic_verification=True,
            )
        self.store.save(plan)
        if plan.is_authorized:
            self._event("privileged_plan_approved", plan)
            return self._execute_plan(plan)
        return WorkflowResult(
            "awaiting_confirmation",
            render_plan(plan)
            + "\nConfirm once with: confirm-priv %s" % plan.plan_id,
            plan,
        )

    def submit_readonly(self, goal: str, command: str) -> WorkflowResult:
        argv, reason = PrivilegedRiskPolicy().readonly_argv(command)
        if argv is None:
            return WorkflowResult(
                "clarification",
                "Read-only execution refused: command is not deterministically "
                "read-only (%s). Nothing was executed." % reason,
            )
        execute_readonly = getattr(self.executor, "execute_readonly", None)
        if execute_readonly is None:
            return WorkflowResult(
                "blocked",
                "Read-only execution boundary is unavailable. Nothing was executed.",
            )
        postconditions, level = ensure_postconditions(command, [])
        step = PrivilegedStep(
            step_id="readonly-action",
            title="read-only inspection",
            command=command,
            risk="readonly",
            status="approved",
            postconditions=postconditions,
        )
        plan = PrivilegedPlan(
            plan_id="ephemeral-readonly",
            goal=goal,
            risk="readonly",
            status="approved",
            steps=[step],
            verification_level=level,
        )
        return self._execute_plan(
            plan,
            persist=False,
            deterministic_verification=True,
            readonly_argv=argv,
        )

    def handle_command(self, text: str) -> WorkflowResult:
        parts = " ".join((text or "").split()).split(" ")
        command = parts[0] if parts else ""
        try:
            if command == "list-priv" and len(parts) == 1:
                return WorkflowResult("list", render_plan_list(self.store.list()))
            if command == "show-priv" and len(parts) == 2:
                plan = self.store.load(parts[1])
                return WorkflowResult("show", render_plan(plan), plan)
            if command == "confirm-priv" and len(parts) == 2:
                return self.approve_plan(parts[1])
            if command == "confirm-priv-step" and len(parts) == 3:
                return self.approve_step(parts[1], parts[2])
            if command == "resume-priv" and len(parts) == 2:
                return self.resume(parts[1])
            if command == "abort-priv" and len(parts) == 2:
                return self.abort(parts[1])
        except KeyError as exc:
            return WorkflowResult("error", "Error: %s" % exc)
        return WorkflowResult(
            "error",
            "Error: invalid privileged control command",
        )

    def approve_plan(self, plan_id: str) -> WorkflowResult:
        plan = self.store.load(plan_id)
        if plan.status not in {"awaiting_confirmation", "draft"}:
            return WorkflowResult(
                "blocked",
                "Plan is not awaiting confirmation: %s" % plan.status,
                plan,
            )
        plan.authorize()
        for step in plan.steps:
            if step.approval_scope != "step" and step.status == "pending":
                step.status = "approved"
        self.store.save(plan)
        self._event("privileged_plan_approved", plan)
        return self._execute_plan(plan)

    def approve_step(self, plan_id: str, step_id: str) -> WorkflowResult:
        plan = self.store.load(plan_id)
        if not plan.is_authorized:
            return WorkflowResult(
                "blocked",
                "Plan authorization is missing or stale; confirm the current plan first.",
                plan,
            )
        step = _find_step(plan, step_id)
        if step.approval_scope != "step" or step.status != "awaiting_confirmation":
            return WorkflowResult(
                "blocked",
                "Step is not awaiting exact confirmation: %s" % step_id,
                plan,
            )
        step.status = "approved"
        plan.status = "approved"
        self.store.save(plan)
        return self._execute_plan(plan)

    def execute(self, plan_id: str) -> WorkflowResult:
        return self._execute_plan(self.store.load(plan_id))

    def resume(self, plan_id: str) -> WorkflowResult:
        plan = self.store.recover(plan_id)
        unknown_steps = [
            step for step in plan.steps if step.status == "execution_unknown"
        ]
        for step in unknown_steps:
            verify_recovered = getattr(self.verifier, "verify_recovered_step", None)
            if verify_recovered is None:
                self._event(
                    "privileged_plan_blocked",
                    plan,
                    {"unknown_steps": [item.step_id for item in unknown_steps]},
                )
                return WorkflowResult(
                    "blocked",
                    "Execution outcome is unknown for %s. Current state must be checked; "
                    "these steps will not be auto-reexecuted."
                    % ", ".join(item.step_id for item in unknown_steps),
                    plan,
                )
            decision = verify_recovered(plan, step)
            plan.verification = decision
            self._event(
                "privileged_verification",
                plan,
                {"step_id": step.step_id, "decision": decision.status, "recovery": True},
            )
            if decision.status != "passed":
                step.status = "blocked"
                plan.status = "blocked"
                self.store.save(plan)
                self._event("privileged_plan_blocked", plan, {"reason": decision.reason})
                return WorkflowResult("blocked", render_plan(plan), plan)
            step.status = "completed"
            self.store.save(plan)
        if plan.steps and all(step.status == "completed" for step in plan.steps):
            plan.status = "completed"
            self.store.save(plan)
            self._event("privileged_plan_completed", plan, {"recovered": True})
            return WorkflowResult("completed", render_plan(plan), plan)
        if unknown_steps:
            plan.status = "approved"
            self.store.save(plan)
            return self._execute_plan(plan)
        if plan.status in {"approved", "partially_completed"}:
            return self._execute_plan(plan)
        return WorkflowResult("blocked", render_plan(plan), plan)

    def abort(self, plan_id: str) -> WorkflowResult:
        plan = self.store.load(plan_id)
        if plan.status == "completed":
            return WorkflowResult("blocked", "Completed plan cannot be aborted.", plan)
        plan.status = "aborted"
        for step in plan.steps:
            if step.status in {"pending", "approved", "awaiting_confirmation"}:
                step.status = "skipped"
        self.store.save(plan)
        self._event("privileged_plan_blocked", plan, {"reason": "aborted"})
        return WorkflowResult("aborted", render_plan(plan), plan)

    def replan(self, plan_id: str, *, reason: str) -> WorkflowResult:
        """基于失败证据生成替代步骤；任何旧授权都必须失效。"""

        plan = self.store.load(plan_id)
        context = {
            "previous_plan": plan.to_dict(),
            "replan_reason": reason,
        }
        replacement = self.planner.plan(
            plan.goal,
            environment_context=json.dumps(context, ensure_ascii=False),
        )
        plan.risk = replacement.risk
        plan.verification_level = replacement.verification_level
        plan.verification = None
        plan.replace_steps(replacement.steps)
        for step in plan.steps:
            if step.status == "approved":
                step.status = "pending"
        self.store.save(plan)
        self._event(
            "privileged_plan_created",
            plan,
            {"replan": True, "reason": reason},
        )
        return WorkflowResult(
            "awaiting_confirmation",
            render_plan(plan)
            + "\nReplanned content requires fresh authorization: confirm-priv %s"
            % plan.plan_id,
            plan,
        )

    def _execute_plan(
        self,
        plan: PrivilegedPlan,
        *,
        persist: bool = True,
        deterministic_verification: bool = False,
        readonly_argv: list[str] | None = None,
    ) -> WorkflowResult:
        if plan.risk != "readonly" and not plan.is_authorized:
            plan.status = "blocked"
            if persist:
                self.store.save(plan)
            self._event("privileged_plan_blocked", plan, {"reason": "stale authorization"})
            return WorkflowResult(
                "blocked",
                "Plan authorization is missing or stale; execution refused.",
                plan,
            )

        for step in plan.steps:
            if step.status in {"completed", "skipped"}:
                continue
            if step.status == "execution_unknown":
                plan.status = "blocked"
                if persist:
                    self.store.save(plan)
                return WorkflowResult("blocked", render_plan(plan), plan)
            if step.approval_scope == "step" and step.status != "approved":
                plan.status = "awaiting_confirmation"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "awaiting_step_confirmation",
                    render_plan(plan)
                    + "\nConfirm this step with: confirm-priv-step %s %s"
                    % (plan.plan_id, step.step_id),
                    plan,
                )

            precondition_problem = self._precondition_problem(step)
            if precondition_problem:
                step.status = "blocked"
                step.observation = precondition_problem
                plan.status = "blocked"
                if persist:
                    self.store.save(plan)
                self._event(
                    "privileged_plan_blocked",
                    plan,
                    {"step_id": step.step_id, "reason": precondition_problem},
                )
                return WorkflowResult("blocked", render_plan(plan), plan)

            step_readonly_argv = readonly_argv
            if plan.risk == "readonly" and step_readonly_argv is None:
                step_readonly_argv, reason = PrivilegedRiskPolicy().readonly_argv(
                    step.command
                )
                if step_readonly_argv is None:
                    step.status = "blocked"
                    step.observation = (
                        "read-only validation failed before execution: %s" % reason
                    )
                    plan.status = "blocked"
                    return WorkflowResult("blocked", render_plan(plan), plan)
            if (
                step_readonly_argv is not None
                and not hasattr(self.executor, "execute_readonly")
            ):
                step.status = "blocked"
                step.observation = "read-only execution boundary unavailable"
                plan.status = "blocked"
                return WorkflowResult("blocked", render_plan(plan), plan)

            plan.status = "executing"
            step.status = "running"
            if persist:
                self.store.save(plan)
            self._event("privileged_step_started", plan, {"step_id": step.step_id})

            if step_readonly_argv is not None:
                step.evidence = self.executor.execute_readonly(
                    step,
                    step_readonly_argv,
                )
            else:
                step.evidence = self.executor.execute(step)
            step.status = "executed" if not step.evidence.timed_out else "execution_unknown"
            if persist:
                self.store.save(plan)
            self._event(
                "privileged_step_finished",
                plan,
                {
                    "step_id": step.step_id,
                    "return_code": step.evidence.return_code,
                    "timed_out": step.evidence.timed_out,
                },
            )

            plan.status = "verifying"
            if step.status != "execution_unknown":
                step.status = "verifying"
            if persist:
                self.store.save(plan)
            if deterministic_verification:
                verify = getattr(
                    self.verifier,
                    "verify_deterministic_step",
                    self.verifier.verify_step,
                )
                decision = verify(plan, step)
            else:
                decision = self.verifier.verify_step(plan, step)
            plan.verification = decision
            self._event(
                "privileged_verification",
                plan,
                {"step_id": step.step_id, "decision": decision.status},
            )
            if decision.status == "passed":
                step.status = "completed"
                if persist:
                    self.store.save(plan)
                continue
            if decision.status == "replan_required" and persist:
                step.status = "failed"
                plan.status = (
                    "partially_completed"
                    if any(item.status == "completed" for item in plan.steps)
                    else "failed"
                )
                self.store.save(plan)
                return self.replan(plan.plan_id, reason=decision.reason)

            step.status = (
                "failed" if decision.status in {"failed", "replan_required"} else "blocked"
            )
            completed = any(item.status == "completed" for item in plan.steps)
            if decision.status in {"failed", "replan_required"}:
                plan.status = "partially_completed" if completed else "failed"
            else:
                plan.status = "blocked"
            if persist:
                self.store.save(plan)
            self._event("privileged_plan_blocked", plan, {"reason": decision.reason})
            return WorkflowResult(plan.status, render_plan(plan), plan)

        plan.status = "completed"
        if persist:
            self.store.save(plan)
        self._event("privileged_plan_completed", plan)
        return WorkflowResult("completed", render_plan(plan), plan)

    def _precondition_problem(self, step: PrivilegedStep) -> str:
        if not step.preconditions:
            return ""
        registry = getattr(self.verifier, "registry", None)
        if registry is None:
            return "precondition checker registry unavailable"
        results = [registry.run(item, evidence=None) for item in step.preconditions]
        step.checks = results
        problems = [
            "%s=%s" % (item.checker, item.status)
            for item in results
            if item.status != "passed"
        ]
        if problems:
            return "precondition not satisfied: " + ", ".join(problems)
        return ""

    def _event(
        self,
        name: str,
        plan: PrivilegedPlan,
        extra: dict | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        payload = {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "risk": plan.risk,
            "status": plan.status,
        }
        payload.update(extra or {})
        self.event_sink(name, payload)


def _find_step(plan: PrivilegedPlan, step_id: str) -> PrivilegedStep:
    for step in plan.steps:
        if step.step_id == step_id:
            return step
    raise KeyError("unknown privileged step: %s" % step_id)


def render_plan(plan: PrivilegedPlan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)


def render_plan_list(plans: list[PrivilegedPlan]) -> str:
    if not plans:
        return "No privileged plans."
    return "\n".join(
        "%s status=%s risk=%s goal=%s"
        % (plan.plan_id, plan.status, plan.risk, plan.goal)
        for plan in plans
    )
