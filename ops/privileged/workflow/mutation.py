"""Confirmation-gated mutation workflow for Ops-Privilege."""

from __future__ import annotations

from copy import deepcopy
import inspect
import re
import uuid
from typing import Any, Iterable

from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.contracts import (
    component_port_arg, PrivilegedPlan, PrivilegedStep, VerificationDecision,
)
from klonet_agent.ops.privileged.execution_agent import (
    _canonical_action_postconditions,
    ExecutionBindingError,
    validate_authorizable_change_plan,
)
from klonet_agent.ops.privileged.workflow.coordinator import WorkflowResult
from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
from klonet_agent.ops.privileged.workflow.contracts import (
    ChangePlan, ChangeStep, FailureRecord, GoalOutcome, RecoveryOption,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory
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
        response: Any | None = None,
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
        self.response = response
        self.max_replanning_rounds = max(0, int(max_replanning_rounds))
        self.max_candidate_replans = max(0, int(max_candidate_replans))

    def plan_once(
        self,
        goal: str,
        *,
        evidence_bundle: Any,
        evidence_conclusion: Any,
        intent_context: dict[str, Any] | None = None,
        binding_feedback: str = "",
        candidate_plan: ChangePlan | None = None,
    ) -> GoalOutcome:
        kwargs: dict[str, Any] = {}
        parameters = inspect.signature(self.planner.plan).parameters
        accepts_kwargs = any(
            item.kind == inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        if binding_feedback and ("binding_feedback" in parameters or accepts_kwargs):
            kwargs["binding_feedback"] = binding_feedback
        if intent_context and ("intent_context" in parameters or accepts_kwargs):
            kwargs["intent_context"] = dict(intent_context)
        if candidate_plan is not None and (
            "candidate_plan" in parameters or accepts_kwargs
        ):
            kwargs["candidate_plan"] = candidate_plan
        return self.planner.plan(
            goal, evidence_bundle, evidence_conclusion, **kwargs,
        )

    def classify_recovery_reply(self, **kwargs: Any) -> dict[str, Any]:
        classify = getattr(self.planner, "classify_reply_relation", None)
        if classify is None:
            return {
                "relation": "supplement",
                "reason": "现有 Planner 未提供关系分类时保留基础目标",
                "normalized_decision": str(kwargs.get("reply") or "").strip(),
                "candidate_base_goal": "",
                "conflicts": [],
            }
        return dict(classify(**kwargs))

    def bind_once(
        self, plan: ChangePlan, *, evidence_bundle: Any,
        predecessor_plan: ChangePlan | None = None,
    ) -> GoalOutcome:
        self.store.save(plan)
        grounded_context = self._binding_context(evidence_bundle)
        try:
            plan = self.binder.bind(
                plan,
                grounded_context=grounded_context,
            )
        except ChangeBindingError as exc:
            # Binding may have expanded some semantic steps before rejecting a
            # later atomic contract.  This is still a successor of the
            # executed predecessor, so retain every already-proved component
            # effect before exposing or persisting the failed candidate.
            self._inherit_completed_component_effects(plan, predecessor_plan)
            plan.status = "draft"
            plan.authorized_hash = ""
            self.store.save(plan)
            return GoalOutcome(
                status="need_replan",
                reason=str(exc),
                candidate_plan=plan,
                failed_criteria=list(exc.failed_criteria),
                missing_decisions=list(exc.missing_decisions),
                replan_context=dict(exc.replan_context),
            )
        self._inherit_completed_component_effects(plan, predecessor_plan)
        try:
            validate_authorizable_change_plan(plan)
        except ExecutionBindingError as exc:
            plan.status = "draft"
            plan.authorized_hash = ""
            self.store.save(plan)
            return GoalOutcome(
                status="need_replan",
                reason=str(exc),
                candidate_plan=plan,
                failed_criteria=list(exc.failed_criteria),
                missing_decisions=list(exc.missing_decisions),
                replan_context=dict(exc.replan_context),
            )
        plan.status = "awaiting_confirmation"
        plan.authorized_hash = ""
        self.store.save(plan)
        return GoalOutcome(
            status="needs_user_decision",
            user_question=self._confirmation_message(plan),
            plan=plan,
        )

    @staticmethod
    def _inherit_completed_component_effects(
        plan: ChangePlan,
        predecessor_plan: ChangePlan | None,
    ) -> None:
        """Carry Screen effect state and identity into the successor plan tree.

        A recovery plan is a successor state of the same goal, not a fresh
        task.  Completed Screen lifecycle nodes therefore remain in the one
        authoritative plan tree.  Unfinished nodes may change implementation
        (for example start -> restart), but their project/component Screen
        identity remains authoritative.  This prevents a later Replan from
        reintroducing a completed component or inventing a parallel session
        name for the same failed effect.
        """

        if predecessor_plan is None or predecessor_plan is plan:
            return
        completed: dict[tuple[str, str], PrivilegedStep] = {}
        identities: dict[tuple[str, str], tuple[str, str]] = {}
        for change in predecessor_plan.steps:
            implementation = change.implementation_plan
            for step in list(getattr(implementation, "steps", []) or []):
                binding = step.execution_binding
                if binding is None or binding.action not in {
                    "start_screen_component", "restart_screen_component",
                }:
                    continue
                root = str(binding.args.get("project_root") or "").rstrip("/")
                component = str(binding.args.get("component") or "").strip()
                if root and component:
                    session = str(
                        binding.args.get("screen_session") or ""
                    ).strip()
                    platform = str(binding.args.get("platform") or "").strip()
                    if session:
                        identities[(root, component)] = (session, platform)
                    if str(step.status or "") == "completed":
                        completed[(root, component)] = deepcopy(step)
        if not completed and not identities:
            return

        root_by_change: dict[str, str] = {}
        for resource in plan.resources:
            if str(resource.role or "") != "instance_root":
                continue
            root = str(resource.value or "").rstrip("/")
            for consumer in resource.consumers:
                root_by_change[str(consumer).split(".", 1)[0]] = root
        for change in plan.steps:
            implementation = change.implementation_plan
            if implementation is None:
                continue
            root = root_by_change.get(change.step_id, "")
            if not root:
                for step in implementation.steps:
                    binding = step.execution_binding
                    candidate = str(
                        (binding.args if binding is not None else {}).get(
                            "project_root"
                        ) or ""
                    ).rstrip("/")
                    if candidate:
                        root = candidate
                        break
            for step in implementation.steps:
                binding = step.execution_binding
                if binding is None or binding.action not in {
                    "start_screen_component", "restart_screen_component",
                }:
                    continue
                component = str(binding.args.get("component") or "").strip()
                identity = identities.get((root, component))
                if identity is None:
                    continue
                session, platform = identity
                binding.args["screen_session"] = session
                if platform:
                    binding.args["platform"] = platform
                for checks in (binding.postconditions, step.postconditions):
                    for check in checks:
                        if (
                            isinstance(check, dict)
                            and check.get("checker") == "screen_session_exists"
                            and isinstance(check.get("args"), dict)
                        ):
                            check["args"]["session"] = session
                marker = "recovery preserves predecessor Screen identity"
                if marker not in str(binding.binding_reason or ""):
                    binding.binding_reason = "%s; %s" % (
                        str(binding.binding_reason or "").rstrip("; "), marker,
                    )
            inherited = {
                component: step
                for (effect_root, component), step in completed.items()
                if effect_root == root
            }
            if not inherited:
                continue
            replacements: dict[str, str] = {}
            retained: list[PrivilegedStep] = []
            present_completed: set[str] = set()
            for step in implementation.steps:
                binding = step.execution_binding
                component = str(
                    (binding.args if binding is not None else {}).get(
                        "component"
                    ) or ""
                ).strip()
                if (
                    binding is not None
                    and binding.action in {
                        "start_screen_component", "restart_screen_component",
                    }
                    and component in inherited
                ):
                    prior = inherited[component]
                    replacements[step.step_id] = prior.step_id
                    if component not in present_completed:
                        retained.append(prior)
                        present_completed.add(component)
                    continue
                retained.append(step)
            for component, prior in inherited.items():
                if component not in present_completed:
                    retained.append(prior)
            for step in retained:
                step.depends_on = list(dict.fromkeys(
                    replacements.get(item, item) for item in step.depends_on
                    if replacements.get(item, item) != step.step_id
                ))
            implementation.steps = retained

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
            "docker_images": 0,
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
        inventory = RuntimeInventory.from_bundle(evidence_bundle)
        runtime_instances = [
            {
                "platform": instance.platform,
                "project_root": instance.project_root,
                "roles": list(instance.roles),
                "configured_ports": dict(instance.configured_ports),
                "fields": dict(instance.fields),
                "evidence_id": instance.evidence_id,
            }
            for instance in inventory.instances
        ]
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
            facts={"runtime_instances": runtime_instances},
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
            outcome = GoalOutcome(
                "achieved", reason="该计划已经通过最终目标级验证。",
            )
            return WorkflowResult(
                True, "completed", _execution_receipt(plan), plan=plan,
                outcome=outcome,
            )
        pending_failure = self._pending_plan_failure(plan)
        if pending_failure is not None and plan.status in {"paused", "blocked"}:
            message = self._render_failure_message(pending_failure)
            outcome = GoalOutcome(
                "needs_user_decision",
                user_question=message,
                failed_criteria=list(pending_failure.failed_checks),
                missing_decisions=list(pending_failure.missing_decisions),
            )
            return WorkflowResult(
                True,
                "awaiting_user_decision",
                message,
                plan=plan,
                failure=pending_failure,
                outcome=outcome,
            )
        if plan.status == "paused" and plan.is_authorized:
            # A paused approved plan is immutable execution evidence.  It must
            # return to Coordinator's post-execution diagnosis/Replan loop;
            # reconfirming the old hash must never retry the old implementation.
            return WorkflowResult(
                True,
                "paused",
                _paused_plan_reason(plan),
                plan=plan,
            )
        if plan.status != "awaiting_confirmation":
            return WorkflowResult(
                True,
                "confirmation_rejected",
                "confirmation hash or plan state does not match; nothing executed.",
                plan=plan,
            )
        try:
            validate_authorizable_change_plan(plan)
        except ExecutionBindingError as exc:
            return self.failure_result(
                stage="binding",
                category="persisted_plan_contract_invalid",
                summary=(
                    "该计划由旧绑定合同生成，已不满足当前安全实施约束，"
                    "因此没有执行。"
                ),
                technical_reason=str(exc),
                goal=plan.goal,
                goal_kind="execution",
                plan=plan,
                attempted_recoveries=["确认前重新校验精确绑定合同"],
                failed_checks=[str(exc)],
            )
        self._approve_shell_artifacts(plan)
        plan.authorize()
        self.store.save(plan)
        return self._execute(plan)

    def resume_paused_plan(self, plan_id: str) -> WorkflowResult | None:
        """Expose persisted paused execution to the existing recovery loop."""

        try:
            plan = self.store.load(str(plan_id or ""))
        except (KeyError, ValueError):
            return None
        if plan.status != "paused" or not plan.is_authorized:
            return None
        return WorkflowResult(
            True, "paused", _paused_plan_reason(plan), plan=plan,
        )

    def retry_unchanged_paused_action(
        self, plan_id: str,
    ) -> WorkflowResult | None:
        """Retry one proved-no-change failure under the original approval.

        A non-zero action result which explicitly records
        ``environment_changed=false`` did not consume any mutation effect.
        Replaying that exact frozen action once is therefore a deterministic
        continuation of the approved plan, not a new planning decision.  All
        unknown, timed-out, changed, or repeatedly failed executions remain
        owned by the normal Discovery/Replan path.
        """

        try:
            plan = self.store.load(str(plan_id or ""))
        except (KeyError, ValueError):
            return None
        if plan.status != "paused" or not plan.is_authorized:
            return None
        retryable = []
        for change in plan.steps:
            for step in self._execution_steps(change):
                evidence = step.evidence
                if str(step.status or "") != "paused" or evidence is None:
                    continue
                if (
                    evidence.return_code is None
                    or evidence.return_code == 0
                    or bool(evidence.timed_out)
                    or evidence.environment_changed is not False
                    or int(step.execution_attempts or 0) != 1
                ):
                    continue
                retryable.append(step)
        if len(retryable) != 1:
            return None
        self._emit_execution_progress(
            "确定性恢复：上次动作明确未改变环境，正在按原审批重试：%s"
            % _localized_plan_text(
                retryable[0].title or retryable[0].objective
            )
        )
        return self._execute(plan)

    def plan_context(self, *, active_plan_id: str = "", goal: str = "") -> str:
        """Return compact persisted facts for semantic plan-turn routing."""

        list_plans = getattr(self.store, "list", None)
        if list_plans is not None:
            plans = list(list_plans())
        elif active_plan_id:
            try:
                plans = [self.store.load(active_plan_id)]
            except (KeyError, ValueError):
                plans = []
        else:
            plans = []
        expected_goal = str(goal or "").strip()
        relevant = [
            plan for plan in plans
            if plan.plan_id == active_plan_id
            or not expected_goal
            or str(plan.goal or "").strip() == expected_goal
        ][:8]
        lines = [
            "active_plan_id=%s" % (active_plan_id or "none"),
            "persisted_plan_count=%s" % len(relevant),
        ]
        for plan in relevant:
            contract = "not_applicable"
            if plan.status == "awaiting_confirmation":
                try:
                    validate_authorizable_change_plan(plan)
                    contract = "valid"
                except ExecutionBindingError as exc:
                    contract = "invalid:%s" % str(exc)
            lines.append(
                "plan_id=%s status=%s contract=%s goal=%s"
                % (plan.plan_id, plan.status, contract, plan.goal)
            )
        return "\n".join(lines)

    def manage_plan_turn(
        self,
        plan_id: str,
        *,
        question: str,
        conversation_context: str = "",
    ) -> WorkflowResult:
        """Manage one natural-language plan query from persisted state."""

        try:
            plan = self.store.load(plan_id)
        except (KeyError, ValueError):
            return WorkflowResult(
                True, "not_found", "未找到指定的变更计划。",
            )
        contract_error = ""
        if plan.status == "awaiting_confirmation":
            try:
                validate_authorizable_change_plan(plan)
            except ExecutionBindingError as exc:
                contract_error = str(exc)
                result = self.failure_result(
                    stage="binding",
                    category="persisted_plan_contract_invalid",
                    summary=(
                        "当前持久化计划仍是旧版本，且不满足现行绑定合同；"
                        "本轮没有执行任何操作。"
                    ),
                    technical_reason=contract_error,
                    goal=plan.goal,
                    goal_kind="execution",
                    plan=plan,
                    attempted_recoveries=["查询计划时复核当前绑定合同"],
                    failed_checks=[contract_error],
                )
                fallback = (
                    "你之前确实可能在对话中讨论过修订，但当前没有新的正式 "
                    "Plan ID；持久化的仍是旧计划 %s。该计划已被判定为不符合"
                    "当前合同并阻塞，没有执行。要落实之前的修改，需要进入现有"
                    " Replan 流程生成新计划。" % plan.plan_id
                )
                result.message = self._render_plan_turn(
                    question,
                    conversation_context=conversation_context,
                    selected_plan=plan,
                    contract_error=contract_error,
                    fallback=fallback,
                )
                return result
        fallback = self.render_plan_status(plan.plan_id)
        return WorkflowResult(
            True,
            "plan_status",
            self._render_plan_turn(
                question,
                conversation_context=conversation_context,
                selected_plan=plan,
                contract_error=contract_error,
                fallback=fallback,
            ),
            plan=plan,
        )

    def _render_plan_turn(
        self,
        question: str,
        *,
        conversation_context: str,
        selected_plan: ChangePlan,
        contract_error: str,
        fallback: str,
    ) -> str:
        render = getattr(self.response, "render_plan_turn", None)
        if render is None:
            return fallback
        context = self.plan_context(
            active_plan_id=selected_plan.plan_id,
            goal=selected_plan.goal,
        )
        context += "\nselected_plan_hash=%s" % selected_plan.content_hash
        if contract_error:
            context += "\nselected_plan_contract_error=%s" % contract_error
        return str(render(
            question,
            conversation_context=conversation_context,
            plan_context=context,
            fallback=fallback,
        ))


    def handle_control(self, text: str) -> WorkflowResult | None:
        normalized = str(text or "").strip()
        pending_failure = self._latest_pending_failure()
        natural_choice = (
            re.fullmatch(
                r"(?:(?:选择|选)\s*)?(\d+)"
                r"(?:(?:[ \t]*[,，:：][ \t]*|[ \t]+)(.+))?",
                normalized,
            )
            if pending_failure is not None
            else None
        )
        choice_tail = (
            str(natural_choice.group(2) or "").strip()
            if natural_choice is not None
            else ""
        )
        if _is_failure_detail_query(normalized) and natural_choice is None:
            try:
                failures = self.store.list_failures()
                failure = pending_failure or (failures[0] if failures else None)
                if failure is None:
                    raise KeyError("no failure")
            except (AttributeError, KeyError, ValueError):
                return WorkflowResult(True, "not_found", "当前会话没有可处理的失败记录。")
            return WorkflowResult(
                True, "failure_details", self._render_failure_details(failure),
                failure=failure,
            )
        failure_detail = re.fullmatch(
            r"show-priv-failure-details\s+(failure-[A-Za-z0-9_-]{1,64})",
            normalized,
        )
        if failure_detail is not None:
            if (
                pending_failure is not None
                and failure_detail.group(1) != pending_failure.failure_id
            ):
                return self._pending_failure_gate(pending_failure)
            try:
                failure = self.store.load_failure(failure_detail.group(1))
            except (AttributeError, KeyError, ValueError):
                return WorkflowResult(True, "not_found", "未找到指定的失败记录。")
            return WorkflowResult(
                True, "failure_details", self._render_failure_details(failure),
                failure=failure,
            )
        awaiting_direction = _selected_recovery_option(pending_failure)
        if (
            pending_failure is not None
            and awaiting_direction is not None
            and awaiting_direction.action == "provide_direction"
        ):
            # Choosing to provide direction starts a persisted input phase; it
            # does not mean the direction has already been supplied.  The next
            # semantic turn belongs to this FailureRecord and must not fall
            # through to the ordinary intent classifier.
            pending_failure.user_direction = normalized
            self.store.save_failure(pending_failure)
            return WorkflowResult(
                True,
                "failure_direction_provided",
                normalized,
                failure=pending_failure,
            )
        choice = re.fullmatch(
            r"choose-priv-option\s+(failure-[A-Za-z0-9_-]{1,64})\s+"
            r"([a-z][a-z0-9_-]{1,63})",
            str(text or "").strip(),
        )
        labelled_choice: tuple[Any, str] | None = None
        if choice is None and natural_choice is None:
            if pending_failure is not None:
                option = _matching_recovery_option(
                    normalized, pending_failure.options,
                )
                if option is not None:
                    labelled_choice = (pending_failure, option.option_id)
        if choice is not None or natural_choice is not None or labelled_choice is not None:
            try:
                if choice is not None:
                    if (
                        pending_failure is not None
                        and choice.group(1) != pending_failure.failure_id
                    ):
                        return self._pending_failure_gate(pending_failure)
                    failure = self.store.load_failure(choice.group(1))
                    if str(failure.selected_option_id or ""):
                        raise KeyError("failure already resolved")
                    option_id = choice.group(2)
                elif natural_choice is not None:
                    if pending_failure is None:
                        raise KeyError("no failure")
                    failure = pending_failure
                    index = int(natural_choice.group(1)) - 1
                    option_id = failure.options[index].option_id
                else:
                    failure, option_id = labelled_choice
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
                if choice_tail and not _is_failure_detail_query(choice_tail):
                    failure.user_direction = choice_tail
                    self.store.save_failure(failure)
                    return WorkflowResult(
                        True,
                        "failure_direction_provided",
                        choice_tail,
                        failure=failure,
                    )
                explanation = (
                    self._render_failure_details(failure) + "\n\n"
                    if choice_tail
                    else ""
                )
                return WorkflowResult(
                    True, "clarification",
                    explanation + _direction_question(failure),
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
            if (
                pending_failure is not None
                and cancel_failure.group(1) != pending_failure.failure_id
            ):
                return self._pending_failure_gate(pending_failure)
            return self.handle_control(
                "choose-priv-option %s cancel" % cancel_failure.group(1)
            )
        if pending_failure is not None:
            return self._pending_failure_gate(pending_failure)
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

    def _latest_pending_failure(self) -> FailureRecord | None:
        """Return only the latest unresolved workflow decision, never old history."""

        try:
            failures = self.store.list_failures()
        except AttributeError:
            return None
        if not failures:
            return None
        latest = failures[0]
        if not str(latest.selected_option_id or ""):
            return latest
        selected = _selected_recovery_option(latest)
        return (
            latest
            if (
                selected is not None
                and selected.action == "provide_direction"
                and not str(latest.user_direction or "").strip()
            )
            else None
        )

    @staticmethod
    def _pending_failure_gate(failure: FailureRecord) -> WorkflowResult:
        """Keep an awaiting-user-decision workflow out of semantic routing."""

        options = "\n".join(
            "%d. %s" % (index, option.label)
            for index, option in enumerate(failure.options, start=1)
        )
        return WorkflowResult(
            True,
            "awaiting_user_decision",
            "当前目标正在等待恢复方案选择，暂不接受新的语义操作。\n"
            "%s\n"
            "请回复选项序号、完整选项名称，或使用："
            "choose-priv-option %s <option_id>\n"
            "查看技术详情：show-priv-failure-details %s"
            % (options, failure.failure_id, failure.failure_id),
            failure=failure,
        )

    def _render_failure_message(self, failure: FailureRecord) -> str:
        fallback = _failure_explanation_fallback(failure)
        render = getattr(self.response, "render_failure", None)
        explanation = fallback
        if render is not None:
            explanation = str(render(failure, fallback=fallback) or fallback).strip()
        return _failure_message(failure, explanation=explanation)

    def _render_failure_details(self, failure: FailureRecord) -> str:
        fallback = _failure_explanation_fallback(failure)
        render = getattr(self.response, "render_failure", None)
        explanation = fallback
        if render is not None:
            explanation = str(render(failure, fallback=fallback) or fallback).strip()
        return _failure_detail_message(failure, explanation=explanation)

    def failure_result(
        self,
        *,
        stage: str,
        category: str,
        summary: str,
        technical_reason: str,
        goal: str,
        goal_kind: str,
        plan: ChangePlan | None = None,
        plan_id: str = "",
        attempted_recoveries: list[str] | None = None,
        environment_changed: str = "false",
        failed_step: str = "",
        completed_steps: list[str] | None = None,
        failed_checks: list[str] | None = None,
        evidence: Any | None = None,
        evidence_requests: list[Any] | None = None,
        missing_decisions: list[str] | None = None,
    ) -> WorkflowResult:
        """Persist the sole workflow failure transition and involve the user."""

        options = _recovery_options(
            missing_decisions=list(missing_decisions or []),
        )
        rendered_completed_steps = (
            _completed_step_labels(plan)
            if plan is not None and completed_steps is None
            else list(completed_steps or [])
        )
        failure = FailureRecord(
            failure_id="failure-" + uuid.uuid4().hex[:12],
            stage=stage,
            category=category,
            summary=summary,
            technical_reason=str(technical_reason or "unknown failure"),
            environment_changed=environment_changed,
            failed_step=failed_step,
            completed_steps=rendered_completed_steps,
            failed_checks=list(failed_checks or []),
            attempted_recoveries=list(attempted_recoveries or []),
            options=options,
            goal=goal,
            goal_kind=goal_kind,
            plan_id=plan.plan_id if plan is not None else str(plan_id or ""),
            evidence_requests=list(evidence_requests or []),
            missing_decisions=list(missing_decisions or []),
        )
        if plan is not None:
            plan.failure = failure
            plan.status = (
                "paused"
                if stage in {"execution", "verification"}
                or environment_changed != "false"
                else "blocked"
            )
            self.store.save(plan)
        save_failure = getattr(self.store, "save_failure", None)
        if save_failure is not None:
            save_failure(failure)
        message = self._render_failure_message(failure)
        outcome = GoalOutcome(
            "needs_user_decision",
            reason=failure.technical_reason,
            user_question=message,
            failed_criteria=list(failure.failed_checks),
            missing_decisions=list(failure.missing_decisions),
        )
        return WorkflowResult(
            True,
            "awaiting_user_decision",
            message,
            plan=plan,
            evidence=evidence,
            failure=failure,
            outcome=outcome,
        )

    def render_plan_status(self, plan_id: str) -> str:
        """Render only the plan explicitly selected by operational context."""

        try:
            plan = self.store.load(str(plan_id or ""))
        except (KeyError, ValueError):
            return "未找到指定的变更计划。"
        if plan.status == "awaiting_confirmation":
            return self._confirmation_message(plan)
        pending_failure = self._pending_plan_failure(plan)
        if pending_failure is not None:
            return self._render_failure_message(pending_failure)
        return _execution_receipt(plan)

    def _pending_plan_failure(self, plan: ChangePlan) -> FailureRecord | None:
        """Resolve failure control state from FailureStore, not a stale snapshot."""

        failure = plan.failure
        if failure is None:
            return None
        load_failure = getattr(self.store, "load_failure", None)
        if callable(load_failure):
            try:
                canonical = load_failure(failure.failure_id)
            except (KeyError, ValueError):
                canonical = None
            if canonical is not None:
                failure = canonical
        selected = _selected_recovery_option(failure)
        if not str(failure.selected_option_id or ""):
            return failure
        if (
            selected is not None
            and selected.action == "provide_direction"
            and not str(failure.user_direction or "").strip()
        ):
            return failure
        return None

    def plan_exists(self, plan_id: str) -> bool:
        """Whether a proposed Plan reference is backed by persisted state."""

        try:
            self.store.load(str(plan_id or ""))
        except (KeyError, ValueError):
            return False
        return True

    def latest_plan_id_for_goal(self, goal: str) -> str:
        """Resolve a missing session pointer from the authoritative PlanStore."""

        expected = str(goal or "").strip()
        if not expected:
            return ""
        for plan in self.store.list():
            if str(plan.goal or "").strip() == expected:
                return plan.plan_id
        return ""

    def load_plan(self, plan_id: str) -> ChangePlan:
        """Expose a persisted plan to the Coordinator's single transition loop."""

        return self.store.load(plan_id)

    def reconcile_recovery_state(self, plan: ChangePlan) -> ChangePlan:
        """Refresh a paused atomic verdict without replaying its mutation.

        Checker semantics can become more precise after an execution was
        persisted.  A zero-return-code atomic step may therefore be provable
        from its immutable execution evidence and current deterministic state
        even though its old stored verdict was paused.  Re-evaluate that one
        existing Plan state here so Replan receives the real remaining
        effects; never call Executor and never promote a non-zero/unknown run.
        """

        verify = getattr(self.verifier, "verify_deterministic_step", None)
        verify_recovered = getattr(self.verifier, "verify_recovered_step", None)
        if not callable(verify) and not callable(verify_recovered):
            return plan
        verification_plan = self._verification_plan(plan)
        changed = False
        for change in plan.steps:
            implementation = change.implementation_plan
            steps = (
                list(implementation.steps)
                if implementation is not None else [change]
            )
            for step in steps:
                if (
                    str(step.status or "") == "execution_unknown"
                    and callable(verify_recovered)
                ):
                    # Interrupted execution has exactly one authority source:
                    # current-state postconditions.  Never send it through the
                    # goal-level Verifier and never replay the mutation.
                    candidate = self._verification_step(step)
                    decision = verify_recovered(verification_plan, candidate)
                    step.checks = list(candidate.checks)
                    recovered_status = str(
                        getattr(decision, "status", "") or ""
                    )
                    if recovered_status == "passed":
                        step.status = "completed"
                        step.observation = str(
                            getattr(decision, "reason", "")
                            or "current state proves the interrupted action completed"
                        )
                        changed = True
                    elif recovered_status == "failed":
                        step.status = "paused"
                        step.observation = str(
                            getattr(decision, "reason", "")
                            or "current state proves the interrupted action did not satisfy its postconditions"
                        )
                        changed = True
                    continue
                if str(step.status or "") != "paused":
                    continue
                evidence = step.evidence
                if (
                    evidence is None
                    or evidence.return_code != 0
                    or bool(evidence.timed_out)
                ):
                    continue
                candidate = self._verification_step(step)
                if not callable(verify):
                    continue
                decision = verify(verification_plan, candidate)
                step.checks = list(candidate.checks)
                if str(getattr(decision, "status", "") or "") != "passed":
                    continue
                step.status = "completed"
                step.observation = str(
                    getattr(decision, "reason", "")
                    or "persisted execution satisfies current deterministic checks"
                )
                changed = True
            if implementation is not None and any(
                item.status in {"paused", "execution_unknown"}
                for item in implementation.steps
            ):
                implementation.status = "paused"
                change.status = "paused"
            elif implementation is not None and all(
                item.status in {"completed", "skipped"}
                for item in implementation.steps
            ):
                implementation.status = "completed"
                # Older persisted plans could record every atomic action as
                # completed and then pause only the enclosing semantic step.
                # Such an action is not a completed component effect until
                # the component-level acceptance checks also pass.
                if str(change.status or "") == "paused" and str(
                    change.observation or ""
                ).strip():
                    semantic = self._semantic_verification_step(change)
                    if not callable(verify):
                        continue
                    decision = verify(verification_plan, semantic)
                    if str(getattr(decision, "status", "") or "") == "passed":
                        change.status = "completed"
                        change.observation = str(
                            getattr(decision, "reason", "")
                            or "semantic acceptance checks passed during recovery"
                        )
                        changed = True
                    elif self._demote_semantically_unaccepted_components(
                        change, semantic,
                    ):
                        changed = True
        if changed:
            self.store.save(plan)
        return plan

    def abort_plan(self, plan_id: str) -> bool:
        """Supersede one unfinished plan while preserving its audit record."""

        try:
            plan = self.store.load(str(plan_id or ""))
        except (KeyError, ValueError):
            return False
        if plan.status in {"completed", "failed", "aborted"}:
            return False
        plan.status = "aborted"
        plan.authorized_hash = ""
        self.store.save(plan)
        return True

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
                try:
                    step.evidence = self.executor.execute(step)
                except BaseException:
                    # The CLI owns KeyboardInterrupt/SystemExit presentation,
                    # but the workflow owns durable execution truth.  Never
                    # leave a persisted ``running`` node after the process was
                    # interrupted, and never infer whether the mutation did or
                    # did not happen without execution evidence.
                    step.status = "execution_unknown"
                    step.observation = (
                        "workflow interrupted while execution was active; "
                        "inspect current state and never auto-reexecute"
                    )
                    change.status = plan.status = "paused"
                    if change.implementation_plan is not None:
                        change.implementation_plan.status = "paused"
                    self.store.save(plan)
                    raise
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
                    self._demote_semantically_unaccepted_components(
                        change, semantic_step,
                    )
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
        plan_verification = self.verifier.verify_plan_execution(plan)
        if plan_verification.status != "passed":
            plan.status = "paused"
            self.store.save(plan)
            return WorkflowResult(
                True,
                "paused",
                str(plan_verification.reason or "目标级验收未通过"),
                plan=plan,
                verification=plan_verification,
            )
        plan.status = "verifying"
        self.store.save(plan)
        return WorkflowResult(
            True,
            "goal_verification",
            "计划步骤已通过校验，正在验证完整用户目标。",
            plan=plan,
            verification=plan_verification,
        )

    def complete_goal(self, plan: ChangePlan, outcome: GoalOutcome) -> WorkflowResult:
        """Commit completion only after the authoritative goal Verifier agrees."""

        if outcome.status != "achieved":
            raise ValueError("goal completion requires an achieved post-execution plan")
        plan_verification = self.verifier.verify_plan_execution(plan)
        gaps = plan.completion_gaps
        if (
            not plan.is_authorized
            or plan_verification.status != "passed"
            or gaps
        ):
            plan.status = "paused"
            self.store.save(plan)
            reason = (
                "拒绝提交目标完成状态：获批计划仍有未完成节点：%s"
                % ", ".join(gaps[:12])
                if gaps else
                "拒绝提交目标完成状态：计划尚未授权或未通过计划级验收。"
            )
            return WorkflowResult(
                True,
                "paused",
                reason,
                plan=plan,
                outcome=GoalOutcome("blocked", reason=reason),
                verification=plan_verification,
            )
        plan.status = "completed"
        self.store.save(plan)
        return WorkflowResult(
            True, "completed", _execution_receipt(plan), plan=plan,
            outcome=outcome,
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
        if stage == "verification":
            # A deterministic failed postcondition is evidence for the
            # existing post-execution diagnostic/Replan loop.  Escalating here
            # would make the user manually trigger work the system can still
            # perform safely.  Unknown execution state remains an immediate
            # FailureRecord in the execution-stage callers above.
            return WorkflowResult(
                True,
                "paused",
                reason,
                plan=plan,
                verification=verification,
            )
        result = self.failure_result(
            stage=stage,
            category=category,
            summary=(
                "执行后的验收未通过，任务尚未完成。"
                if stage == "verification"
                else "执行结果无法被安全确认为成功。"
            ),
            technical_reason=reason,
            goal=plan.goal,
            goal_kind="execution",
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
        evidence_steps = [
            step for step in atomic_steps if step.evidence is not None
        ]
        latest_evidence = (
            max(
                evidence_steps,
                key=lambda item: str(
                    getattr(item.evidence, "finished_at", "")
                    or getattr(item.evidence, "started_at", "")
                    or ""
                ),
            ).evidence
            if evidence_steps else None
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
            evidence=latest_evidence,
        )

    @staticmethod
    def _demote_semantically_unaccepted_components(
        change: ChangeStep,
        semantic_step: PrivilegedStep,
    ) -> bool:
        """Keep one completion truth across atomic and semantic verification.

        A Screen action proves that a process was launched under the approved
        session.  It does not by itself prove that the requested component is
        healthy.  When a semantic acceptance check fails or is unavailable,
        return the owning component action to ``paused`` so recovery planning
        cannot incorrectly treat that component effect as complete.
        """

        implementation = change.implementation_plan
        if implementation is None:
            return False
        lifecycle_steps = [
            step for step in implementation.steps
            if str(step.status or "") == "completed"
            and step.execution_binding is not None
            and step.execution_binding.kind == "registered_action"
            and step.execution_binding.action in {
                "start_screen_component", "restart_screen_component",
            }
        ]
        if not lifecycle_steps:
            return False

        rejected_specs = [
            specification
            for specification, result in zip(
                semantic_step.postconditions, semantic_step.checks,
            )
            if str(getattr(result, "status", "") or "") != "passed"
        ]
        owners = {
            step.step_id
            for specification in rejected_specs
            for step in lifecycle_steps
            if MutationWorkflow._semantic_check_owned_by_component(
                specification, step,
            )
        }
        if not owners:
            # The semantic contract may contain a valid long-tail checker that
            # cannot be mapped to a role-specific port or session.  Preserve
            # earlier accepted effects and invalidate the most recently
            # executed lifecycle action—the one this semantic verdict follows.
            evidence_steps = [
                step for step in lifecycle_steps if step.evidence is not None
            ]
            candidates = evidence_steps or lifecycle_steps
            latest = max(
                candidates,
                key=lambda item: str(
                    getattr(item.evidence, "finished_at", "")
                    or getattr(item.evidence, "started_at", "")
                    or ""
                ),
            )
            owners.add(latest.step_id)

        changed = False
        failed_results = [
            result for result in semantic_step.checks
            if str(getattr(result, "status", "") or "") != "passed"
        ]
        for step in lifecycle_steps:
            if step.step_id not in owners:
                continue
            step.status = "paused"
            step.checks.extend(
                result for result in failed_results
                if result not in step.checks
            )
            step.observation = "component action passed but semantic acceptance failed"
            changed = True
        if changed:
            implementation.status = "paused"
        return changed

    @staticmethod
    def _semantic_check_owned_by_component(
        specification: dict[str, Any],
        step: PrivilegedStep,
    ) -> bool:
        binding = step.execution_binding
        if binding is None:
            return False
        check_args = specification.get("args") or {}
        binding_args = binding.args or {}
        component = str(binding_args.get("component") or "").strip().lower()
        declared_component = str(check_args.get("component") or "").strip().lower()
        if declared_component:
            return declared_component == component

        session = str(check_args.get("session") or "").strip()
        if session:
            return session == str(binding_args.get("screen_session") or "").strip()

        checker = str(specification.get("checker") or "").strip()
        pattern = str(check_args.get("pattern") or "").lower()
        if checker in {"process_running", "process_not_running"} and pattern:
            return component in pattern

        raw_port = check_args.get("port")
        if raw_port in (None, ""):
            match = re.search(
                r":([1-9]\d{0,4})(?:/|$)",
                str(check_args.get("url") or ""),
            )
            raw_port = match.group(1) if match else None
        try:
            check_port = int(raw_port)
        except (TypeError, ValueError):
            return False
        return component_port_arg(binding_args, component) == check_port

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
                    prefix = (
                        "已完成（不会重复执行）："
                        if step.status in {"completed", "skipped"}
                        else "执行方式："
                    )
                    lines.append(
                        "  %s%s"
                        % (prefix, _binding_summary(binding.action, binding.args))
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


def _paused_plan_reason(plan: ChangePlan) -> str:
    """Render the persisted failed boundary without changing plan state."""

    for change in plan.steps:
        for step in MutationWorkflow._execution_steps(change):
            if step.status in {"paused", "execution_unknown"}:
                return str(
                    step.observation
                    or change.observation
                    or "已审批计划在执行后验证阶段暂停。"
                )
        if change.status == "paused":
            return str(change.observation or "语义步骤的执行后验收未通过。")
    return "已审批计划的目标级执行后验收未通过。"


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


def _matching_recovery_option(
    text: str,
    options: list[RecoveryOption],
) -> RecoveryOption | None:
    """Resolve a natural reply from the persisted options shown to the user."""

    normalized = re.sub(r"[\s，,。.!！？?：:]", "", str(text or "")).lower()
    normalized = re.sub(r"^(?:我)?(?:选择|选|采用|按)", "", normalized)
    matches = [
        option
        for option in options
        if normalized
        == re.sub(r"[\s，,。.!！？?：:]", "", option.label).lower()
    ]
    return matches[0] if len(matches) == 1 else None


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


def _recovery_options(
    *, missing_decisions: list[str] | None = None,
) -> list[RecoveryOption]:
    """Expose user controls only; concrete recovery strategy belongs to Planner."""

    unresolved = [
        str(item).strip() for item in missing_decisions or []
        if str(item).strip()
    ]
    direction_description = (
        "需要你决定：%s。" % "；".join(unresolved[:3])
        if unresolved
        else (
            "当前失败记录没有显示必须补充的用户边界；仅当你希望改变"
            "既定范围或约束时选择此项。"
        )
    )
    return [
        RecoveryOption(
            option_id="continue_current_goal",
            label="继续处理",
            description=(
                "保留原目标和已有事实，补齐失败记录中的真实缺口后回到原工作流。"
            ),
            action="continue_current_goal",
            recommended=not bool(unresolved),
        ),
        RecoveryOption(
            option_id="provide_direction",
            label="调整目标或处理范围",
            description=direction_description,
            action="provide_direction",
            recommended=bool(unresolved),
        ),
        RecoveryOption(
            option_id="cancel",
            label="取消本次操作",
            description="保留失败记录，不执行剩余步骤。",
            action="cancel",
            requires_new_approval=False,
        ),
    ]


def _failure_explanation_fallback(failure: FailureRecord) -> str:
    return "%s 具体失败：%s" % (
        failure.summary.rstrip("。"),
        redact_sensitive_text(failure.technical_reason)[:800],
    )


def _failure_message(
    failure: FailureRecord,
    *,
    explanation: str = "",
) -> str:
    lines = [
        "本轮目标尚未完成，系统没有隐藏失败。",
        "失败阶段：%s" % failure.stage,
        "失败说明：%s" % (
            str(explanation or "").strip()
            or _failure_explanation_fallback(failure)
        ),
        "环境是否已改变：%s" % {
            "true": "是", "false": "否", "unknown": "无法确定",
        }[failure.environment_changed],
    ]
    if failure.failed_step:
        lines.append("失败步骤：%s" % failure.failed_step)
    if failure.failed_checks:
        lines.append("系统确认的缺口：%s" % "；".join(failure.failed_checks[:4]))
    if failure.missing_decisions:
        lines.append(
            "需要你决定：%s" % "；".join(failure.missing_decisions[:4])
        )
    elif failure.stage in {"planning", "binding"}:
        lines.append(
            "是否需要补充用户边界：否；当前记录显示的是系统规划或绑定缺口。"
        )
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
    choice_range = "/".join(
        str(index) for index in range(1, len(failure.options) + 1)
    )
    lines.extend([
        "回复“选择 %s”，或使用：choose-priv-option %s <option_id>"
        % (choice_range, failure.failure_id),
        "查看技术详情：show-priv-failure-details %s" % failure.failure_id,
    ])
    return "\n".join(lines)


def _selected_recovery_option(
    failure: FailureRecord | None,
) -> RecoveryOption | None:
    if failure is None or not str(failure.selected_option_id or ""):
        return None
    return next(
        (
            item for item in failure.options
            if item.option_id == failure.selected_option_id
        ),
        None,
    )


def _direction_question(failure: FailureRecord) -> str:
    """Ask for the unresolved boundary carried by this exact failure."""

    context = []
    if failure.failed_step:
        context.append("失败步骤：%s" % failure.failed_step)
    if failure.failed_checks:
        context.append("未满足的验收：%s" % "；".join(failure.failed_checks[:4]))
    if failure.evidence_requests:
        facts = [
            fact
            for request in failure.evidence_requests[:4]
            for fact in request.required_facts
        ]
        if facts:
            context.append("当前缺口：%s" % "、".join(facts[:6]))
    if failure.missing_decisions:
        context.append(
            "需要你决定：%s" % "；".join(failure.missing_decisions[:4])
        )
    prefix = "。".join(context)
    if prefix:
        prefix += "。"
    if not failure.missing_decisions:
        return (
            prefix
            + "当前失败记录没有显示必须由你补充的边界；失败源于系统规划或"
            "绑定没有形成完整实施方案。如果你仍希望改变原目标，请明确说明"
            "目标范围中要保留或排除的组件，以及要放宽或保持的资源约束；否则应选择"
            "“继续处理”，由系统保持原目标修复实施计划。你的下一条回复会"
            "直接用于修订原目标，不会重新进入普通意图分类。"
        )
    return (
        prefix
        + "请回答上面的未决事项，并说明要保留、排除或新增的目标范围。"
        "你的下一条回复会直接用于修订原目标，"
        "不会重新进入普通意图分类。"
    )


def _failure_detail_message(
    failure: FailureRecord,
    *,
    explanation: str = "",
) -> str:
    lines = [
        "失败记录 %s（技术详情，已脱敏）" % failure.failure_id,
        "目标：%s" % failure.goal,
        "阶段/类别：%s / %s" % (failure.stage, failure.category),
        "摘要：%s" % failure.summary,
        "技术原因：%s" % redact_sensitive_text(failure.technical_reason),
        "通俗说明：%s" % (
            str(explanation or "").strip()
            or _failure_explanation_fallback(failure)
        ),
        "环境变化：%s" % failure.environment_changed,
    ]
    if failure.completed_steps:
        lines.append("已完成步骤：%s" % "；".join(failure.completed_steps))
    if failure.failed_checks:
        lines.append("失败校验：%s" % "；".join(failure.failed_checks))
    if failure.missing_decisions:
        lines.append("需要用户决定：%s" % "；".join(failure.missing_decisions))
    elif failure.stage in {"planning", "binding"}:
        lines.append("需要用户补充边界：否；这是系统规划或绑定缺口。")
    if failure.attempted_recoveries:
        lines.append("恢复尝试：%s" % "；".join(failure.attempted_recoveries))
    return "\n".join(lines)


def _is_failure_detail_query(text: str) -> bool:
    """Recognize a failure-state meta question without leaving that state."""

    normalized = str(text or "").strip().rstrip("。？?")
    if not normalized:
        return False
    return bool(
        re.search(r"(?:为什么|为啥|怎么).*(?:失败|错误)", normalized)
        or re.search(
            r"(?:失败|错误).*(?:原因|详情|信息|怎么回事|为什么|为啥)",
            normalized,
        )
        or re.fullmatch(
            r"(?:查看|显示|告诉我)(?:一下)?(?:刚才|最近|上次|这次)?(?:的)?"
            r"(?:失败|错误)(?:的)?(?:详细|具体|技术)?(?:原因|详情|信息)?",
            normalized,
        )
    )


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
