"""Top-level routing for the staged Ops-Privilege workflow."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from pathlib import Path
import re
from typing import Any

from klonet_agent.ops.privileged.goal_guard import GoalSafetyGuard
from klonet_agent.ops.privileged.workflow.operational_context import (
    OperationalContextSnapshot,
)
from klonet_agent.ops.privileged.workflow.contracts import (
    ResolvedPlatformIdentity,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory


@dataclass
class WorkflowResult:
    handled: bool
    kind: str
    message: str = ""
    plan: Any | None = None
    evidence: Any | None = None
    verification: Any | None = None
    failure: Any | None = None


class PrivilegedOpsCoordinator:
    def __init__(
        self,
        *,
        classifier: Any,
        discovery: Any,
        synthesis: Any,
        response: Any,
        mutation_workflow: Any,
        verifier: Any,
        goal_guard: GoalSafetyGuard | None = None,
        context_store: Any | None = None,
    ) -> None:
        self.classifier = classifier
        self.discovery = discovery
        self.synthesis = synthesis
        self.response = response
        self.mutation_workflow = mutation_workflow
        self.verifier = verifier
        self.goal_guard = goal_guard or GoalSafetyGuard()
        self.context_store = context_store

    def handle(
        self,
        text: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> WorkflowResult:
        normalized = str(text or "").lstrip("\ufeff\u200b").strip()
        safety = self.goal_guard.check(normalized)
        if safety.denied:
            return WorkflowResult(
                True,
                "denied",
                "请求被安全策略拒绝；本轮没有执行任何操作。原因：%s"
                % str(safety.reason or "目标不在允许范围内"),
            )
        snapshot = self.context_store.load() if self.context_store is not None else None
        handle_control = getattr(self.mutation_workflow, "handle_control", None)
        if handle_control is not None:
            control = handle_control(normalized)
            if control is not None:
                if (
                    control.kind == "paused"
                    and control.plan is not None
                ):
                    return self._recover_paused_change(
                        control,
                        snapshot=snapshot,
                        conversation_context=conversation_context,
                    )
                if control.kind == "failure_option_selected":
                    return self._recover_failure_option(
                        control,
                        snapshot=snapshot,
                        conversation_context=conversation_context,
                    )
                return control
        if _is_plan_status_query(normalized):
            render_status = getattr(
                self.mutation_workflow, "render_latest_status", None,
            )
            if render_status is not None:
                return WorkflowResult(
                    True,
                    "plan_status",
                    str(render_status()),
                )
        classifier_context = conversation_context
        if snapshot is not None and snapshot.resolved_goal:
            classifier_context = (
                "%s\n\nPersisted operational goal:\n%s"
                % (conversation_context or "(none)", snapshot.resolved_goal)
            )
        decision = self.classifier.classify(
            normalized,
            conversation_context=classifier_context,
        )
        if decision.intent == "classifier_error":
            return WorkflowResult(
                True,
                "blocked",
                "意图识别失败，已安全停止；本轮没有执行任何操作。",
            )
        if bool(getattr(decision, "should_clarify", False)):
            return WorkflowResult(
                True,
                "clarification",
                str(getattr(decision, "clarification_question", "") or "请补充说明目标实例和操作。"),
            )
        continuation = _continuation_kind(normalized)
        goal_kind = str(getattr(decision, "goal_kind", "") or "")
        if not goal_kind:
            goal_kind = (
                "causal_diagnosis" if continuation == "diagnose"
                else "execution" if decision.intent == "mutating_action"
                else "health_check" if decision.intent == "readonly_action"
                else "conversation"
            )
        goal_relation = str(getattr(decision, "goal_relation", "new") or "new")
        if continuation == "diagnose" and goal_relation == "new":
            goal_relation = "continue_previous"
        if decision.intent == "conversation" and not (
            snapshot is not None
            and snapshot.resolved_goal
            and (continuation or goal_relation != "new")
        ):
            return WorkflowResult(False, "conversation")
        resolved_goal = normalized
        effective_intent = decision.intent
        if snapshot is not None and snapshot.resolved_goal:
            if continuation == "submit_plan":
                resolved_goal = snapshot.resolved_goal
                effective_intent = "mutating_action"
            elif goal_relation == "refine_previous":
                resolved_goal = _refined_goal(snapshot.resolved_goal, normalized)
                effective_intent = "readonly_action"
            elif goal_relation == "continue_previous":
                resolved_goal = snapshot.resolved_goal
                effective_intent = (
                    "readonly_action"
                    if decision.intent == "conversation"
                    else decision.intent
                )
            elif continuation == "diagnose":
                resolved_goal = snapshot.resolved_goal
                effective_intent = "readonly_action"
        elif decision.intent == "resume_plan":
            return WorkflowResult(
                True,
                "clarification",
                "当前会话没有可继续的运维目标或计划，请重新说明目标实例和操作。",
            )
        seed_bundle = (
            snapshot.reusable_evidence(resolved_goal)
            if snapshot is not None and (
                resolved_goal == snapshot.resolved_goal
                or goal_relation in {"continue_previous", "refine_previous"}
            )
            else None
        )
        collection_goal = (
            "只读诊断并补齐以下运维目标所需证据：%s" % resolved_goal
            if continuation == "diagnose" or goal_relation in {
                "continue_previous", "refine_previous",
            }
            else resolved_goal
        )
        begin_probe_session = getattr(self.discovery, "begin_probe_session", None)
        end_probe_session = getattr(self.discovery, "end_probe_session", None)
        if begin_probe_session is not None:
            begin_probe_session()
        try:
            collect_kwargs = {
                "command": str(getattr(decision, "command", "") or ""),
                "conversation_context": conversation_context,
            }
            collect_parameters = inspect.signature(self.discovery.collect).parameters
            if "seed_bundle" in collect_parameters:
                collect_kwargs["seed_bundle"] = seed_bundle
            if "preload_capabilities" in collect_parameters:
                collect_kwargs["preload_capabilities"] = True
            bundle = self.discovery.collect(collection_goal, **collect_kwargs)
            _collect_traceback_source_evidence(resolved_goal, bundle, self.discovery)
            conclusion = self.synthesis.synthesize(collection_goal, bundle)
            if effective_intent == "readonly_action":
                return self._run_readonly_goal_loop(
                    snapshot,
                    resolved_goal=(
                        snapshot.resolved_goal
                        if snapshot is not None and continuation == "diagnose"
                        else resolved_goal
                    ),
                    collection_goal=collection_goal,
                    bundle=bundle,
                    conclusion=conclusion,
                    goal_kind=goal_kind,
                )
            target_question = _ambiguous_abnormal_target_question(resolved_goal, bundle)
            if target_question:
                return WorkflowResult(
                    True,
                    "clarification",
                    target_question,
                    evidence=bundle,
                )
            already_satisfied = _explicit_runtime_targets_already_healthy(
                resolved_goal,
                bundle,
            )
            if already_satisfied:
                return WorkflowResult(
                    True,
                    "completed",
                    already_satisfied,
                    evidence=bundle,
                )
            resolved_identity = _resolve_platform_identity(resolved_goal, bundle)
            intent_context = {
                "operation": str(getattr(decision, "operation", "") or ""),
                "scope": str(getattr(decision, "scope", "") or ""),
                "components": list(getattr(decision, "components", ()) or ()),
            }
            if resolved_identity is not None:
                intent_context.update(resolved_identity.to_dict())
                intent_context["resolved_project_root"] = (
                    resolved_identity.project_root
                )
            result = self._submit_mutation(
                resolved_goal,
                evidence_bundle=bundle,
                evidence_conclusion=conclusion,
                intent_context=intent_context,
            )
            self._save_context(
                snapshot,
                resolved_goal=resolved_goal,
                bundle=bundle,
                phase={
                    "awaiting_confirmation": "awaiting_confirmation",
                    "awaiting_user_decision": "awaiting_user_decision",
                    "completed": "completed",
                    "blocked": "blocked",
                }.get(result.kind, "draft_ready"),
            )
            return _localized_result(result)
        finally:
            if end_probe_session is not None:
                end_probe_session()

    def _submit_mutation(
        self,
        goal: str,
        *,
        evidence_bundle: Any,
        evidence_conclusion: Any,
        intent_context: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Own the single bounded Plan/Replan transition loop."""

        workflow = self.mutation_workflow

        def plan(*, feedback: str = "") -> Any:
            return workflow.plan_once(
                goal,
                evidence_bundle=evidence_bundle,
                evidence_conclusion=evidence_conclusion,
                intent_context=intent_context,
                binding_feedback=feedback,
            )

        outcome = plan()
        replanning_rounds = 0
        candidate_replans = 0
        while True:
            if outcome.status == "need_evidence":
                if replanning_rounds >= workflow.max_replanning_rounds:
                    return WorkflowResult(
                        True, "blocked",
                        "Planner-to-Discovery evidence budget exhausted.",
                        evidence=evidence_bundle,
                    )
                if self.discovery is None or self.synthesis is None:
                    return WorkflowResult(
                        True, "blocked",
                        "Planner requested evidence but Discovery is unavailable.",
                        evidence=evidence_bundle,
                    )
                evidence_bundle = self.discovery.collect_requests(
                    outcome.evidence_requests,
                    evidence_bundle,
                )
                evidence_conclusion = self.synthesis.synthesize(
                    goal, evidence_bundle,
                )
                replanning_rounds += 1
                finalize_candidate = getattr(
                    workflow.planner, "finalize_candidate", None,
                )
                if outcome.candidate_plan is not None and finalize_candidate is not None:
                    outcome = finalize_candidate(
                        outcome.candidate_plan, evidence_bundle,
                    )
                else:
                    outcome = plan()
                continue
            occupied_candidate = (
                outcome.status == "blocked"
                and str(outcome.reason or "").startswith(
                    "candidate ports became occupied:"
                )
            )
            if (
                occupied_candidate
                and candidate_replans < workflow.max_candidate_replans
            ):
                candidate_replans += 1
                outcome = plan(feedback=(
                    "%s. Select different ports that are not reported as occupied, "
                    "freeze them, and preserve every other grounded decision."
                    % outcome.reason
                ))
                continue
            break
        if outcome.status != "need_execution" or outcome.plan is None:
            return workflow._failure_result(
                stage="planning",
                category="planning_contract_unresolved",
                summary="变更规划在有限次数校正后仍未形成安全、可审批的计划。",
                technical_reason=(
                    outcome.reason
                    or "change planning did not produce an executable plan"
                ),
                goal=goal,
                attempted_recoveries=["补充计划证据", "有限次数重新规划"],
                evidence=evidence_bundle,
            )

        first_binding_error = ""
        for binding_attempt in range(2):
            binding = workflow.bind_once(
                outcome.plan,
                evidence_bundle=evidence_bundle,
            )
            if binding.status == "needs_user_decision" and binding.plan is not None:
                return WorkflowResult(
                    True,
                    "awaiting_confirmation",
                    binding.user_question,
                    plan=binding.plan,
                    evidence=evidence_bundle,
                )
            if binding.status != "need_replan":
                raise ValueError("invalid binding transition")
            if binding_attempt == 0:
                first_binding_error = binding.reason
                outcome = plan(feedback=binding.reason)
                if outcome.status == "need_execution" and outcome.plan is not None:
                    continue
                return workflow._failure_result(
                    stage="binding",
                    category="binding_replan_unresolved",
                    summary="实施绑定发现计划缺少安全落地所需的实例或动作约束。",
                    technical_reason="%s; replan=%s" % (
                        binding.reason,
                        outcome.reason
                        or "binding replan did not produce a ready plan",
                    ),
                    goal=goal,
                    attempted_recoveries=["首次实施绑定", "保持目标不变局部重建"],
                    evidence=evidence_bundle,
                )
            failed_plan = binding.candidate_plan or outcome.plan
            failed_plan.status = "blocked"
            workflow.store.save(failed_plan)
            return workflow._failure_result(
                stage="binding",
                category="binding_contract_invalid",
                summary="实施绑定无法证明动作只作用于目标实例，已安全拒绝。",
                technical_reason="first_binding=%s; final_binding=%s" % (
                    first_binding_error, binding.reason,
                ),
                goal=goal,
                plan=failed_plan,
                attempted_recoveries=["首次实施绑定", "局部重新规划", "第二次实施绑定"],
                evidence=evidence_bundle,
            )
        raise AssertionError("unreachable binding loop")

    def _run_readonly_goal_loop(
        self,
        previous: OperationalContextSnapshot | None,
        *,
        resolved_goal: str,
        collection_goal: str,
        bundle: Any,
        conclusion: Any,
        goal_kind: str = "health_check",
    ) -> WorkflowResult:
        """Continue safe evidence collection until the requested result exists."""

        decision, bundle, conclusion = self._advance_diagnostic_evidence(
            collection_goal, bundle, conclusion, goal_kind=goal_kind,
        )
        status = str(getattr(decision, "status", "") or "")
        phase = {
            "achieved": "completed",
            "needs_user_decision": "awaiting_user_decision",
        }.get(status, "blocked")
        self._save_context(
            previous,
            resolved_goal=resolved_goal,
            bundle=bundle,
            phase=phase,
        )
        if status == "achieved":
            response_kwargs: dict[str, Any] = {}
            if "evidence_bundle" in inspect.signature(
                self.response.render_readonly
            ).parameters:
                response_kwargs["evidence_bundle"] = bundle
            return WorkflowResult(
                True,
                "completed",
                self.response.render_readonly(
                    collection_goal, conclusion, **response_kwargs,
                ),
                evidence=bundle,
            )
        if status == "needs_user_decision":
            return WorkflowResult(
                True,
                "clarification",
                str(getattr(decision, "user_question", "") or (
                    "需要你明确目标实例或检查范围。"
                )),
                evidence=bundle,
            )
        return WorkflowResult(
            True,
            "blocked",
            "已穷尽当前安全的只读诊断路径，仍无法取得完成目标所需的关键证据。"
            + (
                " 原因：%s" % str(getattr(decision, "reason", ""))
                if str(getattr(decision, "reason", "") or "").strip()
                else ""
            ),
            evidence=bundle,
        )

    def _advance_diagnostic_evidence(
        self,
        goal: str,
        bundle: Any,
        conclusion: Any,
        *,
        phase: str = "readonly",
        goal_kind: str = "health_check",
    ) -> tuple[Any, Any, Any]:
        attempted_keys = {
            item.request.cache_key for item in getattr(bundle, "records", [])
        }
        max_cycles = 8
        for cycle in range(1, max_cycles + 1):
            assess_kwargs: dict[str, Any] = {}
            parameters = inspect.signature(self.verifier.verify_goal).parameters
            if "attempted_keys" in parameters:
                assess_kwargs["attempted_keys"] = set(attempted_keys)
            if "phase" in parameters:
                assess_kwargs["phase"] = phase
            if "goal_kind" in parameters:
                assess_kwargs["goal_kind"] = goal_kind
            decision = self.verifier.verify_goal(
                goal, bundle, conclusion, **assess_kwargs,
            )
            status = str(getattr(decision, "status", "") or "")
            if status in {
                "achieved", "need_replan", "needs_user_decision", "blocked",
            }:
                return decision, bundle, conclusion
            if status != "need_evidence":
                raise ValueError("invalid diagnostic loop decision")
            requests = list(getattr(decision, "evidence_requests", []) or [])
            before = len(getattr(bundle, "records", []))
            progress = getattr(self.discovery, "on_progress", None)
            if progress is not None:
                progress(
                    "诊断补证循环 %s/%s：继续获取形成最终结论所需的证据。"
                    % (cycle, max_cycles)
                )
            self.discovery.collect_requests(requests, bundle)
            attempted_keys.update(request.cache_key for request in requests)
            attempted_keys.update(
                item.request.cache_key
                for item in getattr(bundle, "records", [])
            )
            _collect_traceback_source_evidence(goal, bundle, self.discovery)
            if len(getattr(bundle, "records", [])) <= before:
                continue
            conclusion = self.synthesis.synthesize(goal, bundle)
        from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

        return GoalOutcome(
            status="blocked",
            reason="目标验证补证循环达到安全上限。",
        ), bundle, conclusion

    def _recover_paused_change(
        self,
        control: WorkflowResult,
        *,
        snapshot: OperationalContextSnapshot | None,
        conversation_context: str,
    ) -> WorkflowResult:
        """Diagnose a failed approved plan and replan without another nudge."""

        from klonet_agent.ops.privileged.workflow.contracts import (
            EvidenceBundle, EvidenceRecord, ProbeRequest,
        )

        plan = control.plan
        goal = str(getattr(plan, "goal", "") or "").strip()
        if not goal:
            return control
        bundle = EvidenceBundle(goal=goal)
        execution_request = ProbeRequest(
            "plan_execution",
            {
                "plan_id": str(getattr(plan, "plan_id", "") or "unknown"),
                "status": str(getattr(plan, "status", "") or "paused"),
            },
            "已审批计划的执行与验证结果",
        )
        bundle.add(EvidenceRecord.from_probe(
            execution_request,
            _paused_plan_evidence(control),
        ))
        recovery_goal = "自动诊断已审批任务未达到目标的原因，并补齐恢复规划证据：%s" % goal
        begin = getattr(self.discovery, "begin_probe_session", None)
        end = getattr(self.discovery, "end_probe_session", None)
        if begin is not None:
            begin()
        try:
            kwargs = {
                "command": "",
                "conversation_context": conversation_context,
            }
            parameters = inspect.signature(self.discovery.collect).parameters
            if "seed_bundle" in parameters:
                kwargs["seed_bundle"] = bundle
            if "preload_capabilities" in parameters:
                kwargs["preload_capabilities"] = True
            bundle = self.discovery.collect(recovery_goal, **kwargs)
            conclusion = self.synthesis.synthesize(recovery_goal, bundle)
            decision, bundle, conclusion = self._advance_diagnostic_evidence(
                recovery_goal, bundle, conclusion, phase="post_execution",
            )
        finally:
            if end is not None:
                end()
        status = str(getattr(decision, "status", "") or "")
        if status == "needs_user_decision":
            return WorkflowResult(
                True,
                "clarification",
                str(getattr(decision, "user_question", "") or (
                    "恢复任务需要你明确目标边界。"
                )),
                plan=plan,
                evidence=bundle,
            )
        if status == "achieved":
            return WorkflowResult(
                True,
                "completed",
                "重新验证后，原目标当前已经满足，无需生成恢复计划。",
                plan=plan,
                evidence=bundle,
            )
        if status != "need_replan":
            return WorkflowResult(
                True,
                "blocked",
                "已自动诊断执行失败，但尚未取得足以安全重规划的根因证据。",
                plan=plan,
                evidence=bundle,
            )
        result = self._submit_mutation(
            goal,
            evidence_bundle=bundle,
            evidence_conclusion=conclusion,
        )
        self._save_context(
            snapshot,
            resolved_goal=goal,
            bundle=bundle,
            phase={
                "awaiting_confirmation": "awaiting_confirmation",
                "completed": "completed",
            }.get(result.kind, "blocked"),
        )
        return _localized_result(result)

    def _recover_failure_option(
        self,
        control: WorkflowResult,
        *,
        snapshot: OperationalContextSnapshot | None,
        conversation_context: str,
    ) -> WorkflowResult:
        """Resume the existing loop from a persisted user-selected recovery."""

        failure = control.failure
        if failure is None:
            return WorkflowResult(
                True, "blocked", "失败恢复记录缺失，无法安全继续。",
            )
        option = next(
            (
                item for item in failure.options
                if item.option_id == failure.selected_option_id
            ),
            None,
        )
        if option is None:
            return WorkflowResult(
                True, "blocked", "所选恢复方案不存在，无法安全继续。",
                failure=failure,
            )
        goal = str(failure.goal or "").strip()
        seed = snapshot.reusable_evidence(goal) if snapshot is not None else None
        begin = getattr(self.discovery, "begin_probe_session", None)
        end = getattr(self.discovery, "end_probe_session", None)
        if begin is not None:
            begin()
        try:
            collect_kwargs: dict[str, Any] = {
                "command": "",
                "conversation_context": conversation_context,
            }
            if "seed_bundle" in inspect.signature(self.discovery.collect).parameters:
                collect_kwargs["seed_bundle"] = seed
            if "preload_capabilities" in inspect.signature(
                self.discovery.collect
            ).parameters:
                collect_kwargs["preload_capabilities"] = True
            bundle = self.discovery.collect(goal, **collect_kwargs)
            conclusion = self.synthesis.synthesize(goal, bundle)
            identity = _resolve_platform_identity(goal, bundle)
            intent_context: dict[str, Any] = {}
            if option.action == "component_restart":
                intent_context.update({"operation": "restart", "scope": "platform"})
            if identity is not None:
                intent_context.update(identity.to_dict())
                intent_context["resolved_project_root"] = identity.project_root
            result = self._submit_mutation(
                goal,
                evidence_bundle=bundle,
                evidence_conclusion=conclusion,
                intent_context=intent_context,
            )
            self._save_context(
                snapshot,
                resolved_goal=goal,
                bundle=bundle,
                phase={
                    "awaiting_confirmation": "awaiting_confirmation",
                    "awaiting_user_decision": "awaiting_user_decision",
                }.get(result.kind, "blocked"),
            )
            return result
        finally:
            if end is not None:
                end()

    def _save_context(
        self,
        previous: OperationalContextSnapshot | None,
        *,
        resolved_goal: str,
        bundle: Any,
        phase: str,
    ) -> None:
        if self.context_store is None:
            return
        roots = sorted(set(re.findall(r"/[A-Za-z0-9._/-]+", resolved_goal)))
        for instance in RuntimeInventory.from_bundle(bundle).matching(resolved_goal):
            if instance.project_root not in roots:
                roots.append(instance.project_root)
        self.context_store.save(OperationalContextSnapshot(
            resolved_goal=resolved_goal,
            output_locale=(previous.output_locale if previous is not None else "zh-CN"),
            target_roots=roots,
            phase=phase,
            evidence=bundle,
        ))

    def handle_with_context(
        self,
        text: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> WorkflowResult:
        return self.handle(
            text,
            environment_context=environment_context,
            conversation_context=conversation_context,
        )


def _ambiguous_abnormal_target_question(text: str, bundle: Any) -> str:
    normalized = str(text or "")
    lowered = normalized.lower()
    repair_requested = any(
        marker in lowered for marker in ("修复", "恢复", "排查", "fix", "repair", "recover")
    )
    if not repair_requested:
        return ""
    inventory = RuntimeInventory.from_bundle(bundle)
    roots = [item.project_root for item in inventory.abnormal]
    if len(roots) < 2:
        return ""
    if inventory.matching(normalized):
        return ""
    if any(marker in lowered for marker in ("全部", "所有", "all of", "all abnormal")):
        return ""
    return (
        "检测到多个后端异常运行候选，必须先由你确定修复边界；"
        "同名项目不会自动合并。请明确给出要修复的项目根目录：\n\n- "
        + "\n- ".join(roots)
    )

def _resolve_platform_identity(
    text: str, bundle: Any,
) -> ResolvedPlatformIdentity | None:
    """Resolve a user alias to exactly one canonical runtime root."""

    matches = RuntimeInventory.from_bundle(bundle).matching(text)
    if len(matches) != 1:
        return None
    instance = matches[0]
    aliases = tuple(sorted(instance.aliases))
    primary = next(
        (item for item in aliases if not item.startswith("klonet_")),
        aliases[0],
    )
    return ResolvedPlatformIdentity(
        project_root=instance.project_root,
        primary_alias=primary,
        aliases=aliases,
        evidence_refs=(instance.evidence_id,),
    )


def _continuation_kind(text: str) -> str:
    value = str(text or "").strip().lower()
    if any(marker in value for marker in (
        "只读诊断", "先诊断", "继续诊断", "补齐信息", "补齐证据",
        "你自己定位", "自己定位", "继续查清楚", "接着排查",
        "自己查", "继续查", "查清楚", "继续定位",
        "read-only diagnosis", "diagnose first",
    )):
        return "diagnose"
    if any(marker in value for marker in (
        "提交这个", "提交刚才", "提交重启计划", "走审批", "重新开始审批",
        "根据刚才的信息", "根据你确认的信息", "帮我重启这个平台",
        "submit this plan", "submit the plan", "start approval",
    )):
        return "submit_plan"
    return ""


def _refined_goal(previous_goal: str, current_request: str) -> str:
    """Preserve the resolved target while adding a semantic refinement."""

    previous = str(previous_goal or "").strip()
    current = str(current_request or "").strip()
    if not previous:
        return current
    if not current or current in previous:
        return previous
    return "%s；进一步要求：%s" % (previous, current)


def _is_plan_status_query(text: str) -> bool:
    """Recognize execution receipt questions before operational routing."""

    value = str(text or "").strip()
    return bool(re.search(
        r"(?:执行|操作|计划|重启|刚才|之前).{0,12}"
        r"(?:完(?:成|了吗?)|成功(?:了吗?)?|结果|状态|做了什么)|"
        r"(?:完(?:成|了吗?)|成功(?:了吗?)?|做了什么).{0,12}"
        r"(?:执行|操作|计划|重启)|"
        r"(?:did (?:it|that) (?:finish|succeed)|execution status|plan status)",
        value,
        re.I,
    ))


def _localized_result(result: WorkflowResult) -> WorkflowResult:
    """Keep internal English diagnostics out of the user-facing channel."""

    if result.kind != "blocked":
        return result
    message = str(result.message or "")
    mappings = (
        (
            "blocked cannot offload discoverable implementation details",
            "计划器仍有可由系统自动查明的实现信息未解析；本轮没有执行任何变更。",
        ),
        (
            "Change Planner output invalid after bounded repairs",
            "变更计划经过有限次数校正后仍未满足安全合同；本轮没有执行任何变更。",
        ),
        (
            "binding replan did not produce a ready plan",
            "执行绑定校正后仍未形成可审批计划；本轮没有执行任何变更。",
        ),
        (
            "binding replan budget exhausted",
            "实现绑定经过有限次数校正后仍未满足安全合同；本轮没有执行任何变更。",
        ),
        (
            "invalid_run_as_uid",
            "目标组件的运行用户尚未被可靠绑定。",
        ),
        (
            "Planner-to-Discovery evidence budget exhausted",
            "计划补证达到本轮上限；本轮没有执行任何变更。",
        ),
    )
    translated = [replacement for marker, replacement in mappings if marker in message]
    if not translated:
        translated = ["内部安全校验未通过；本轮没有执行任何变更。"]
    return WorkflowResult(
        result.handled,
        result.kind,
        " ".join(dict.fromkeys(translated)),
        plan=result.plan,
        evidence=result.evidence,
        verification=result.verification,
    )


def _paused_plan_evidence(result: WorkflowResult) -> str:
    """Render persisted execution state as bounded replanning evidence."""

    plan = result.plan
    lines = [
        "plan_execution",
        "plan_id=%s status=%s" % (
            str(getattr(plan, "plan_id", "") or "unknown"),
            str(getattr(plan, "status", "") or "paused"),
        ),
        "workflow_observation=%s" % " ".join(
            str(result.message or "").split()
        )[:1000],
    ]
    for change in list(getattr(plan, "steps", []) or [])[:20]:
        lines.append(
            "change=%s status=%s observation=%s"
            % (
                str(getattr(change, "step_id", "") or "unknown"),
                str(getattr(change, "status", "") or "unknown"),
                " ".join(str(getattr(change, "observation", "") or "").split())[:1000],
            )
        )
        implementation = getattr(change, "implementation_plan", None)
        steps = (
            list(getattr(implementation, "steps", []) or [])
            if implementation is not None
            else [change]
        )
        for step in steps[:20]:
            evidence = getattr(step, "evidence", None)
            lines.append(
                "step=%s status=%s attempts=%s return_code=%s timed_out=%s "
                "environment_changed=%s observation=%s"
                % (
                    str(getattr(step, "step_id", "") or "unknown"),
                    str(getattr(step, "status", "") or "unknown"),
                    int(getattr(step, "execution_attempts", 0) or 0),
                    getattr(evidence, "return_code", None),
                    getattr(evidence, "timed_out", None),
                    getattr(evidence, "environment_changed", None),
                    " ".join(
                        str(getattr(step, "observation", "") or "").split()
                    )[:1000],
                )
            )
            for check in list(getattr(step, "checks", []) or [])[:20]:
                lines.append(
                    "check=%s status=%s observed=%s"
                    % (
                        str(getattr(check, "checker", "") or "unknown"),
                        str(getattr(check, "status", "") or "unknown"),
                        " ".join(
                            str(getattr(check, "observed", "") or "").split()
                        )[:500],
                    )
                )
    return "\n".join(lines)[:20000]


def _explicit_runtime_targets_already_healthy(text: str, bundle: Any) -> str:
    """Treat an already-achieved root-bound repair goal as idempotent success.

    A mutating verb must not force a restart when the user names exact project
    roots and the deterministic runtime inventory already proves every named
    target healthy.  Unnamed abnormal instances are deliberately excluded: the
    explicit roots define the requested mutation boundary.
    """

    lowered = str(text or "").lower()
    if not any(
        marker in lowered
        for marker in ("修复", "恢复", "启动", "fix", "repair", "recover", "start")
    ):
        return ""
    inventory = RuntimeInventory.from_bundle(bundle)
    targets = [
        item for item in inventory.instances
        if item.project_root in str(text or "")
    ]
    if not targets:
        return ""
    if any(item.backend_status != "healthy" for item in targets):
        return ""

    lines = [
        "目标实例已经分别满足后端健康标准，无需变更或重启："
    ]
    for item in targets:
        lines.append(
            "- project_root=%s；platform=%s；backend_status=healthy；"
            "master_port=%s，master_endpoint=%s；worker_port=%s，worker_endpoint=%s"
            % (
                item.project_root,
                item.platform,
                item.configured_ports.get("master_port", "unknown"),
                item.endpoints.get("master", "unknown"),
                item.configured_ports.get("worker_port", "unknown"),
                item.endpoints.get("worker", "unknown"),
            )
        )
    return "\n".join(lines)


def _collect_traceback_source_evidence(text: str, bundle: Any, discovery: Any) -> None:
    """Read target-owned traceback sources before mutation planning.

    Log evidence can prove the exception and exact source path while still
    leaving the planner unable to choose a bounded edit.  Source inspection is
    read-only and discoverable, so collect it deterministically instead of
    allowing the planner to offload that implementation detail to the user.
    """

    deterministic = getattr(discovery, "collect_traceback_source_evidence", None)
    if deterministic is not None:
        deterministic(bundle)
        return
    collect_requests = getattr(discovery, "collect_requests", None)
    if collect_requests is None:
        return
    roots = []
    for raw in re.findall(r"/[A-Za-z0-9._/-]+", str(text or "")):
        try:
            root = Path(raw.rstrip("/"))
        except (OSError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    inventory = RuntimeInventory.from_bundle(bundle)
    for item in inventory.matching(text):
        root = Path(item.project_root)
        if root not in roots:
            roots.append(root)
    if not roots and len(inventory.instances) == 1:
        roots.append(Path(inventory.instances[0].project_root))
    if not roots:
        return
    candidates = []
    for record in getattr(bundle, "records", []):
        request = getattr(record, "request", None)
        if getattr(request, "probe", "") != "logs":
            continue
        for raw in re.findall(r'File "(/[^"]+\.py)", line \d+', str(getattr(record, "output", "") or "")):
            candidate = Path(raw)
            if not any(candidate == root or root in candidate.parents for root in roots):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    if not candidates:
        return
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest

    collect_requests(
        [
            ProbeRequest(
                "ops_file",
                {"path": str(path), "view": "head", "max_chars": 20000},
                "inspect the target-owned Python source named by the startup traceback",
            )
            for path in candidates[:2]
        ],
        bundle,
    )
