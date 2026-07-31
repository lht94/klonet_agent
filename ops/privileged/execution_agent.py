"""Implementation Binding Agent: compile semantic steps into safe contracts."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from klonet_agent.ops.actions import (
    OpsActionRegistry,
    configured_ops_action_registry,
)
from klonet_agent.ops.command_policy import command_exists, decide_ops_command
from klonet_agent.ops.privileged.action_runner import DIRECT_PRIVILEGED_ACTIONS
from klonet_agent.ops.privileged.checkers import (
    CHECKER_REQUIRED_ARGS,
    DefaultCheckerRegistry,
    infer_postconditions,
)
from klonet_agent.ops.privileged.contracts import (
    ExecutionBinding,
    ImplementationPlan,
    PrivilegedPlan,
    PrivilegedStep,
    RISK_LEVELS,
    ShellArtifact,
)
from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.planner import (
    _action_risk,
    _default_action_postconditions,
    _effective_declared_risk,
    _parse_json_object,
    _validate_action_semantics,
    _validate_environment_model,
    _validate_host_facts,
)
from klonet_agent.ops.privileged.planner_schema import REQUIRED_ACTION_ARGS
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.shell_artifact import (
    MAX_SCRIPT_BYTES,
    MAX_SCRIPT_LINES,
    ShellArtifactPolicy,
    create_shell_artifact,
)


EXECUTION_SELECTION_PROMPT = """
You are stage 1 of the Klonet Implementation Binding Agent. Select exactly one
implementation capability for one frozen semantic step. Do not generate Action
arguments, shell code, checker contracts, or a revised semantic plan.

Return one JSON object with status exactly:
- registered_action:
  {"status":"registered_action","action":"<exact allowed name>",
   "selection_reason":"...","resolved_from_evidence":[]}
- shell_artifact, only when no registered Action covers the objective:
  {"status":"shell_artifact","selection_reason":"...",
   "resolved_from_evidence":[]}
- verification_only, when the semantic step only observes post-execution state
  and needs registered checkers but no state-changing Action or shell command.
- need_evidence: include at most 3 registered read-only probe_requests.
- blocked: {"status":"blocked","reason":"..."}, only when no registered
  Action, safe one-time shell artifact, or verification-only checker contract
  can implement the unchanged semantic objective.

Prefer a registered Action only when its declared capability actually covers
the objective. The absence of a matching registered Action is not by itself a
reason to return blocked: select shell_artifact when a bounded, reviewable
one-time shell implementation is possible. Never invent Action names. All
implementation parameters will be generated and validated by a separate
stage 2 call.
""".strip()

MAX_BINDING_PROBE_ROUNDS = 2
MAX_BINDING_INVALID_REPAIRS = 2
MAX_ACTION_CONTRACT_REPAIRS = 2
MAX_SHELL_CONTRACT_REPAIRS = 2
MAX_SHELL_VERIFICATION_REPAIRS = 2
MAX_IMPLEMENTATION_RESELECTIONS = 2
MAX_IMPLEMENTATION_PLAN_REBUILDS = 2
MAX_BINDING_GROUNDING_SECTION_CHARS = 12000


SHELL_VERIFICATION_PROMPT = """
You complete the verification contract for an already frozen Klonet one-time
shell artifact. You may not rewrite, replace, extend, or reinterpret the shell
script. Select deterministic postconditions from the exact registered checker
catalog that prove the semantic step's observable success criteria.

Return exactly one JSON object with status:
- ready: include postconditions, each as {"checker":"...","args":{...}}.
- blocked: include reason when the frozen script cannot be verified using the
  registered checkers and grounded values.

Every required checker argument must be present and grounded in the supplied
semantic step or frozen artifact. For a mutating shell artifact,
exit_code_zero alone is not proof of the resulting state. Do not return a
script or any other replacement implementation.
""".strip()


VERIFICATION_ONLY_PROMPT = """
You bind a frozen verification-only semantic step. It must execute no Action
and no shell command. Select deterministic postconditions from the exact
registered checker catalog that directly prove the supplied success criteria.

Return exactly one JSON object with status:
- ready: include binding_reason, resolved_from_evidence, and postconditions.
- blocked: include reason when the goal cannot be proven by registered
  checkers with grounded arguments.

Do not return an Action, command, shell script, precondition, or probe request.
exit_code_zero is not valid evidence for a verification-only step.
""".strip()


IMPLEMENTATION_PLANNING_PROMPT = """
You are the Klonet Implementation Planner. Expand exactly one frozen semantic
step into a small ordered implementation plan. Preserve the parent objective;
do not revise the outer semantic plan.

Each implementation step must be atomic enough to bind to exactly one
registered Action, one safe shell artifact, or one verification_only contract.
Do not create standalone steps whose only purpose is to discover, determine,
locate, or choose arguments for a later step. The Binding Agent performs such
read-only probes internally. Include a read-only step only when it verifies an
observable success criterion through a registered checker. Every other step
must describe one observable state change. Express dependencies between
implementation steps, but do not emit Action names, commands, shell, paths,
ports, or other concrete arguments at this stage. Later binding calls will
ground those details.

Return status=ready with 1-12 implementation_steps. Each step needs id, title,
objective, reason, depends_on, expected_changes, success_criteria, and
risk_suggestion. Verification steps must be provable by the supplied exact
registered checker catalog and must not require secret discovery. Dependencies
may reference only earlier step ids. A dependency's expected changes are future
facts at execution time: do not add current-state probes that require those
effects to exist before their producing step runs. Return status=blocked only
when the frozen semantic objective itself cannot be implemented or safely
decomposed with the supplied evidence.
""".strip()


ACTION_CONTRACT_PROMPT = """
You complete the argument contract for one already selected registered Klonet
Action. The Action name and semantic objective are frozen. Return exactly one
JSON object with status:
- ready: include args, binding_reason, resolved_from_evidence, and optional
  registered checker preconditions/postconditions.
- blocked: include reason if the required Action arguments cannot be grounded.

Use the exact required argument names and grounded evidence supplied. Do not
change the Action, return a shell script, request probes, or alter the semantic
objective.
""".strip()


SHELL_CONTRACT_PROMPT = """
You are stage 2 of the Klonet Implementation Binding Agent. The semantic step
and the decision to use a one-time shell artifact are frozen. Generate only the
complete shell execution contract for this one step.

Return exactly one JSON object with status:
- ready: include script, cwd, run_as, timeout, declared_changes, rollback,
  binding_reason, resolved_from_evidence, and registered postconditions.
- blocked: include reason if a safe, grounded and verifiable shell contract
  cannot implement the frozen objective.

The script must use ordinary bash, be non-interactive, produce observable
state, contain at most {max_lines} lines, and occupy at most {max_bytes} UTF-8
bytes. Prefer compact commands and heredocs when writing configuration. Do not
use eval, source, command substitution, background execution, dynamic
download-and-execute, unbounded deletion, or changes to sudoers, SSH, or Agent
security policy. Every checker and required argument must come from the
supplied checker catalog. Do not return an Action or alter the objective.
""".strip()
SHELL_CONTRACT_PROMPT = SHELL_CONTRACT_PROMPT.format(
    max_lines=MAX_SCRIPT_LINES,
    max_bytes=MAX_SCRIPT_BYTES,
)


class ExecutionBindingError(Exception):
    """A binding failure annotated with whether semantic replanning can help."""

    def __init__(
        self,
        message: str,
        *,
        replan_recommended: bool = True,
        category: str = "semantic_binding",
    ) -> None:
        super().__init__(message)
        self.replan_recommended = replan_recommended
        self.category = category


class ImplementationRejected(Exception):
    """The selected implementation cannot realize the frozen semantic step."""

    def __init__(self, implementation: str, reason: str) -> None:
        super().__init__(reason)
        self.implementation = implementation


class PrivilegedExecutionAgent:
    def __init__(
        self,
        llm: Any,
        *,
        action_registry: OpsActionRegistry | None = None,
        probe_runner: Callable[[list[dict[str, Any]]], str] | None = None,
        shell_policy: ShellArtifactPolicy | None = None,
        on_progress: Callable[[str], None] | None = None,
        enable_implementation_plans: bool = True,
    ) -> None:
        self.llm = llm
        self.action_registry = (
            action_registry or configured_ops_action_registry()
        )
        self.probe_runner = probe_runner
        self.shell_policy = shell_policy or ShellArtifactPolicy()
        self.checkers = DefaultCheckerRegistry()
        self.on_progress = on_progress
        self.enable_implementation_plans = bool(enable_implementation_plans)

    def prepare_plan(
        self,
        plan: PrivilegedPlan,
        *,
        grounded_context: GroundedPlanContext | None,
    ) -> PrivilegedPlan:
        if self.enable_implementation_plans:
            return self._prepare_hierarchical_plan(
                plan,
                grounded_context=grounded_context,
            )
        for index, step in enumerate(plan.steps, start=1):
            self._progress(
                "实现节点 %s/%s：正在为“%s”匹配注册 Action 或一次性脚本…"
                % (
                    index,
                    len(plan.steps),
                    _progress_text(step.title or step.objective),
                )
            )
            step.execution_binding = self.prepare_step(
                plan,
                step,
                grounded_context=grounded_context,
            )
            binding = step.execution_binding
            step.risk = binding.risk
            step.approval_scope = binding.approval_scope
            step.preconditions = list(binding.preconditions)
            step.postconditions = list(binding.postconditions)
            step.timeout = (
                binding.shell_artifact.timeout
                if binding.shell_artifact is not None
                else step.timeout
            )
            step.status = (
                "awaiting_confirmation"
                if binding.approval_scope == "step"
                else "pending"
            )
        if grounded_context is not None:
            registered_steps = []
            for step in plan.steps:
                binding = step.execution_binding
                if binding is None or binding.kind != "registered_action":
                    continue
                registered_steps.append(
                    PrivilegedStep(
                        step_id=step.step_id,
                        title=step.title,
                        objective=step.objective,
                        reason=step.reason,
                        depends_on=list(step.depends_on),
                        action=binding.action,
                        args=dict(binding.args),
                        risk=binding.risk,
                    )
                )
            # Environment consistency is a property of the complete route.  A
            # start step may be valid only because an earlier semantic step
            # prepares the runtime directory, so validating it in isolation
            # would incorrectly turn the old workflow order into a rule.
            _validate_environment_model(registered_steps, grounded_context)
        plan.risk = max(
            (step.risk for step in plan.steps),
            key=RISK_LEVELS.index,
        )
        plan.verification_level = (
            "partial"
            if any(
                not step.postconditions
                or all(
                    item.get("checker") == "exit_code_zero"
                    for item in step.postconditions
                )
                for step in plan.steps
                if step.risk != "readonly"
            )
            else "full"
        )
        plan.status = "awaiting_confirmation"
        return plan

    def _prepare_hierarchical_plan(
        self,
        plan: PrivilegedPlan,
        *,
        grounded_context: GroundedPlanContext | None,
    ) -> PrivilegedPlan:
        bound_micro_steps: list[PrivilegedStep] = []
        for index, semantic_step in enumerate(plan.steps, start=1):
            self._progress(
                "语义步骤 %s/%s：正在把“%s”展开为原子 Implementation Plan…"
                % (
                    index,
                    len(plan.steps),
                    _progress_text(
                        semantic_step.title or semantic_step.objective
                    ),
                )
            )
            feedback = ""
            last_error = "implementation plan was not generated"
            for attempt in range(MAX_IMPLEMENTATION_PLAN_REBUILDS):
                try:
                    micro_steps = self._decompose_semantic_step(
                        semantic_step,
                        grounded_context=grounded_context,
                        feedback=feedback,
                    )
                    for micro_index, micro_step in enumerate(
                        micro_steps,
                        start=1,
                    ):
                        self._progress(
                            "实现子步骤 %s/%s：正在为“%s”绑定原子能力…"
                            % (
                                micro_index,
                                len(micro_steps),
                                _progress_text(
                                    micro_step.title or micro_step.objective
                                ),
                            )
                        )
                        binding = self.prepare_step(
                            plan,
                            micro_step,
                            grounded_context=grounded_context,
                        )
                        _apply_binding_to_step(micro_step, binding)
                except ExecutionBindingError as exc:
                    last_error = str(exc)
                    if attempt + 1 >= MAX_IMPLEMENTATION_PLAN_REBUILDS:
                        raise ExecutionBindingError(
                            "Implementation Plan 在局部重建后仍无法绑定：%s"
                            % last_error,
                            replan_recommended=True,
                            category="implementation_plan_unavailable",
                        ) from exc
                    feedback = (
                        "The previous implementation decomposition could not be"
                        " bound. Rebuild the micro-plan without changing the"
                        " semantic objective. Binding failure: %s" % last_error
                    )
                    self._progress(
                        "Implementation Plan 的原子步骤无法落地，正在保持语义目标"
                        "不变并进行第 %s/%s 次局部重建。"
                        % (attempt + 2, MAX_IMPLEMENTATION_PLAN_REBUILDS)
                    )
                    continue
                implementation = ImplementationPlan(
                    implementation_id="impl-%s-%s"
                    % (semantic_step.step_id, uuid.uuid4().hex[:8]),
                    semantic_step_id=semantic_step.step_id,
                    objective=semantic_step.objective or semantic_step.title,
                    steps=micro_steps,
                    status="awaiting_confirmation",
                )
                semantic_step.execution_binding = None
                semantic_step.implementation_plan = implementation
                semantic_step.risk = max(
                    (item.risk for item in micro_steps),
                    key=RISK_LEVELS.index,
                )
                semantic_step.approval_scope = "plan"
                semantic_step.status = "pending"
                bound_micro_steps.extend(micro_steps)
                break
            else:
                raise ExecutionBindingError(last_error)

        if grounded_context is not None:
            registered_steps = []
            for micro_step in bound_micro_steps:
                binding = micro_step.execution_binding
                if binding is None or binding.kind != "registered_action":
                    continue
                registered_steps.append(
                    PrivilegedStep(
                        step_id=micro_step.step_id,
                        title=micro_step.title,
                        objective=micro_step.objective,
                        reason=micro_step.reason,
                        depends_on=list(micro_step.depends_on),
                        action=binding.action,
                        args=dict(binding.args),
                        risk=binding.risk,
                    )
                )
            _validate_environment_model(registered_steps, grounded_context)
        plan.risk = max(
            (step.risk for step in plan.steps),
            key=RISK_LEVELS.index,
        )
        plan.verification_level = (
            "partial"
            if any(
                not step.postconditions
                or all(
                    item.get("checker") == "exit_code_zero"
                    for item in step.postconditions
                )
                for step in bound_micro_steps
                if step.risk != "readonly"
            )
            else "full"
        )
        plan.status = "awaiting_confirmation"
        return plan

    def _decompose_semantic_step(
        self,
        semantic_step: PrivilegedStep,
        *,
        grounded_context: GroundedPlanContext | None,
        feedback: str = "",
    ) -> list[PrivilegedStep]:
        payload = {
            "semantic_step": {
                "step_id": semantic_step.step_id,
                "title": semantic_step.title,
                "objective": semantic_step.objective,
                "reason": semantic_step.reason,
                "depends_on": semantic_step.depends_on,
                "expected_changes": semantic_step.expected_changes,
                "success_criteria": semantic_step.success_criteria,
                "risk_suggestion": semantic_step.risk,
            },
            "grounded_context": _selection_grounded_context(
                grounded_context
            ),
            "registered_checker_catalog": self.checkers.render_catalog(),
            "previous_attempt_feedback": feedback,
        }
        result = self._call_function(
            [
                {"role": "system", "content": IMPLEMENTATION_PLANNING_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            self._implementation_plan_function_tool(),
        )
        status = str(result.get("status") or "").strip().lower()
        if status == "blocked":
            raise ExecutionBindingError(
                str(result.get("reason") or "semantic step cannot be decomposed"),
                replan_recommended=True,
                category="semantic_step_unimplementable",
            )
        if status != "ready":
            raise ExecutionBindingError(
                "invalid implementation planning status=%s"
                % (status or "<missing>"),
                replan_recommended=False,
                category="implementation_contract_invalid",
            )
        items = result.get("implementation_steps")
        if not isinstance(items, list) or not items or len(items) > 12:
            raise ExecutionBindingError(
                "implementation_steps must contain 1-12 items",
                replan_recommended=False,
                category="implementation_contract_invalid",
            )
        raw_ids: list[str] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ExecutionBindingError(
                    "implementation step must be an object",
                    replan_recommended=False,
                    category="implementation_contract_invalid",
                )
            raw_id = _safe_implementation_step_id(
                item.get("id"),
                fallback="step-%s" % index,
            )
            if raw_id in raw_ids:
                raise ExecutionBindingError(
                    "duplicate implementation step id=%s" % raw_id,
                    replan_recommended=False,
                    category="implementation_contract_invalid",
                )
            raw_ids.append(raw_id)
        id_map = {
            raw_id: "%s__%s" % (semantic_step.step_id, raw_id)
            for raw_id in raw_ids
        }
        steps = []
        for index, item in enumerate(items):
            raw_id = raw_ids[index]
            dependencies = [
                _safe_implementation_step_id(value, fallback="")
                for value in item.get("depends_on", [])
            ] if isinstance(item.get("depends_on"), list) else []
            if any(dep not in raw_ids[:index] for dep in dependencies):
                raise ExecutionBindingError(
                    "implementation dependencies must reference earlier steps",
                    replan_recommended=False,
                    category="implementation_contract_invalid",
                )
            risk = str(item.get("risk_suggestion") or semantic_step.risk).lower()
            risk = {
                "normal": "readonly",
                "controlled": "medium",
                "privileged": "high",
                "dangerous": "destructive",
            }.get(risk, risk)
            if risk not in RISK_LEVELS:
                risk = semantic_step.risk
            if _is_discovery_only_implementation_step(item, risk=risk):
                raise ExecutionBindingError(
                    "standalone discovery step is not executable: %s"
                    % str(item.get("title") or item.get("objective") or raw_id),
                    replan_recommended=False,
                    category="implementation_contract_invalid",
                )
            dependency_evidence = []
            for dependency in dependencies:
                producer = steps[raw_ids[:index].index(dependency)]
                dependency_evidence.append(
                    "planned predecessor %s executes first; objective=%s; expected_changes=%s"
                    % (
                        producer.step_id,
                        producer.objective,
                        " | ".join(producer.expected_changes) or "not declared",
                    )
                )
            steps.append(
                PrivilegedStep(
                    step_id=id_map[raw_id],
                    title=str(item.get("title") or raw_id).strip()[:300],
                    objective=str(
                        item.get("objective") or item.get("title") or raw_id
                    ).strip()[:1000],
                    reason=str(item.get("reason") or "").strip()[:1000],
                    evidence_refs=dependency_evidence,
                    depends_on=[id_map[dep] for dep in dependencies],
                    expected_changes=_strings(
                        item.get("expected_changes"),
                        20,
                    ),
                    success_criteria=_strings(
                        item.get("success_criteria"),
                        20,
                    ),
                    risk=risk,
                )
            )
        return steps

    def prepare_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        *,
        grounded_context: GroundedPlanContext | None,
        implementation_feedback: str = "",
    ) -> ExecutionBinding:
        messages = [
            {"role": "system", "content": EXECUTION_SELECTION_PROMPT},
            {
                "role": "user",
                "content": self._selection_request_content(
                    plan,
                    step,
                    grounded_context,
                ),
            },
        ]
        probe_round = 0
        invalid_repairs = 0
        reselection_attempts = 0
        shell_attempted = False
        shell_fallback_reviewed = False
        rejected_implementations: list[str] = []
        if implementation_feedback:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Implementation feedback from a previous confirmed"
                        " execution attempt:\n%s\nSelect a materially different"
                        " implementation when the previous one did not satisfy"
                        " the semantic objective."
                        % implementation_feedback[:6000]
                    ),
                }
            )
        while True:
            data: dict[str, Any] = {}
            try:
                data = _normalize_selection(
                    self._call_function(
                        messages,
                        self._selection_function_tool(),
                    )
                )
                status = str(data.get("status") or "").strip().lower()
                if status == "need_evidence":
                    if (
                        self.probe_runner is None
                        or probe_round >= MAX_BINDING_PROBE_ROUNDS
                    ):
                        raise ExecutionBindingError(
                            "执行绑定所需事实不足，且无法继续安全探测"
                        )
                    requests = self._probe_requests(
                        data.get("probe_requests")
                    )
                    self._progress(
                        "实现结论：步骤“%s”还缺少事实，准备执行只读检查（%s）。"
                        % (
                            _progress_text(step.title or step.objective),
                            "、".join(item["probe"] for item in requests),
                        )
                    )
                    evidence = self.probe_runner(requests)
                    probe_round += 1
                    plan.probe_history.append(
                        {
                            "phase": "execution_binding",
                            "step_id": step.step_id,
                            "round": probe_round,
                            "requests": requests,
                            "evidence": str(evidence)[:16000],
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Read-only binding evidence round %s:\n%s"
                                % (probe_round, str(evidence)[:16000])
                            ),
                        }
                    )
                    continue
                if status == "registered_action":
                    action = str(data.get("action") or "").strip()
                    if (
                        self.action_registry.get(action) is None
                        or action not in DIRECT_PRIVILEGED_ACTIONS
                    ):
                        raise ValueError(
                            "action_not_directly_registered=%s"
                            % (action or "<missing>")
                        )
                    self._progress(
                        "选择结论：步骤“%s”选用注册 Action：%s；"
                        "进入参数绑定阶段。"
                        % (
                            _progress_text(step.title or step.objective),
                            _progress_text(action),
                        )
                    )
                    try:
                        binding = self._complete_registered_action_contract(
                            data={
                                "action": action,
                                "binding_reason": data.get("selection_reason"),
                                "resolved_from_evidence": data.get(
                                    "resolved_from_evidence"
                                ),
                            },
                            semantic_step=step,
                            grounded_context=grounded_context,
                            initial_error=(
                                "stage 2 Action arguments are not bound yet"
                            ),
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ImplementationRejected(
                            "registered_action:%s" % action,
                            str(exc),
                        ) from exc
                    self._progress(
                        "实现结论：步骤“%s”将使用注册 Action：%s。"
                        % (
                            _progress_text(step.title or step.objective),
                            _progress_text(binding.action),
                        )
                    )
                    return binding
                if status == "shell_artifact":
                    shell_attempted = True
                    self._progress(
                        "选择结论：步骤“%s”没有匹配的注册 Action；"
                        "进入一次性脚本合同阶段。"
                        % _progress_text(step.title or step.objective)
                    )
                    try:
                        binding = self._complete_shell_contract(
                            selection=data,
                            semantic_step=step,
                            grounded_context=grounded_context,
                        )
                    except ExecutionBindingError as exc:
                        raise ImplementationRejected(
                            "shell_artifact",
                            str(exc),
                        ) from exc
                    self._progress(
                        "实现结论：没有合适的注册 Action，步骤“%s”需要一次性脚本并单独确认。"
                        % _progress_text(step.title or step.objective)
                    )
                    return binding
                if status == "verification_only":
                    try:
                        binding = self._complete_verification_only_contract(
                            selection=data,
                            semantic_step=step,
                            grounded_context=grounded_context,
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ImplementationRejected(
                            "verification_only",
                            str(exc),
                        ) from exc
                    self._progress(
                        "实现结论：步骤“%s”仅运行确定性验收检查，不执行变更命令。"
                        % _progress_text(step.title or step.objective)
                    )
                    return binding
                if status == "blocked":
                    reason = str(
                        data.get("reason")
                        or "Implementation Binding Agent 无法实现该步骤"
                    )
                    if not shell_attempted and not shell_fallback_reviewed:
                        shell_fallback_reviewed = True
                        self._progress(
                            "实现结论：注册能力未覆盖当前步骤，正在强制评估"
                            "一次性 Shell 兜底，而不是直接阻塞。"
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "A blocked result is premature because no shell"
                                    " fallback has been evaluated. Reconsider the same"
                                    " frozen objective. Return shell_artifact if a bounded,"
                                    " non-interactive, reviewable and verifiable one-time"
                                    " bash implementation is possible. Otherwise return"
                                    " blocked again and name the concrete shell safety or"
                                    " verification constraint; absence of a registered"
                                    " Action is not sufficient. Previous reason: %s"
                                    % reason[:1000]
                                ),
                            }
                        )
                        continue
                    self._progress(
                        "实现结论：当前证据下无法安全实现步骤“%s”。"
                        % _progress_text(step.title or step.objective)
                    )
                    raise ExecutionBindingError(
                        reason,
                        replan_recommended=True,
                        category="capability_mismatch",
                    )
                raise ValueError(
                    "invalid execution binding status=%s"
                    % (status or "<missing>")
                )
            except ImplementationRejected as exc:
                rejected_implementations.append(
                    "%s: %s" % (exc.implementation, str(exc)[:500])
                )
                if reselection_attempts >= MAX_IMPLEMENTATION_RESELECTIONS:
                    raise ExecutionBindingError(
                        "所有候选实现均无法完成语义步骤：%s"
                        % "；".join(rejected_implementations),
                        replan_recommended=True,
                        category="capability_mismatch",
                    ) from exc
                reselection_attempts += 1
                self._progress(
                    "实现结论：%s 不适合当前步骤，正在重新选择"
                    " Action、Shell 或纯验证实现（第 %s/%s 次）。"
                    % (
                        _progress_text(exc.implementation),
                        reselection_attempts,
                        MAX_IMPLEMENTATION_RESELECTIONS,
                    )
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The selected implementation was rejected after"
                            " contract validation. Do not repeat it unchanged."
                            " Select another registered Action, shell_artifact,"
                            " or verification_only. If none can implement the"
                            " frozen semantic step, return blocked. Rejected: %s"
                            % rejected_implementations[-1]
                        ),
                    }
                )
                continue
            except ExecutionBindingError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                if invalid_repairs >= MAX_BINDING_INVALID_REPAIRS:
                    raise ExecutionBindingError(
                        "Implementation Binding Agent 实现合同修复耗尽：%s"
                        % exc,
                        replan_recommended=False,
                        category="implementation_contract_invalid",
                    ) from exc
                invalid_repairs += 1
                self._progress(
                    "实现节点：候选实现不完整（%s）；正在请求第 %s 次结构化修复…"
                    % (
                        _binding_candidate_summary(data),
                        invalid_repairs,
                    )
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair only the stage 1 capability selection JSON."
                            " Do not return args, script, or checker contracts."
                            " Return one complete JSON object. status must be one of"
                            " registered_action, shell_artifact, need_evidence,"
                            " verification_only, or blocked; stage 2 status ready"
                            " is invalid here."
                            " Error: %s" % exc
                        ),
                    }
                )

    def _progress(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def _selection_request_content(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> str:
        """Build a small stage 1 request; exact evidence belongs to stage 2."""

        payload = {
            "goal": plan.goal,
            "semantic_step": {
                "step_id": step.step_id,
                "objective": step.objective,
                "reason": step.reason,
                "evidence_refs": step.evidence_refs,
                "depends_on": step.depends_on,
                "expected_effects": step.expected_changes,
                "success_criteria": step.success_criteria,
                "risk_suggestion": step.risk,
            },
            "selection_context": _selection_grounded_context(
                grounded_context
            ),
            "registered_action_catalog": self._action_catalog(),
            "allowed_action_names": self._direct_action_names(),
            "registered_probe_catalog": DEFAULT_READONLY_PROBES.render(),
            "required_status_values": [
                "registered_action",
                "shell_artifact",
                "verification_only",
                "need_evidence",
                "blocked",
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    def _direct_action_names(self) -> list[str]:
        return sorted(
            spec.name
            for spec in self.action_registry.describe()
            if spec.name in DIRECT_PRIVILEGED_ACTIONS
        )

    def _registered_binding(
        self,
        data: dict[str, Any],
        semantic_step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> ExecutionBinding:
        action = str(data.get("action") or "").strip()
        spec = self.action_registry.get(action)
        if spec is None or action not in DIRECT_PRIVILEGED_ACTIONS:
            raise ValueError(
                "action_not_directly_registered=%s"
                % (action or "<missing>")
            )
        args = _json_object_value(data.get("args"))
        if args is None:
            raise ValueError(
                "action=%s args must be an object, got=%s"
                % (action, type(data.get("args")).__name__)
            )
        missing = [
            key
            for key in REQUIRED_ACTION_ARGS.get(action, ())
            if not args.get(key)
        ]
        if missing:
            raise ValueError(
                "action=%s missing_required_args=%s"
                % (action, ",".join(missing))
            )
        problem = self.action_registry.validate_args(spec, args)
        if problem:
            raise ValueError(problem)
        problem = _validate_action_semantics(action, args)
        if problem:
            raise ValueError(problem)
        if grounded_context is not None:
            problem = _validate_host_facts(action, args)
            if problem:
                raise ValueError(problem)
        command_decision = (
            decide_ops_command(args)
            if action == "run_ops_command"
            else None
        )
        if command_decision is not None:
            if not command_decision.allowed:
                raise ValueError(
                    "controlled_argv_not_allowed=%s"
                    % command_decision.reason
                )
            if grounded_context is not None and not command_exists(
                command_decision.program
            ):
                raise ValueError(
                    "controlled_argv_program_not_found=%s"
                    % command_decision.program
                )
        risk_floor = _action_risk(
            command_decision.risk
            if command_decision is not None
            else spec.risk
        )
        risk = _effective_declared_risk(
            risk_floor,
            semantic_step.risk,
        )
        checks = self._valid_checks(data.get("postconditions"))
        checks = checks or _default_action_postconditions(action, args)
        if not checks:
            checks = [{"checker": "exit_code_zero", "args": {}}]
        binding = ExecutionBinding(
            kind="registered_action",
            action=action,
            args=_clean_binding_args(args),
            risk=risk,
            approval_scope="plan",
            resolved_from_evidence=_strings(
                data.get("resolved_from_evidence"),
                20,
            ),
            preconditions=self._valid_checks(data.get("preconditions")),
            postconditions=checks,
            binding_reason=str(data.get("binding_reason") or "").strip()[:1000],
        )
        return binding

    def _complete_registered_action_contract(
        self,
        *,
        data: dict[str, Any],
        semantic_step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
        initial_error: str,
    ) -> ExecutionBinding:
        """Repair Action arguments without reopening semantic route selection."""

        action = str(data.get("action") or "").strip()
        self._progress(
            "实现节点：已选定注册 Action“%s”，正在单独补全参数合同…"
            % _progress_text(action)
        )
        payload = {
            "frozen_action": action,
            "required_args": list(REQUIRED_ACTION_ARGS.get(action, ())),
            "optional_args": (
                ["anchor"] if action == "edit_text_file" else []
            ),
            "action_description": (
                self.action_registry.get(action).description
                if self.action_registry.get(action) is not None
                else ""
            ),
            "semantic_step": {
                "step_id": semantic_step.step_id,
                "objective": semantic_step.objective,
                "depends_on": semantic_step.depends_on,
                "planned_dependency_evidence": semantic_step.evidence_refs,
                "expected_effects": semantic_step.expected_changes,
                "success_criteria": semantic_step.success_criteria,
            },
            "grounded_context": _binding_grounded_context(grounded_context),
            "registered_checker_catalog": self.checkers.render_catalog(),
            "rejected_contract": {
                "args": data.get("args"),
                "preconditions": data.get("preconditions"),
                "postconditions": data.get("postconditions"),
            },
            "validation_error": initial_error,
        }
        messages = [
            {"role": "system", "content": ACTION_CONTRACT_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        last_error = initial_error
        for attempt in range(1, MAX_ACTION_CONTRACT_REPAIRS + 1):
            try:
                result = self._call_function(
                    messages,
                    self._action_contract_function_tool(action),
                )
                status = str(result.get("status") or "").strip().lower()
                if status == "blocked":
                    raise ValueError(
                        "selected action arguments cannot be grounded: %s"
                        % str(result.get("reason") or "unspecified")[:500]
                    )
                if status != "ready":
                    raise ValueError(
                        "invalid action contract status=%s"
                        % (status or "<missing>")
                    )
                repaired = dict(data)
                repaired["action"] = action
                for key in (
                    "args",
                    "binding_reason",
                    "resolved_from_evidence",
                    "preconditions",
                    "postconditions",
                ):
                    if key in result:
                        repaired[key] = result[key]
                binding = self._registered_binding(
                    repaired,
                    semantic_step,
                    grounded_context,
                )
                self._progress(
                    "实现节点：注册 Action“%s”的参数合同已补全。"
                    % _progress_text(action)
                )
                return binding
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if attempt >= MAX_ACTION_CONTRACT_REPAIRS:
                    break
                self._progress(
                    "实现节点：Action 参数合同无效，正在请求第 %s 次定向修复…"
                    % attempt
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair only args and checker contracts for frozen"
                            " action=%s. Error: %s" % (action, exc)
                        ),
                    }
                )
        raise ValueError(
            "registered action contract repair exhausted for %s: %s"
            % (action, last_error)
        )

    def _complete_shell_contract(
        self,
        *,
        selection: dict[str, Any],
        semantic_step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> ExecutionBinding:
        """Generate stage 2 Shell fields without reopening capability choice."""

        payload = {
            "frozen_implementation_kind": "shell_artifact",
            "selection_reason": selection.get("selection_reason"),
            "semantic_step": {
                "step_id": semantic_step.step_id,
                "objective": semantic_step.objective,
                "depends_on": semantic_step.depends_on,
                "planned_dependency_evidence": semantic_step.evidence_refs,
                "expected_effects": semantic_step.expected_changes,
                "success_criteria": semantic_step.success_criteria,
            },
            "grounded_context": _binding_grounded_context(grounded_context),
            "registered_checker_catalog": self.checkers.render_catalog(),
        }
        messages = [
            {"role": "system", "content": SHELL_CONTRACT_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        last_error = "shell contract was not generated"
        for attempt in range(1, MAX_SHELL_CONTRACT_REPAIRS + 1):
            try:
                result = self._call_function(
                    messages,
                    self._shell_contract_function_tool(),
                )
                status = str(result.get("status") or "").strip().lower()
                if status == "blocked":
                    raise ExecutionBindingError(
                        str(
                            result.get("reason")
                            or "无法为该语义步骤生成安全的一次性脚本"
                        ),
                        category="semantic_binding",
                    )
                if status != "ready":
                    raise ValueError(
                        "invalid shell contract status=%s"
                        % (status or "<missing>")
                    )
                contract = dict(result)
                contract.pop("status", None)
                contract["binding_reason"] = (
                    contract.get("binding_reason")
                    or selection.get("selection_reason")
                )
                if not contract.get("resolved_from_evidence"):
                    contract["resolved_from_evidence"] = selection.get(
                        "resolved_from_evidence"
                    )
                binding = self._shell_binding(
                    contract,
                    semantic_step,
                    grounded_context,
                )
                self._progress(
                    "实现节点：一次性脚本执行合同已生成并通过安全校验。"
                )
                return binding
            except ExecutionBindingError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if attempt >= MAX_SHELL_CONTRACT_REPAIRS:
                    break
                self._progress(
                    "实现节点：Shell 合同不完整，正在请求第 %s 次定向修复…"
                    % attempt
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair only the stage 2 shell contract. Keep the"
                            " semantic objective and shell implementation kind"
                            " frozen. Return status ready or blocked. The script"
                            " must remain within %s lines and %s UTF-8 bytes;"
                            " compact it if the error reports a size limit."
                            " Error: %s"
                            % (MAX_SCRIPT_LINES, MAX_SCRIPT_BYTES, exc)
                        ),
                    }
                )
        raise ExecutionBindingError(
            "Implementation Binding Agent Shell 合同修复耗尽：%s"
            % last_error,
            replan_recommended=False,
            category="implementation_contract_invalid",
        )

    def _shell_binding(
        self,
        data: dict[str, Any],
        semantic_step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> ExecutionBinding:
        raw_script = str(data.get("script") or "")
        if not raw_script.strip():
            raise ValueError("shell_artifact_empty")
        cwd = str(data.get("cwd") or "").strip()
        if not cwd:
            raise ValueError("shell artifact requires explicit cwd")
        declared_changes = _strings(data.get("declared_changes"), 20)
        if not declared_changes:
            raise ValueError("shell artifact requires declared_changes")
        rollback = str(data.get("rollback") or "").strip()[:2000]
        if not rollback:
            raise ValueError("shell artifact requires rollback")
        fingerprint = ""
        if grounded_context is not None:
            fingerprint = str(
                grounded_context.facts.get("environment_fingerprint") or ""
            )
        artifact = create_shell_artifact(
            artifact_id="shell-" + uuid.uuid4().hex[:12],
            script=raw_script,
            cwd=cwd,
            run_as=str(data.get("run_as") or ""),
            timeout=int(data.get("timeout") or 120),
            environment_fingerprint=fingerprint,
            declared_changes=declared_changes,
            rollback=rollback,
            nonce=uuid.uuid4().hex,
        )
        problem = self.shell_policy.validate(
            artifact,
            allowed_future_cwds=_planned_output_paths(semantic_step),
        )
        if problem:
            raise ValueError(problem)
        try:
            postconditions = self._strict_shell_postconditions(
                data.get("postconditions")
            )
        except ValueError as exc:
            postconditions = self._complete_shell_postconditions(
                data=data,
                semantic_step=semantic_step,
                artifact=artifact,
                initial_error=str(exc),
            )
        return ExecutionBinding(
            kind="shell_artifact",
            shell_artifact=artifact,
            risk=_effective_declared_risk("high", semantic_step.risk),
            approval_scope="step",
            resolved_from_evidence=_strings(
                data.get("resolved_from_evidence"),
                20,
            ),
            postconditions=postconditions,
            binding_reason=str(data.get("binding_reason") or "").strip()[:1000],
        )

    def _complete_verification_only_contract(
        self,
        *,
        selection: dict[str, Any],
        semantic_step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> ExecutionBinding:
        payload = {
            "frozen_implementation_kind": "verification_only",
            "semantic_step": {
                "step_id": semantic_step.step_id,
                "objective": semantic_step.objective,
                "depends_on": semantic_step.depends_on,
                "planned_dependency_evidence": semantic_step.evidence_refs,
                "success_criteria": semantic_step.success_criteria,
            },
            "grounded_context": _binding_grounded_context(grounded_context),
            "registered_checker_catalog": self.checkers.render_catalog(),
        }
        messages = [
            {"role": "system", "content": VERIFICATION_ONLY_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_error = "verification-only contract was not generated"
        for attempt in range(1, MAX_ACTION_CONTRACT_REPAIRS + 1):
            try:
                result = self._call_function(
                    messages,
                    self._verification_only_function_tool(),
                )
                status = str(result.get("status") or "").strip().lower()
                if status == "blocked":
                    raise ValueError(
                        "verification_only_not_grounded=%s"
                        % str(result.get("reason") or "unspecified")[:500]
                    )
                if status != "ready":
                    raise ValueError(
                        "invalid verification-only status=%s"
                        % (status or "<missing>")
                    )
                checks, errors = self._validated_checks(
                    result.get("postconditions")
                )
                if errors:
                    raise ValueError("; ".join(errors))
                if not checks or all(
                    item["checker"] == "exit_code_zero" for item in checks
                ):
                    raise ValueError(
                        "verification_only_requires_observable_postconditions"
                    )
                return ExecutionBinding(
                    kind="verification_only",
                    risk="readonly",
                    approval_scope="plan",
                    resolved_from_evidence=_strings(
                        result.get("resolved_from_evidence"),
                        20,
                    ),
                    postconditions=checks,
                    binding_reason=str(
                        result.get("binding_reason")
                        or selection.get("selection_reason")
                        or ""
                    ).strip()[:1000],
                )
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if attempt >= MAX_ACTION_CONTRACT_REPAIRS:
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair only the verification checker contract."
                            " Keep verification_only frozen. Error: %s" % exc
                        ),
                    }
                )
        raise ValueError(
            "verification-only contract repair exhausted: %s" % last_error
        )

    def _complete_shell_postconditions(
        self,
        *,
        data: dict[str, Any],
        semantic_step: PrivilegedStep,
        artifact: ShellArtifact,
        initial_error: str,
    ) -> list[dict[str, Any]]:
        """Complete only the verifier contract; the validated script is frozen."""

        inferred = infer_postconditions(artifact.script)
        if inferred:
            try:
                return self._strict_shell_postconditions(inferred)
            except ValueError:
                # Inference is deliberately conservative.  If it cannot form a
                # complete state proof, ask the model using the exact catalog.
                pass
        self._progress(
            "实现节点：一次性脚本已通过安全校验并冻结，正在单独补全结果验收条件…"
        )
        payload = {
            "semantic_step": {
                "step_id": semantic_step.step_id,
                "objective": semantic_step.objective,
                "expected_effects": semantic_step.expected_changes,
                "success_criteria": semantic_step.success_criteria,
            },
            "frozen_shell_artifact": {
                "script": artifact.script,
                "sha256": artifact.sha256,
                "cwd": artifact.cwd,
                "run_as": artifact.run_as,
                "declared_changes": artifact.declared_changes,
            },
            "registered_checker_catalog": self.checkers.render_catalog(),
            "rejected_postconditions": data.get("postconditions"),
            "validation_error": initial_error,
        }
        messages = [
            {"role": "system", "content": SHELL_VERIFICATION_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        last_error = initial_error
        for attempt in range(1, MAX_SHELL_VERIFICATION_REPAIRS + 1):
            try:
                result = self._call_function(
                    messages,
                    self._shell_verification_function_tool(),
                )
                status = str(result.get("status") or "").strip().lower()
                if status == "blocked":
                    raise ExecutionBindingError(
                        str(result.get("reason") or "一次性脚本缺少可验证的结果")
                    )
                if status != "ready":
                    raise ValueError(
                        "invalid shell verification status=%s"
                        % (status or "<missing>")
                    )
                checks = self._strict_shell_postconditions(
                    result.get("postconditions")
                )
                self._progress(
                    "实现节点：一次性脚本验收合同已补全（%s 个确定性检查）。"
                    % len(checks)
                )
                return checks
            except ExecutionBindingError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if attempt >= MAX_SHELL_VERIFICATION_REPAIRS:
                    break
                self._progress(
                    "实现节点：验收合同无效，正在请求第 %s 次定向修复…"
                    % attempt
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair only postconditions. The frozen script and"
                            " sha256 must not change. Return status ready or"
                            " blocked. Error: %s" % exc
                        ),
                    }
                )
        raise ExecutionBindingError(
            "一次性脚本已通过安全校验，但无法生成有效验收合同：%s"
            % last_error
        )

    def _strict_shell_postconditions(
        self,
        value: Any,
    ) -> list[dict[str, Any]]:
        checks, errors = self._validated_checks(value)
        if errors:
            raise ValueError("; ".join(errors))
        if not checks:
            raise ValueError("shell artifact requires registered postconditions")
        if all(item["checker"] == "exit_code_zero" for item in checks):
            raise ValueError(
                "mutating shell artifact requires an observable state postcondition"
            )
        return checks

    def _valid_checks(self, value: Any) -> list[dict[str, Any]]:
        checks, _errors = self._validated_checks(value)
        return checks

    def _validated_checks(
        self,
        value: Any,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not isinstance(value, list):
            return [], ["postconditions must be a list"]
        result = []
        errors = []
        known = set(self.checkers.names)
        for index, item in enumerate(value[:20]):
            if not isinstance(item, dict):
                errors.append("postcondition[%s] must be an object" % index)
                continue
            name = str(item.get("checker") or "").strip()
            args = item.get("args")
            if name not in known:
                errors.append(
                    "postcondition[%s] checker is not registered=%s"
                    % (index, name or "<missing>")
                )
                continue
            if not isinstance(args, dict):
                errors.append("postcondition[%s] args must be an object" % index)
                continue
            missing = [
                key
                for key in CHECKER_REQUIRED_ARGS.get(name, ())
                if key not in args or args[key] is None or args[key] == ""
            ]
            if missing:
                errors.append(
                    "postcondition[%s] checker=%s missing_required_args=%s"
                    % (index, name, ",".join(missing))
                )
                continue
            result.append({"checker": name, "args": args})
        return result, errors

    @staticmethod
    def _probe_requests(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValueError("binding need_evidence requires probe_requests")
        known = {
            spec.name for spec in DEFAULT_READONLY_PROBES.describe()
        }
        result = []
        for item in value[:3]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("probe") or "").strip()
            if name not in known:
                raise ValueError("binding probe not registered=%s" % name)
            args = item.get("args")
            result.append(
                {
                    "probe": name,
                    "args": args if isinstance(args, dict) else {},
                    "purpose": str(item.get("purpose") or "").strip()[:500],
                }
            )
        if not result:
            raise ValueError("binding probe requests are empty")
        return result

    def _action_catalog(self) -> str:
        lines = []
        for spec in self.action_registry.describe():
            if spec.name not in DIRECT_PRIVILEGED_ACTIONS:
                continue
            lines.append(
                "- action=%s category=%s risk=%s required_args=%s path_args=%s"
                " description=%s preconditions=%s postconditions=%s"
                % (
                    spec.name,
                    spec.category,
                    spec.risk,
                    ",".join(REQUIRED_ACTION_ARGS.get(spec.name, ())) or "none",
                    ",".join(spec.path_args) or "none",
                    spec.description or spec.name,
                    ",".join(spec.preconditions) or "none",
                    ",".join(spec.postconditions) or "none",
                )
            )
        return "\n".join(lines)

    def _selection_function_tool(self) -> dict[str, Any]:
        probe_names = sorted(
            spec.name for spec in DEFAULT_READONLY_PROBES.describe()
        )
        return _function_tool(
            "select_execution_implementation",
            "Select one frozen implementation kind for the semantic step.",
            {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "registered_action",
                            "shell_artifact",
                            "verification_only",
                            "need_evidence",
                            "blocked",
                        ],
                    },
                    "action": {
                        "type": "string",
                        "enum": ["", *self._direct_action_names()],
                    },
                    "selection_reason": {"type": "string"},
                    "resolved_from_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "probe_requests": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "probe": {
                                    "type": "string",
                                    "enum": probe_names,
                                },
                                "args": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                                "purpose": {"type": "string"},
                            },
                            "required": ["probe", "args", "purpose"],
                            "additionalProperties": False,
                        },
                    },
                    "reason": {"type": "string"},
                },
                "required": [
                    "status",
                    "action",
                    "selection_reason",
                    "resolved_from_evidence",
                    "probe_requests",
                    "reason",
                ],
                "additionalProperties": False,
            },
        )

    def _implementation_plan_function_tool(self) -> dict[str, Any]:
        step_schema = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "reason": {"type": "string"},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "expected_changes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "success_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "risk_suggestion": {
                    "type": "string",
                    "enum": list(RISK_LEVELS),
                },
            },
            "required": [
                "id",
                "title",
                "objective",
                "reason",
                "depends_on",
                "expected_changes",
                "success_criteria",
                "risk_suggestion",
            ],
            "additionalProperties": False,
        }
        return _function_tool(
            "build_implementation_plan",
            "Decompose one semantic step into atomic implementation steps.",
            {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ready", "blocked"],
                    },
                    "reason": {"type": "string"},
                    "implementation_steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": step_schema,
                    },
                },
                "required": ["status", "reason", "implementation_steps"],
                "additionalProperties": False,
            },
        )
    def _action_contract_function_tool(
        self,
        action: str,
    ) -> dict[str, Any]:
        required = list(REQUIRED_ACTION_ARGS.get(action, ()))
        optional = ["anchor"] if action == "edit_text_file" else []
        return _function_tool(
            "bind_action_%s" % action,
            "Bind grounded arguments for frozen registered Action %s." % action,
            {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ready", "blocked"],
                    },
                    "reason": {"type": "string"},
                    "args": {
                        "type": "object",
                        "properties": {
                            name: _action_arg_json_schema(name)
                            for name in [*required, *optional]
                        },
                        "required": required,
                        "additionalProperties": True,
                    },
                    "binding_reason": {"type": "string"},
                    "resolved_from_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "preconditions": self._checker_list_json_schema(),
                    "postconditions": self._checker_list_json_schema(),
                },
                "required": [
                    "status",
                    "reason",
                    "args",
                    "binding_reason",
                    "resolved_from_evidence",
                    "preconditions",
                    "postconditions",
                ],
                "additionalProperties": False,
            },
        )

    def _shell_contract_function_tool(self) -> dict[str, Any]:
        return _function_tool(
            "build_shell_artifact",
            "Build the frozen one-time shell execution contract.",
            {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ready", "blocked"],
                    },
                    "reason": {"type": "string"},
                    "script": {
                        "type": "string",
                        "maxLength": MAX_SCRIPT_BYTES,
                    },
                    "cwd": {"type": "string"},
                    "run_as": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1},
                    "declared_changes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "rollback": {"type": "string"},
                    "binding_reason": {"type": "string"},
                    "resolved_from_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "postconditions": self._checker_list_json_schema(),
                },
                "required": [
                    "status",
                    "reason",
                    "script",
                    "cwd",
                    "run_as",
                    "timeout",
                    "declared_changes",
                    "rollback",
                    "binding_reason",
                    "resolved_from_evidence",
                    "postconditions",
                ],
                "additionalProperties": False,
            },
        )

    def _shell_verification_function_tool(self) -> dict[str, Any]:
        return _function_tool(
            "bind_shell_postconditions",
            "Bind deterministic checks to the already frozen shell artifact.",
            {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ready", "blocked"],
                    },
                    "reason": {"type": "string"},
                    "postconditions": self._checker_list_json_schema(),
                },
                "required": ["status", "reason", "postconditions"],
                "additionalProperties": False,
            },
        )

    def _verification_only_function_tool(self) -> dict[str, Any]:
        return _function_tool(
            "bind_verification_only",
            "Bind deterministic checkers without executing an implementation.",
            {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ready", "blocked"],
                    },
                    "reason": {"type": "string"},
                    "binding_reason": {"type": "string"},
                    "resolved_from_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "postconditions": self._checker_list_json_schema(),
                },
                "required": [
                    "status",
                    "reason",
                    "binding_reason",
                    "resolved_from_evidence",
                    "postconditions",
                ],
                "additionalProperties": False,
            },
        )

    def _checker_list_json_schema(self) -> dict[str, Any]:
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "checker": {
                        "type": "string",
                        "enum": list(self.checkers.names),
                    },
                    "args": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["checker", "args"],
                "additionalProperties": False,
            },
        }

    def _call_function(
        self,
        messages: list[dict[str, str]],
        tool: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(tool["function"]["name"])
        choice = {"type": "function", "function": {"name": name}}
        try:
            response = self.llm.complete(
                messages=messages,
                tools=[tool],
                tool_choice=choice,
                reasoning_effort=None,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except TypeError:
            response = self.llm.complete(messages=messages, tools=[tool])
        return _function_arguments(response, name)


def _function_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _function_arguments(response: Any, expected_name: str) -> dict[str, Any]:
    """Extract one forced function call and reject ordinary text responses."""

    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        raise ValueError("function_call_response_missing_choices")
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message")
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    if not calls:
        raise ValueError("required_function_call_missing=%s" % expected_name)
    if len(calls) != 1:
        raise ValueError("expected_exactly_one_function_call=%s" % expected_name)
    call = calls[0]
    function = getattr(call, "function", None)
    if function is None and isinstance(call, dict):
        function = call.get("function")
    name = getattr(function, "name", None)
    arguments = getattr(function, "arguments", None)
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments")
    if str(name or "") != expected_name:
        raise ValueError(
            "unexpected_function_call=%s expected=%s"
            % (name or "<missing>", expected_name)
        )
    if isinstance(arguments, dict):
        data = arguments
    else:
        try:
            data = json.loads(str(arguments or ""))
        except json.JSONDecodeError as exc:
            raise ValueError("function_arguments_not_valid_json") from exc
    if not isinstance(data, dict):
        raise ValueError("function_arguments_must_be_object")
    return data


def _action_arg_json_schema(name: str) -> dict[str, Any]:
    if name in {"ports"}:
        return {"type": "array", "items": {"type": "integer"}}
    if name in {"packages", "argv", "sources", "entries"}:
        return {"type": "array", "items": {"type": "string"}}
    if name in {"patch"}:
        return {"type": "object", "additionalProperties": True}
    if name in {"pid", "expected_port"}:
        return {"type": "integer"}
    return {"type": "string"}


def _normalize_selection(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize common function-argument aliases, then validate downstream."""

    result = dict(data)
    nested = next(
        (
            result.get(key)
            for key in ("selection", "implementation", "binding")
            if isinstance(result.get(key), dict)
        ),
        {},
    )
    if not result.get("status"):
        result["status"] = (
            nested.get("status")
            or nested.get("kind")
            or result.get("kind")
            or result.get("type")
        )
    if not result.get("action"):
        result["action"] = (
            nested.get("action")
            or nested.get("action_name")
            or nested.get("name")
            or result.get("action_name")
            or result.get("selected_action")
        )
    aliases = {
        "action": "registered_action",
        "registered": "registered_action",
        "shell": "shell_artifact",
        "script": "shell_artifact",
        "verification": "verification_only",
        "checker": "verification_only",
    }
    normalized = str(result.get("status") or "").strip().lower()
    result["status"] = aliases.get(normalized, normalized)
    return result


def _safe_implementation_step_id(value: Any, *, fallback: str) -> str:
    text = re.sub(
        r"[^A-Za-z0-9_-]+",
        "-",
        str(value or "").strip(),
    ).strip("-_")
    return (text or fallback)[:80]


def _is_discovery_only_implementation_step(
    item: dict[str, Any],
    *,
    risk: str,
) -> bool:
    """Reject internal argument discovery accidentally emitted as execution."""

    if risk != "readonly" or _strings(item.get("expected_changes"), 20):
        return False
    text = " ".join(
        str(item.get(key) or "").lower()
        for key in ("title", "objective", "reason")
    )
    discovery_markers = (
        "定位",
        "读取",
        "确定插入位置",
        "选择参数",
        "识别路径",
        "获取配置",
        "locate ",
        "read existing",
        "determine ",
        "choose ",
        "identify path",
        "discover ",
        "obtain configuration",
    )
    return any(marker in text for marker in discovery_markers)


def _planned_output_paths(step: PrivilegedStep) -> tuple[Any, ...]:
    """Extract dependency-produced absolute paths for compile-time cwd checks."""

    paths = []
    for reference in step.evidence_refs:
        for raw in re.findall(r"/(?:[^\s|;,]+)", str(reference)):
            cleaned = raw.rstrip(".。:：)]}'\"")
            if cleaned and cleaned not in paths:
                paths.append(cleaned)
    return tuple(paths)


def _apply_binding_to_step(
    step: PrivilegedStep,
    binding: ExecutionBinding,
) -> None:
    step.execution_binding = binding
    step.implementation_plan = None
    step.risk = binding.risk
    step.approval_scope = binding.approval_scope
    step.preconditions = list(binding.preconditions)
    step.postconditions = list(binding.postconditions)
    if binding.shell_artifact is not None:
        step.timeout = binding.shell_artifact.timeout
    step.status = (
        "awaiting_confirmation"
        if binding.approval_scope == "step"
        else "pending"
    )


def _selection_grounded_context(
    grounded_context: GroundedPlanContext | None,
) -> str:
    """Expose evidence availability, not the large evidence body, in stage 1."""

    if grounded_context is None:
        return "(no structured context)"
    payload = {
        "available_fact_keys": sorted(str(key) for key in grounded_context.facts),
        "environment_model": grounded_context.facts.get("environment_model"),
        "knowledge_evidence_available": bool(
            str(grounded_context.knowledge_evidence or "").strip()
        ),
        "server_evidence_available": bool(
            str(grounded_context.environment_evidence or "").strip()
        ),
        "note": "exact grounded values will be supplied to stage 2",
    }
    return _head_tail(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        6000,
    )


def _strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()[:1000]
        for item in value[:limit]
        if str(item).strip()
    ]


def _clean_binding_args(value: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, item in list(value.items())[:40]:
        normalized = str(key or "").strip()[:80]
        if not normalized or item is None:
            continue
        if isinstance(item, list):
            result[normalized] = [str(part)[:500] for part in item[:40]]
        elif isinstance(item, dict):
            result[normalized] = {
                str(part_key)[:80]: part_value
                for part_key, part_value in list(item.items())[:40]
            }
        else:
            result[normalized] = str(item)[
                :20000 if normalized in {"content", "anchor"} else 500
            ]
    return result


def _json_object_value(value: Any) -> dict[str, Any] | None:
    """Accept an object or a model's JSON-stringified object, nothing else."""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _binding_candidate_summary(data: dict[str, Any]) -> str:
    """Render protocol shape only; never print argument values or secrets."""

    status = str(data.get("status") or "<missing>").strip() or "<missing>"
    action = str(data.get("action") or "<missing>").strip() or "<missing>"
    args = data.get("args")
    args_shape = "object" if isinstance(args, dict) else type(args).__name__
    return "status=%s，action=%s，args_type=%s" % (
        _progress_text(status, 30),
        _progress_text(action, 60),
        args_shape,
    )


def _binding_grounded_context(
    grounded_context: GroundedPlanContext | None,
) -> str:
    """Keep every evidence class while bounding large free-form sections."""

    if grounded_context is None:
        return "(no structured context)"
    environment_model = grounded_context.facts.get("environment_model")
    structured = (
        json.dumps(
            environment_model,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        if environment_model
        else "(none)"
    )
    return (
        "## Klonet knowledge evidence\n%s\n\n"
        "## Structured environment facts (authoritative)\n%s\n\n"
        "## Read-only server evidence\n%s"
        % (
            _head_tail(
                grounded_context.knowledge_evidence,
                MAX_BINDING_GROUNDING_SECTION_CHARS,
            ),
            _head_tail(
                structured,
                MAX_BINDING_GROUNDING_SECTION_CHARS,
            ),
            _head_tail(
                grounded_context.environment_evidence,
                MAX_BINDING_GROUNDING_SECTION_CHARS,
            ),
        )
    )


def _head_tail(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    half = max(1, (limit - 80) // 2)
    return (
        text[:half]
        + "\n...[section compacted; protocol catalogs remain complete]...\n"
        + text[-half:]
    )


def _progress_text(value: Any, limit: int = 80) -> str:
    return " ".join(str(value or "").split())[:limit] or "未命名步骤"
