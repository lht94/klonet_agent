"""Top-level routing for the staged Ops-Privilege workflow."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import re
from typing import Any, Callable

from klonet_agent.ops.privileged.goal_guard import GoalSafetyGuard
from klonet_agent.ops.privileged.workflow.contracts import (
    EvidenceBundle,
    GoalOutcome,
    extract_labeled_deployment_paths,
)
from klonet_agent.ops.privileged.workflow.operational_context import (
    OperationalContextSnapshot,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import (
    RuntimeInventory,
    requests_new_platform_deployment,
)
from klonet_agent.tools.environment import redact_sensitive_text


@dataclass
class WorkflowResult:
    handled: bool
    kind: str
    message: str = ""
    plan: Any | None = None
    evidence: Any | None = None
    verification: Any | None = None
    failure: Any | None = None
    outcome: Any | None = None


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
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.classifier = classifier
        self.discovery = discovery
        self.synthesis = synthesis
        self.response = response
        self.mutation_workflow = mutation_workflow
        self.verifier = verifier
        self.goal_guard = goal_guard or GoalSafetyGuard()
        self.context_store = context_store
        self.on_progress = on_progress

    def handle(
        self,
        text: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> WorkflowResult:
        """Run one turn and retire its active goal after verified success."""

        result = self._handle(
            text,
            environment_context=environment_context,
            conversation_context=conversation_context,
        )
        if result.kind == "completed":
            current = (
                self.context_store.load()
                if self.context_store is not None else None
            )
            self._clear_context(current)
        return result

    def _handle(
        self,
        text: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> WorkflowResult:
        normalized = str(text or "").lstrip("\ufeff\u200b").strip()
        snapshot = self.context_store.load() if self.context_store is not None else None
        evidence_detail = re.fullmatch(
            r"show-priv-evidence\s+(ev-[A-Za-z0-9_-]{4,64})",
            normalized,
        )
        if evidence_detail is not None:
            evidence_id = evidence_detail.group(1)
            records = list(
                getattr(getattr(snapshot, "evidence", None), "records", []) or []
            )
            record = next(
                (item for item in records if item.evidence_id == evidence_id),
                None,
            )
            if record is None:
                return WorkflowResult(
                    True,
                    "clarification",
                    "未找到证据记录 %s。" % evidence_id,
                )
            return WorkflowResult(
                True,
                "technical_details",
                "证据 %s（probe=%s，status=%s，已脱敏）：\n%s"
                % (
                    evidence_id,
                    record.request.probe,
                    record.status,
                    redact_sensitive_text(str(record.output or "")),
                ),
                evidence=snapshot.evidence,
            )
        if snapshot is not None and snapshot.pending_goal_revision:
            if normalized in {"确认", "确认覆盖目标", "确认切换目标"}:
                replacement = snapshot.pending_goal_revision
                relation = snapshot.pending_goal_relation or "revise"
                snapshot.base_goal = replacement
                snapshot.resolved_goal = replacement
                snapshot.decision_history = []
                snapshot.pending_goal_revision = ""
                snapshot.pending_goal_relation = ""
                snapshot.active_plan_id = ""
                if self.context_store is not None:
                    self.context_store.save(snapshot)
                normalized = replacement
            elif normalized in {"取消", "取消覆盖", "保留原目标"}:
                snapshot.pending_goal_revision = ""
                snapshot.pending_goal_relation = ""
                if self.context_store is not None:
                    self.context_store.save(snapshot)
                return WorkflowResult(
                    True,
                    "clarification",
                    "已保留当前完整目标；本轮没有修改目标或执行环境。",
                )
            else:
                return WorkflowResult(
                    True,
                    "clarification",
                    "当前 Planner 发现这条回复会覆盖完整目标。回复“确认覆盖目标”采用候选目标，或回复“保留原目标”。",
                )
        if (
            normalized in {"确认", "同意"}
            and snapshot is not None
            and snapshot.active_plan_id
        ):
            return WorkflowResult(
                True,
                "clarification",
                "裸文本“%s”不会修改或批准变更计划。若要修订，请明确说明要纳入或排除的组件；若要批准，请使用计划展示的 confirm-priv-plan <plan_id> <hash>。"
                % normalized,
            )
        handle_control = getattr(self.mutation_workflow, "handle_control", None)
        if handle_control is not None:
            control = handle_control(normalized)
            if control is not None:
                if (
                    control.kind in {"paused", "goal_verification"}
                    and control.plan is not None
                ):
                    return self._continue_post_execution(
                        control,
                        snapshot=snapshot,
                        conversation_context=conversation_context,
                    )
                if control.kind in {
                    "failure_option_selected", "failure_direction_provided",
                }:
                    return self._recover_failure_option(
                        control,
                        snapshot=snapshot,
                        conversation_context=conversation_context,
                        user_direction=(
                            str(
                                getattr(control.failure, "user_direction", "")
                                or control.message
                            )
                            if control.kind == "failure_direction_provided"
                            else ""
                        ),
                    )
                return control
        if (
            normalized in {"继续", "继续处理", "重试", "重新试试"}
            and snapshot is not None
            and snapshot.active_plan_id
        ):
            resume_paused = getattr(
                self.mutation_workflow, "resume_paused_plan", None,
            )
            control = (
                resume_paused(snapshot.active_plan_id)
                if callable(resume_paused) else None
            )
            if control is not None and control.kind == "paused":
                self._emit_progress("正在恢复已暂停计划并继续执行后诊断/Replan")
                return self._continue_post_execution(
                    control,
                    snapshot=snapshot,
                    conversation_context=conversation_context,
                )
        classifier_context = conversation_context
        if snapshot is not None and snapshot.resolved_goal:
            classifier_context = (
                "%s\n\nHistorical operational context (do not inherit unless "
                "the current request explicitly refers to it):\n"
                "previous_goal=%s\nactive_plan_id=%s\n"
                "previous_workflow_intent=%s\nprevious_goal_kind=%s\n"
                "previous_operation=%s\nprevious_scope=%s\n"
                "previous_components=%s"
                % (
                    conversation_context or "(none)",
                    snapshot.resolved_goal,
                    snapshot.active_plan_id or "none",
                    snapshot.workflow_intent or "none",
                    snapshot.goal_kind or "none",
                    snapshot.operation or "none",
                    snapshot.scope or "none",
                    ",".join(snapshot.components) or "none",
                )
                )
        plan_context = getattr(self.mutation_workflow, "plan_context", None)
        if plan_context is not None:
            persisted_context = str(plan_context(
                active_plan_id=(snapshot.active_plan_id if snapshot is not None else ""),
                goal=(snapshot.resolved_goal if snapshot is not None else ""),
            ) or "").strip()
            if persisted_context:
                classifier_context = (
                    "%s\n\nPersisted plan facts (authoritative):\n%s"
                    % (classifier_context or "(none)", persisted_context)
                )
        decision = self.classifier.classify(
            normalized,
            conversation_context=classifier_context,
        )
        if decision.intent == "classifier_error":
            return WorkflowResult(
                True,
                "clarification",
                "意图识别失败，本轮没有执行任何操作。请重新说明目标实例和期望操作。",
            )
        if decision.intent == "mutating_action":
            safety = self.goal_guard.check(normalized)
            if safety.denied:
                return WorkflowResult(
                    True,
                    "denied",
                    "请求被安全策略拒绝；本轮没有执行任何操作。原因：%s"
                    % str(safety.reason or "目标不在允许范围内"),
                )
        goal_relation = str(getattr(decision, "goal_relation", "new") or "new")
        if bool(getattr(decision, "should_clarify", False)):
            return WorkflowResult(
                True,
                "clarification",
                str(getattr(decision, "clarification_question", "") or "请补充说明目标实例和操作。"),
            )
        if (
            decision.intent == "mutating_action"
            and goal_relation == "new"
            and requests_new_platform_deployment(normalized)
        ):
            missing_boundaries = _deployment_boundary_gaps(normalized)
            if missing_boundaries:
                self._save_context(
                    snapshot,
                    resolved_goal=normalized,
                    bundle=EvidenceBundle(goal=normalized),
                    workflow_intent="mutating_action",
                    goal_kind="execution",
                    operation=str(getattr(decision, "operation", "") or "none"),
                    scope=str(getattr(decision, "scope", "") or "none"),
                    components=list(getattr(decision, "components", ()) or ()),
                )
                return WorkflowResult(
                    True,
                    "clarification",
                    "创建新平台前还需要你决定：%s。"
                    "这些边界确定前不会扫描运行时、Git、Nginx 或端口；"
                    "端口若授权自动选择，不需要逐个指定。"
                    % "、".join(missing_boundaries),
                )
        superseded = False
        refinement_predecessor = None
        turn_base_goal = ""
        turn_decisions: list[str] = []
        if snapshot is not None:
            turn_base_goal = _authoritative_base_goal(snapshot)
            turn_decisions = list(snapshot.decision_history or [])
        if (
            goal_relation == "refine_previous"
            and decision.intent == "mutating_action"
            and snapshot is not None
            and snapshot.active_plan_id
        ):
            classify_relation = getattr(
                self.mutation_workflow, "classify_recovery_reply", None,
            )
            relation_result = (
                classify_relation(
                    base_goal=turn_base_goal,
                    decision_history=turn_decisions,
                    reply=normalized,
                    pending_question="",
                    evidence_bundle=snapshot.evidence,
                )
                if callable(classify_relation)
                else {}
            )
            relation = str(relation_result.get("relation") or "supplement")
            candidate = str(
                relation_result.get("candidate_base_goal") or ""
            ).strip()
            if relation in {"revise", "new_goal"} and candidate:
                snapshot.pending_goal_revision = candidate
                snapshot.pending_goal_relation = relation
                if self.context_store is not None:
                    self.context_store.save(snapshot)
                conflicts = [
                    str(item) for item in relation_result.get("conflicts") or []
                    if str(item).strip()
                ]
                return WorkflowResult(
                    True,
                    "clarification",
                    (
                        "Planner 判断这条回复会覆盖当前完整目标。\n"
                        "当前目标：%s\n候选目标：%s\n将发生的变化：%s\n"
                        "回复“确认覆盖目标”继续，或回复“保留原目标”。"
                        % (
                            turn_base_goal,
                            candidate,
                            "；".join(conflicts) or str(
                                relation_result.get("reason")
                                or "目标语义发生变化"
                            ),
                        )
                    ),
                )
            normalized_decision = str(
                relation_result.get("normalized_decision") or normalized
            ).strip()
            if normalized_decision and normalized_decision not in turn_decisions:
                turn_decisions.append(normalized_decision)
            refinement_predecessor = self._refinement_predecessor(snapshot)
        if goal_relation == "supersede_previous" and snapshot is not None:
            superseded = self._supersede_active_plan(snapshot)
        if (
            goal_relation == "refine_previous"
            and decision.intent == "mutating_action"
            and snapshot is not None
            and snapshot.active_plan_id
        ):
            # A correction replaces an immutable plan.  Revoke the old hash
            # before planning so it can never remain executable in parallel.
            superseded = self._supersede_active_plan(snapshot) or superseded
        if decision.intent == "resume_plan":
            operation = str(getattr(decision, "operation", "") or "")
            reference = str(getattr(decision, "plan_reference", "") or "")
            plan_id = self._resolve_plan_reference(reference, snapshot)
            if not plan_id:
                if (
                    operation == "inspect"
                    and reference == "latest"
                    and snapshot is not None
                    and snapshot.resolved_goal
                ):
                    # The classifier proposed a Plan route, but persisted reality
                    # contains only a conversational/read-only goal.  A state
                    # guard may reject an impossible route; it must not invent a
                    # different operation.
                    return WorkflowResult(False, "conversation")
                if (
                    operation != "inspect"
                    and reference == "latest"
                    and snapshot is not None
                    and snapshot.resolved_goal
                    and snapshot.workflow_intent in {
                        "readonly_action", "mutating_action",
                    }
                ):
                    # A Plan does not exist yet, so resume the persisted goal
                    # that was saved before Discovery/Planning/Binding.  The
                    # classifier proposes a route; persisted workflow state is
                    # authoritative when that route cannot exist.
                    decision = replace(
                        decision,
                        intent=snapshot.workflow_intent,
                        requires_execution=True,
                        plan_reference="",
                        goal_relation="continue_previous",
                        goal_kind=snapshot.goal_kind or (
                            "execution"
                            if snapshot.workflow_intent == "mutating_action"
                            else "health_check"
                        ),
                        operation=snapshot.operation or "none",
                        scope=snapshot.scope or "none",
                        components=tuple(snapshot.components),
                    )
                    goal_relation = "continue_previous"
                else:
                    return WorkflowResult(
                        True,
                        "clarification",
                        "当前没有活动计划可供查询；如需查询历史计划，请提供计划 ID。",
                    )
            else:
                load_plan = getattr(self.mutation_workflow, "load_plan", None)
                persisted_plan = load_plan(plan_id) if load_plan is not None else None
                if (
                    operation != "inspect"
                    and persisted_plan is not None
                    and str(getattr(persisted_plan, "status", "") or "") == "draft"
                ):
                    self._emit_route("变更规划")
                    bundle = (
                        snapshot.evidence
                        if snapshot is not None
                        else EvidenceBundle(goal=persisted_plan.goal)
                    )
                    try:
                        abort = getattr(self.mutation_workflow, "abort_plan", None)
                        if abort is None or not bool(abort(persisted_plan.plan_id)):
                            raise ValueError("orphaned draft could not be superseded")
                        conclusion = self.synthesis.synthesize(
                            persisted_plan.goal, bundle,
                        )
                        result = self._submit_mutation(
                            persisted_plan.goal,
                            evidence_bundle=bundle,
                            evidence_conclusion=conclusion,
                        )
                        self._save_context(
                            snapshot,
                            resolved_goal=persisted_plan.goal,
                            bundle=bundle,
                            active_plan_id=(
                                str(getattr(result.plan, "plan_id", "") or "")
                                if result.kind not in {"completed", "aborted"}
                                else ""
                            ),
                        )
                        return _localized_result(result)
                    except Exception as exc:
                        self._save_context(
                            snapshot,
                            resolved_goal=persisted_plan.goal,
                            bundle=bundle,
                            active_plan_id="",
                        )
                        return self.mutation_workflow.failure_result(
                            stage="binding",
                            category="persisted_draft_resume_failure",
                            summary="替换旧版未绑定计划时重新规划发生异常，目标尚未完成。",
                            technical_reason="%s: %s" % (type(exc).__name__, exc),
                            goal=persisted_plan.goal,
                            goal_kind="execution",
                            plan_id=persisted_plan.plan_id,
                            attempted_recoveries=["废弃旧 draft 并回到唯一 Plan/Replan 主循环"],
                            evidence=bundle,
                        )
                self._emit_route("计划管理")
                manage_plan = getattr(
                    self.mutation_workflow, "manage_plan_turn", None,
                )
                if manage_plan is None:
                    return self.mutation_workflow.failure_result(
                        stage="planning",
                        category="plan_status_transition_unavailable",
                        summary="计划存在，但当前无法进入统一计划管理流程。",
                        technical_reason="当前工作流不支持管理指定计划。",
                        goal=(
                            snapshot.resolved_goal
                            if snapshot is not None else "查询计划状态"
                        ),
                        goal_kind="execution",
                        plan_id=plan_id,
                        attempted_recoveries=["确认计划引用已绑定到持久化状态"],
                        evidence=(snapshot.evidence if snapshot is not None else None),
                    )
                return manage_plan(
                    plan_id,
                    question=normalized,
                    conversation_context=conversation_context,
                )
        goal_kind = str(getattr(decision, "goal_kind", "") or "")
        if not goal_kind:
            goal_kind = (
                "execution" if decision.intent == "mutating_action"
                else "health_check" if decision.intent == "readonly_action"
                else "conversation"
            )
        if goal_relation == "supersede_previous" and decision.intent == "conversation":
            self._emit_route("取消旧目标")
            self._clear_context(snapshot)
            return WorkflowResult(
                True,
                "aborted",
                (
                    "已取消旧计划，本轮不会执行任何变更。"
                    if superseded
                    else "已停止沿用之前的运维目标，本轮不会执行任何变更。"
                ),
            )
        if decision.intent == "conversation":
            return WorkflowResult(False, "conversation")
        resolved_goal = normalized
        effective_intent = decision.intent
        if snapshot is not None and snapshot.resolved_goal:
            if (
                goal_relation == "continue_previous"
                and _structured_goal_semantics_changed(snapshot, decision)
            ):
                # The classifier may propose continuation while its own typed
                # fields describe a different requested result.  Relation only
                # controls target inheritance; it cannot erase the current
                # request when the semantic contract changed.
                goal_relation = "refine_previous"
            if goal_relation == "refine_previous":
                if decision.intent == "mutating_action" and turn_base_goal:
                    # Change planning has one immutable objective.  Confirmed
                    # supplements already live in decision_history and enter
                    # Planner intent_context; serializing them back into goal
                    # creates a second, drifting source of target authority.
                    resolved_goal = turn_base_goal
                else:
                    resolved_goal = _refined_goal(
                        turn_base_goal or snapshot.resolved_goal,
                        normalized,
                    )
                effective_intent = decision.intent
            elif goal_relation == "continue_previous":
                resolved_goal = snapshot.resolved_goal
                effective_intent = decision.intent
        self._emit_route(
            "只读检查"
            if effective_intent == "readonly_action"
            else "变更规划"
        )
        seed_bundle = (
            snapshot.reusable_evidence(resolved_goal)
            if snapshot is not None and (
                resolved_goal == snapshot.resolved_goal
                or goal_relation in {"continue_previous", "refine_previous"}
            )
            else None
        )
        retained_active_plan_id = (
            snapshot.active_plan_id
            if snapshot is not None and goal_relation in {
                "continue_previous", "refine_previous",
            }
            else ""
        )
        snapshot = self._save_context(
            snapshot,
            resolved_goal=resolved_goal,
            bundle=seed_bundle or EvidenceBundle(goal=resolved_goal),
            active_plan_id=retained_active_plan_id,
            workflow_intent=effective_intent,
            goal_kind=goal_kind,
            operation=str(getattr(decision, "operation", "none") or "none"),
            scope=str(getattr(decision, "scope", "none") or "none"),
            components=list(getattr(decision, "components", ()) or ()),
            base_goal=(
                turn_base_goal
                if goal_relation == "refine_previous" and turn_base_goal
                else None
            ),
            decision_history=(
                turn_decisions
                if goal_relation == "refine_previous"
                else None
            ),
        ) or snapshot
        collection_goal = (
            "只读诊断并补齐以下运维目标所需证据：%s" % resolved_goal
            if goal_kind == "causal_diagnosis" or goal_relation in {
                "continue_previous", "refine_previous",
            }
            else resolved_goal
        )
        begin_probe_session = getattr(self.discovery, "begin_probe_session", None)
        end_probe_session = getattr(self.discovery, "end_probe_session", None)
        if begin_probe_session is not None:
            begin_probe_session()
        current_stage = "discovery"
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
            collect_traceback_sources = getattr(
                self.discovery, "collect_traceback_source_evidence", None,
            )
            if collect_traceback_sources is not None:
                collect_traceback_sources(bundle)
            current_stage = "synthesis"
            conclusion = self.synthesis.synthesize(collection_goal, bundle)
            if effective_intent == "readonly_action":
                current_stage = "verification"
                return self._run_readonly_goal_loop(
                    snapshot,
                    resolved_goal=resolved_goal,
                    collection_goal=collection_goal,
                    bundle=bundle,
                    conclusion=conclusion,
                    goal_kind=goal_kind,
                    active_plan_id=retained_active_plan_id,
                )
            pre_execution = self.verifier.verify_pre_execution(
                resolved_goal,
                bundle,
                operation=str(getattr(decision, "operation", "none") or "none"),
                scope=str(getattr(decision, "scope", "none") or "none"),
            )
            if pre_execution is not None and pre_execution.status == "needs_user_decision":
                return WorkflowResult(
                    True,
                    "clarification",
                    pre_execution.user_question,
                    evidence=bundle,
                    outcome=pre_execution,
                )
            if pre_execution is not None and pre_execution.status == "achieved":
                return WorkflowResult(
                    True,
                    "completed",
                    pre_execution.reason,
                    evidence=bundle,
                    outcome=pre_execution,
                )
            resolved_identity = RuntimeInventory.from_bundle(bundle).resolve_identity(
                resolved_goal,
            )
            intent_context = {
                "operation": str(getattr(decision, "operation", "") or ""),
                "scope": str(getattr(decision, "scope", "") or ""),
                "components": list(getattr(decision, "components", ()) or ()),
            }
            if goal_relation == "refine_previous":
                intent_context["base_goal"] = (
                    turn_base_goal or resolved_goal
                )
                intent_context["decision_history"] = list(turn_decisions)
                if refinement_predecessor is not None:
                    intent_context.update(
                        _authoritative_recovery_scope(refinement_predecessor)
                    )
            if resolved_identity is not None:
                intent_context.update(resolved_identity.to_dict())
                intent_context["resolved_project_root"] = (
                    resolved_identity.project_root
                )
            current_stage = "planning"
            result = self._submit_mutation(
                resolved_goal,
                evidence_bundle=bundle,
                evidence_conclusion=conclusion,
                intent_context=intent_context,
                initial_candidate_plan=refinement_predecessor,
                initial_active_gap_steps=(
                    list(
                        intent_context.get("recovery_required_step_ids") or []
                    )
                    if refinement_predecessor is not None
                    else None
                ),
            )
            self._save_context(
                snapshot,
                resolved_goal=resolved_goal,
                bundle=bundle,
                active_plan_id=(
                    str(getattr(result.plan, "plan_id", "") or "")
                    if result.kind not in {"completed", "aborted"}
                    else ""
                ),
            )
            return _localized_result(result)
        except Exception as exc:
            failure_bundle = (
                bundle if "bundle" in locals()
                else seed_bundle or EvidenceBundle(goal=resolved_goal)
            )
            persisted_plan_id = self._latest_plan_id_for_goal(resolved_goal)
            if persisted_plan_id:
                self._save_context(
                    snapshot,
                    resolved_goal=resolved_goal,
                    bundle=failure_bundle,
                    active_plan_id=persisted_plan_id,
                )
            return self.mutation_workflow.failure_result(
                stage=current_stage,
                category=current_stage + "_unhandled_failure",
                summary={
                    "discovery": "只读证据收集发生异常，目标尚未完成。",
                    "synthesis": "证据整理发生异常，目标尚未完成。",
                    "verification": "目标验证发生异常，目标尚未完成。",
                    "planning": "变更规划发生异常，目标尚未完成。",
                }.get(current_stage, "工作流发生异常，目标尚未完成。"),
                technical_reason="%s: %s" % (type(exc).__name__, exc),
                goal=resolved_goal,
                goal_kind=goal_kind,
                plan_id=persisted_plan_id,
                attempted_recoveries=["保留原始目标和当前会话上下文"],
                evidence=failure_bundle,
            )
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
        initial_candidate_plan: Any | None = None,
        initial_active_gap_steps: list[str] | None = None,
    ) -> WorkflowResult:
        """Own the single bounded Plan/Replan transition loop."""

        workflow = self.mutation_workflow
        # Planner is allowed to refine its candidate in place.  Execution
        # effects, however, belong to the already-run predecessor and must not
        # be mutable through that candidate reference.  Freeze the predecessor
        # once at the Replan boundary; Binder uses it only to inherit proved
        # effects into the single successor plan.
        predecessor_snapshot = (
            deepcopy(initial_candidate_plan)
            if initial_candidate_plan is not None
            else None
        )

        def merge_gap_steps(*groups: Any) -> list[str]:
            """Accumulate one Replan cycle's semantic repair boundary."""

            return list(dict.fromkeys(
                str(item)
                for group in groups
                for item in (group or [])
                if str(item)
            ))

        def plan(
            *, feedback: str = "", candidate_plan: Any | None = None,
            active_gap_affected_steps: list[str] | None = None,
        ) -> Any:
            current_intent = dict(intent_context or {})
            if active_gap_affected_steps:
                current_intent["active_gap_affected_steps"] = list(
                    active_gap_affected_steps
                )
            parameters = inspect.signature(workflow.plan_once).parameters
            accepts_kwargs = any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            )
            kwargs: dict[str, Any] = {
                "evidence_bundle": evidence_bundle,
                "evidence_conclusion": evidence_conclusion,
            }
            optional = {
                "intent_context": current_intent,
                "binding_feedback": feedback,
                "candidate_plan": candidate_plan,
            }
            for name, value in optional.items():
                if name in parameters or accepts_kwargs:
                    kwargs[name] = value
            return workflow.plan_once(goal, **kwargs)

        outcome = plan(
            candidate_plan=initial_candidate_plan,
            active_gap_affected_steps=initial_active_gap_steps,
        )
        replanning_rounds = 0
        no_progress_replans = 0
        candidate_replans = 0
        binding_attempt = 0
        binding_resume_attempts = 0
        first_binding_error = ""
        first_binding_failed_criteria: list[str] = []
        first_binding_missing_decisions: list[str] = []
        persistent_candidate = (
            outcome.plan or outcome.candidate_plan or initial_candidate_plan
        )

        def recovery_failure_context() -> dict[str, Any]:
            """Preserve predecessor effects across every Replan failure exit."""

            if initial_candidate_plan is None:
                return {}
            return {
                "plan": initial_candidate_plan,
                "environment_changed": _paused_plan_environment_changed(
                    initial_candidate_plan
                ),
            }
        active_gap_steps = merge_gap_steps(
            initial_active_gap_steps,
            outcome.replan_context.get("active_gap_affected_steps"),
        )
        while True:
            if outcome.status == "need_evidence":
                if replanning_rounds >= workflow.max_replanning_rounds:
                    return workflow.failure_result(
                        **recovery_failure_context(),
                        stage="planning",
                        category="planning_evidence_budget_exhausted",
                        summary="规划补证达到安全上限，尚未形成可审批计划。",
                        technical_reason=(
                            "Planner-to-Discovery evidence budget exhausted."
                        ),
                        goal=goal,
                        goal_kind="execution",
                        attempted_recoveries=["按 Planner 请求补充只读证据"],
                        evidence=evidence_bundle,
                        evidence_requests=list(outcome.evidence_requests),
                    )
                if self.discovery is None or self.synthesis is None:
                    return workflow.failure_result(
                        **recovery_failure_context(),
                        stage="planning",
                        category="planning_discovery_unavailable",
                        summary="Planner 需要补证，但当前补证能力不可用。",
                        technical_reason=(
                            "Planner requested evidence but Discovery is unavailable."
                        ),
                        goal=goal,
                        goal_kind="execution",
                        attempted_recoveries=["检查 Planner 的补证请求"],
                        evidence=evidence_bundle,
                        evidence_requests=list(outcome.evidence_requests),
                    )
                before_available = _evidence_progress_state(
                    evidence_bundle, outcome.evidence_requests,
                )
                requested_summary = _evidence_request_summary(
                    outcome.evidence_requests,
                )
                self._emit_progress(
                    "Replan %s/%s：Planner 需要补证：%s"
                    % (
                        replanning_rounds + 1,
                        workflow.max_replanning_rounds,
                        requested_summary,
                    )
                )
                evidence_bundle = self.discovery.collect_requests(
                    outcome.evidence_requests,
                    evidence_bundle,
                )
                after_available = _evidence_progress_state(
                    evidence_bundle, outcome.evidence_requests,
                )
                if (
                    before_available is not None
                    and after_available is not None
                    and after_available == before_available
                ):
                    requested = ",".join(
                        str(item.probe) for item in outcome.evidence_requests
                    ) or "none"
                    if (
                        no_progress_replans < 1
                        and replanning_rounds < workflow.max_replanning_rounds
                    ):
                        no_progress_replans += 1
                        replanning_rounds += 1
                        self._emit_progress(
                            "Replan %s/%s：注册 Probe 与安全只读补证均未产生新事实；"
                            "正在把具体缺口和已尝试路径交回 Planner。"
                            % (replanning_rounds, workflow.max_replanning_rounds)
                        )
                        feedback = json.dumps(
                            {
                                "failure": "evidence_no_progress",
                                "requested": [
                                    {
                                        "probe": item.probe,
                                        "args": item.args,
                                        "required_facts": [
                                            fact.to_dict()
                                            for fact in item.required_facts
                                        ],
                                        "subject": (
                                            item.subject.to_dict()
                                            if item.subject is not None else None
                                        ),
                                        "need_key": item.need_key,
                                    }
                                    for item in outcome.evidence_requests
                                ],
                                "instruction": (
                                    "Do not repeat the same evidence need. Change the"
                                    " target/arguments, request a different semantic"
                                    " read-only capability (Discovery may bind Shell),"
                                    " or return a grounded ready/blocked outcome."
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        outcome = plan(
                            feedback=feedback,
                            candidate_plan=persistent_candidate,
                            active_gap_affected_steps=active_gap_steps,
                        )
                        persistent_candidate = (
                            outcome.candidate_plan or persistent_candidate
                        )
                        active_gap_steps = merge_gap_steps(
                            active_gap_steps,
                            outcome.replan_context.get(
                                "active_gap_affected_steps"
                            ),
                        )
                        self._emit_progress(
                            "Replan %s/%s：Planner 返回 %s。"
                            % (
                                replanning_rounds,
                                workflow.max_replanning_rounds,
                                outcome.status,
                            )
                        )
                        continue
                    return workflow.failure_result(
                        **recovery_failure_context(),
                        stage="planning",
                        category="planning_evidence_no_progress",
                        summary="规划请求的补证没有产生新的可用事实，已停止重复查询。",
                        technical_reason=(
                            "Planner-to-Discovery contract made no progress; probes=%s"
                            % requested
                        ),
                        goal=goal,
                        goal_kind="execution",
                        attempted_recoveries=[
                            "尝试注册 Probe 与安全只读命令补证",
                            "把缺失事实和已尝试路径交回 Planner 重新规划",
                        ],
                        evidence=evidence_bundle,
                        evidence_requests=list(outcome.evidence_requests),
                    )
                evidence_conclusion = self.synthesis.synthesize(
                    goal, evidence_bundle,
                )
                replanning_rounds += 1
                progress_summary = _evidence_progress_summary(
                    before_available,
                    after_available,
                    outcome.evidence_requests,
                )
                self._emit_progress(
                    "Replan %s/%s：%s；正在重新规划。"
                    % (
                        replanning_rounds,
                        workflow.max_replanning_rounds,
                        progress_summary,
                    )
                )
                finalize_candidate = getattr(
                    workflow.planner, "finalize_candidate", None,
                )
                if outcome.candidate_plan is not None and finalize_candidate is not None:
                    outcome = finalize_candidate(
                        outcome.candidate_plan, evidence_bundle,
                    )
                else:
                    outcome = plan(
                        candidate_plan=persistent_candidate,
                        active_gap_affected_steps=active_gap_steps,
                    )
                persistent_candidate = outcome.candidate_plan or persistent_candidate
                active_gap_steps = merge_gap_steps(
                    active_gap_steps,
                    outcome.replan_context.get("active_gap_affected_steps"),
                )
                self._emit_progress(
                    "Replan %s/%s：Planner 返回 %s。"
                    % (replanning_rounds, workflow.max_replanning_rounds, outcome.status)
                )
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
                ), candidate_plan=persistent_candidate,
                    active_gap_affected_steps=active_gap_steps)
                continue
            if outcome.status != "need_execution" or outcome.plan is None:
                if binding_attempt:
                    return workflow.failure_result(
                        **recovery_failure_context(),
                        stage="binding",
                        category="binding_replan_unresolved",
                        summary=(
                            "实施绑定发现计划缺少安全落地所需的实例或动作约束。"
                        ),
                        technical_reason="%s; replan=%s" % (
                            first_binding_error,
                            outcome.reason
                            or "binding replan did not produce a ready plan",
                        ),
                        goal=goal,
                        goal_kind="execution",
                        attempted_recoveries=[
                            "首次实施绑定",
                            "保持目标不变进入统一 Plan/Discovery/Replan 循环",
                        ],
                        evidence=evidence_bundle,
                        evidence_requests=list(outcome.evidence_requests),
                        failed_checks=list(dict.fromkeys([
                            *first_binding_failed_criteria,
                            *outcome.failed_criteria,
                        ])),
                        missing_decisions=list(dict.fromkeys([
                            *first_binding_missing_decisions,
                            *outcome.missing_decisions,
                        ])),
                    )
                return workflow.failure_result(
                    **recovery_failure_context(),
                    stage="planning",
                    category="planning_contract_unresolved",
                    summary="变更规划在有限次数校正后仍未形成安全、可审批的计划。",
                    technical_reason=(
                        outcome.reason
                        or "change planning did not produce an executable plan"
                    ),
                    goal=goal,
                    goal_kind="execution",
                    attempted_recoveries=["补充计划证据", "有限次数重新规划"],
                    evidence=evidence_bundle,
                    evidence_requests=list(outcome.evidence_requests),
                    failed_checks=list(outcome.failed_criteria),
                    missing_decisions=list(outcome.missing_decisions),
                )

            evidence_scope = getattr(self.discovery, "evidence_scope", None)
            scope = (
                evidence_scope(evidence_bundle)
                if callable(evidence_scope)
                else nullcontext()
            )
            # Binder expands and normalizes the candidate in place. If a later
            # atomic contract rejects, that partially bound object is useful
            # diagnostics but is no longer the authoritative semantic plan.
            # Preserve the pre-binding semantics for a scoped Planner repair.
            semantic_candidate_snapshot = deepcopy(outcome.plan)
            before_binding_evidence = _available_evidence_state(evidence_bundle)
            try:
                with scope:
                    bind_kwargs: dict[str, Any] = {
                        "evidence_bundle": evidence_bundle,
                    }
                    bind_parameters = inspect.signature(workflow.bind_once).parameters
                    if (
                        predecessor_snapshot is not None
                        and "predecessor_plan" in bind_parameters
                    ):
                        bind_kwargs["predecessor_plan"] = predecessor_snapshot
                    binding = workflow.bind_once(outcome.plan, **bind_kwargs)
            except Exception as exc:
                failed_plan = outcome.plan
                failed_plan.status = "blocked"
                workflow.store.save(failed_plan)
                return workflow.failure_result(
                    **{
                        **recovery_failure_context(),
                        "plan": failed_plan,
                    },
                    stage="binding",
                    category="binding_unhandled_failure",
                    summary="实施绑定发生未处理异常，已在执行前安全停止。",
                    technical_reason="%s: %s" % (
                        type(exc).__name__, str(exc)[:1000],
                    ),
                    goal=goal,
                    goal_kind="execution",
                    attempted_recoveries=["保存当前语义计划和 Binding 断点"],
                    evidence=evidence_bundle,
                )
            after_binding_evidence = _available_evidence_state(evidence_bundle)
            if binding.status == "needs_user_decision" and binding.plan is not None:
                return WorkflowResult(
                    True,
                    "awaiting_confirmation",
                    binding.user_question,
                    plan=binding.plan,
                    evidence=evidence_bundle,
                    outcome=binding,
                )
            if binding.status != "need_replan":
                raise ValueError("invalid binding transition")
            if binding.replan_context.get("resume_binding"):
                partial_plan = binding.candidate_plan or outcome.plan
                if partial_plan is None:
                    raise ValueError("binding resume requested without candidate plan")
                if binding_resume_attempts < 1:
                    binding_resume_attempts += 1
                    cursor = dict(binding.replan_context.get("binding_cursor") or {})
                    self._emit_progress(
                        "Binding 恢复 1/1：临时调用失败，正在从语义步骤 %s 的"
                        "原子步骤 %s 继续绑定；不重新运行 Planner。"
                        % (
                            cursor.get("semantic_step_id") or "unknown",
                            cursor.get("atomic_step_index", "unknown"),
                        )
                    )
                    outcome = GoalOutcome(
                        status="need_execution",
                        reason=binding.reason,
                        plan=partial_plan,
                        candidate_plan=partial_plan,
                    )
                    continue
                partial_plan.status = "blocked"
                workflow.store.save(partial_plan)
                cursor = dict(binding.replan_context.get("binding_cursor") or {})
                return workflow.failure_result(
                    **{
                        **recovery_failure_context(),
                        "plan": partial_plan,
                    },
                    stage="binding",
                    category=str(
                        binding.replan_context.get("failure_category")
                        or "binding_provider_transient"
                    ),
                    summary="实施绑定的模型调用连续失败，已保存当前原子步骤断点。",
                    technical_reason=binding.reason,
                    goal=goal,
                    goal_kind="execution",
                    attempted_recoveries=[
                        "保存已绑定原子步骤",
                        "从 Binding 断点原地重试一次",
                    ],
                    failed_checks=list(binding.failed_criteria),
                    missing_decisions=list(binding.missing_decisions),
                    evidence=evidence_bundle,
                    semantic_step_id=str(cursor.get("semantic_step_id") or ""),
                    atomic_step_index=int(cursor.get("atomic_step_index", -1)),
                )
            if binding_attempt == 0:
                first_binding_error = binding.reason
                first_binding_failed_criteria = list(binding.failed_criteria)
                first_binding_missing_decisions = list(binding.missing_decisions)
                self._emit_progress(
                    "Binding Replan 1/1：实施绑定未满足合同，正在保持目标不变重建语义计划。"
                )
                if (
                    self.synthesis is not None
                    and before_binding_evidence != after_binding_evidence
                ):
                    evidence_conclusion = self.synthesis.synthesize(
                        goal, evidence_bundle,
                    )
                    self._emit_progress(
                        "Binding Replan 1/1：实施补证已写入统一证据上下文。"
                    )
                binding_allowed_steps = [
                    str(item)
                    for item in binding.replan_context.get(
                        "affected_steps", []
                    )
                    if str(item)
                ] or [
                    str(binding.replan_context.get("step_id") or "")
                ]
                binding_allowed_steps = [
                    item for item in binding_allowed_steps if item
                ]
                outcome = plan(feedback=json.dumps(
                    {
                        "reason": binding.reason,
                        "failed_criteria": list(binding.failed_criteria),
                        "missing_decisions": list(binding.missing_decisions),
                        "implementation_gap": {
                            key: value
                            for key, value in binding.replan_context.items()
                            if key not in {
                                "failure_category", "replan_recommended",
                                "resume_binding", "binding_cursor",
                                "exception_type",
                            }
                        },
                        "instruction": (
                            "Preserve the goal and grounded instance identity; repair"
                            " only the rejected semantic effect."
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ), candidate_plan=semantic_candidate_snapshot,
                    active_gap_affected_steps=binding_allowed_steps)
                persistent_candidate = (
                    outcome.candidate_plan
                    or semantic_candidate_snapshot
                    or persistent_candidate
                )
                active_gap_steps = merge_gap_steps(
                    active_gap_steps,
                    binding_allowed_steps,
                    outcome.replan_context.get("active_gap_affected_steps"),
                )
                self._emit_progress(
                    "Binding Replan 1/1：Planner 返回 %s。" % outcome.status
                )
                binding_attempt += 1
                # Re-enter the one authoritative Plan/Discovery loop.  In
                # particular, ``need_evidence`` is a transition to Discovery,
                # not a terminal Binding failure.
                continue
            failed_plan = binding.candidate_plan or outcome.plan
            failed_plan.status = "blocked"
            workflow.store.save(failed_plan)
            return workflow.failure_result(
                stage="binding",
                category="binding_contract_invalid",
                summary="实施绑定无法证明动作只作用于目标实例，已安全拒绝。",
                technical_reason="first_binding=%s; final_binding=%s" % (
                    first_binding_error, binding.reason,
                ),
                goal=goal,
                goal_kind="execution",
                plan=failed_plan,
                attempted_recoveries=["首次实施绑定", "局部重新规划", "第二次实施绑定"],
                evidence=evidence_bundle,
                failed_checks=list(dict.fromkeys([
                    *first_binding_failed_criteria,
                    *binding.failed_criteria,
                ])),
                missing_decisions=list(dict.fromkeys([
                    *first_binding_missing_decisions,
                    *binding.missing_decisions,
                ])),
            )

    def _run_readonly_goal_loop(
        self,
        previous: OperationalContextSnapshot | None,
        *,
        resolved_goal: str,
        collection_goal: str,
        bundle: Any,
        conclusion: Any,
        goal_kind: str = "health_check",
        active_plan_id: str = "",
    ) -> WorkflowResult:
        """Continue safe evidence collection until the requested result exists."""

        decision, bundle, conclusion = self._advance_diagnostic_evidence(
            resolved_goal,
            bundle,
            conclusion,
            goal_kind=goal_kind,
            synthesis_goal=collection_goal,
        )
        status = str(getattr(decision, "status", "") or "")
        self._save_context(
            previous,
            resolved_goal=resolved_goal,
            bundle=bundle,
            active_plan_id=active_plan_id,
        )
        if status == "achieved":
            response_kwargs: dict[str, Any] = {}
            if "evidence_bundle" in inspect.signature(
                self.response.render_readonly
            ).parameters:
                response_kwargs["evidence_bundle"] = bundle
            if "goal_kind" in inspect.signature(
                self.response.render_readonly
            ).parameters:
                response_kwargs["goal_kind"] = goal_kind
            return WorkflowResult(
                True,
                "completed",
                self.response.render_readonly(
                    resolved_goal, conclusion, **response_kwargs,
                ),
                evidence=bundle,
                outcome=decision,
            )
        if status == "needs_user_decision":
            return WorkflowResult(
                True,
                "clarification",
                str(getattr(decision, "user_question", "") or (
                    "需要你明确目标实例或检查范围。"
                )),
                evidence=bundle,
                outcome=decision,
            )
        technical_reason = str(getattr(decision, "reason", "") or "").strip()
        return self.mutation_workflow.failure_result(
            stage="verification",
            category=(
                "readonly_invalid_replan_transition"
                if status == "need_replan"
                else "readonly_goal_verification_blocked"
            ),
            summary=(
                "只读目标无法进入变更重规划，必须由人工确认下一步。"
                if status == "need_replan"
                else "只读补证已停止，目标尚未取得可靠的完成结论。"
            ),
            technical_reason=(
                technical_reason or "只读目标验证返回了无法继续的状态：%s"
                % (status or "unknown")
            ),
            goal=resolved_goal,
            goal_kind=goal_kind,
            attempted_recoveries=[
                "执行初始只读 Discovery",
                "运行 Verifier 驱动的有限补证循环",
            ],
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
        synthesis_goal: str = "",
    ) -> tuple[Any, Any, Any]:
        attempted_keys = {
            item.request.need_key for item in getattr(bundle, "records", [])
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
            before = _available_evidence_state(bundle)
            progress = getattr(self.discovery, "on_progress", None)
            if progress is not None:
                progress(
                    "诊断补证循环 %s/%s：继续获取形成最终结论所需的证据。"
                    % (cycle, max_cycles)
                )
            self.discovery.collect_requests(requests, bundle)
            attempted_keys.update(request.need_key for request in requests)
            attempted_keys.update(
                item.request.need_key
                for item in getattr(bundle, "records", [])
            )
            collect_traceback_sources = getattr(
                self.discovery, "collect_traceback_source_evidence", None,
            )
            if collect_traceback_sources is not None:
                collect_traceback_sources(bundle)
            if _available_evidence_state(bundle) == before:
                continue
            conclusion = self.synthesis.synthesize(synthesis_goal or goal, bundle)
        from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

        return GoalOutcome(
            status="blocked",
            reason="目标验证补证循环达到安全上限。",
        ), bundle, conclusion

    def _continue_post_execution(
        self,
        control: WorkflowResult,
        *,
        snapshot: OperationalContextSnapshot | None,
        conversation_context: str,
    ) -> WorkflowResult:
        """Run the sole goal-level verdict after execution, then replan if needed."""

        from klonet_agent.ops.privileged.workflow.contracts import (
            EvidenceBundle, EvidenceRecord, GoalOutcome, ProbeRequest,
        )

        plan = control.plan
        goal = str(getattr(plan, "goal", "") or "").strip()
        if not goal:
            return control
        unknown_step_ids = {
            str(getattr(step, "step_id", "") or "")
            for change in list(getattr(plan, "steps", []) or [])
            for step in (
                list(getattr(change.implementation_plan, "steps", []) or [])
                if getattr(change, "implementation_plan", None) is not None
                else [change]
            )
            if str(getattr(step, "status", "") or "") == "execution_unknown"
        }
        reconcile = getattr(
            self.mutation_workflow, "reconcile_recovery_state", None,
        )
        if callable(reconcile):
            plan = reconcile(plan)
            control.plan = plan
        recovered_unmet_step_ids = [
            str(getattr(step, "step_id", "") or "")
            for change in list(getattr(plan, "steps", []) or [])
            for step in (
                list(getattr(change.implementation_plan, "steps", []) or [])
                if getattr(change, "implementation_plan", None) is not None
                else [change]
            )
            if str(getattr(step, "step_id", "") or "") in unknown_step_ids
            and str(getattr(step, "status", "") or "") == "paused"
            and any(
                str(getattr(check, "status", "") or "") == "failed"
                for check in list(getattr(step, "checks", []) or [])
            )
        ]
        environment_changed = _paused_plan_environment_changed(plan)
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
        # Do not assert failure before the sole goal Verifier has made its
        # decision.  This text is consumed by Discovery, Synthesis and the
        # knowledge retriever; wording it as an already-failed task turns a
        # transitional ``paused`` plan into synthetic failure evidence.
        recovery_goal = (
            "验证已审批任务是否已经达到完整用户目标；仅当当前证据证明仍有"
            "未满足效果时，定位该具体缺口并补齐局部恢复规划证据：%s" % goal
        )
        begin = getattr(self.discovery, "begin_probe_session", None)
        end = getattr(self.discovery, "end_probe_session", None)
        if begin is not None:
            begin()
        current_stage = "discovery"
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
            current_stage = "synthesis"
            conclusion = self.synthesis.synthesize(recovery_goal, bundle)
            current_stage = "verification"
            if recovered_unmet_step_ids:
                # Current-state Checkers already proved the interrupted
                # mutation did not satisfy its exact postconditions.  That is
                # a complete Replan contract; asking the goal Verifier for
                # repeated process/screen probes loses the atomic failure.
                decision = GoalOutcome(
                    "need_replan",
                    reason=(
                        "Interrupted steps are deterministically unmet: %s"
                        % ",".join(recovered_unmet_step_ids)
                    ),
                )
            else:
                decision, bundle, conclusion = self._advance_diagnostic_evidence(
                    goal,
                    bundle,
                    conclusion,
                    phase="post_execution",
                    synthesis_goal=recovery_goal,
                )
        except Exception as exc:
            return self.mutation_workflow.failure_result(
                stage=current_stage,
                category="post_execution_%s_failure" % current_stage,
                summary="执行后的自动诊断发生异常，目标仍未完成。",
                technical_reason="%s: %s" % (type(exc).__name__, exc),
                goal=goal,
                goal_kind="execution",
                plan=plan,
                attempted_recoveries=["保存执行结果", "启动执行后自动诊断"],
                environment_changed=environment_changed,
                evidence=bundle,
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
                outcome=decision,
            )
        if status == "achieved":
            complete_goal = getattr(self.mutation_workflow, "complete_goal", None)
            if complete_goal is None:
                return self.mutation_workflow.failure_result(
                    stage="verification",
                    category="completion_transition_unavailable",
                    summary="目标已经验证，但无法安全提交计划完成状态。",
                    technical_reason="目标已验证，但计划完成转换不可用。",
                    goal=goal,
                    goal_kind="execution",
                    plan=plan,
                    attempted_recoveries=["完成执行后只读验证"],
                    environment_changed=environment_changed,
                    evidence=bundle,
                )
            result = complete_goal(plan, decision)
            result.evidence = bundle
            return result
        if status != "need_replan":
            return self.mutation_workflow.failure_result(
                stage="verification",
                category="post_execution_replan_not_grounded",
                summary="执行后目标尚未完成，且当前证据不足以安全重规划。",
                technical_reason=(
                    str(getattr(decision, "reason", "") or "").strip()
                    or "已自动诊断执行失败，但尚未取得足以安全重规划的根因证据。"
                ),
                goal=goal,
                goal_kind="execution",
                plan=plan,
                attempted_recoveries=[
                    "保存执行结果",
                    "执行后自动只读诊断",
                    "尝试形成局部 Replan 依据",
                ],
                environment_changed=environment_changed,
                evidence=bundle,
            )
        try:
            self._emit_progress(
                "执行后 Replan：Verifier 已确认目标未完成，正在只重建未满足的效果。"
            )
            recovery_scope = _authoritative_recovery_scope(plan)
            intent_context: dict[str, Any] = {
                **recovery_scope,
                "operation": str(
                    getattr(snapshot, "operation", "") or "restart"
                ),
                "scope": str(getattr(snapshot, "scope", "") or "platform"),
                "components": list(
                    getattr(snapshot, "components", []) or []
                ),
                "base_goal": str(
                    getattr(snapshot, "base_goal", "") or goal
                ),
                "decision_history": list(
                    getattr(snapshot, "decision_history", []) or []
                ),
                "recovery_failure_stage": "verification",
                "recovery_failure_category": "post_execution",
                "recovery_technical_reason": str(
                    getattr(decision, "reason", "") or control.message or ""
                ),
            }
            result = self._submit_mutation(
                goal,
                evidence_bundle=bundle,
                evidence_conclusion=conclusion,
                intent_context=intent_context,
                initial_candidate_plan=plan,
                initial_active_gap_steps=list(
                    recovery_scope.get("recovery_required_step_ids") or []
                ),
            )
        except Exception as exc:
            return self.mutation_workflow.failure_result(
                stage="planning",
                category="post_execution_replan_failure",
                summary="执行后局部重规划发生异常，目标仍未完成。",
                technical_reason="%s: %s" % (type(exc).__name__, exc),
                goal=goal,
                goal_kind="execution",
                plan=plan,
                attempted_recoveries=[
                    "保存执行结果",
                    "完成执行后自动诊断",
                    "启动局部 Replan",
                ],
                environment_changed=environment_changed,
                evidence=bundle,
            )
        self._save_context(
            snapshot,
            resolved_goal=goal,
            bundle=bundle,
            active_plan_id=(
                str(getattr(result.plan, "plan_id", "") or "")
                if result.kind not in {"completed", "aborted"}
                else ""
            ),
        )
        return _localized_result(result)

    def _recover_failure_option(
        self,
        control: WorkflowResult,
        *,
        snapshot: OperationalContextSnapshot | None,
        conversation_context: str,
        user_direction: str = "",
    ) -> WorkflowResult:
        """Resume the existing loop from a persisted user-selected recovery."""

        failure = control.failure
        if failure is None:
            return WorkflowResult(
                True, "clarification",
                "失败恢复记录缺失，本轮没有执行任何操作。请重新说明原目标。",
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
                True, "clarification",
                "所选恢复方案不存在，本轮没有执行任何操作。请重新选择已显示的方案。",
                failure=failure,
            )
        self._emit_progress("已选择恢复方案：%s" % option.label)
        original_goal = str(failure.goal or "").strip()
        base_goal = str(
            getattr(snapshot, "base_goal", "") or original_goal
        ).strip()
        decisions = list(getattr(snapshot, "decision_history", []) or [])
        relation = "supplement"
        relation_result: dict[str, Any] = {}
        if user_direction.strip():
            classify = getattr(
                self.mutation_workflow, "classify_recovery_reply", None,
            )
            relation_result = (
                classify(
                    base_goal=base_goal,
                    decision_history=decisions,
                    reply=user_direction,
                    pending_question="\n".join(failure.missing_decisions),
                    evidence_bundle=(snapshot.evidence if snapshot is not None else None),
                )
                if callable(classify)
                else {}
            )
            relation = str(relation_result.get("relation") or "supplement")
            if relation in {"revise", "new_goal"}:
                candidate = str(
                    relation_result.get("candidate_base_goal") or ""
                ).strip()
                if not candidate:
                    relation = "supplement"
                else:
                    if snapshot is None:
                        snapshot = OperationalContextSnapshot(
                            resolved_goal=base_goal,
                            base_goal=base_goal,
                            evidence=EvidenceBundle(goal=base_goal),
                        )
                    snapshot.pending_goal_revision = candidate
                    snapshot.pending_goal_relation = relation
                    if self.context_store is not None:
                        self.context_store.save(snapshot)
                    conflicts = [
                        str(item) for item in relation_result.get("conflicts") or []
                        if str(item).strip()
                    ]
                    return WorkflowResult(
                        True,
                        "clarification",
                        (
                            "Planner 判断这条回复会覆盖当前完整目标。\n"
                            "当前目标：%s\n候选目标：%s\n将发生的变化：%s\n"
                            "回复“确认覆盖目标”继续，或回复“保留原目标”。"
                            % (
                                base_goal,
                                candidate,
                                "；".join(conflicts) or str(
                                    relation_result.get("reason") or "目标语义发生变化"
                                ),
                            )
                        ),
                        failure=failure,
                    )
            normalized_decision = str(
                relation_result.get("normalized_decision") or user_direction
            ).strip()
            if normalized_decision and normalized_decision not in decisions:
                decisions.append(normalized_decision)
        goal = base_goal or original_goal
        if snapshot is not None:
            snapshot.base_goal = goal
            snapshot.resolved_goal = goal
            snapshot.decision_history = list(decisions)
            if self.context_store is not None:
                self.context_store.save(snapshot)
        recovery_plan = None
        recovery_scope: dict[str, Any] = {}
        load_plan = getattr(self.mutation_workflow, "load_plan", None)
        if callable(load_plan) and str(failure.plan_id or ""):
            try:
                candidate = load_plan(failure.plan_id)
            except (FileNotFoundError, KeyError, ValueError):
                candidate = None
            if (
                candidate is not None
                and str(getattr(candidate, "goal", "") or "").strip() == goal
            ):
                candidate_scope = _authoritative_recovery_scope(candidate)
                # A draft that never reached approval or execution is a
                # disposable Planner candidate, not authoritative recovery
                # state.  Fresh runtime identity may legitimately replace it.
                # A persisted execution-state plan remains authoritative
                # evidence even when an old/minimal plan has no resource map
                # from which target scope can be reconstructed.  Do not lose
                # its completed/failed action history merely because scope is
                # unavailable; scope and execution evidence are independent.
                candidate_status = str(
                    getattr(candidate, "status", "") or ""
                )
                if candidate_scope or bool(
                    getattr(candidate, "binding_cursor", {})
                ) or candidate_status in {
                    "approved", "executing", "verifying", "paused", "blocked",
                }:
                    recovery_plan = candidate
                    recovery_scope = candidate_scope
        executed_predecessor = (
            self._refinement_predecessor(snapshot)
            if snapshot is not None else None
        )
        if recovery_plan is None and executed_predecessor is not None and str(
            getattr(executed_predecessor, "status", "") or ""
        ) in {"paused", "blocked", "executing", "verifying"}:
            recovery_plan = executed_predecessor
            recovery_scope = _authoritative_recovery_scope(executed_predecessor)
        if (
            option.action == "continue_current_goal"
            and recovery_plan is not None
            and str(failure.category or "").startswith("post_execution_")
        ):
            # The reported stage may be planning because the Replan call
            # itself raised, but the durable resume point is still the
            # executed plan's goal-verification loop.  Sending this case to
            # ordinary planning discards successful action/check evidence and
            # can propose executing the same effects again.
            self._emit_progress(
                "正在恢复已执行计划的目标终验，不会重复执行已完成动作"
            )
            return self._continue_post_execution(
                WorkflowResult(
                    True,
                    "goal_verification",
                    str(failure.technical_reason or failure.summary or ""),
                    plan=recovery_plan,
                ),
                snapshot=snapshot,
                conversation_context=conversation_context,
            )
        if (
            option.action == "continue_current_goal"
            and recovery_plan is not None
            and failure.stage == "binding"
            and failure.environment_changed == "false"
            and bool(getattr(recovery_plan, "binding_cursor", {}))
        ):
            # A Binding checkpoint is already the authoritative resume point.
            # Re-entering Discovery+Planner here would discard successful
            # atomic bindings and recreate the failure at a coarser level.
            bundle = (
                snapshot.evidence
                if snapshot is not None else EvidenceBundle(goal=goal)
            )
            if failure.evidence_requests:
                self._emit_progress(
                    "正在刷新当前 Binding 步骤明确依赖的运行态事实"
                )
                self.discovery.collect_requests(
                    [replace(item, freshness="refresh") for item in failure.evidence_requests],
                    bundle,
                )
            cursor = dict(recovery_plan.binding_cursor)
            self._emit_progress(
                "正在从 Binding 断点继续：语义步骤 %s，原子步骤 %s；"
                "不会重新运行 Planner"
                % (
                    cursor.get("semantic_step_id") or failure.semantic_step_id or "unknown",
                    cursor.get("atomic_step_index", failure.atomic_step_index),
                )
            )
            binding = self.mutation_workflow.bind_once(
                recovery_plan,
                evidence_bundle=bundle,
            )
            if binding.status == "needs_user_decision" and binding.plan is not None:
                return WorkflowResult(
                    True,
                    "awaiting_confirmation",
                    binding.user_question,
                    plan=binding.plan,
                    evidence=bundle,
                    outcome=binding,
                )
            if binding.status == "need_replan" and binding.replan_context.get(
                "resume_binding"
            ):
                resumed_plan = binding.candidate_plan or recovery_plan
                resumed_cursor = dict(
                    binding.replan_context.get("binding_cursor") or cursor
                )
                return self.mutation_workflow.failure_result(
                    stage="binding",
                    category=str(
                        binding.replan_context.get("failure_category")
                        or "binding_provider_transient"
                    ),
                    summary="实施绑定恢复仍未成功，断点已保留。",
                    technical_reason=binding.reason,
                    goal=goal,
                    goal_kind="execution",
                    plan=resumed_plan,
                    attempted_recoveries=["从持久化 Binding 断点原地恢复"],
                    failed_checks=list(binding.failed_criteria),
                    missing_decisions=list(binding.missing_decisions),
                    evidence=bundle,
                    semantic_step_id=str(
                        resumed_cursor.get("semantic_step_id") or ""
                    ),
                    atomic_step_index=int(
                        resumed_cursor.get("atomic_step_index", -1)
                    ),
                )
            # A deterministic semantic/evidence rejection invalidates only the
            # affected step; fall through to the ordinary scoped Replan loop.
        if (
            option.action == "continue_current_goal"
            and recovery_plan is not None
        ):
            retry_unchanged = getattr(
                self.mutation_workflow,
                "retry_unchanged_paused_action",
                None,
            )
            retried = (
                retry_unchanged(recovery_plan.plan_id)
                if callable(retry_unchanged) else None
            )
            if retried is not None:
                self._emit_progress(
                    "失败动作已证明未改变环境；正在原审批范围内确定性续跑"
                )
                if retried.kind in {"paused", "goal_verification"}:
                    return self._continue_post_execution(
                        retried,
                        snapshot=snapshot,
                        conversation_context=conversation_context,
                    )
                return retried
        seed = snapshot.reusable_evidence(goal) if snapshot is not None else None
        begin = getattr(self.discovery, "begin_probe_session", None)
        end = getattr(self.discovery, "end_probe_session", None)
        if begin is not None:
            begin()
        current_stage = "discovery"
        try:
            goal_kind = failure.goal_kind
            readonly_recovery = (
                goal_kind != "execution"
                and not str(getattr(failure, "plan_id", "") or "")
                and failure.stage in {"discovery", "synthesis", "verification"}
                and option.action == "continue_current_goal"
            )
            self._emit_progress("正在从 Discovery 阶段恢复原目标并刷新运行态证据")
            decision_context = "\n".join(
                "- %s" % item for item in decisions
            ) or "（无）"
            collection_goal = (
                "只读诊断并补齐以下运维目标所需证据：%s" % goal
                if readonly_recovery and goal_kind == "causal_diagnosis"
                else "%s\n\n已确认的用户决策（不得缩小基础目标）：\n%s"
                % (goal, decision_context)
            )
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
            bundle = self.discovery.collect(collection_goal, **collect_kwargs)
            if failure.evidence_requests:
                self._emit_progress(
                    "正在按失败记录中的具体证据缺口执行定向补证"
                )
                refresh_requests = [
                    replace(item, freshness="refresh")
                    for item in failure.evidence_requests
                ]
                self.discovery.collect_requests(refresh_requests, bundle)
            if (
                recovery_plan is not None
                and str(getattr(recovery_plan, "status", "") or "")
                in {"paused", "blocked"}
                and not any(
                    item.request.probe == "plan_execution"
                    for item in getattr(bundle, "records", []) or []
                )
            ):
                from klonet_agent.ops.privileged.workflow.contracts import (
                    EvidenceRecord, ProbeRequest,
                )

                bundle.add(EvidenceRecord.from_probe(
                    ProbeRequest(
                        "plan_execution",
                        {
                            "plan_id": str(recovery_plan.plan_id),
                            "status": str(recovery_plan.status),
                        },
                        "失败计划的已持久化执行与验证结果",
                    ),
                    _paused_plan_evidence(WorkflowResult(
                        True,
                        "paused",
                        str(failure.technical_reason or failure.summary or ""),
                        plan=recovery_plan,
                    )),
                ))
            current_stage = "synthesis"
            conclusion = self.synthesis.synthesize(collection_goal, bundle)
            if readonly_recovery:
                current_stage = "verification"
                result = self._run_readonly_goal_loop(
                    snapshot,
                    resolved_goal=goal,
                    collection_goal=collection_goal,
                    bundle=bundle,
                    conclusion=conclusion,
                    goal_kind=goal_kind,
                )
                return result
            self._emit_progress("证据补充完成，正在重新规划")
            identity = RuntimeInventory.from_bundle(bundle).resolve_identity(goal)
            intent_context: dict[str, Any] = {
                "recovery_failure_stage": failure.stage,
                "recovery_failure_category": failure.category,
                "recovery_technical_reason": failure.technical_reason,
                "recovery_attempted_paths": list(failure.attempted_recoveries),
                "rejected_evidence_need_keys": [
                    item.need_key for item in failure.evidence_requests
                ],
                **recovery_scope,
            }
            if snapshot is not None:
                intent_context.update({
                    "operation": snapshot.operation,
                    "scope": snapshot.scope,
                    "components": list(snapshot.components),
                })
            intent_context.update({
                "base_goal": goal,
                "decision_history": decisions,
                "user_reply_relation": relation,
            })
            if user_direction.strip():
                self._emit_progress(
                    "已收到目标补充，基础目标保持不变，正在重新规划"
                )
            if identity is not None:
                intent_context.update(identity.to_dict())
                intent_context["resolved_project_root"] = identity.project_root
            current_stage = "planning"
            result = self._submit_mutation(
                goal,
                evidence_bundle=bundle,
                evidence_conclusion=conclusion,
                intent_context=intent_context,
                initial_candidate_plan=recovery_plan,
                initial_active_gap_steps=list(
                    recovery_scope.get("recovery_required_step_ids") or []
                ),
            )
            self._save_context(
                snapshot,
                resolved_goal=goal,
                bundle=bundle,
                active_plan_id=(
                    str(getattr(result.plan, "plan_id", "") or "")
                    if result.kind not in {"completed", "aborted"}
                    else ""
                ),
                operation=None,
                scope=None,
                components=None,
                base_goal=goal,
                decision_history=decisions,
            )
            return result
        except Exception as exc:
            return self.mutation_workflow.failure_result(
                stage=current_stage,
                category="recovery_%s_failure" % current_stage,
                summary="所选恢复路径执行失败，目标仍未完成。",
                technical_reason="%s: %s" % (type(exc).__name__, exc),
                goal=goal,
                goal_kind=goal_kind,
                plan_id=failure.plan_id,
                attempted_recoveries=[
                    *list(failure.attempted_recoveries),
                    "执行人工选择的恢复方案：%s" % option.label,
                ],
                evidence=(bundle if "bundle" in locals() else seed),
            )
        finally:
            if end is not None:
                end()

    def _save_context(
        self,
        previous: OperationalContextSnapshot | None,
        *,
        resolved_goal: str,
        bundle: Any,
        active_plan_id: str = "",
        workflow_intent: str | None = None,
        goal_kind: str | None = None,
        operation: str | None = None,
        scope: str | None = None,
        components: list[str] | None = None,
        base_goal: str | None = None,
        decision_history: list[str] | None = None,
        pending_goal_revision: str | None = None,
        pending_goal_relation: str | None = None,
    ) -> OperationalContextSnapshot | None:
        if self.context_store is None:
            return previous
        roots = sorted(set(re.findall(r"/[A-Za-z0-9._/-]+", resolved_goal)))
        for instance in RuntimeInventory.from_bundle(bundle).matching(resolved_goal):
            if instance.project_root not in roots:
                roots.append(instance.project_root)
        same_goal = previous is not None and previous.resolved_goal == resolved_goal
        saved = OperationalContextSnapshot(
            resolved_goal=resolved_goal,
            base_goal=(
                str(base_goal)
                if base_goal is not None
                else previous.base_goal
                if previous is not None and (same_goal or previous.base_goal == resolved_goal)
                else resolved_goal
            ),
            decision_history=(
                list(decision_history)
                if decision_history is not None
                else list(previous.decision_history)
                if previous is not None and (same_goal or previous.base_goal == resolved_goal)
                else []
            ),
            pending_goal_revision=(
                str(pending_goal_revision)
                if pending_goal_revision is not None
                else previous.pending_goal_revision if previous is not None else ""
            ),
            pending_goal_relation=(
                str(pending_goal_relation)
                if pending_goal_relation is not None
                else previous.pending_goal_relation if previous is not None else ""
            ),
            active_plan_id=active_plan_id,
            workflow_intent=(
                workflow_intent
                if workflow_intent is not None
                else previous.workflow_intent if same_goal else ""
            ),
            goal_kind=(
                goal_kind
                if goal_kind is not None
                else previous.goal_kind if same_goal else ""
            ),
            operation=(
                operation
                if operation is not None
                else previous.operation if same_goal else "none"
            ),
            scope=(
                scope
                if scope is not None
                else previous.scope if same_goal else "none"
            ),
            components=(
                list(components)
                if components is not None
                else list(previous.components) if same_goal else []
            ),
            output_locale=(previous.output_locale if previous is not None else "zh-CN"),
            target_roots=roots,
            evidence=bundle,
        )
        self.context_store.save(saved)
        return saved

    def _supersede_active_plan(
        self, snapshot: OperationalContextSnapshot,
    ) -> bool:
        plan_id = str(snapshot.active_plan_id or "")
        snapshot.active_plan_id = ""
        if not plan_id:
            return False
        abort = getattr(self.mutation_workflow, "abort_plan", None)
        return bool(abort is not None and abort(plan_id))

    def _refinement_predecessor(
        self, snapshot: OperationalContextSnapshot,
    ) -> Any | None:
        """Load the one persisted plan whose effects a refinement succeeds."""

        load_plan = getattr(self.mutation_workflow, "load_plan", None)
        if not callable(load_plan):
            return None
        plan_ids: list[str] = []
        for record in reversed(list(snapshot.evidence.records or [])):
            request = getattr(record, "request", None)
            if str(getattr(request, "probe", "") or "") != "plan_execution":
                continue
            plan_id = str((getattr(request, "args", {}) or {}).get("plan_id") or "")
            if plan_id and plan_id not in plan_ids:
                plan_ids.append(plan_id)
        active = str(snapshot.active_plan_id or "")
        if active and active not in plan_ids:
            plan_ids.append(active)
        for plan_id in plan_ids:
            try:
                candidate = load_plan(plan_id)
            except (KeyError, ValueError):
                continue
            if candidate is not None:
                return candidate
        return None

    def _resolve_plan_reference(
        self,
        reference: str,
        snapshot: OperationalContextSnapshot | None,
    ) -> str:
        """Accept a classifier-proposed Plan route only when state can bind it."""

        plan_id = ""
        if reference == "latest" and snapshot is not None:
            plan_id = str(snapshot.active_plan_id or "")
            if not plan_id:
                plan_id = self._latest_plan_id_for_goal(snapshot.resolved_goal)
        else:
            plan_id = str(reference or "")
        if not plan_id or plan_id == "latest":
            return ""
        exists = getattr(self.mutation_workflow, "plan_exists", None)
        if exists is not None and not bool(exists(plan_id)):
            return ""
        return plan_id

    def _latest_plan_id_for_goal(self, goal: str) -> str:
        resolve = getattr(
            self.mutation_workflow, "latest_plan_id_for_goal", None,
        )
        return str(resolve(goal) or "") if resolve is not None else ""

    def _emit_route(self, route: str) -> None:
        """Expose the validated route before entering a long-running stage."""

        self._emit_progress("已识别：%s" % route)

    def _emit_progress(self, message: str) -> None:
        """Expose a workflow transition without changing its semantic route."""

        callback = getattr(self, "on_progress", None)
        if callback is not None:
            callback(message)

    def _clear_context(
        self, previous: OperationalContextSnapshot | None,
    ) -> None:
        if self.context_store is None:
            return
        self.context_store.save(OperationalContextSnapshot(
            output_locale=(previous.output_locale if previous is not None else "zh-CN"),
            evidence=EvidenceBundle(goal=""),
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


def _refined_goal(previous_goal: str, current_request: str) -> str:
    """Preserve the resolved target while adding a semantic refinement."""

    previous = str(previous_goal or "").strip()
    current = str(current_request or "").strip()
    if not previous:
        return current
    if not current or current in previous:
        return previous
    return "%s；进一步要求：%s" % (previous, current)


def _authoritative_base_goal(snapshot: OperationalContextSnapshot) -> str:
    """Return the immutable goal, repairing the old refinement serialization."""

    value = str(snapshot.base_goal or snapshot.resolved_goal or "").strip()
    # Older snapshots stored the rendered ``base；进一步要求：decision`` text
    # back into base_goal.  The delimiter is Coordinator-owned, so removing its
    # suffix restores the original authority without interpreting user prose.
    if "；进一步要求：" in value:
        value = value.split("；进一步要求：", 1)[0].strip()
    return value


def _structured_goal_semantics_changed(
    snapshot: OperationalContextSnapshot,
    decision: Any,
) -> bool:
    """Reject a continuation that contradicts persisted typed semantics.

    Empty/``none`` values are unspecified and therefore inherit the previous
    value.  This keeps legacy snapshots and pure retries working while making
    a classifier-proposed relation subordinate to the existing goal model.
    """

    comparisons = (
        (snapshot.workflow_intent, getattr(decision, "intent", ""), {""}),
        (snapshot.goal_kind, getattr(decision, "goal_kind", ""), {""}),
        (snapshot.operation, getattr(decision, "operation", ""), {"", "none"}),
        (snapshot.scope, getattr(decision, "scope", ""), {"", "none"}),
    )
    for previous, current, unspecified in comparisons:
        previous_value = str(previous or "").strip().lower()
        current_value = str(current or "").strip().lower()
        if previous_value in unspecified or current_value in unspecified:
            continue
        if previous_value != current_value:
            return True

    current_components = {
        str(item).strip().lower()
        for item in getattr(decision, "components", ()) or ()
        if str(item).strip()
    }
    previous_scope = str(snapshot.scope or "").strip().lower()
    if current_components and (
        previous_scope not in {"", "none"} or snapshot.components
    ):
        previous_components = {
            str(item).strip().lower()
            for item in snapshot.components
            if str(item).strip()
        }
        if current_components != previous_components:
            return True
    return False


def _authoritative_recovery_scope(plan: Any) -> dict[str, Any]:
    """Derive unfinished target scope from the persisted failed plan.

    The plan already owns approved instance identity.  Recovery therefore
    reuses it instead of creating a second target-state model or allowing a
    fresh runtime inventory to redefine which targets still belong to the
    goal.
    """

    plan_steps = list(getattr(plan, "steps", []) or [])
    if all(
        str(getattr(step, "status", "pending") or "pending") == "pending"
        and int(getattr(step, "execution_attempts", 0) or 0) == 0
        for step in plan_steps
    ):
        return {}
    unfinished = {
        str(getattr(step, "step_id", "") or "")
        for step in plan_steps
        if str(getattr(step, "status", "pending") or "pending")
        not in {"completed", "skipped"}
        and str(getattr(step, "step_id", "") or "")
    }
    roots: list[str] = []
    root_by_step: dict[str, str] = {}
    for resource in list(getattr(plan, "resources", []) or []):
        if str(getattr(resource, "role", "") or "") != "instance_root":
            continue
        consumers = [
            str(item) for item in getattr(resource, "consumers", []) or []
        ]
        if not any(
            consumer.split(".", 1)[0] in unfinished
            for consumer in consumers
        ):
            continue
        value = str(getattr(resource, "value", "") or "").rstrip("/")
        if value.startswith("/") and value not in roots:
            roots.append(value)
        for consumer in consumers:
            owner = consumer.split(".", 1)[0]
            if owner in unfinished and value.startswith("/"):
                root_by_step[owner] = value
    completed_components: dict[str, list[str]] = {}
    for change in list(getattr(plan, "steps", []) or []):
        step_id = str(getattr(change, "step_id", "") or "")
        root = root_by_step.get(step_id, "")
        if not root:
            continue
        implementation = getattr(change, "implementation_plan", None)
        atomic_steps = list(getattr(implementation, "steps", []) or [])
        for atomic in atomic_steps:
            if str(getattr(atomic, "status", "") or "") != "completed":
                continue
            binding = getattr(atomic, "execution_binding", None)
            if str(getattr(binding, "action", "") or "") not in {
                "start_screen_component", "restart_screen_component",
            }:
                continue
            component = str(
                (getattr(binding, "args", {}) or {}).get("component") or ""
            ).strip()
            if component:
                completed_components.setdefault(root, []).append(component)
    identifiers_by_root: dict[str, str] = {}
    for resource in list(getattr(plan, "resources", []) or []):
        if str(getattr(resource, "role", "") or "") != "instance_identifier":
            continue
        value = str(getattr(resource, "value", "") or "").strip()
        if not value:
            continue
        for consumer in list(getattr(resource, "consumers", []) or []):
            owner = str(consumer).split(".", 1)[0]
            root = root_by_step.get(owner, "")
            if owner in unfinished and root:
                identifiers_by_root[root] = value
    if not unfinished or not roots:
        return {}
    return {
        "recovery_source_plan_id": str(getattr(plan, "plan_id", "") or ""),
        "recovery_scope_authoritative": True,
        "recovery_required_step_ids": sorted(unfinished),
        "recovery_required_project_roots": roots,
        "recovery_completed_components_by_root": {
            root: sorted(set(components))
            for root, components in completed_components.items()
        },
        "recovery_instance_identifiers_by_root": dict(identifiers_by_root),
    }


def _available_evidence_state(bundle: Any) -> dict[str, str] | None:
    """Return usable fact fingerprints, including refreshed volatile content."""

    records = getattr(bundle, "records", None)
    if records is None:
        return None
    state: dict[str, str] = {}
    for record in records:
        request = getattr(record, "request", None)
        cache_key = getattr(request, "cache_key", None)
        if request is None or cache_key is None:
            return None
        if str(getattr(record, "status", "available") or "") == "available":
            output = str(getattr(record, "output", "") or "")
            state[str(cache_key)] = hashlib.sha256(
                output.encode("utf-8", errors="replace")
            ).hexdigest()
    return state


def _evidence_progress_state(
    bundle: Any,
    requests: list[Any],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]] | None:
    """Measure progress only by active fact ids, never by output volume."""

    records = getattr(bundle, "records", None)
    if records is None:
        return None
    state: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for request in requests:
        requirements = list(getattr(request, "required_facts", ()) or ())
        if not requirements:
            # Older/control requests without a fact contract retain raw-output
            # progress semantics, but cannot resolve a structured gap.
            raw = _available_evidence_state(bundle)
            if raw is None:
                return None
            state[str(getattr(request, "need_key", ""))] = (
                tuple(sorted(raw.values())), (),
            )
            continue
        fact_ids = {str(item.fact_id) for item in requirements}
        latest: dict[str, str] = {}
        need_key = str(getattr(request, "need_key", ""))
        for record in records:
            record_request = getattr(record, "request", None)
            if str(getattr(record_request, "need_key", "")) != need_key:
                continue
            for observation in getattr(record, "observations", ()) or ():
                if str(getattr(observation, "fact_id", "")) in fact_ids:
                    latest[str(observation.fact_id)] = str(observation.status)
        unresolved = tuple(sorted(
            fact_id for fact_id in fact_ids
            if latest.get(fact_id) not in {"confirmed", "contradicted"}
        ))
        contradicted = tuple(sorted(
            fact_id for fact_id in fact_ids
            if latest.get(fact_id) == "contradicted"
        ))
        state[need_key] = (unresolved, contradicted)
    return state


def _evidence_progress_summary(
    before: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] | None,
    after: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] | None,
    requests: list[Any],
) -> str:
    """Render the exact gap/fact delta accepted as Replan progress."""

    before = before or {}
    after = after or {}
    summaries = []
    for request in requests:
        requirements = list(getattr(request, "required_facts", ()) or ())
        if not requirements:
            continue
        gap_id = str(getattr(request, "need_key", "") or "unknown-gap")
        before_state = before.get(gap_id, ((), ()))
        after_state = after.get(gap_id, ((), ()))
        resolved = sorted(set(before_state[0]) - set(after_state[0]))
        remaining = sorted(set(after_state[0]))
        if not resolved and before_state == after_state:
            continue
        summaries.append(
            "gap=%s 已解决=%s 仍未解决=%s"
            % (
                gap_id,
                ",".join(resolved) or "none",
                ",".join(remaining) or "none",
            )
        )
    return "；".join(summaries) or "结构化证据状态已更新"


def _evidence_request_summary(requests: list[Any]) -> str:
    rendered = []
    for request in requests:
        facts = list(getattr(request, "required_facts", ()) or ())
        rendered.append(
            "%s（%s）"
            % (
                str(getattr(request, "probe", "unknown") or "unknown"),
                "、".join(
                    "%s[%s]" % (item.predicate, item.fact_id)
                    for item in facts
                )
                or str(getattr(request, "purpose", "补齐目标事实") or "补齐目标事实"),
            )
        )
    return "；".join(rendered) or "未说明的目标事实"


def _deployment_boundary_gaps(text: str) -> list[str]:
    """Return user decisions that cannot be discovered from the host."""

    value = str(text or "")
    path = r"/[A-Za-z0-9_.+@%=-]+(?:/[A-Za-z0-9_.+@%=-]+)+"
    explicit_name = bool(re.search(
        r"(?:平台(?:实例)?名|实例名|instance\s+name)\s*(?:固定)?\s*[:：=是为]?\s*"
        r"[A-Za-z0-9_.-]{2,64}",
        value,
        re.I,
    ))
    target_match = re.search(
        r"(?:目标(?:实例)?(?:根)?目录|部署目录|实例目录|target(?:\s+(?:directory|path))?)"
        r"\s*(?:固定)?\s*[:：=是为]?\s*`?" + path,
        value,
        re.I,
    ) or re.search(
        r"(?:创建|新建|部署|create|deploy)[^\n。；;]{0,40}"
        r"(?:到|至|at|to)\s*`?" + path,
        value,
        re.I,
    )
    has_target = bool(target_match)
    # An exact deployment root is itself a stable instance identity; Planner
    # derives its display alias from that root when no separate name is given.
    has_name = explicit_name or has_target
    has_source = bool(
        extract_labeled_deployment_paths(value).get("source_directory")
        or re.search(r"sync_directory[^\n。；;]{0,32}从\s*`?" + path, value, re.I)
        or re.search(r"(?:git@|https?://|ssh://)\S+", value, re.I)
    )
    return [
        label for present, label in (
            (has_name, "平台名"),
            (has_target, "目标目录"),
            (has_source, "源码来源或模板目录"),
        )
        if not present
    ]


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
        "plan_environment_changed=%s" % _paused_plan_environment_changed(plan),
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
            execution_output = " ".join(
                (
                    "%s %s"
                    % (
                        str(getattr(evidence, "stdout", "") or ""),
                        str(getattr(evidence, "stderr", "") or ""),
                    )
                ).split()
            )[:1000]
            lines.append(
                "step=%s status=%s attempts=%s return_code=%s timed_out=%s "
                "environment_changed=%s observation=%s execution_output=%s"
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
                    execution_output,
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


def _paused_plan_environment_changed(plan: Any) -> str:
    """Aggregate persisted execution evidence without inventing state."""

    states: list[str] = []
    for change in list(getattr(plan, "steps", []) or []):
        implementation = getattr(change, "implementation_plan", None)
        steps = (
            list(getattr(implementation, "steps", []) or [])
            if implementation is not None
            else [change]
        )
        for step in steps:
            evidence = getattr(step, "evidence", None)
            if evidence is None:
                continue
            value = getattr(evidence, "environment_changed", None)
            states.append(
                "true" if value is True else "false" if value is False else "unknown"
            )
    if "unknown" in states:
        return "unknown"
    if "true" in states:
        return "true"
    return "false" if states else "unknown"
