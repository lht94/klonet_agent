"""Ops-Privilege Supervisor：审批、执行、验证与恢复的唯一协调入口。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from klonet_agent.ops.privileged.checkers import ensure_postconditions
from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy
from klonet_agent.ops.privileged.store import PrivilegedPlanStore
from klonet_agent.tools.environment import redact_sensitive_text


COMMAND_PATTERN = re.compile(
    r"^(?:list-priv|"
    r"(?:show-priv|audit-priv|confirm-priv|resume-priv|abort-priv|continue-priv|"
    r"retry-priv|replan-priv)\s+\S+|"
    r"confirm-priv-step\s+\S+\s+\S+)$"
)


@dataclass
class WorkflowResult:
    kind: str
    message: str
    plan: PrivilegedPlan | None = None


class RecoveryPlanUnavailable(Exception):
    """A natural-language recovery limitation safe to show to the user."""


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
        context_builder: Any | None = None,
        summarizer: Any | None = None,
        recovery_agent: Any | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.store = store
        self.event_sink = event_sink
        self.context_builder = context_builder
        self.summarizer = summarizer
        self.recovery_agent = recovery_agent
        self.on_progress = on_progress

    @staticmethod
    def is_control_command(text: str) -> bool:
        return bool(COMMAND_PATTERN.fullmatch(" ".join((text or "").split())))

    def submit(
        self,
        goal: str,
        *,
        environment_context: str = "",
    ) -> WorkflowResult:
        try:
            grounded_context = (
                self.context_builder.build(
                    goal,
                    supplemental_environment_context=environment_context,
                )
                if self.context_builder is not None
                else None
            )
            if grounded_context is not None:
                blocker = grounded_context.planning_blocker()
                if blocker:
                    return WorkflowResult(
                        "blocked",
                        "%s，因此没有生成或执行操作计划。请先恢复知识索引或环境探测后重试。"
                        % blocker,
                    )
            planner_kwargs = {"environment_context": environment_context}
            if grounded_context is not None:
                planner_kwargs["grounded_context"] = grounded_context
            plan = self.planner.plan(goal, **planner_kwargs)
        except PermissionError as exc:
            return WorkflowResult(
                "denied",
                "该操作计划被安全策略拒绝：%s" % exc,
            )
        except Exception:
            return WorkflowResult(
                "blocked",
                "安全计划生成失败或超时，当前没有执行任何操作。请稍后重试。",
            )
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
            + "\n\n确认执行：confirm-priv %s" % plan.plan_id
            + "\n查看完整计划：show-priv %s" % plan.plan_id,
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
                return WorkflowResult(
                    "show",
                    self._render_detailed_plan(plan),
                    plan,
                )
            if command == "audit-priv" and len(parts) == 2:
                plan = self.store.load(parts[1])
                return WorkflowResult("audit", render_plan_audit(plan), plan)
            if command == "confirm-priv" and len(parts) == 2:
                return self.approve_plan(parts[1])
            if command == "confirm-priv-step" and len(parts) == 3:
                return self.approve_step(parts[1], parts[2])
            if command == "resume-priv" and len(parts) == 2:
                return self.resume(parts[1])
            if command == "continue-priv" and len(parts) == 2:
                return self.continue_plan(parts[1])
            if command == "retry-priv" and len(parts) == 2:
                return self.retry_plan(parts[1])
            if command == "replan-priv" and len(parts) == 2:
                return self.replan(
                    parts[1],
                    reason="用户要求根据现有执行证据重新规划",
                )
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
        recovered_readonly = False
        for step in plan.steps:
            if (
                step.status == "blocked"
                and step.risk == "readonly"
                and step.evidence is not None
                and not step.evidence.timed_out
                and step.evidence.return_code == 0
            ):
                verify = getattr(
                    self.verifier,
                    "verify_deterministic_step",
                    None,
                ) or self.verifier.verify_step
                decision = verify(plan, step)
                plan.verification = decision
                if decision.status == "passed":
                    step.status = "completed"
                    recovered_readonly = True
                    self._event(
                        "privileged_verification",
                        plan,
                        {
                            "step_id": step.step_id,
                            "decision": "passed",
                            "recovery": True,
                            "deterministic": True,
                        },
                    )
        if recovered_readonly:
            plan.status = "approved"
            self.store.save(plan)

        unknown_steps = [
            step for step in plan.steps if step.status == "execution_unknown"
        ]
        for step in unknown_steps:
            verify_recovered = getattr(self.verifier, "verify_recovered_step", None)
            if verify_recovered is None:
                plan.status = "paused"
                self.store.save(plan)
                self._event(
                    "privileged_plan_paused",
                    plan,
                    {"unknown_steps": [item.step_id for item in unknown_steps]},
                )
                return WorkflowResult(
                    "paused",
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
                step.status = "paused"
                plan.status = "paused"
                self.store.save(plan)
                self._event("privileged_plan_paused", plan, {"reason": decision.reason})
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )
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
        if plan.status == "paused":
            return WorkflowResult(
                "paused",
                render_plan(plan) + _pause_controls(plan),
                plan,
            )
        return WorkflowResult("blocked", render_plan(plan), plan)

    def abort(self, plan_id: str) -> WorkflowResult:
        plan = self.store.load(plan_id)
        if plan.status == "completed":
            return WorkflowResult("blocked", "Completed plan cannot be aborted.", plan)
        plan.status = "aborted"
        for step in plan.steps:
            if step.status in {
                "pending",
                "approved",
                "awaiting_confirmation",
                "paused",
            }:
                step.status = "skipped"
        self.store.save(plan)
        self._event("privileged_plan_blocked", plan, {"reason": "aborted"})
        return WorkflowResult("aborted", render_plan(plan), plan)

    def continue_plan(self, plan_id: str) -> WorkflowResult:
        """Skip the paused step only after an explicit user decision."""

        plan = self.store.load(plan_id)
        step = _first_step_with_status(plan, "paused")
        if plan.status != "paused" or step is None:
            return WorkflowResult(
                "blocked",
                "计划当前没有等待用户决定的暂停步骤。",
                plan,
            )
        step.status = "skipped"
        step.observation = _append_observation(
            step.observation,
            "用户选择跳过该异常步骤并继续",
        )
        plan.status = "approved"
        self.store.save(plan)
        self._event(
            "privileged_user_decision",
            plan,
            {"decision": "continue", "step_id": step.step_id},
        )
        return self._execute_plan(plan)

    def retry_plan(self, plan_id: str) -> WorkflowResult:
        """Retry the paused step only after an explicit user decision."""

        plan = self.store.load(plan_id)
        step = _first_step_with_status(plan, "paused")
        if plan.status != "paused" or step is None:
            return WorkflowResult(
                "blocked",
                "计划当前没有可重试的暂停步骤。",
                plan,
            )
        step.status = "approved"
        step.observation = _append_observation(
            step.observation,
            "用户选择重试该步骤",
        )
        plan.status = "approved"
        self.store.save(plan)
        self._event(
            "privileged_user_decision",
            plan,
            {"decision": "retry", "step_id": step.step_id},
        )
        return self._execute_plan(plan)

    def replan(self, plan_id: str, *, reason: str) -> WorkflowResult:
        """基于失败证据生成替代步骤；任何旧授权都必须失效。"""

        plan = self.store.load(plan_id)
        failed_step = next(
            (
                step
                for step in reversed(plan.steps)
                if step.evidence is not None
                and step.status in {
                    "paused",
                    "failed",
                    "blocked",
                    "execution_unknown",
                }
            ),
            None,
        )
        if failed_step is not None and self.context_builder is not None:
            recovered = self._automatic_recovery_plan(
                plan,
                failed_step,
                persist=True,
            )
            if recovered is not None:
                return recovered
        context = {
            "previous_plan": plan.to_dict(),
            "replan_reason": reason,
        }
        replanning_environment = json.dumps(context, ensure_ascii=False)
        planner_kwargs = {"environment_context": replanning_environment}
        if self.context_builder is not None:
            grounded_context = self.context_builder.build(
                plan.goal,
                supplemental_environment_context=replanning_environment,
            )
            blocker = grounded_context.planning_blocker()
            if blocker:
                plan.status = "blocked"
                self.store.save(plan)
                return WorkflowResult(
                    "blocked",
                    "%s，因此不能安全地重新规划；现有步骤不会自动重试。" % blocker,
                    plan,
                )
            planner_kwargs["grounded_context"] = grounded_context
        replacement = self.planner.plan(plan.goal, **planner_kwargs)
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
            + "\n\n计划已更新，需要重新确认：confirm-priv %s" % plan.plan_id
            + "\n查看完整计划：show-priv %s" % plan.plan_id,
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
        if plan.risk != "readonly":
            if plan.schema_version < 2:
                plan.status = "blocked"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "blocked",
                    "这是旧版命令型计划，已失效且不会执行。请重新提交目标以生成基于注册动作的新计划。",
                    plan,
                )
            missing_actions = [
                step.step_id for step in plan.steps if not step.action
            ]
            if missing_actions:
                plan.status = "blocked"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "blocked",
                    "计划包含未注册的命令型步骤，执行已拒绝：%s"
                    % ", ".join(missing_actions),
                    plan,
                )
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

        for step_index, step in enumerate(plan.steps, start=1):
            if step.status in {"completed", "skipped"}:
                continue
            if step.status == "execution_unknown":
                plan.status = "paused"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )
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
                step.status = "paused"
                step.observation = precondition_problem
                plan.status = "paused"
                if persist:
                    self.store.save(plan)
                self._event(
                    "privileged_plan_blocked",
                    plan,
                    {"step_id": step.step_id, "reason": precondition_problem},
                )
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )

            step_readonly_argv = readonly_argv
            if (
                plan.risk == "readonly"
                and not step.action
                and step_readonly_argv is None
            ):
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

            if self.on_progress is not None:
                self.on_progress(
                    self._describe_step_execution(
                        step,
                        index=step_index,
                        total=len(plan.steps),
                    )
                )
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
            if deterministic_verification or step.risk == "readonly":
                verify = getattr(
                    self.verifier,
                    "verify_deterministic_step",
                    None,
                ) or self.verifier.verify_step
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
                step.observation = self._summarize_step(
                    step,
                    "completed",
                    decision.reason,
                )
                if persist:
                    self.store.save(plan)
                continue
            step.status = "paused"
            step.observation = self._summarize_step(
                step,
                decision.status,
                decision.reason,
            )
            if not step.observation:
                step.observation = (
                    decision.reason or "当前步骤结果需要用户决定"
                )
            plan.status = "paused"
            if persist:
                self.store.save(plan)
            self._event(
                "privileged_plan_paused",
                plan,
                {
                    "reason": decision.reason,
                    "verification_status": decision.status,
                },
            )
            recovery = self._automatic_recovery_plan(
                plan,
                step,
                persist=persist,
            )
            if recovery is not None:
                return recovery
            return WorkflowResult(
                "paused",
                render_plan(plan) + _pause_controls(plan),
                plan,
            )

        plan.status = "completed"
        if persist:
            self.store.save(plan)
        self._event("privileged_plan_completed", plan)
        return WorkflowResult("completed", render_plan(plan), plan)

    def _summarize_step(
        self,
        step: PrivilegedStep,
        status: str,
        decision_reason: str = "",
    ) -> str:
        if self.summarizer is not None:
            try:
                return self.summarizer.summarize(
                    step,
                    status=status,
                    decision_reason=decision_reason,
                )
            except Exception:
                pass
        return _fallback_step_summary(step, status)

    def _render_detailed_plan(self, plan: PrivilegedPlan) -> str:
        if self.summarizer is not None:
            try:
                return self.summarizer.describe_plan(plan)
            except Exception:
                pass
        return render_plan_details(plan)

    def _describe_step_execution(
        self,
        step: PrivilegedStep,
        *,
        index: int,
        total: int,
    ) -> str:
        if self.summarizer is not None:
            try:
                return self.summarizer.describe_execution(
                    step,
                    index=index,
                    total=total,
                )
            except Exception:
                pass
        impact = (
            "这是只读检查，不会修改服务器环境"
            if step.risk == "readonly"
            else "这一步可能修改服务器环境，且已包含在用户确认的计划中"
        )
        return "第 %s/%s 步：正在执行“%s”；%s。" % (
            index,
            total,
            step.title,
            impact,
        )

    def _automatic_recovery_plan(
        self,
        plan: PrivilegedPlan,
        failed_step: PrivilegedStep,
        *,
        persist: bool,
    ) -> WorkflowResult | None:
        """Run bounded read-only diagnosis and draft, but never authorize, a repair."""

        if not persist or self.context_builder is None:
            return None
        if self.on_progress is not None:
            self.on_progress("步骤失败，正在进行安全的只读诊断并生成修复计划…")
        failure_summary = failed_step.observation or _fallback_step_summary(
            failed_step,
            "failed",
        )
        recovery_snapshot = {
            "failed_step": failed_step.to_dict(),
            "failure_summary": failure_summary,
            "previous_verification": (
                plan.verification.to_dict() if plan.verification else None
            ),
        }
        try:
            conclusion = None
            diagnostic_evidence = ""
            if self.recovery_agent is not None:
                catalog = getattr(
                    self.context_builder,
                    "recovery_probe_catalog",
                    lambda: "",
                )()
                analysis = self.recovery_agent.analyze(
                    plan,
                    failed_step,
                    probe_catalog=catalog,
                )
                analysis.probes = _merge_recovery_probes(
                    _deterministic_recovery_probes(failed_step),
                    analysis.probes,
                )
                run_diagnostics = getattr(
                    self.context_builder,
                    "run_recovery_diagnostics",
                    None,
                )
                if run_diagnostics is not None:
                    diagnostic_evidence = run_diagnostics(analysis.probes)
                conclusion = self.recovery_agent.conclude(
                    plan,
                    failed_step,
                    analysis,
                    diagnostic_evidence,
                )
                if conclusion.summary:
                    failure_summary = conclusion.summary
                recovery_snapshot["recovery_analysis"] = analysis.__dict__
                recovery_snapshot["diagnostic_evidence"] = diagnostic_evidence
                recovery_snapshot["recovery_conclusion"] = conclusion.__dict__

            recovery_fingerprint = _recovery_fingerprint(
                failed_step,
                diagnostic_evidence,
                conclusion.__dict__ if conclusion is not None else {},
            )
            recovery_snapshot["recovery_fingerprint"] = recovery_fingerprint
            previous_attempt = (
                plan.recovery_history[-1]
                if plan.recovery_history
                and isinstance(plan.recovery_history[-1], dict)
                else {}
            )
            if (
                previous_attempt.get("outcome") == "unavailable"
                and previous_attempt.get("recovery_fingerprint")
                == recovery_fingerprint
            ):
                raise RecoveryPlanUnavailable(
                    "本次只读诊断与上一次相比没有发现新证据，因此没有再次调用"
                    " Planner 或重复生成同一失败计划。请先改变相关环境状态、"
                    "补充证据，或查看审计记录后再重新规划。"
                )

            planner_evidence = redact_sensitive_text(
                json.dumps(recovery_snapshot, ensure_ascii=False)
            )[:22000]
            guidance = (
                "\n已确认根因：%s\n修复能力要求：%s\n规划约束：%s"
                % (
                    conclusion.confirmed_cause or "尚未完全确认",
                    conclusion.required_capability or "根据诊断证据确定",
                    conclusion.planning_guidance or "不得原样重试失败动作",
                )
                if conclusion is not None
                else ""
            )
            recovery_goal = (
                "原目标：%s\n"
                "先处理已诊断的失败原因，再安全地继续完成原目标。\n"
                "失败摘要：%s%s"
                % (plan.goal, failure_summary, guidance)
            )
            grounded_context = self.context_builder.build(
                recovery_goal,
                supplemental_environment_context=planner_evidence,
            )
            blocker = grounded_context.planning_blocker()
            if blocker:
                raise ValueError(blocker)
            try:
                replacement = self.planner.plan(
                    recovery_goal,
                    environment_context=planner_evidence,
                    grounded_context=grounded_context,
                )
            except Exception as exc:
                if conclusion is not None:
                    required = (
                        conclusion.required_capability
                        or "根据诊断结论实施修复"
                    )
                    raise RecoveryPlanUnavailable(
                        "只读诊断已经完成，但 Planner 无法用当前注册动作构造"
                        "经过证据支持的修复步骤。可靠修复需要具备“%s”的"
                        "受控执行能力；系统没有原样重试失败操作。" % required
                    ) from exc
                raise
            if _repeats_failed_action_without_repair(
                replacement.steps,
                failed_step,
            ):
                required = (
                    conclusion.required_capability
                    if conclusion is not None
                    else ""
                )
                raise RecoveryPlanUnavailable(
                    "诊断已经完成，但候选计划只是重复失败操作，没有处理根因。"
                    + (
                        "可靠修复需要具备“%s”的受控执行能力；当前动作目录中没有"
                        "得到证据支持的实施步骤。" % required
                        if required
                        else "当前动作目录中没有得到证据支持的修复步骤。"
                    )
                )
            if self.recovery_agent is not None and conclusion is not None:
                review = self.recovery_agent.review_plan(
                    failed_step,
                    conclusion,
                    replacement,
                )
                recovery_snapshot["repair_plan_review"] = review.__dict__
                if not review.covers_cause:
                    detail = review.explanation or "候选步骤未覆盖已诊断根因"
                    if review.missing_capability:
                        detail += "；缺少的受控能力：%s" % review.missing_capability
                    raise RecoveryPlanUnavailable(detail)
        except Exception as exc:
            plan.status = "paused"
            if isinstance(exc, RecoveryPlanUnavailable):
                user_reason = str(exc)
            else:
                user_reason = (
                    "自动恢复分析未能完整结束，因此没有生成可能误操作的修复计划。"
                    "你可以稍后重新规划，原计划不会自动重试。"
                )
            failed_step.observation = _append_observation(
                failure_summary,
                user_reason,
            )
            recovery_snapshot["outcome"] = "unavailable"
            recovery_snapshot["failure_reason"] = _short_error(exc)
            recovery_snapshot["user_reason"] = user_reason
            if "recovery_fingerprint" not in recovery_snapshot:
                recovery_snapshot["recovery_fingerprint"] = (
                    _recovery_fingerprint(
                        failed_step,
                        recovery_snapshot.get("diagnostic_evidence", ""),
                        recovery_snapshot.get("recovery_conclusion", {}),
                    )
                )
            plan.recovery_history.append(recovery_snapshot)
            self.store.save(plan)
            self._event(
                "privileged_recovery_plan_failed",
                plan,
                {
                    "step_id": failed_step.step_id,
                    "reason": _short_error(exc),
                    "user_reason": user_reason,
                },
            )
            return WorkflowResult(
                "paused",
                _render_recovery_unavailable(
                    plan,
                    failure_summary=failure_summary,
                    reason=user_reason,
                ),
                plan,
            )

        recovery_snapshot["outcome"] = "planned"
        plan.recovery_history.append(recovery_snapshot)
        plan.risk = replacement.risk
        plan.verification_level = replacement.verification_level
        plan.verification = None
        plan.grounding = replacement.grounding
        plan.replace_steps(replacement.steps)
        for step in plan.steps:
            if step.status == "approved":
                step.status = "pending"
        self.store.save(plan)
        self._event(
            "privileged_plan_created",
            plan,
            {
                "replan": True,
                "automatic_recovery": True,
                "failed_step_id": failed_step.step_id,
            },
        )
        return WorkflowResult(
            "awaiting_confirmation",
            "原计划已停止，未继续执行后续变更。\n"
            "失败原因：%s\n"
            "已完成安全的只读诊断，并生成以下修复计划；修复尚未执行：\n\n%s"
            "\n\n确认执行新计划：confirm-priv %s"
            "\n查看详细修复计划：show-priv %s"
            "\n查看原始诊断与审计证据：audit-priv %s"
            % (
                failure_summary,
                render_plan(plan),
                plan.plan_id,
                plan.plan_id,
                plan.plan_id,
            ),
            plan,
        )

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
    risk_labels = {
        "readonly": "只读",
        "low": "低",
        "medium": "中",
        "high": "高",
        "destructive": "破坏性",
    }
    status_labels = {
        "draft": "草稿",
        "awaiting_confirmation": "等待确认",
        "approved": "已确认",
        "pending": "待执行",
        "running": "执行中",
        "executed": "已执行",
        "executing": "执行中",
        "verifying": "验证中",
        "completed": "已完成",
        "partially_completed": "部分完成",
        "paused": "已暂停",
        "blocked": "已阻止",
        "failed": "失败",
        "execution_unknown": "结果未知",
        "skipped": "已跳过",
        "aborted": "已取消",
    }
    lines = [
        "高权限操作计划 %s" % plan.plan_id,
        "目标：%s" % plan.goal,
        "风险：%s｜状态：%s"
        % (
            risk_labels.get(plan.risk, plan.risk),
            status_labels.get(plan.status, plan.status),
        ),
        "步骤：",
    ]
    preview_limit = 6
    for index, step in enumerate(plan.steps[:preview_limit], start=1):
        step_status = status_labels.get(step.status, step.status)
        line = "%s. %s（%s）" % (index, step.title, step_status)
        if step.observation and step.status in {
            "completed",
            "paused",
            "blocked",
            "failed",
            "execution_unknown",
        }:
            line += "：" + _visible_observation(step.observation)[:160]
        lines.append(line)
    remaining = len(plan.steps) - preview_limit
    if remaining > 0:
        lines.append("…另有 %s 个步骤，使用 show-priv 查看详情。" % remaining)
    return "\n".join(lines)


def render_plan_details(plan: PrivilegedPlan) -> str:
    risk_labels = {
        "readonly": "只读",
        "low": "低",
        "medium": "中",
        "high": "高",
        "destructive": "破坏性",
    }
    status_labels = {
        "awaiting_confirmation": "等待确认",
        "approved": "已确认",
        "completed": "已完成",
        "paused": "已暂停",
        "pending": "待执行",
        "skipped": "已跳过",
        "blocked": "已阻止",
    }
    lines = [
        "计划目标：%s" % plan.goal,
        "整体风险：%s；当前状态：%s。"
        % (
            risk_labels.get(plan.risk, plan.risk),
            status_labels.get(plan.status, plan.status),
        ),
        "",
        "详细步骤：",
    ]
    for index, step in enumerate(plan.steps, start=1):
        lines.extend(
            [
                "",
                "%s. %s" % (index, step.title),
                "   当前状态：%s；风险：%s。"
                % (
                    status_labels.get(step.status, step.status),
                    risk_labels.get(step.risk, step.risk),
                ),
                "   执行内容：%s" % _natural_step_scope(step),
                "   环境影响：%s"
                % (
                    "只读取和检查信息，不修改服务器。"
                    if step.risk == "readonly"
                    else "可能修改服务器状态，只有确认当前计划后才会执行。"
                ),
            ]
        )
        if step.expected_changes:
            lines.append("   预期变化：%s。" % "；".join(step.expected_changes))
        if step.rollback:
            lines.append("   回退方式：%s" % step.rollback)
        if step.observation:
            lines.append(
                "   当前结果：%s" % _visible_observation(step.observation)
            )
    lines.append("")
    if plan.status in {"awaiting_confirmation", "draft"}:
        lines.append("确认当前计划：confirm-priv %s" % plan.plan_id)
    elif plan.status == "paused":
        lines.extend(
            [
                "根据证据重新诊断并规划：replan-priv %s" % plan.plan_id,
                "重试暂停步骤：retry-priv %s" % plan.plan_id,
                "终止计划：abort-priv %s" % plan.plan_id,
            ]
        )
    elif plan.status == "completed":
        lines.append("该计划已经完成，无需再次确认。")
    lines.append("查看原始审计数据：audit-priv %s" % plan.plan_id)
    return "\n".join(lines)


def render_plan_audit(plan: PrivilegedPlan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)


def render_plan_list(plans: list[PrivilegedPlan]) -> str:
    if not plans:
        return "No privileged plans."
    return "\n".join(
        "%s status=%s risk=%s goal=%s"
        % (plan.plan_id, plan.status, plan.risk, plan.goal)
        for plan in plans
    )


def _first_step_with_status(
    plan: PrivilegedPlan,
    status: str,
) -> PrivilegedStep | None:
    return next((step for step in plan.steps if step.status == status), None)


def _pause_controls(plan: PrivilegedPlan) -> str:
    return (
        "\n\n当前步骤未按预期完成，已暂停后续操作。具体原因见上方步骤，请选择："
        "\n- 跳过并继续：continue-priv %s"
        "\n- 重试当前步骤：retry-priv %s"
        "\n- 根据证据重新规划：replan-priv %s"
        "\n- 终止计划：abort-priv %s"
        % (plan.plan_id, plan.plan_id, plan.plan_id, plan.plan_id)
    )


def _fallback_step_summary(step: PrivilegedStep, status: str) -> str:
    evidence = step.evidence
    if status == "completed":
        return "“%s”已执行完成并通过当前检查。" % step.title
    if evidence is not None and evidence.timed_out:
        return "“%s”执行超时，当前结果无法确定。" % step.title
    if evidence is not None and evidence.return_code is not None:
        return "“%s”执行失败，退出码为 %s；完整原因可在审计证据中查看。" % (
            step.title,
            evidence.return_code,
        )
    return "“%s”未按预期完成，当前结果无法确定。" % step.title


def _natural_step_scope(step: PrivilegedStep) -> str:
    values = []
    for key, value in step.args.items():
        lowered_key = str(key).lower()
        if lowered_key in {"content", "anchor", "argv", "env"} or any(
            marker in lowered_key
            for marker in ("password", "token", "secret", "key")
        ):
            continue
        text = " ".join(str(value or "").split())
        if text and text not in values:
            values.append(text[:240])
    if values:
        return "执行“%s”，操作对象为 %s。" % (
            step.title,
            "、".join(values[:3]),
        )
    return "执行“%s”。" % step.title


def _visible_observation(observation: str) -> str:
    """Hide obsolete internal recovery-guard wording in persisted old plans."""

    text = str(observation or "").strip()
    legacy_marker = "；自动只读诊断已完成，但暂时无法生成可靠修复计划："
    if legacy_marker in text:
        text = text.split(legacy_marker, 1)[0]
        text += "；此前未形成可执行修复计划，可重新运行 replan-priv 进行证据驱动诊断。"
    return text


def _repeats_failed_action_without_repair(
    steps: list[PrivilegedStep],
    failed_step: PrivilegedStep,
) -> bool:
    """Reject a draft whose first material action merely repeats the failure."""

    for step in steps:
        if step.risk == "readonly":
            continue
        return step.action == failed_step.action and step.args == failed_step.args
    return False


def _deterministic_recovery_probes(
    failed_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Add evidence probes implied by common failure shapes.

    The model may request additional probes, but it cannot omit the basic
    evidence needed to distinguish configuration, dependency, port, process,
    permission and resource failures.
    """

    evidence = failed_step.evidence
    execution_raw = " ".join(
        (
            evidence.stdout if evidence else "",
            evidence.stderr if evidence else "",
        )
    )
    raw = " ".join((execution_raw, failed_step.observation))
    lowered = raw.lower()
    root_text = str(failed_step.args.get("project_root") or "").strip()
    root = Path(root_text).expanduser() if root_text else None
    probes: list[dict[str, Any]] = []

    if root is not None and root.is_absolute():
        probes.append(
            {
                "probe": "project_layout",
                "args": {"project_roots": [str(root)]},
                "purpose": "确认失败组件使用的源码包、配置文件和运行目录",
            }
        )
        probes.append(
            {
                "probe": "klonet_config_consistency",
                "args": {"project_root": str(root)},
                "purpose": "核对活动配置类及 Master、Worker、终端和依赖端口",
            }
        )
        for candidate in (
            root / "vemu_uestc" / "vemu_config" / "config.py",
            root / "vemu_config" / "config.py",
            root / "config.py",
        ):
            if candidate.is_file():
                probes.append(
                    {
                        "probe": "ops_file",
                        "args": {
                            "path": str(candidate),
                            "max_chars": 8000,
                            "view": "head",
                        },
                        "purpose": "读取并脱敏核对当前项目的实际连接配置",
                    }
                )
                break

    ports = []
    for value in re.findall(
        r"(?i)(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::\])[:](\d{2,5})",
        execution_raw,
    ):
        number = int(value)
        if 1 <= number <= 65535 and number not in ports:
            ports.append(number)
    if ports or "connection refused" in lowered or "address already in use" in lowered:
        probes.append(
            {
                "probe": "ports",
                "args": {"ports": ports},
                "purpose": "确认目标端口是否监听以及端口所有者",
            }
        )
    if "redis" in lowered:
        probes.extend(
            (
                {
                    "probe": "docker",
                    "args": {},
                    "purpose": "核对 Redis 容器状态和宿主机端口映射",
                },
                {
                    "probe": "redis",
                    "args": {},
                    "purpose": "核对 Redis 服务状态",
                },
            )
        )
    if "no module named" in lowered and root is not None:
        module_match = re.search(r"No module named ['\"]([^'\"]+)", raw)
        if module_match:
            probes.append(
                {
                    "probe": "python_import",
                    "args": {
                        "python_executable": "/usr/bin/python3",
                        "cwd": str(root),
                        "modules": [module_match.group(1)],
                    },
                    "purpose": "使用明确 cwd 复现并定位 Python 导入失败",
                }
            )
    if "permission denied" in lowered:
        paths = re.findall(r"(/[A-Za-z0-9_./-]{2,500})", raw)[:8]
        probes.append(
            {
                "probe": "path_permissions",
                "args": {"paths": paths},
                "purpose": "核对失败路径的存在性、权限和属主",
            }
        )
    if "no space left" in lowered or "disk full" in lowered:
        probes.append(
            {
                "probe": "disk",
                "args": {},
                "purpose": "核对磁盘容量和文件系统状态",
            }
        )
    return probes


def _merge_recovery_probes(
    deterministic: list[dict[str, Any]],
    adaptive: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for request in [*deterministic, *adaptive]:
        if not isinstance(request, dict):
            continue
        key = json.dumps(
            {
                "probe": request.get("probe"),
                "args": request.get("args") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(request)
        if len(result) >= 8:
            break
    return result


def _recovery_fingerprint(
    failed_step: PrivilegedStep,
    diagnostic_evidence: str,
    _conclusion: dict[str, Any],
) -> str:
    evidence = failed_step.evidence
    payload = {
        "action": failed_step.action,
        "args": failed_step.args,
        "return_code": evidence.return_code if evidence else None,
        "stdout": evidence.stdout if evidence else "",
        "stderr": evidence.stderr if evidence else "",
        "diagnostic_evidence": diagnostic_evidence,
    }
    serialized = redact_sensitive_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _render_recovery_unavailable(
    plan: PrivilegedPlan,
    *,
    failure_summary: str,
    reason: str,
) -> str:
    return (
        "本次没有生成新的修复计划，原计划仍保持暂停，失败步骤不会自动重试。\n"
        "诊断结论：%s\n"
        "未生成新计划的原因：%s\n\n"
        "这不是一份新的待执行计划。你可以：\n"
        "- 查看完整诊断证据：audit-priv %s\n"
        "- 在补充证据或服务器状态发生变化后重新规划：replan-priv %s\n"
        "- 终止原计划：abort-priv %s"
        % (
            failure_summary,
            reason,
            plan.plan_id,
            plan.plan_id,
            plan.plan_id,
        )
    )


def _short_error(exc: Exception) -> str:
    message = " ".join(str(exc or exc.__class__.__name__).split())
    return message[:180] or exc.__class__.__name__


def _append_observation(current: str, message: str) -> str:
    values = [item for item in (str(current or "").strip(), message) if item]
    return "；".join(values)[:500]
