"""Ops-Privilege Supervisor：审批、执行、验证与恢复的唯一协调入口。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from klonet_agent.ops.privileged.checkers import ensure_postconditions
from klonet_agent.ops.privileged.contracts import (
    FailurePacket,
    PrivilegedPlan,
    PrivilegedStep,
)
from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy
from klonet_agent.ops.privileged.planner import PlanningBlocked
from klonet_agent.ops.privileged.store import PrivilegedPlanStore
from klonet_agent.ops.privileged.shell_artifact import artifact_is_expired
from klonet_agent.tools.environment import redact_sensitive_text


COMMAND_PATTERN = re.compile(
    r"^(?:list-priv|"
    r"(?:show-priv|audit-priv|confirm-priv|resume-priv|abort-priv|continue-priv|"
    r"retry-priv|replan-priv)\s+\S+|"
    r"confirm-priv-step\s+\S+\s+\S+)$"
)
MAX_INITIAL_BINDING_REPLAN_ATTEMPTS = 2
MAX_SAFE_EXECUTION_ATTEMPTS = 2
MAX_RUNTIME_IMPLEMENTATION_REBINDS = 1
SAFE_RETRY_ACTIONS = {
    "start_docker_container",
    "start_platform_screens",
    "start_screen_component",
    "restart_screen_component",
    "reload_nginx",
}
TRANSIENT_FAILURE_MARKERS = (
    "temporarily unavailable",
    "try again",
    "connection refused",
    "connection reset",
    "resource busy",
    "startup_not_ready",
    "service_not_ready",
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
        execution_agent: Any | None = None,
        store: PrivilegedPlanStore,
        event_sink: Callable[[str, dict], None] | None = None,
        context_builder: Any | None = None,
        summarizer: Any | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.execution_agent = execution_agent
        self.store = store
        self.event_sink = event_sink
        self.context_builder = context_builder
        self.summarizer = summarizer
        self.on_progress = on_progress

    @staticmethod
    def is_control_command(text: str) -> bool:
        return bool(COMMAND_PATTERN.fullmatch(" ".join((text or "").split())))

    def submit(
        self,
        goal: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> WorkflowResult:
        begin = getattr(self.context_builder, "begin_probe_session", None)
        end = getattr(self.context_builder, "end_probe_session", None)
        if begin is not None:
            begin()
        try:
            return self._submit_with_evidence_session(
                goal,
                environment_context=environment_context,
                conversation_context=conversation_context,
            )
        finally:
            if end is not None:
                end()

    def _submit_with_evidence_session(
        self,
        goal: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> WorkflowResult:
        try:
            context_supplement = environment_context
            if conversation_context:
                context_supplement = (
                    (context_supplement + "\n\n") if context_supplement else ""
                ) + (
                    "Recent dialogue for resolving the current request:\n"
                    + conversation_context
                )
            grounded_context = (
                self.context_builder.build(
                    goal,
                    supplemental_environment_context=context_supplement,
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
            if conversation_context:
                planner_kwargs["conversation_context"] = conversation_context
            plan = self.planner.plan(goal, **planner_kwargs)
            if self.execution_agent is None:
                raise RuntimeError("Implementation Binding Agent is unavailable")
            plan = self._bind_with_replanning(
                goal,
                plan,
                grounded_context=grounded_context,
                planner_kwargs=planner_kwargs,
            )
        except PlanningBlocked as exc:
            return WorkflowResult(
                "clarification",
                "规划需要你补充一个无法从服务器探测得到的决定：%s；当前没有执行任何操作。"
                % _short_error(exc),
            )
        except PermissionError as exc:
            return WorkflowResult(
                "denied",
                "该操作计划被安全策略拒绝：%s" % exc,
            )
        except ExecutionBindingError as exc:
            return self._binding_error_result(exc)
        except Exception as exc:
            return WorkflowResult(
                "blocked",
                "无法生成可确认的安全计划：%s。当前没有执行任何操作。"
                % _short_error(exc),
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

    def _bind_with_replanning(
        self,
        goal: str,
        plan: PrivilegedPlan,
        *,
        grounded_context: Any | None,
        planner_kwargs: dict[str, Any],
    ) -> PrivilegedPlan:
        """Return binding failures to the semantic Planner before giving up."""

        failures: list[str] = []
        workflow_plan_id = plan.plan_id
        for attempt in range(MAX_INITIAL_BINDING_REPLAN_ATTEMPTS + 1):
            try:
                return self.execution_agent.prepare_plan(
                    plan,
                    grounded_context=grounded_context,
                )
            except ExecutionBindingError as exc:
                failure = redact_sensitive_text(_short_error(exc))
                failures.append(failure)
                if not getattr(exc, "replan_recommended", True):
                    if self.on_progress is not None:
                        self.on_progress(
                            "Implementation Binding Agent：内部合同格式修复已耗尽；"
                            "语义目标没有变化，"
                            "因此不消耗 Planner 重规划次数。"
                        )
                    terminal = ExecutionBindingError(
                        "Implementation Binding Agent 无法形成有效实现合同，"
                        "未触发无意义的 Planner "
                        "重规划：%s" % failure,
                        replan_recommended=False,
                        category=getattr(
                            exc,
                            "category",
                            "implementation_contract_invalid",
                        ),
                    )
                    self._attach_paused_binding_plan(terminal, plan)
                    raise terminal from exc
                if attempt >= MAX_INITIAL_BINDING_REPLAN_ATTEMPTS:
                    terminal = ExecutionBindingError(
                        "执行绑定在 %s 次重新规划后仍失败：%s"
                        % (
                            MAX_INITIAL_BINDING_REPLAN_ATTEMPTS,
                            "；".join(failures),
                        )
                    )
                    self._attach_paused_binding_plan(terminal, plan)
                    raise terminal from exc
                if self.on_progress is not None:
                    self.on_progress(
                        "Implementation Binding Agent：实现无法安全绑定"
                        "（第 %s/%s 次）：%s；"
                        "正在把原因返回 Planner 重新规划…"
                        % (
                            attempt + 1,
                            MAX_INITIAL_BINDING_REPLAN_ATTEMPTS,
                            failure,
                        )
                    )
                previous_probe_history = list(plan.probe_history)
                feedback = redact_sensitive_text(
                    json.dumps(
                        {
                            "phase": "execution_binding",
                            "attempt": attempt + 1,
                            "binding_error": failure,
                            "evidence_already_collected": (
                                _probe_evidence_for_replan(
                                    previous_probe_history
                                )
                            ),
                            "previous_semantic_steps": [
                                {
                                    "step_id": item.step_id,
                                    "objective": item.objective,
                                    "reason": item.reason,
                                    "success_criteria": item.success_criteria,
                                }
                                for item in plan.steps
                            ],
                            "instruction": (
                                "Replan the semantic route using the same user goal"
                                " and evidence. Split or revise steps when that makes"
                                " them implementable. Treat evidence_already_collected"
                                " as authoritative and do not request duplicate probes."
                                " Do not emit actions, commands, or scripts."
                            ),
                        },
                        ensure_ascii=False,
                    )
                )[:12000]
                retry_kwargs = dict(planner_kwargs)
                retry_kwargs["planning_feedback"] = feedback
                retry_kwargs["prior_probe_history"] = previous_probe_history
                replacement = self.planner.plan(goal, **retry_kwargs)
                replacement.plan_id = workflow_plan_id
                replacement.probe_history = (
                    previous_probe_history
                    + list(replacement.probe_history)
                )
                plan = replacement
        raise AssertionError("unreachable binding replan loop")

    def _attach_paused_binding_plan(
        self,
        error: ExecutionBindingError,
        plan: PrivilegedPlan,
    ) -> None:
        plan.status = "paused"
        unbound = next(
            (step for step in _execution_steps(plan) if step.execution_binding is None),
            None,
        )
        if unbound is not None:
            unbound.status = "paused"
            unbound.observation = _append_observation(
                unbound.observation,
                "实现绑定与自动重规划均未形成安全路线，等待用户决定",
            )
        self.store.save(plan)
        error.paused_plan = plan

    @staticmethod
    def _binding_error_result(exc: ExecutionBindingError) -> WorkflowResult:
        paused_plan = getattr(exc, "paused_plan", None)
        if paused_plan is None:
            return WorkflowResult(
                "blocked",
                "无法生成可确认的安全计划：%s。当前没有执行任何操作。"
                % _short_error(exc),
            )
        return WorkflowResult(
            "paused",
            "Implementation Binding Agent 无法形成完整实现，所有自动"
            "重选与 Planner replan 已停止；没有执行任何服务器变更。\n"
            "原因：%s\n\n请由你决定：\n"
            "- 查看当前证据：audit-priv %s\n"
            "- 再次要求 Planner 重规划：replan-priv %s\n"
            "- 放弃该计划：abort-priv %s\n"
            "- 或直接补充目标/约束，提交一条新的请求"
            % (
                _short_error(exc),
                paused_plan.plan_id,
                paused_plan.plan_id,
                paused_plan.plan_id,
            ),
            paused_plan,
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

    def unfinished_plan_context(self) -> str:
        """Give the intent classifier a bounded summary of resumable plans."""

        unfinished = [
            plan
            for plan in self.store.list()
            if plan.status not in {"completed", "aborted", "blocked", "failed"}
        ]
        if not unfinished:
            return ""
        lines = ["Available unfinished privileged plans:"]
        for plan in unfinished[:5]:
            lines.append(
                "- plan_id=%s status=%s goal=%s"
                % (plan.plan_id, plan.status, plan.goal[:300])
            )
        return "\n".join(lines)

    def unfinished_plan_options(
        self,
        plan_reference: str = "",
    ) -> WorkflowResult | None:
        """Resolve a classified resume intent into explicit safe choices."""

        unfinished = [
            plan
            for plan in self.store.list()
            if plan.status not in {"completed", "aborted", "blocked", "failed"}
        ]
        if not unfinished:
            return None
        reference = " ".join(str(plan_reference or "").lower().split())
        candidates = unfinished
        if reference and reference != "latest":
            exact = [plan for plan in unfinished if plan.plan_id.lower() == reference]
            if exact:
                candidates = exact
            else:
                candidates = [
                    plan
                    for plan in unfinished
                    if reference in plan.goal.lower()
                    or reference in plan.plan_id.lower()
                ]
                if not candidates:
                    return None
        if len(candidates) > 1 and reference not in {"", "latest"}:
            return WorkflowResult(
                "recovery_options",
                "找到多个可能的未完成计划，请先选择一个：\n%s"
                % "\n".join(
                    "- %s status=%s goal=%s；查看：show-priv %s"
                    % (plan.plan_id, plan.status, plan.goal, plan.plan_id)
                    for plan in candidates[:5]
                ),
            )
        plan = candidates[0]
        lines = [
            "发现一个尚未结束的高权限计划；为避免把“继续”误当成新目标，"
            "系统不会自动执行。",
            "计划：%s；状态：%s；目标：%s"
            % (plan.plan_id, plan.status, plan.goal),
            "查看计划：show-priv %s" % plan.plan_id,
            "查看证据：audit-priv %s" % plan.plan_id,
        ]
        if plan.status in {"awaiting_confirmation", "draft"}:
            lines.append("确认并执行：confirm-priv %s" % plan.plan_id)
        elif plan.status in {"approved", "executing", "verifying", "partially_completed"}:
            lines.append("检查现场状态后恢复：resume-priv %s" % plan.plan_id)
        elif plan.status == "paused":
            lines.extend(
                (
                    "重试暂停步骤：retry-priv %s" % plan.plan_id,
                    "根据证据重规划：replan-priv %s" % plan.plan_id,
                    "跳过暂停步骤：continue-priv %s" % plan.plan_id,
                )
            )
        lines.append("放弃计划：abort-priv %s" % plan.plan_id)
        return WorkflowResult("recovery_options", "\n".join(lines), plan)

    def approve_plan(self, plan_id: str) -> WorkflowResult:
        plan = self.store.load(plan_id)
        if plan.status not in {"awaiting_confirmation", "draft"}:
            return WorkflowResult(
                "blocked",
                "Plan is not awaiting confirmation: %s" % plan.status,
                plan,
            )
        plan.authorize()
        for step in _all_plan_steps(plan):
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
        binding = step.execution_binding
        if (
            binding is not None
            and binding.kind == "shell_artifact"
            and binding.shell_artifact is not None
        ):
            artifact = binding.shell_artifact
            if artifact_is_expired(artifact):
                artifact.status = "expired"
                step.status = "paused"
                step.observation = (
                    "一次性 Shell 脚本已过期，未执行；请重新规划以生成新的固定脚本。"
                )
                plan.status = "paused"
                self.store.save(plan)
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )
            fingerprint = getattr(
                self.executor,
                "current_environment_fingerprint",
                lambda: artifact.environment_fingerprint,
            )()
            artifact.environment_fingerprint = str(fingerprint or "")
            artifact.approved_contract_hash = artifact.contract_hash
            artifact.status = "approved"
        plan.status = "approved"
        self.store.save(plan)
        return self._execute_plan(plan)

    def execute(self, plan_id: str) -> WorkflowResult:
        return self._execute_plan(self.store.load(plan_id))

    def resume(self, plan_id: str) -> WorkflowResult:
        plan = self.store.recover(plan_id)
        recovered_readonly = False
        for step in _execution_steps(plan):
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
            step for step in _execution_steps(plan) if step.status == "execution_unknown"
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
        if plan.steps and all(
            step.status in {"completed", "skipped"}
            for step in _execution_steps(plan)
        ):
            for semantic_step in plan.steps:
                if semantic_step.implementation_plan is not None:
                    semantic_step.implementation_plan.status = "completed"
                    semantic_step.status = "completed"
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
        for step in _all_plan_steps(plan):
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
                for step in reversed(_execution_steps(plan))
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
        try:
            replacement = self.planner.plan(plan.goal, **planner_kwargs)
            replacement.plan_id = plan.plan_id
            if self.execution_agent is None:
                raise RuntimeError("Implementation Binding Agent is unavailable")
            replacement = self._bind_with_replanning(
                plan.goal,
                replacement,
                grounded_context=planner_kwargs.get("grounded_context"),
                planner_kwargs=planner_kwargs,
            )
        except PlanningBlocked as exc:
            plan.status = "paused"
            self.store.save(plan)
            return WorkflowResult(
                "paused",
                "Planner 重新规划时仍缺少必须由你决定的信息：%s；"
                "现有步骤没有执行。" % _short_error(exc),
                plan,
            )
        except ExecutionBindingError as exc:
            return self._binding_error_result(exc)
        plan.risk = replacement.risk
        plan.verification_level = replacement.verification_level
        plan.verification = None
        plan.resources = list(replacement.resources)
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
            if plan.schema_version < 3:
                plan.status = "blocked"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "blocked",
                    "这是旧版计划，已失效且不会执行。请重新提交目标以生成 Agentic V3 计划。",
                    plan,
                )
            missing_bindings = [
                step.step_id
                for step in _execution_steps(plan)
                if step.execution_binding is None
            ]
            if missing_bindings:
                plan.status = "blocked"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "blocked",
                    "计划包含尚未完成执行绑定的语义步骤：%s"
                    % ", ".join(missing_bindings),
                    plan,
                )
            legacy_bindings = [
                step.step_id
                for step in _execution_steps(plan)
                if step.execution_binding is not None
                and step.execution_binding.kind == "legacy_command"
            ]
            if legacy_bindings:
                plan.status = "blocked"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "blocked",
                    "旧版原始命令只保留用于审计，不能执行：%s。请重新提交目标。"
                    % ", ".join(legacy_bindings),
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

        if any(step.implementation_plan is not None for step in plan.steps):
            return self._execute_hierarchical_plan(
                plan,
                persist=persist,
                deterministic_verification=deterministic_verification,
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
            incomplete_dependencies = [
                dependency
                for dependency in step.depends_on
                if _find_step(plan, dependency).status != "completed"
            ]
            if incomplete_dependencies:
                step.status = "paused"
                step.observation = (
                    "依赖步骤尚未完成：%s"
                    % "、".join(incomplete_dependencies)
                )
                plan.status = "paused"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )
            if step.approval_scope == "step" and step.status != "approved":
                step.status = "awaiting_confirmation"
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
                and step.execution_binding is None
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
                    "Execution Agent：%s"
                    % self._describe_step_execution(
                        step,
                        index=step_index,
                        total=len(plan.steps),
                    )
                )
            plan.status = "executing"
            step.status = "running"
            step.execution_attempts += 1
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
            if self.on_progress is not None:
                self.on_progress(
                    "Verifier：第 %s/%s 步验收%s：%s"
                    % (
                        step_index,
                        len(plan.steps),
                        "通过" if decision.status == "passed" else "未通过",
                        decision.reason or decision.status,
                    )
                )
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
            if self._can_safely_retry_execution(step, decision):
                step.status = "approved"
                step.observation = _append_observation(
                    step.observation,
                    "检测到可安全重试的瞬时错误；将进行第 %s/%s 次执行"
                    % (
                        step.execution_attempts + 1,
                        MAX_SAFE_EXECUTION_ATTEMPTS,
                    ),
                )
                step.checks = []
                plan.status = "approved"
                if persist:
                    self.store.save(plan)
                if self.on_progress is not None:
                    self.on_progress(
                        "Execution Agent：步骤“%s”出现瞬时错误，"
                        "该 Action 已登记为可安全重试；重新检查后有限重试。"
                        % step.title
                    )
                return self._execute_plan(
                    plan,
                    persist=persist,
                    deterministic_verification=deterministic_verification,
                    readonly_argv=readonly_argv,
                )
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
            rebound = self._automatic_implementation_rebind(
                plan,
                step,
                persist=persist,
            )
            if rebound is not None:
                return rebound
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

    def _execute_hierarchical_plan(
        self,
        plan: PrivilegedPlan,
        *,
        persist: bool,
        deterministic_verification: bool,
    ) -> WorkflowResult:
        """Run the inner Binding-Agent/Executor/Verifier implementation loop."""

        execution_steps = _execution_steps(plan)
        total_micro_steps = len(execution_steps)
        completed_micro_steps = sum(
            step.status in {"completed", "skipped"}
            for step in execution_steps
        )
        for semantic_step in plan.steps:
            if semantic_step.status in {"completed", "skipped"}:
                continue
            implementation = semantic_step.implementation_plan
            if implementation is None:
                semantic_step.status = "paused"
                semantic_step.observation = (
                    "层级计划缺少 Implementation Plan，等待重新绑定"
                )
                plan.status = "paused"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )
            incomplete_semantic_dependencies = [
                dependency
                for dependency in semantic_step.depends_on
                if _find_semantic_step(plan, dependency).status != "completed"
            ]
            if incomplete_semantic_dependencies:
                semantic_step.status = "paused"
                semantic_step.observation = "语义依赖尚未完成：%s" % "、".join(
                    incomplete_semantic_dependencies
                )
                plan.status = "paused"
                if persist:
                    self.store.save(plan)
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )

            semantic_step.status = "running"
            implementation.status = "executing"
            for micro_step in implementation.steps:
                if micro_step.status in {"completed", "skipped"}:
                    continue
                if micro_step.status == "execution_unknown":
                    semantic_step.status = "paused"
                    implementation.status = "paused"
                    plan.status = "paused"
                    if persist:
                        self.store.save(plan)
                    return WorkflowResult(
                        "paused",
                        render_plan(plan) + _pause_controls(plan),
                        plan,
                    )
                incomplete_dependencies = [
                    dependency
                    for dependency in micro_step.depends_on
                    if _find_step(plan, dependency).status != "completed"
                ]
                if incomplete_dependencies:
                    micro_step.status = "paused"
                    micro_step.observation = "实现依赖尚未完成：%s" % "、".join(
                        incomplete_dependencies
                    )
                    semantic_step.status = "paused"
                    implementation.status = "paused"
                    plan.status = "paused"
                    if persist:
                        self.store.save(plan)
                    return WorkflowResult(
                        "paused",
                        render_plan(plan) + _pause_controls(plan),
                        plan,
                    )
                if (
                    micro_step.approval_scope == "step"
                    and micro_step.status != "approved"
                ):
                    micro_step.status = "awaiting_confirmation"
                    semantic_step.status = "awaiting_confirmation"
                    implementation.status = "awaiting_confirmation"
                    plan.status = "awaiting_confirmation"
                    if persist:
                        self.store.save(plan)
                    return WorkflowResult(
                        "awaiting_step_confirmation",
                        render_plan(plan)
                        + "\n确认该实现子步骤：confirm-priv-step %s %s"
                        % (plan.plan_id, micro_step.step_id),
                        plan,
                    )
                precondition_problem = self._precondition_problem(micro_step)
                if precondition_problem:
                    micro_step.status = "paused"
                    micro_step.observation = precondition_problem
                    semantic_step.status = "paused"
                    implementation.status = "paused"
                    plan.status = "paused"
                    if persist:
                        self.store.save(plan)
                    return WorkflowResult(
                        "paused",
                        render_plan(plan) + _pause_controls(plan),
                        plan,
                    )

                current_index = completed_micro_steps + 1
                if self.on_progress is not None:
                    self.on_progress(
                        "Execution Agent：Implementation %s/%s（语义步骤“%s”）：%s"
                        % (
                            current_index,
                            total_micro_steps,
                            semantic_step.title,
                            self._describe_step_execution(
                                micro_step,
                                index=current_index,
                                total=total_micro_steps,
                            ),
                        )
                    )
                plan.status = "executing"
                micro_step.status = "running"
                micro_step.execution_attempts += 1
                if persist:
                    self.store.save(plan)
                self._event(
                    "privileged_implementation_step_started",
                    plan,
                    {
                        "semantic_step_id": semantic_step.step_id,
                        "step_id": micro_step.step_id,
                    },
                )
                micro_step.evidence = self.executor.execute(micro_step)
                micro_step.status = (
                    "executed"
                    if not micro_step.evidence.timed_out
                    else "execution_unknown"
                )
                if persist:
                    self.store.save(plan)
                plan.status = "verifying"
                if micro_step.status != "execution_unknown":
                    micro_step.status = "verifying"
                if persist:
                    self.store.save(plan)
                if deterministic_verification or micro_step.risk == "readonly":
                    verify = (
                        getattr(
                            self.verifier,
                            "verify_deterministic_step",
                            None,
                        )
                        or self.verifier.verify_step
                    )
                else:
                    verify = self.verifier.verify_step
                decision = verify(plan, micro_step)
                plan.verification = decision
                if self.on_progress is not None:
                    self.on_progress(
                        "Verifier：Implementation %s/%s 验收%s：%s"
                        % (
                            current_index,
                            total_micro_steps,
                            "通过" if decision.status == "passed" else "未通过",
                            decision.reason or decision.status,
                        )
                    )
                if decision.status == "passed":
                    micro_step.status = "completed"
                    micro_step.observation = self._summarize_step(
                        micro_step,
                        "completed",
                        decision.reason,
                    )
                    completed_micro_steps += 1
                    if persist:
                        self.store.save(plan)
                    continue
                if self._can_safely_retry_execution(micro_step, decision):
                    micro_step.status = "approved"
                    micro_step.checks = []
                    plan.status = "approved"
                    if persist:
                        self.store.save(plan)
                    if self.on_progress is not None:
                        self.on_progress(
                            "Execution Agent：实现子步骤“%s”出现可安全重试的"
                            "瞬时错误，正在有限重试。" % micro_step.title
                        )
                    return self._execute_hierarchical_plan(
                        plan,
                        persist=persist,
                        deterministic_verification=deterministic_verification,
                    )

                micro_step.status = "paused"
                micro_step.observation = self._summarize_step(
                    micro_step,
                    decision.status,
                    decision.reason,
                )
                semantic_step.status = "paused"
                implementation.status = "paused"
                plan.status = "paused"
                if persist:
                    self.store.save(plan)
                rebound = self._automatic_implementation_rebind(
                    plan,
                    micro_step,
                    persist=persist,
                )
                if rebound is not None:
                    semantic_step.status = "pending"
                    implementation.status = "awaiting_confirmation"
                    if persist:
                        self.store.save(plan)
                    return rebound
                recovery = self._automatic_recovery_plan(
                    plan,
                    micro_step,
                    persist=persist,
                )
                if recovery is not None:
                    return recovery
                return WorkflowResult(
                    "paused",
                    render_plan(plan) + _pause_controls(plan),
                    plan,
                )

            implementation.status = "completed"
            semantic_step.status = "completed"
            semantic_step.observation = (
                "Implementation Plan 的全部原子步骤均已执行并通过验收。"
            )
            if persist:
                self.store.save(plan)

        plan.status = "completed"
        if persist:
            self.store.save(plan)
        self._event("privileged_plan_completed", plan)
        return WorkflowResult("completed", render_plan(plan), plan)

    @staticmethod
    def _can_safely_retry_execution(
        step: PrivilegedStep,
        decision: Any,
    ) -> bool:
        binding = step.execution_binding
        evidence = step.evidence
        if (
            binding is None
            or binding.kind != "registered_action"
            or binding.action not in SAFE_RETRY_ACTIONS
            or evidence is None
            or evidence.timed_out
            or evidence.environment_changed
            or step.execution_attempts >= MAX_SAFE_EXECUTION_ATTEMPTS
        ):
            return False
        failure_text = " ".join(
            (
                str(evidence.stderr or ""),
                str(getattr(decision, "reason", "") or ""),
            )
        ).lower()
        return any(marker in failure_text for marker in TRANSIENT_FAILURE_MARKERS)

    def _automatic_implementation_rebind(
        self,
        plan: PrivilegedPlan,
        failed_step: PrivilegedStep,
        *,
        persist: bool,
    ) -> WorkflowResult | None:
        """Try one materially different implementation before semantic replan."""

        old_binding = failed_step.execution_binding
        evidence = failed_step.evidence
        if (
            not persist
            or self.context_builder is None
            or self.execution_agent is None
            or not hasattr(self.execution_agent, "prepare_step")
            or old_binding is None
            or old_binding.kind == "verification_only"
            or evidence is None
            or evidence.timed_out
            or failed_step.implementation_rebind_attempts
                >= MAX_RUNTIME_IMPLEMENTATION_REBINDS
        ):
            return None
        feedback = redact_sensitive_text(
            json.dumps(
                {
                    "rejected_implementation": old_binding.to_dict(),
                    "execution_evidence": evidence.to_dict(),
                    "verification": (
                        plan.verification.to_dict()
                        if plan.verification is not None
                        else {}
                    ),
                    "instruction": (
                        "Keep the semantic objective unchanged and select a"
                        " materially different implementation."
                    ),
                },
                ensure_ascii=False,
            )
        )[:12000]
        try:
            grounded_context = self.context_builder.build(
                failed_step.objective or failed_step.title,
                supplemental_environment_context=feedback,
            )
            if grounded_context.planning_blocker():
                return None
            candidate = PrivilegedStep.from_dict(failed_step.to_dict())
            candidate.execution_binding = None
            candidate.evidence = None
            candidate.checks = []
            candidate.status = "pending"
            replacement = self.execution_agent.prepare_step(
                plan,
                candidate,
                grounded_context=grounded_context,
                implementation_feedback=feedback,
            )
        except Exception as exc:
            if self.on_progress is not None:
                self.on_progress(
                    "Implementation Binding Agent：运行失败后的实现重选未成功：%s；"
                    "将把 Failure Packet 交还 Planner。" % _short_error(exc)
                )
            return None
        if _binding_execution_signature(replacement) == _binding_execution_signature(
            old_binding
        ):
            if self.on_progress is not None:
                self.on_progress(
                    "Implementation Binding Agent：拒绝重复刚刚失败的实现；"
                    "将把 Failure Packet 交还 Planner。"
                )
            return None

        failed_step.execution_binding = replacement
        failed_step.risk = replacement.risk
        failed_step.approval_scope = replacement.approval_scope
        failed_step.preconditions = list(replacement.preconditions)
        failed_step.postconditions = list(replacement.postconditions)
        failed_step.evidence = None
        failed_step.checks = []
        failed_step.observation = _append_observation(
            failed_step.observation,
            "Implementation Binding Agent 已选择不同实现；旧授权失效，"
            "等待用户确认",
        )
        failed_step.status = (
            "awaiting_confirmation"
            if replacement.approval_scope == "step"
            else "pending"
        )
        failed_step.implementation_rebind_attempts += 1
        plan.authorized_hash = ""
        plan.status = "awaiting_confirmation"
        plan.verification = None
        if persist:
            self.store.save(plan)
        self._event(
            "privileged_implementation_rebound",
            plan,
            {"step_id": failed_step.step_id, "kind": replacement.kind},
        )
        return WorkflowResult(
            "awaiting_confirmation",
            "原实现未通过验收，Implementation Binding Agent 已为同一语义目标"
            "选择不同实现。"
            "新实现尚未执行，旧授权已经失效。\n\n%s\n\n"
            "确认新实现：confirm-priv %s\n"
            "查看完整计划：show-priv %s\n"
            "放弃计划：abort-priv %s"
            % (
                render_plan(plan),
                plan.plan_id,
                plan.plan_id,
                plan.plan_id,
            ),
            plan,
        )

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
                description = self.summarizer.describe_plan(plan)
                shell_review = _render_shell_review(plan)
                return (
                    description
                    + ("\n\n" + shell_review if shell_review else "")
                )
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
        """Return a structured failure packet to the same semantic Planner."""

        if (
            not persist
            or self.context_builder is None
            or self.execution_agent is None
        ):
            return None
        failure_summary = failed_step.observation or _fallback_step_summary(
            failed_step,
            "failed",
        )
        if self.on_progress is not None:
            self.on_progress(
                "Planner：步骤未达到成功标准，正在根据 Failure Packet"
                " 反思并重新规划…"
            )
        try:
            fingerprint = self.context_builder.current_environment_fingerprint()
        except Exception:
            fingerprint = ""
        packet = _build_failure_packet(
            plan,
            failed_step,
            environment_fingerprint=fingerprint,
        )
        if plan.replan_attempts >= 3:
            return self._pause_agentic_replan(
                plan,
                failed_step,
                packet,
                failure_summary,
                "已经连续重新规划 3 次，系统停止自动循环并等待用户决定。",
            )
        if (
            plan.failure_packets
            and plan.failure_packets[-1].failure_fingerprint
            == packet.failure_fingerprint
        ):
            return self._pause_agentic_replan(
                plan,
                failed_step,
                packet,
                failure_summary,
                "环境和失败证据与上次相同，没有新证据支持再次执行。",
            )

        previous_signature = _semantic_plan_signature(
            [
                step
                for step in plan.steps
                if step.status not in {"completed", "skipped"}
            ]
        )
        plan.failure_packets.append(packet)
        plan.replan_attempts += 1
        planner_evidence = redact_sensitive_text(
            json.dumps(packet.to_dict(), ensure_ascii=False)
        )[:24000]
        recovery_goal = (
            "原始目标：%s\n"
            "上一执行步骤失败。请根据 Failure Packet 自主反思并重新规划"
            "剩余路线；保留已完成步骤作为不可更改事实，不得原样重复失败方案。"
            % plan.goal
        )
        try:
            grounded_context = self.context_builder.build(
                recovery_goal,
                supplemental_environment_context=planner_evidence,
            )
            blocker = grounded_context.planning_blocker()
            if blocker:
                raise RecoveryPlanUnavailable(blocker)
            recovery_planner_kwargs = {
                "environment_context": planner_evidence,
                "grounded_context": grounded_context,
            }
            replacement = self.planner.plan(
                recovery_goal,
                **recovery_planner_kwargs,
            )
            replacement = self._bind_with_replanning(
                recovery_goal,
                replacement,
                grounded_context=grounded_context,
                planner_kwargs=recovery_planner_kwargs,
            )
            if _semantic_plan_signature(replacement.steps) == previous_signature:
                raise RecoveryPlanUnavailable(
                    "新计划与失败计划没有实质差异，已拒绝再次执行。"
                )
        except Exception as exc:
            return self._pause_agentic_replan(
                plan,
                failed_step,
                packet,
                failure_summary,
                "Planner 未能形成有实质差异的可靠新路线：%s"
                % _short_error(exc),
                append_packet=False,
            )

        old_goal = plan.goal
        plan.risk = replacement.risk
        plan.verification_level = replacement.verification_level
        plan.verification = None
        plan.grounding = replacement.grounding
        plan.assumptions = replacement.assumptions
        plan.probe_history.extend(replacement.probe_history)
        plan.replace_steps(replacement.steps)
        plan.goal = old_goal
        self.store.save(plan)
        self._event(
            "agentic_replan_created",
            plan,
            {
                "failed_step_id": failed_step.step_id,
                "failure_fingerprint": packet.failure_fingerprint,
                "replan_attempt": plan.replan_attempts,
            },
        )
        return WorkflowResult(
            "awaiting_confirmation",
            "原计划已停止，后续步骤未执行。\n"
            "失败与反思：%s\n"
            "Planner 已根据真实证据生成一条有实质差异的新路线：\n\n%s"
            "\n\n确认新计划：confirm-priv %s"
            "\n查看详细计划：show-priv %s"
            "\n查看 Failure Packet：audit-priv %s"
            % (
                failure_summary,
                render_plan(plan),
                plan.plan_id,
                plan.plan_id,
                plan.plan_id,
            ),
            plan,
        )

    def _pause_agentic_replan(
        self,
        plan: PrivilegedPlan,
        failed_step: PrivilegedStep,
        packet: FailurePacket,
        failure_summary: str,
        reason: str,
        *,
        append_packet: bool = True,
    ) -> WorkflowResult:
        if append_packet:
            plan.failure_packets.append(packet)
        failed_step.observation = _append_observation(
            failure_summary,
            reason,
        )
        plan.status = "paused"
        self.store.save(plan)
        self._event(
            "agentic_replan_paused",
            plan,
            {
                "step_id": failed_step.step_id,
                "reason": reason,
                "failure_fingerprint": packet.failure_fingerprint,
            },
        )
        return WorkflowResult(
            "paused",
            "当前步骤失败，后续操作已停止。\n"
            "失败与反思：%s\n"
            "未继续自动规划：%s\n\n"
            "请由你决定下一步：\n"
            "- 查看证据：audit-priv %s\n"
            "- 重试当前步骤：retry-priv %s\n"
            "- 跳过当前步骤继续：continue-priv %s\n"
            "- 再次要求规划：replan-priv %s\n"
            "- 终止计划：abort-priv %s"
            % (
                failure_summary,
                reason,
                plan.plan_id,
                plan.plan_id,
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
    for step in _all_plan_steps(plan):
        if step.step_id == step_id:
            return step
    raise KeyError("unknown privileged step: %s" % step_id)


def _find_semantic_step(plan: PrivilegedPlan, step_id: str) -> PrivilegedStep:
    for step in plan.steps:
        if step.step_id == step_id:
            return step
    raise KeyError("unknown semantic step: %s" % step_id)


def _execution_steps(plan: PrivilegedPlan) -> list[PrivilegedStep]:
    """Return the atomic steps that may reach Executor/Verifier."""

    result: list[PrivilegedStep] = []
    for semantic_step in plan.steps:
        implementation = semantic_step.implementation_plan
        if implementation is None:
            result.append(semantic_step)
        else:
            result.extend(implementation.steps)
    return result


def _all_plan_steps(plan: PrivilegedPlan) -> list[PrivilegedStep]:
    """Return semantic parents and atomic implementation children."""

    result: list[PrivilegedStep] = []
    for semantic_step in plan.steps:
        result.append(semantic_step)
        if semantic_step.implementation_plan is not None:
            result.extend(semantic_step.implementation_plan.steps)
    return result


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
    ]
    if plan.resources:
        lines.append("计划资源（路径、端口等在执行前统一确定）：")
        for resource in plan.resources[:12]:
            if resource.status == "frozen":
                value = redact_sensitive_text(str(resource.value))[:240]
                lines.append("- %s=%s（已冻结）" % (resource.name, value))
            else:
                lines.append(
                    "- %s（待补全，最晚在 %s 前确定）：%s"
                    % (
                        resource.name,
                        resource.resolve_before,
                        resource.reason,
                    )
                )
    lines.append("步骤：")
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
        implementation = step.implementation_plan
        if implementation is not None:
            completed = sum(
                item.status in {"completed", "skipped"}
                for item in implementation.steps
            )
            lines.append(
                "   Implementation Plan：%s/%s 个原子步骤完成（%s）"
                % (
                    completed,
                    len(implementation.steps),
                    status_labels.get(implementation.status, implementation.status),
                )
            )
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
    ]
    if plan.resources:
        lines.extend(["", "计划资源："])
        for resource in plan.resources:
            if resource.status == "frozen":
                lines.append(
                    "- %s：%s（已冻结，来源：%s）"
                    % (
                        resource.name,
                        redact_sensitive_text(str(resource.value))[:500],
                        resource.source or "计划证据",
                    )
                )
            else:
                lines.append(
                    "- %s：待补全；最晚在 %s 前确定；原因：%s"
                    % (
                        resource.name,
                        resource.resolve_before,
                        resource.reason,
                    )
                )
    lines.extend(["", "详细步骤："])
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
        implementation = step.implementation_plan
        implementation_steps = (
            implementation.steps if implementation is not None else [step]
        )
        for implementation_index, implementation_step in enumerate(
            implementation_steps,
            start=1,
        ):
            binding = implementation_step.execution_binding
            if implementation is not None:
                lines.extend(
                    (
                        "   Implementation %s.%s：%s"
                        % (index, implementation_index, implementation_step.title),
                        "      状态：%s；风险：%s。"
                        % (
                            status_labels.get(
                                implementation_step.status,
                                implementation_step.status,
                            ),
                            risk_labels.get(
                                implementation_step.risk,
                                implementation_step.risk,
                            ),
                        ),
                    )
                )
            if binding is None:
                continue
            implementation_description = {
                "registered_action": "已注册受控动作，随计划确认后自动执行。",
                "shell_artifact": (
                    "固定的一次性 Shell 脚本，执行前还需单步确认。"
                ),
                "verification_only": (
                    "纯验证步骤，只运行已注册检查器，不执行变更命令。"
                ),
            }.get(binding.kind, "未知执行绑定，不会自动执行。")
            prefix = "      " if implementation is not None else "   "
            lines.append("%s执行方式：%s" % (prefix, implementation_description))
            if binding.binding_reason:
                lines.append("%s绑定依据：%s" % (prefix, binding.binding_reason))
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
    shell_review = _render_shell_review(plan)
    if shell_review:
        lines.extend(("", shell_review))
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


def _render_shell_review(plan: PrivilegedPlan) -> str:
    sections = []
    for index, step in enumerate(_execution_steps(plan), start=1):
        binding = step.execution_binding
        artifact = (
            binding.shell_artifact
            if binding is not None and binding.kind == "shell_artifact"
            else None
        )
        if artifact is None:
            continue
        sections.append(
            "\n".join(
                (
                    "一次性 Shell 审核（第 %s 步：%s）" % (index, step.title),
                    "工作目录：%s" % artifact.cwd,
                    "执行用户：%s" % (artifact.run_as or "当前用户"),
                    "最长执行：%s 秒；到期时间：%s"
                    % (artifact.timeout, artifact.expires_at),
                    "预期改动：%s"
                    % ("；".join(artifact.declared_changes) or "未声明"),
                    "脚本 SHA-256：%s" % artifact.sha256,
                    "固定脚本如下：",
                    "```bash",
                    artifact.script.rstrip(),
                    "```",
                    "确认该脚本：confirm-priv-step %s %s"
                    % (plan.plan_id, step.step_id),
                )
            )
        )
    return "\n\n".join(sections)


def _first_step_with_status(
    plan: PrivilegedPlan,
    status: str,
) -> PrivilegedStep | None:
    nested = next(
        (step for step in _execution_steps(plan) if step.status == status),
        None,
    )
    if nested is not None:
        return nested
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
    if step.objective:
        scope = "目标是%s" % step.objective.rstrip("。")
        if step.reason:
            scope += "；依据是%s" % step.reason.rstrip("。")
        return scope + "。"
    binding = step.execution_binding
    args = (
        binding.args
        if binding is not None and binding.kind == "registered_action"
        else step.args
    )
    values = []
    for key, value in args.items():
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


def _build_failure_packet(
    plan: PrivilegedPlan,
    failed_step: PrivilegedStep,
    *,
    environment_fingerprint: str,
) -> FailurePacket:
    binding = (
        failed_step.execution_binding.to_dict()
        if failed_step.execution_binding is not None
        else {}
    )
    evidence = (
        failed_step.evidence.to_dict()
        if failed_step.evidence is not None
        else {}
    )
    verification = (
        plan.verification.to_dict()
        if plan.verification is not None
        else {}
    )
    changes = []
    if (
        failed_step.evidence is not None
        and failed_step.evidence.environment_changed
    ):
        changes = list(failed_step.expected_changes)
        artifact = (
            failed_step.execution_binding.shell_artifact
            if failed_step.execution_binding is not None
            else None
        )
        if artifact is not None:
            changes = list(artifact.declared_changes)
    fingerprint_payload = {
        "step_objective": failed_step.objective or failed_step.title,
        "binding": binding,
        "evidence": evidence,
        "verification": verification,
        "environment_fingerprint": environment_fingerprint,
    }
    fingerprint = hashlib.sha256(
        redact_sensitive_text(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
        ).encode("utf-8")
    ).hexdigest()
    semantic_parent = next(
        (
            item
            for item in plan.steps
            if item is failed_step
            or (
                item.implementation_plan is not None
                and failed_step in item.implementation_plan.steps
            )
        ),
        failed_step,
    )
    atomic_steps = _execution_steps(plan)
    return FailurePacket(
        original_goal=plan.goal,
        failed_step={
            "step_id": failed_step.step_id,
            "title": failed_step.title,
            "objective": failed_step.objective or failed_step.title,
            "reason": failed_step.reason,
            "success_criteria": failed_step.success_criteria,
            "expected_effects": failed_step.expected_changes,
            "status": failed_step.status,
            "semantic_step_id": semantic_parent.step_id,
            "semantic_objective": semantic_parent.objective or semantic_parent.title,
        },
        execution_binding=binding,
        execution_evidence=evidence,
        verification=verification,
        environment_changes=changes,
        completed_steps=[
            {
                "step_id": item.step_id,
                "objective": item.objective or item.title,
                "result": item.observation,
            }
            for item in atomic_steps
            if item.status == "completed"
        ],
        remaining_steps=[
            {
                "step_id": item.step_id,
                "objective": item.objective or item.title,
                "depends_on": item.depends_on,
            }
            for item in atomic_steps
            if item.step_id != failed_step.step_id
            and item.status not in {"completed", "skipped"}
        ],
        reflection=(
            plan.verification.reflection
            if plan.verification is not None
            else ""
        ),
        environment_fingerprint=environment_fingerprint,
        failure_fingerprint=fingerprint,
    )


def _semantic_plan_signature(steps: list[PrivilegedStep]) -> str:
    payload = [
        {
            "objective": " ".join(
                (step.objective or step.title).lower().split()
            ),
            "depends_on": sorted(step.depends_on),
            "success_criteria": sorted(
                " ".join(item.lower().split())
                for item in step.success_criteria
            ),
            "binding_kind": (
                step.execution_binding.kind
                if step.execution_binding is not None
                else ""
            ),
            "binding_action": (
                step.execution_binding.action
                if step.execution_binding is not None
                else ""
            ),
            "binding_args": (
                step.execution_binding.args
                if step.execution_binding is not None
                and step.execution_binding.kind == "registered_action"
                else {}
            ),
            "shell_sha256": (
                step.execution_binding.shell_artifact.sha256
                if step.execution_binding is not None
                and step.execution_binding.shell_artifact is not None
                else ""
            ),
            "implementation_plan": (
                step.implementation_plan.executable_dict()
                if step.implementation_plan is not None
                else None
            ),
        }
        for step in steps
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _binding_execution_signature(binding: Any) -> str:
    artifact = getattr(binding, "shell_artifact", None)
    payload = {
        "kind": getattr(binding, "kind", ""),
        "action": getattr(binding, "action", ""),
        "args": getattr(binding, "args", {}),
        "shell_sha256": getattr(artifact, "sha256", "") if artifact else "",
        "postconditions": getattr(binding, "postconditions", []),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _short_error(exc: Exception) -> str:
    message = redact_sensitive_text(
        " ".join(str(exc or exc.__class__.__name__).split())
    )
    return message[:180] or exc.__class__.__name__


def _probe_evidence_for_replan(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep a bounded, redacted ledger so replans do not rediscover facts."""

    ledger = []
    for item in history[-8:]:
        if not isinstance(item, dict):
            continue
        requests = item.get("requests")
        ledger.append(
            {
                "phase": str(item.get("phase") or "semantic_planning"),
                "round": item.get("round"),
                "step_id": str(item.get("step_id") or ""),
                "requests": requests if isinstance(requests, list) else [],
                "evidence": redact_sensitive_text(
                    str(item.get("evidence") or "")
                )[:1000],
            }
        )
    return ledger


def _append_observation(current: str, message: str) -> str:
    values = [item for item in (str(current or "").strip(), message) if item]
    return "；".join(values)[:500]
