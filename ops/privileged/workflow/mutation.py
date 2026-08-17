"""Confirmation-gated mutation workflow for Ops-Privilege."""

from __future__ import annotations

from copy import deepcopy
import inspect
import re
import uuid
from typing import Any, Iterable

from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.ops.privileged.execution_agent import (
    _canonical_action_postconditions,
)
from klonet_agent.ops.privileged.workflow.coordinator import WorkflowResult
from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
from klonet_agent.ops.privileged.workflow.contracts import (
    ChangePlan, ChangeStep, FailureOutcome, RecoveryOption,
)
from klonet_agent.tools.environment import redact_sensitive_text


def _head_tail(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    half = max(1, (limit - 64) // 2)
    return text[:half] + "\n...[evidence compacted]...\n" + text[-half:]


class MutationWorkflow:
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
        max_replanning_rounds: int = 4,
        max_candidate_replans: int = 1,
    ) -> None:
        self.planner = planner
        self.binder = binder
        self.store = store
        self.executor = executor
        self.verifier = verifier
        self.discovery = discovery
        self.synthesis = synthesis
        self.max_replanning_rounds = max(0, int(max_replanning_rounds))
        self.max_candidate_replans = max(0, int(max_candidate_replans))

    def submit(
        self,
        goal: str,
        *,
        evidence_bundle: Any,
        evidence_conclusion: Any,
        conversation_context: str = "",
        intent_context: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        def plan(*, feedback: str = "") -> Any:
            kwargs: dict[str, Any] = {}
            parameters = inspect.signature(self.planner.plan).parameters
            accepts_kwargs = any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            )
            if feedback and ("binding_feedback" in parameters or accepts_kwargs):
                kwargs["binding_feedback"] = feedback
            if intent_context and ("intent_context" in parameters or accepts_kwargs):
                kwargs["intent_context"] = dict(intent_context)
            return self.planner.plan(
                goal, evidence_bundle, evidence_conclusion, **kwargs,
            )

        outcome = plan()
        replanning_rounds = 0
        candidate_replans = 0
        while True:
            if outcome.status == "need_evidence":
                if replanning_rounds >= self.max_replanning_rounds:
                    return WorkflowResult(
                        True,
                        "blocked",
                        "Planner-to-Discovery evidence budget exhausted.",
                        evidence=evidence_bundle,
                    )
                if self.discovery is None or self.synthesis is None:
                    return WorkflowResult(
                        True,
                        "blocked",
                        "Planner requested evidence but Discovery is unavailable.",
                        evidence=evidence_bundle,
                    )
                evidence_bundle = self.discovery.collect_requests(
                    outcome.evidence_requests,
                    evidence_bundle,
                )
                evidence_conclusion = self.synthesis.synthesize(goal, evidence_bundle)
                replanning_rounds += 1
                candidate_plan = getattr(outcome, "candidate_plan", None)
                finalize_candidate = getattr(
                    self.planner,
                    "finalize_candidate",
                    None,
                )
                if candidate_plan is not None and finalize_candidate is not None:
                    outcome = finalize_candidate(candidate_plan, evidence_bundle)
                else:
                    outcome = plan()
                continue
            occupied_candidate = (
                outcome.status == "blocked"
                and str(outcome.reason or "").startswith(
                    "candidate ports became occupied:"
                )
            )
            if occupied_candidate and candidate_replans < self.max_candidate_replans:
                candidate_replans += 1
                outcome = plan(
                    feedback=(
                        "%s. Select different ports that are not reported as occupied, "
                        "freeze them, and preserve every other grounded decision."
                        % outcome.reason
                    ),
                )
                continue
            break
        if outcome.status != "need_execution" or outcome.plan is None:
            return self._failure_result(
                stage="planning",
                category="planning_contract_unresolved",
                summary="变更规划在有限次数校正后仍未形成安全、可审批的计划。",
                technical_reason=(
                    outcome.reason or "change planning did not produce an executable plan"
                ),
                goal=goal,
                attempted_recoveries=["补充计划证据", "有限次数重新规划"],
                evidence=evidence_bundle,
            )
        self.store.save(outcome.plan)
        grounded_context = self._binding_context(evidence_bundle)
        try:
            plan = self.binder.bind(
                outcome.plan,
                grounded_context=grounded_context,
            )
        except ChangeBindingError as exc:
            outcome = plan(feedback=str(exc))
            if outcome.status != "need_execution" or outcome.plan is None:
                return self._failure_result(
                    stage="binding",
                    category="binding_replan_unresolved",
                    summary="实施绑定发现计划缺少安全落地所需的实例或动作约束。",
                    technical_reason="%s; replan=%s" % (
                        exc,
                        outcome.reason or "binding replan did not produce a ready plan",
                    ),
                    goal=goal,
                    attempted_recoveries=["首次实施绑定", "保持目标不变局部重建"],
                    evidence=evidence_bundle,
                )
            self.store.save(outcome.plan)
            try:
                plan = self.binder.bind(
                    outcome.plan,
                    grounded_context=grounded_context,
                )
            except ChangeBindingError as final_error:
                outcome.plan.status = "blocked"
                self.store.save(outcome.plan)
                return self._failure_result(
                    stage="binding",
                    category="binding_contract_invalid",
                    summary="实施绑定无法证明动作只作用于目标实例，已安全拒绝。",
                    technical_reason=(
                        "first_binding=%s; final_binding=%s" % (exc, final_error)
                    ),
                    goal=goal,
                    plan=outcome.plan,
                    attempted_recoveries=["首次实施绑定", "局部重新规划", "第二次实施绑定"],
                    evidence=evidence_bundle,
                )
        plan.status = "awaiting_confirmation"
        plan.authorized_hash = ""
        self.store.save(plan)
        return WorkflowResult(
            True,
            "awaiting_confirmation",
            self._confirmation_message(plan),
            plan=plan,
            evidence=evidence_bundle,
        )

    @staticmethod
    def _binding_context(evidence_bundle: Any) -> GroundedPlanContext | None:
        """Expose the same frozen knowledge and host evidence to Binding."""

        records = getattr(evidence_bundle, "records", None)
        if not isinstance(records, list):
            return None
        sections = []
        knowledge_sections = []
        priority = {
            "ops_file": 0,
            "process_logs": 1,
            "running_platforms": 2,
        }
        ordered_records = sorted(
            enumerate(records),
            key=lambda item: (
                priority.get(
                    str(getattr(getattr(item[1], "request", None), "probe", "")),
                    10,
                ),
                item[0],
            ),
        )
        for _index, record in ordered_records:
            output = str(getattr(record, "output", "") or "").strip()
            if not output:
                continue
            evidence_id = str(getattr(record, "evidence_id", "") or "evidence")
            probe = str(getattr(getattr(record, "request", None), "probe", ""))
            if probe == "klonet_knowledge":
                knowledge_sections.append(
                    "[%s probe=%s]\n%s"
                    % (evidence_id, probe, _head_tail(output, 12000))
                )
                continue
            sections.append(
                "[%s probe=%s]\n%s"
                % (evidence_id, probe or "unknown", _head_tail(output, 8000))
            )
        if not sections and not knowledge_sections:
            return None
        return GroundedPlanContext(
            knowledge_evidence=_head_tail(
                redact_sensitive_text("\n\n".join(knowledge_sections)),
                18000,
            ) if knowledge_sections else "未执行 Klonet RAG",
            environment_evidence=_head_tail(
                redact_sensitive_text("\n\n".join(sections)),
                24000,
            ),
            action_catalog="audited Action/Shell registry",
        )
    def confirm(self, plan_id: str, content_hash: str) -> WorkflowResult:
        plan = self.store.load(plan_id)
        if content_hash != plan.content_hash:
            return WorkflowResult(
                True,
                "confirmation_rejected",
                "confirmation hash or plan state does not match; nothing executed.",
                plan=plan,
            )
        if plan.status == "completed" and plan.is_authorized:
            return WorkflowResult(
                True, "completed", _execution_receipt(plan), plan=plan,
            )
        if plan.status == "awaiting_user_decision" and plan.failure is not None:
            return WorkflowResult(
                True,
                "awaiting_user_decision",
                _failure_message(plan.failure),
                plan=plan,
                failure=plan.failure,
            )
        if plan.status == "paused" and plan.is_authorized:
            return self._resume_verified_state(plan)
        if plan.status != "awaiting_confirmation":
            return WorkflowResult(
                True,
                "confirmation_rejected",
                "confirmation hash or plan state does not match; nothing executed.",
                plan=plan,
            )
        self._approve_shell_artifacts(plan)
        plan.authorize()
        self.store.save(plan)
        return self._execute(plan)

    def _resume_verified_state(self, plan: ChangePlan) -> WorkflowResult:
        """Resume an authorized plan only after checking paused effects in place."""

        verification_plan = self._verification_plan(plan)
        for change in plan.steps:
            execution_steps = list(self._execution_steps(change))
            for step in execution_steps:
                if step.status not in {"paused", "execution_unknown"}:
                    continue
                verification_step = self._verification_step(step)
                decision = self.verifier.verify_recovered_step(
                    verification_plan,
                    verification_step,
                )
                step.checks = list(verification_step.checks)
                if decision.status != "passed":
                    if self._can_retry_conclusive_no_change(step):
                        step.status = "pending"
                        step.observation = ""
                        step.checks = []
                        change.status = "pending"
                        self.store.save(plan)
                        continue
                    step.observation = str(
                        getattr(decision, "reason", "current state is not verified")
                    )
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return WorkflowResult(
                        True,
                        "paused",
                        step.observation,
                        plan=plan,
                        verification=decision,
                    )
                step.status = "completed"
                step.observation = str(
                    getattr(decision, "reason", "current state verified")
                )
                if change.implementation_plan is None:
                    change.status = "completed"
                    change.observation = step.observation
                self.store.save(plan)
            if change.implementation_plan is not None and change.status == "paused":
                change.status = "pending"
        plan.status = "approved"
        self.store.save(plan)
        return self._execute(plan)

    @staticmethod
    def _can_retry_conclusive_no_change(step: PrivilegedStep) -> bool:
        """An exact reconfirmation may retry only a proven no-change failure."""

        evidence = step.evidence
        if bool(
            evidence is not None
            and evidence.return_code not in {None, 0}
            and not evidence.timed_out
            and evidence.environment_changed is False
        ):
            return True
        binding = step.execution_binding
        if (
            evidence is None
            or evidence.return_code is None
            or evidence.timed_out
            or binding is None
            or binding.kind != "registered_action"
        ):
            return False
        if binding.action == "install_nginx_config":
            file_checks = [
                item for item in step.checks if item.checker == "file_exists"
            ]
            return bool(
                file_checks
                and all(item.status == "failed" for item in file_checks)
            )
        if binding.action != "start_screen_component":
            return False
        session_checks = [
            item for item in step.checks
            if item.checker == "screen_session_exists"
        ]
        state_checks = [
            item for item in step.checks
            if item.checker in {"screen_session_exists", "port_listening"}
        ]
        return bool(
            session_checks
            and all(item.status == "failed" for item in state_checks)
        )

    def handle_control(self, text: str) -> WorkflowResult | None:
        normalized = str(text or "").strip()
        contextual_detail = re.fullmatch(
            r"(?:查看|显示|告诉我)(?:一下)?(?:刚才|最近|上次)?(?:的)?"
            r"(?:失败|错误)(?:的)?(?:详细|具体|技术)?(?:原因|详情|信息)?[。？?]?",
            normalized,
        )
        contextual_cancel = re.fullmatch(
            r"(?:算了[，, ]*)?(?:取消|终止|放弃)(?:这次|本次|刚才的|上次的)?"
            r"(?:操作|恢复|处理|计划)?[。！!]?",
            normalized,
        )
        if contextual_detail is not None or contextual_cancel is not None:
            try:
                failures = self.store.list_failures()
                if not failures:
                    raise KeyError("no failure")
                failure = failures[0]
            except (AttributeError, KeyError, ValueError):
                return WorkflowResult(True, "not_found", "当前会话没有可处理的失败记录。")
            if contextual_detail is not None:
                return WorkflowResult(
                    True, "failure_details", _failure_detail_message(failure),
                    failure=failure,
                )
            return self.handle_control(
                "choose-priv-option %s cancel" % failure.failure_id
            )
        failure_detail = re.fullmatch(
            r"show-priv-failure-details\s+(failure-[A-Za-z0-9_-]{1,64})",
            normalized,
        )
        if failure_detail is not None:
            try:
                failure = self.store.load_failure(failure_detail.group(1))
            except (AttributeError, KeyError, ValueError):
                return WorkflowResult(True, "not_found", "未找到指定的失败记录。")
            return WorkflowResult(
                True, "failure_details", _failure_detail_message(failure),
                failure=failure,
            )
        choice = re.fullmatch(
            r"choose-priv-option\s+(failure-[A-Za-z0-9_-]{1,64})\s+"
            r"([a-z][a-z0-9_-]{1,63})",
            str(text or "").strip(),
        )
        natural_choice = re.fullmatch(
            r"(?:选择|选)\s*([1-3])",
            str(text or "").strip(),
        )
        if choice is not None or natural_choice is not None:
            try:
                if choice is not None:
                    failure = self.store.load_failure(choice.group(1))
                    option_id = choice.group(2)
                else:
                    failures = self.store.list_failures()
                    if not failures:
                        raise KeyError("no failure")
                    failure = failures[0]
                    index = int(natural_choice.group(1)) - 1
                    option_id = failure.options[index].option_id
                option = next(
                    item for item in failure.options if item.option_id == option_id
                )
            except (AttributeError, IndexError, KeyError, StopIteration, ValueError):
                return WorkflowResult(True, "not_found", "未找到指定的失败选项。")
            failure.selected_option_id = option.option_id
            self.store.save_failure(failure)
            if option.action == "cancel":
                if failure.plan_id:
                    try:
                        plan = self.store.load(failure.plan_id)
                        plan.status = "aborted"
                        self.store.save(plan)
                    except (KeyError, ValueError):
                        pass
                return WorkflowResult(
                    True, "aborted", "已取消本次操作；不会执行剩余步骤。",
                    failure=failure,
                )
            if option.action == "provide_direction":
                return WorkflowResult(
                    True, "clarification",
                    "请说明你希望调整的目标范围或可接受的处理方式。",
                    failure=failure,
                )
            return WorkflowResult(
                True, "failure_option_selected",
                "已选择恢复方案：%s" % option.label,
                failure=failure,
            )
        cancel_failure = re.fullmatch(
            r"cancel-priv-failure\s+(failure-[A-Za-z0-9_-]{1,64})",
            str(text or "").strip(),
        )
        if cancel_failure is not None:
            return self.handle_control(
                "choose-priv-option %s cancel" % cancel_failure.group(1)
            )
        detail = re.fullmatch(
            r"show-priv-plan-details\s+(priv-ops-[A-Za-z0-9_-]{1,64})",
            str(text or "").strip(),
        )
        if detail is not None:
            try:
                plan = self.store.load(detail.group(1))
            except (KeyError, ValueError):
                return WorkflowResult(
                    True, "not_found", "未找到指定的变更计划。",
                )
            return WorkflowResult(
                True, "plan_details", self._detail_message(plan), plan=plan,
            )
        match = re.fullmatch(
            r"confirm-priv-plan\s+(priv-ops-[A-Za-z0-9_-]{1,64})\s+([0-9a-f]{64})",
            str(text or "").strip(),
        )
        if match is None:
            return None
        return self.confirm(match.group(1), match.group(2))

    def _failure_result(
        self,
        *,
        stage: str,
        category: str,
        summary: str,
        technical_reason: str,
        goal: str,
        plan: ChangePlan | None = None,
        attempted_recoveries: list[str] | None = None,
        environment_changed: str = "false",
        failed_step: str = "",
        completed_steps: list[str] | None = None,
        failed_checks: list[str] | None = None,
        evidence: Any | None = None,
    ) -> WorkflowResult:
        failure = FailureOutcome(
            failure_id="failure-" + uuid.uuid4().hex[:12],
            stage=stage,
            category=category,
            summary=summary,
            technical_reason=str(technical_reason or "unknown failure"),
            environment_changed=environment_changed,
            failed_step=failed_step,
            completed_steps=list(completed_steps or []),
            failed_checks=list(failed_checks or []),
            attempted_recoveries=list(attempted_recoveries or []),
            options=_recovery_options(goal, stage),
            goal=goal,
            plan_id=plan.plan_id if plan is not None else "",
        )
        if plan is not None:
            plan.failure = failure
            plan.status = "awaiting_user_decision"
            self.store.save(plan)
        save_failure = getattr(self.store, "save_failure", None)
        if save_failure is not None:
            save_failure(failure)
        return WorkflowResult(
            True,
            "awaiting_user_decision",
            _failure_message(failure),
            plan=plan,
            evidence=evidence,
            failure=failure,
        )

    def render_latest_status(self) -> str:
        plans = self.store.list()
        failures = (
            self.store.list_failures()
            if hasattr(self.store, "list_failures") else []
        )
        if failures and (
            not plans or failures[0].created_at >= plans[0].updated_at
        ):
            return _failure_message(failures[0])
        if not plans:
            return "当前会话没有可查询的变更计划。"
        if plans[0].failure is not None and plans[0].status == "awaiting_user_decision":
            return _failure_message(plans[0].failure)
        return _execution_receipt(plans[0])

    def _execute(self, plan: ChangePlan) -> WorkflowResult:
        if not plan.is_authorized:
            return WorkflowResult(
                True, "confirmation_rejected", "plan is not exactly authorized", plan=plan
            )
        verification_plan = self._verification_plan(plan)
        execution_steps = [
            step
            for change in plan.steps
            for step in self._execution_steps(change)
            if step.status not in {"completed", "skipped"}
        ]
        total_steps = len(execution_steps)
        execution_index = 0
        for change in plan.steps:
            for step in self._execution_steps(change):
                if step.status in {"completed", "skipped"}:
                    continue
                if step.status == "execution_unknown":
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return self._execution_failure_result(
                        plan, step,
                        stage="execution",
                        category="execution_state_unknown",
                        reason=step.observation or "执行状态未知，禁止自动重试。",
                        environment_changed="unknown",
                    )
                execution_index += 1
                self._emit_execution_progress(
                    "[%s/%s] %s" % (
                        execution_index,
                        total_steps,
                        _localized_plan_text(step.title or step.objective),
                    )
                )
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
                    return self._execution_failure_result(
                        plan, step,
                        stage="execution",
                        category="execution_timeout_unknown",
                        reason="执行超时，当前环境状态无法确定，禁止自动重试。",
                        environment_changed="unknown",
                    )
                try:
                    verification_step = self._verification_step(step)
                    decision = self.verifier.verify_step(
                        verification_plan,
                        verification_step,
                    )
                    step.checks = list(verification_step.checks)
                except Exception as exc:
                    step.status = "paused"
                    step.observation = "Verifier or Checker failed safely: %s" % exc
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return self._execution_failure_result(
                        plan, step,
                        stage="verification",
                        category="checker_execution_failed",
                        reason=step.observation,
                        environment_changed=_step_environment_changed(step),
                    )
                if decision.status != "passed":
                    step.status = "paused"
                    step.observation = str(getattr(decision, "reason", "verification failed"))
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return self._execution_failure_result(
                        plan, step,
                        stage="verification",
                        category="postcondition_failed",
                        reason=step.observation,
                        environment_changed=_step_environment_changed(step),
                        verification=decision,
                    )
                step.status = "completed"
                step.observation = str(getattr(decision, "reason", "passed"))
                self.store.save(plan)
                self._emit_execution_progress(
                    "[%s/%s] 完成：%s"
                    % (
                        execution_index,
                        total_steps,
                        _localized_plan_text(step.title or step.objective),
                    )
                )
            if change.implementation_plan is not None:
                semantic_step = self._semantic_verification_step(change)
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
                    return self._execution_failure_result(
                        plan, semantic_step,
                        stage="verification",
                        category="semantic_checker_failed",
                        reason=change.observation,
                        environment_changed=_plan_environment_changed(plan),
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
                    return self._execution_failure_result(
                        plan, semantic_step,
                        stage="verification",
                        category="semantic_postcondition_failed",
                        reason=change.observation,
                        environment_changed=_plan_environment_changed(plan),
                        verification=semantic_decision,
                    )
                change.implementation_plan.status = "completed"
            change.status = "completed"
            change.observation = "all executable changes passed verification"
            self.store.save(plan)
        plan.status = "completed"
        self.store.save(plan)
        return WorkflowResult(
            True, "completed", _execution_receipt(plan), plan=plan,
        )

    def _emit_execution_progress(self, message: str) -> None:
        callback = getattr(self.executor, "on_start", None)
        if callback is not None:
            callback(message)

    def _execution_failure_result(
        self,
        plan: ChangePlan,
        step: PrivilegedStep,
        *,
        stage: str,
        category: str,
        reason: str,
        environment_changed: str,
        verification: Any | None = None,
    ) -> WorkflowResult:
        failed_checks = [
            "%s:%s" % (item.checker, item.observed or item.status)
            for item in getattr(step, "checks", []) or []
            if item.status == "failed"
        ]
        result = self._failure_result(
            stage=stage,
            category=category,
            summary=(
                "执行后的验收未通过，任务尚未完成。"
                if stage == "verification"
                else "执行结果无法被安全确认为成功。"
            ),
            technical_reason=reason,
            goal=plan.goal,
            plan=plan,
            attempted_recoveries=["保存执行证据", "执行精确后置校验"],
            environment_changed=environment_changed,
            failed_step=step.title or step.step_id,
            completed_steps=_completed_step_labels(plan),
            failed_checks=failed_checks,
        )
        result.verification = verification
        return result

    @staticmethod
    def _execution_steps(change: ChangeStep) -> Iterable[PrivilegedStep]:
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
    def _verification_step(step: PrivilegedStep) -> PrivilegedStep:
        """Derive checks from the exact authorized binding without changing it."""

        binding = step.execution_binding
        if binding is None or binding.kind != "registered_action":
            return step
        candidate = deepcopy(step)
        candidate.postconditions = _canonical_action_postconditions(
            binding.action,
            binding.args,
            list(step.postconditions),
        )
        return candidate

    @staticmethod
    def _semantic_verification_step(change: ChangeStep) -> PrivilegedStep:
        """Compose hierarchical verification from its authorized atomic bindings."""

        implementation = change.implementation_plan
        assert implementation is not None
        atomic_steps = list(implementation.steps)
        postconditions = list(change.postconditions)
        config_steps = [
            step
            for step in atomic_steps
            if step.execution_binding is not None
            and step.execution_binding.kind == "registered_action"
            and step.execution_binding.action == "set_python_class_attribute"
        ]
        if config_steps:
            # Preserve the canonical runtime outcome while replacing only
            # weak/model-authored file checks with exact typed config checks.
            # Global process-name checks are invalid in a multi-root server.
            listening_ports = {
                int((check.get("args") or {}).get("port"))
                for check in postconditions
                if str(check.get("checker") or "") == "port_listening"
                and str((check.get("args") or {}).get("port") or "").isdigit()
            }
            filtered = []
            for check in postconditions:
                checker = str(check.get("checker") or "")
                if checker in {"port_listening", "port_not_listening"}:
                    filtered.append(check)
                    continue
                if checker != "backend_health":
                    continue
                url = str((check.get("args") or {}).get("url") or "")
                match = re.search(r":([1-9]\d{3,4})(?:/|$)", url)
                if match and int(match.group(1)) in listening_ports:
                    filtered.append(check)
            postconditions = filtered
            for step in config_steps:
                postconditions.extend(
                    MutationWorkflow._verification_step(step).postconditions
                )
            first_args = config_steps[0].execution_binding.args
            path = str(first_args.get("path") or "").strip()
            class_name = str(first_args.get("class_name") or "").strip()
            if path and class_name:
                postconditions.append(
                    {
                        "checker": "file_contains",
                        "args": {
                            "path": path,
                            "text": "PROJ_CONFIG = %s()" % class_name,
                        },
                    }
                )
        return PrivilegedStep(
            step_id=change.step_id,
            title=change.title,
            objective=change.objective,
            reason=change.reason,
            risk=change.risk,
            expected_changes=list(change.expected_changes),
            postconditions=postconditions,
            status="verifying",
            evidence=atomic_steps[-1].evidence,
        )

    @staticmethod
    def _verification_plan(plan: ChangePlan) -> PrivilegedPlan:
        steps = []
        for change in plan.steps:
            steps.extend(MutationWorkflow._execution_steps(change))
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
    def _approve_shell_artifacts(plan: ChangePlan) -> None:
        for change in plan.steps:
            for step in MutationWorkflow._execution_steps(change):
                binding = step.execution_binding
                if binding is None or binding.shell_artifact is None:
                    continue
                artifact = binding.shell_artifact
                artifact.approved_contract_hash = artifact.contract_hash
                artifact.status = "approved"

    @staticmethod
    def _confirmation_message(plan: ChangePlan) -> str:
        lines = [
            "变更计划 %s" % plan.plan_id,
            "目标：%s" % plan.goal,
            "风险：%s" % plan.risk,
        ]
        if plan.resources:
            visible_resources = [
                resource for resource in plan.resources
                if resource.kind in {"path", "port", "identifier", "url"}
                and not str(resource.role or "").startswith("runtime_component_spec:")
                and resource.role not in {"python_executable", "python_env", "run_as_uid"}
            ]
            if visible_resources:
                lines.append("关键资源：")
            for resource in visible_resources:
                lines.append(
                    "- %s: %s"
                    % (_localized_resource_name(resource.role or resource.name), resource.value)
                )
        lines.append("变更步骤：")
        for change in plan.steps:
            lines.append(
                "- %s: %s — %s"
                % (
                    change.step_id,
                    _localized_plan_text(change.title),
                    _localized_plan_text(change.objective),
                )
            )
            lines.append(
                "  预期：%s"
                % "; ".join(
                    _localized_plan_text(item) for item in change.expected_changes
                )
            )
            lines.append(
                "  验收：%s"
                % ", ".join(
                    str(item.get("checker") or "unknown")
                    for item in change.postconditions
                )
            )
            for step in MutationWorkflow._execution_steps(change):
                binding = step.execution_binding
                if binding is None:
                    lines.append("  执行绑定：缺失")
                    continue
                if binding.kind == "registered_action":
                    lines.append(
                        "  执行方式：%s"
                        % _binding_summary(binding.action, binding.args)
                    )
                    continue
                artifact = binding.shell_artifact
                lines.append(
                    "  执行方式：受控脚本 %s（哈希 %s…）"
                    % (
                        artifact.artifact_id if artifact is not None else "missing",
                        artifact.sha256[:12] if artifact is not None else "missing",
                    )
                )
        lines.extend(
            [
                "精确计划哈希：%s" % plan.content_hash,
                "查看完整绑定：show-priv-plan-details %s" % plan.plan_id,
                "请使用以下命令确认这份精确计划：",
                "confirm-priv-plan %s %s" % (plan.plan_id, plan.content_hash),
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _detail_message(plan: ChangePlan) -> str:
        lines = [
            "变更计划 %s 的完整执行绑定（已脱敏）：" % plan.plan_id,
        ]
        for change in plan.steps:
            for step in MutationWorkflow._execution_steps(change):
                binding = step.execution_binding
                lines.append("- %s" % _localized_plan_text(step.title or step.step_id))
                if binding is None:
                    lines.append("  执行绑定：缺失")
                elif binding.kind == "registered_action":
                    lines.append(
                        "  Action=%s 参数=%s"
                        % (binding.action, _redacted_binding_args(binding.args))
                    )
                elif binding.shell_artifact is not None:
                    lines.extend([
                        "  Shell Artifact=%s sha256=%s" % (
                            binding.shell_artifact.artifact_id,
                            binding.shell_artifact.sha256,
                        ),
                        "```bash",
                        redact_sensitive_text(binding.shell_artifact.script),
                        "```",
                    ])
        lines.append("精确计划哈希：%s" % plan.content_hash)
        return "\n".join(lines)


def _localized_plan_text(value: Any) -> str:
    """Translate deterministic runtime phrases at the presentation boundary."""

    text = str(value or "")
    substitutions = (
        (r"\bPrepare project root entry files\b", "准备项目根目录入口文件"),
        (r"\bStart ([A-Za-z][A-Za-z0-9_-]*) screen component\b", r"启动 \1 Screen 组件"),
        (r"\bRestart ([A-Za-z][A-Za-z0-9_-]*) screen component\b", r"重启 \1 Screen 组件"),
        (r"start missing (master|worker) role at (\d+) and backend health succeeds",
         r"启动缺失的 \1 角色（端口 \2），并确认后端健康"),
        (r"restart requested (master|worker) role at (\d+) and backend health succeeds",
         r"按要求重启 \1 角色（端口 \2），并确认后端健康"),
        (r"start missing celery role and process readiness succeeds",
         "启动缺失的 celery 角色，并确认进程就绪"),
        (r"restart requested celery role and process readiness succeeds",
         "按要求重启 celery 角色，并确认进程就绪"),
        (r"start missing web_terminal role at (\d+) and listener readiness succeeds",
         r"启动缺失的 web_terminal 角色（端口 \1），并确认监听就绪"),
        (r"restart requested web_terminal role at (\d+) and listener readiness succeeds",
         r"按要求重启 web_terminal 角色（端口 \1），并确认监听就绪"),
        (r"start missing managed component ([A-Za-z][A-Za-z0-9_-]*) and component readiness succeeds",
         r"启动缺失的受管组件 \1，并确认组件就绪"),
        (r"restart requested managed component ([A-Za-z][A-Za-z0-9_-]*) and component readiness succeeds",
         r"按要求重启受管组件 \1，并确认组件就绪"),
        (r"recover (master|worker) for (/[^;\s]+)", r"恢复 \1（实例 \2）"),
    )
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def _execution_receipt(plan: ChangePlan) -> str:
    """Render persisted execution facts without exposing binding internals."""

    status_text = {
        "completed": "已执行完成",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "executing": "正在执行",
        "verifying": "正在验证",
        "approved": "已确认，等待执行",
        "awaiting_confirmation": "等待确认",
        "draft": "草拟中",
    }.get(str(plan.status or ""), "状态：%s" % plan.status)
    lines = ["计划 %s %s。" % (plan.plan_id, status_text), "目标：%s" % plan.goal]
    lines.append("执行结果：")
    for change in plan.steps:
        steps = list(MutationWorkflow._execution_steps(change))
        for step in steps:
            label = _localized_plan_text(step.title or step.objective or step.step_id)
            state = {
                "completed": "完成", "skipped": "跳过", "paused": "暂停",
                "failed": "失败", "running": "执行中", "verifying": "验证中",
                "pending": "未执行", "execution_unknown": "结果未知",
            }.get(step.status, step.status)
            lines.append("- %s：%s" % (label, state))
            for command in getattr(step.evidence, "commands", []) or []:
                if not bool(command.get("changes_state")):
                    continue
                location = str(command.get("cwd") or "").strip()
                lines.append(
                    "  - 命令：%s%s"
                    % (
                        str(command.get("command") or ""),
                        "（cwd=%s）" % location if location else "",
                    )
                )
            mutation = dict(getattr(step.evidence, "mutation", {}) or {})
            if mutation.get("kind") == "component_restart":
                lines.append(
                    "  - 进程：旧 PID %s → 新 PID %s；Screen=%s"
                    % (
                        mutation.get("old_pids") or "none",
                        mutation.get("new_pids") or "none",
                        mutation.get("session") or "unknown",
                    )
                )
            for check in step.checks:
                check_state = "通过" if check.status == "passed" else (
                    "失败" if check.status == "failed" else check.status
                )
                observed = str(check.observed or "").strip()
                lines.append(
                    "  - %s：%s%s"
                    % (
                        check.checker,
                        check_state,
                        "（%s）" % observed if observed else "",
                    )
                )
            if step.observation and step.status not in {"completed", "skipped"}:
                lines.append("  - 说明：%s" % redact_sensitive_text(step.observation))
    return "\n".join(lines)


def _binding_summary(action: str, args: dict[str, Any]) -> str:
    component = str(args.get("component") or "").strip()
    session = str(args.get("screen_session") or "").strip()
    if action == "prepare_project_files":
        hashes = args.get("entry_sha256s")
        count = len(hashes) if isinstance(hashes, dict) else 0
        overwrite = args.get("overwrite_files")
        names = [str(item) for item in overwrite] if isinstance(overwrite, list) else []
        return "准备项目根目录入口文件（%s 个；覆盖：%s）" % (
            count,
            ", ".join(names) if names else "无",
        )
    if action in {"start_screen_component", "restart_screen_component"}:
        verb = "启动" if action.startswith("start_") else "重启"
        return "%s交互式 Screen 组件 %s%s" % (
            verb,
            component or "unknown",
            "（%s）" % session if session else "",
        )
    if action in {"stop_screen_component", "stop_klonet_component"}:
        return "停止组件 %s%s" % (
            component or "unknown",
            "（%s）" % session if session else "",
        )
    return "受控动作 %s（敏感参数已隐藏）" % action


def _localized_resource_name(value: str) -> str:
    names = {
        "instance_root": "项目根目录",
        "project_root": "项目根目录",
        "instance_identifier": "实例标识",
        "master_port": "Master 端口",
        "worker_port": "Worker 端口",
        "web_terminal_port": "Web Terminal 端口",
        "public_port": "对外端口",
    }
    return names.get(str(value or ""), str(value or "资源"))


def _redacted_binding_args(args: dict[str, Any]) -> str:
    """Render a reviewable contract without disclosing credential values."""

    safe: dict[str, Any] = {}
    sensitive = re.compile(r"password|passwd|pwd|secret|token|credential", re.I)
    for key, value in args.items():
        if sensitive.search(str(key)):
            safe[key] = "[REDACTED]"
            continue
        if key == "environment" and isinstance(value, list):
            rendered = []
            for item in value:
                name, separator, _content = str(item).partition("=")
                rendered.append(
                    "%s=[REDACTED]" % name
                    if separator and sensitive.search(name)
                    else str(item)
                )
            safe[key] = rendered
            continue
        if key == "command" and isinstance(value, list):
            rendered = list(value)
            for index, item in enumerate(rendered[:-1]):
                if str(item).lower() in {"--requirepass", "--password", "--passwd"}:
                    rendered[index + 1] = "[REDACTED]"
            safe[key] = rendered
            continue
        safe[key] = value
    return redact_sensitive_text(str(safe))


def _recovery_options(goal: str, stage: str) -> list[RecoveryOption]:
    restart = any(marker in str(goal or "").lower() for marker in ("重启", "restart"))
    if restart and stage in {"planning", "binding", "execution", "verification"}:
        primary = RecoveryOption(
            option_id="component_restart",
            label="改用逐组件安全重启",
            description="重新核验目标根目录和原端口，按组件生成可单独验证的交互式 Screen 重启计划。",
            action="component_restart",
            recommended=True,
        )
    else:
        primary = RecoveryOption(
            option_id="collect_more_evidence",
            label="继续补充只读证据",
            description="保留当前目标和静态证据，刷新运行状态后重新规划。",
            action="collect_more_evidence",
            recommended=True,
        )
    secondary = (
        RecoveryOption(
            option_id="collect_more_evidence",
            label="继续补充只读证据",
            description="补查失败步骤所需的进程、端口、文件或日志证据后重新规划。",
            action="collect_more_evidence",
        )
        if primary.option_id != "collect_more_evidence"
        else RecoveryOption(
            option_id="provide_direction",
            label="调整目标或处理范围",
            description="由用户补充目标边界、可接受风险或期望处理方式。",
            action="provide_direction",
        )
    )
    return [
        primary,
        secondary,
        RecoveryOption(
            option_id="cancel",
            label="取消本次操作",
            description="保留失败记录，不执行剩余步骤。",
            action="cancel",
            requires_new_approval=False,
        ),
    ]


def _failure_message(failure: FailureOutcome) -> str:
    lines = [
        "本轮目标尚未完成，系统没有隐藏失败。",
        "失败阶段：%s" % failure.stage,
        "原因：%s" % failure.summary,
        "具体失败：%s" % redact_sensitive_text(failure.technical_reason)[:800],
        "环境是否已改变：%s" % {
            "true": "是", "false": "否", "unknown": "无法确定",
        }[failure.environment_changed],
    ]
    if failure.failed_step:
        lines.append("失败步骤：%s" % failure.failed_step)
    if failure.attempted_recoveries:
        lines.append("已自动尝试：%s" % "；".join(failure.attempted_recoveries))
    lines.append("可选处理方式：")
    for index, option in enumerate(failure.options, start=1):
        lines.append(
            "%s. %s%s — %s" % (
                index,
                option.label,
                "（推荐）" if option.recommended else "",
                option.description,
            )
        )
    lines.extend([
        "回复“选择 1/2/3”，或使用：choose-priv-option %s <option_id>"
        % failure.failure_id,
        "查看技术详情：show-priv-failure-details %s" % failure.failure_id,
    ])
    return "\n".join(lines)


def _failure_detail_message(failure: FailureOutcome) -> str:
    lines = [
        "失败记录 %s（技术详情，已脱敏）" % failure.failure_id,
        "目标：%s" % failure.goal,
        "阶段/类别：%s / %s" % (failure.stage, failure.category),
        "摘要：%s" % failure.summary,
        "技术原因：%s" % redact_sensitive_text(failure.technical_reason),
        "环境变化：%s" % failure.environment_changed,
    ]
    if failure.completed_steps:
        lines.append("已完成步骤：%s" % "；".join(failure.completed_steps))
    if failure.failed_checks:
        lines.append("失败校验：%s" % "；".join(failure.failed_checks))
    if failure.attempted_recoveries:
        lines.append("恢复尝试：%s" % "；".join(failure.attempted_recoveries))
    return "\n".join(lines)


def _step_environment_changed(step: PrivilegedStep) -> str:
    evidence = getattr(step, "evidence", None)
    value = getattr(evidence, "environment_changed", None)
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _plan_environment_changed(plan: ChangePlan) -> str:
    states = [
        _step_environment_changed(step)
        for change in plan.steps
        for step in MutationWorkflow._execution_steps(change)
        if getattr(step, "evidence", None) is not None
    ]
    if "unknown" in states:
        return "unknown"
    if "true" in states:
        return "true"
    return "false"


def _completed_step_labels(plan: ChangePlan) -> list[str]:
    return [
        _localized_plan_text(step.title or step.objective or step.step_id)
        for change in plan.steps
        for step in MutationWorkflow._execution_steps(change)
        if step.status == "completed"
    ]
