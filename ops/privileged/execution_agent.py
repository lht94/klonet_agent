"""Implementation Binding Agent: compile semantic steps into safe contracts."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import textwrap
import uuid
from pathlib import Path
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
    PlanResource,
    PrivilegedPlan,
    PrivilegedStep,
    RISK_LEVELS,
    ShellArtifact,
    component_port_arg,
)
from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
from klonet_agent.ops.privileged.action_contracts import (
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
- blocked: {"status":"blocked","reason":"...",
  "shell_blocker_category":"<optional hard blocker>"}, only when no registered
  Action, safe one-time shell artifact, or verification-only checker contract
  can implement the unchanged semantic objective. The only hard Shell blocker
  categories are hard_policy, unverifiable, and no_safe_command. Do not claim
  a hard blocker merely because one candidate script or contract failed.

Prefer a registered Action only when its declared capability actually covers
the objective. The absence of a matching registered Action is not by itself a
reason to return blocked: select shell_artifact when a bounded, reviewable
one-time shell implementation is possible. Never invent Action names. All
implementation parameters will be generated and validated by a separate
stage 2 call.
For adding, replacing, or moving a top-level Python class, prefer the
structural upsert_python_class Action over generic text anchors or Shell. For
switching a top-level assignment such as PROJ_CONFIG to an already declared
configuration class, prefer set_python_config_assignment; it does not require
guessing the old source text. For
setting a scalar field such as master_ip, master_port, worker_port,
public_port, or web_terminal_port on a Python configuration class, prefer
set_python_class_attribute over text replacement; class_name may be omitted
when PROJ_CONFIG identifies the active class. For
checking a Python class/module attribute value, prefer verification_only with
python_attribute_equals over run_ops_command or Shell.
Plan resources are immutable shared inputs. Never replace a frozen resource
with a currently existing value from another instance. A deferred resource is
unknown, not permission to guess; choose an implementation that does not need
it or return blocked with the missing resource name.
Planned predecessor effects are valid future-state grounding. When an earlier
semantic or implementation step creates a path/resource, bind its consumer to
that frozen future value and verify it after the dependency runs. Do not probe
or reject the future target merely because it does not exist during binding.
""".strip()

MAX_BINDING_PROBE_ROUNDS = 2
MAX_BINDING_INVALID_REPAIRS = 2
MAX_ACTION_CONTRACT_REPAIRS = 2
MAX_SHELL_CONTRACT_REPAIRS = 2
MAX_SHELL_VERIFICATION_REPAIRS = 2
MAX_SHELL_CANDIDATE_ATTEMPTS = 3
MAX_IMPLEMENTATION_RESELECTIONS = 2
MAX_IMPLEMENTATION_PLAN_REBUILDS = 2
MAX_BINDING_GROUNDING_SECTION_CHARS = 12000
HARD_SHELL_BLOCKER_CATEGORIES = {
    "hard_policy",
    "unverifiable",
    "no_safe_command",
}


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
If a declared predecessor creates the checked target, bind the checker to that
future target; its absence during plan compilation is expected, not a blocker.
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
The supplied plan resource manifest is already frozen. Implementation steps
must preserve it; a deferred value remains unknown until its declared boundary.
An earlier semantic dependency's expected changes are valid future facts. Bind
later actions and checks to those declared outputs instead of probing for them
before their producing step has executed.
Use the registered Action catalog as capability boundaries. Do not split an
indivisible Action into fake preparation effects (for example, do not create
empty screen sessions separately from starting their components), and do not
describe one atomic step as changing multiple components when only a singular
component Action can cover it. Do not emit Action names in the micro-plan.
Do not create a mutation step merely to "preserve" state already carried by a
copy/predecessor. If preservation matters, express it as a readonly final
verification with no expected_changes.

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
objective. For every matching resource consumer, use the frozen value exactly.
Never substitute another instance's existing path or port. Do not guess a
deferred resource.
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
supplied checker catalog. Frozen plan resources must be used exactly and
deferred resources must not be guessed. Do not return an Action or alter the
objective.
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
        _validate_runtime_knowledge_contract(plan, grounded_context)
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
                if binding is None:
                    continue
                if binding.kind == "shell_artifact":
                    registered_steps.append(
                        PrivilegedStep(
                            step_id=step.step_id,
                            title=step.title,
                            objective=step.objective,
                            depends_on=list(step.depends_on),
                            action="shell_artifact",
                            args={
                                "cwd": binding.shell_artifact.cwd,
                                "declared_changes": list(
                                    binding.shell_artifact.declared_changes
                                )
                            },
                            risk=binding.risk,
                        )
                    )
                    continue
                if binding.kind != "registered_action":
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
            _validate_semantic_destination_availability(
                semantic_step,
                plan.resources,
            )
            _normalize_resolved_resource_semantic_step(
                semantic_step,
                plan.resources,
            )
            _normalize_semantic_backend_health_contract(
                semantic_step,
                plan.resources,
            )
            self._progress(
                "语义步骤 %s/%s：正在把“%s”展开为原子实施计划…"
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
                        plan,
                        semantic_step,
                        grounded_context=grounded_context,
                        feedback=feedback,
                    )
                    _validate_micro_plan_dependency_shape(
                        plan,
                        semantic_step,
                        micro_steps,
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
                    _validate_runtime_recovery_action_coverage(
                        semantic_step,
                        micro_steps,
                    )
                    _validate_source_mutation_action_coverage(
                        semantic_step,
                        micro_steps,
                    )
                    _validate_semantic_resource_coverage(
                        semantic_step,
                        micro_steps,
                        plan.resources,
                    )
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
                if binding is None:
                    continue
                if binding.kind == "shell_artifact":
                    registered_steps.append(
                        PrivilegedStep(
                            step_id=micro_step.step_id,
                            title=micro_step.title,
                            objective=micro_step.objective,
                            depends_on=list(micro_step.depends_on),
                            action="shell_artifact",
                            args={
                                "cwd": binding.shell_artifact.cwd,
                                "declared_changes": list(
                                    binding.shell_artifact.declared_changes
                                )
                            },
                            risk=binding.risk,
                        )
                    )
                    continue
                if binding.kind != "registered_action":
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
        plan: PrivilegedPlan,
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
            "registered_action_capability_catalog": self._action_catalog(),
            "frozen_plan_resources": _resource_manifest_payload(
                plan.resources
            ),
            "previous_attempt_feedback": feedback,
        }
        deterministic = (
            _deterministic_runtime_stop_items(semantic_step)
            or _deterministic_klonet_config_items(plan, semantic_step)
        )
        if deterministic:
            result = {"status": "ready", "implementation_steps": deterministic}
        else:
            try:
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
            except ValueError as exc:
                raise ExecutionBindingError(
                    "invalid implementation function response: %s" % exc,
                    replan_recommended=False,
                    category="implementation_contract_invalid",
                ) from exc
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
        if not isinstance(items, list) or not items or len(items) > 16:
            raise ExecutionBindingError(
                "implementation_steps must contain 1-16 items",
                replan_recommended=False,
                category="implementation_contract_invalid",
            )
        items = _split_multi_attribute_config_items(
            items,
            semantic_step,
            plan.resources,
        )
        items = _drop_unauthorized_config_mutations(items, semantic_step)
        items = _drop_preservation_only_config_mutations(items, semantic_step)
        items = _collapse_redundant_config_mutations(items)
        items = _drop_unauthorized_source_mutations(items, semantic_step)
        items = _collapse_redundant_python_removal_items(items)
        items = _split_multi_component_runtime_items(items, semantic_step)
        items = _filter_unauthorized_runtime_role_mutations(items, semantic_step)
        items = _drop_runtime_stops_for_roles_known_missing(items, semantic_step)
        items = _drop_runtime_stops_covered_by_semantic_predecessor(
            items, semantic_step, plan,
        )
        items = _drop_runtime_stops_covered_by_same_step_restart(items)
        items = _ensure_runtime_migration_stop_item(items, semantic_step)
        items = _ensure_runtime_role_recovery_items(items, semantic_step)
        items = _normalize_runtime_role_recovery_verbs(items, semantic_step)
        items = _drop_runtime_stops_covered_by_same_step_restart(items)
        items = _expand_unhealthy_role_restarts(items, semantic_step)
        items = _collapse_redundant_runtime_role_mutations(items)
        items = _ground_runtime_item_roots(items, semantic_step, plan.resources)
        items = _drop_redundant_implementation_verifications(
            items, semantic_step,
        )
        items = _order_runtime_migration_items(items, semantic_step)
        items = _order_source_runtime_recovery_items(items, semantic_step)
        items = _ensure_runtime_entry_preparation_items(
            items, semantic_step, plan.resources,
        )
        if len(items) > 16:
            raise ExecutionBindingError(
                "atomic configuration decomposition exceeds 16 items",
                replan_recommended=False,
                category="implementation_contract_invalid",
            )
        semantic_is_observational = _semantic_step_is_observational(
            semantic_step
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
            if (
                semantic_is_observational
                and not _implementation_item_is_verification(item)
            ):
                raise ExecutionBindingError(
                    "observational_semantic_step_contains_mutation=%s"
                    % str(item.get("title") or item.get("objective") or raw_id),
                    replan_recommended=False,
                    category="implementation_contract_invalid",
                )
            raw_ids.append(raw_id)
        items = _collapse_redundant_container_starts(items)
        items = _collapse_redundant_container_policy_steps(items)
        items = _collapse_redundant_container_setup_steps(
            items, semantic_step
        )
        items = _drop_nginx_activation_from_prepare(items, semantic_step)
        items = _collapse_redundant_nginx_activations(items, semantic_step)
        _remove_outer_semantic_dependencies(items, semantic_step.depends_on)
        items = _topologically_order_implementation_items(items)
        raw_ids = [
            _safe_implementation_step_id(
                item.get("id"),
                fallback="step-%s" % index,
            )
            for index, item in enumerate(items, start=1)
        ]
        id_map = {
            raw_id: "%s__%s" % (semantic_step.step_id, raw_id)
            for raw_id in raw_ids
        }
        semantic_dependency_evidence = []
        for dependency_id in semantic_step.depends_on:
            predecessor = next(
                (item for item in plan.steps if item.step_id == dependency_id),
                None,
            )
            if predecessor is None:
                continue
            semantic_dependency_evidence.append(
                "planned semantic predecessor %s executes first; objective=%s; "
                "expected_changes=%s"
                % (
                    predecessor.step_id,
                    predecessor.objective or predecessor.title,
                    " | ".join(predecessor.expected_changes) or "not declared",
                )
            )
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
            expected_changes = _strings(item.get("expected_changes"), 20)
            if _implementation_item_is_verification(item):
                expected_changes = []
            risk = str(item.get("risk_suggestion") or semantic_step.risk).lower()
            risk = {
                "normal": "readonly",
                "controlled": "medium",
                "privileged": "high",
                "dangerous": "destructive",
            }.get(risk, risk)
            if risk not in RISK_LEVELS:
                risk = semantic_step.risk
            # A micro-step with no declared state transition is a verifier.
            # Do not let an inherited outer risk turn it into a fake mutation
            # and force the binder away from verification_only.
            if not expected_changes:
                risk = "readonly"
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
                    evidence_refs=[
                        *semantic_dependency_evidence,
                        *dependency_evidence,
                    ],
                    depends_on=[id_map[dep] for dep in dependencies],
                    expected_changes=expected_changes,
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
        forced_action = _forced_registered_action_for_step(step)
        if forced_action:
            if self.action_registry.get(forced_action) is None:
                raise ExecutionBindingError(
                    "required registered action is unavailable=%s" % forced_action,
                    replan_recommended=False,
                    category="capability_mismatch",
                )
            self._progress(
                "能力边界：步骤“%s”固定使用已注册动作：%s。"
                % (
                    _progress_text(step.title or step.objective),
                    forced_action,
                )
            )
            if forced_action in {
                "start_screen_component",
                "restart_screen_component",
            }:
                # A new Screen session is derived from the frozen instance root
                # and the atomic component role.  Requiring evidence that the
                # session already exists contradicts the start operation and
                # lets the probabilistic contract stage invent a false blocker.
                try:
                    return self._registered_binding(
                        {
                            "action": forced_action,
                            "args": {},
                            "binding_reason": (
                                "screen identity is compiled from the frozen "
                                "instance root and component role"
                            ),
                            "resolved_from_evidence": [],
                        },
                        step,
                        grounded_context,
                        plan.resources,
                    )
                except (KeyError, TypeError, ValueError):
                    # Keep the existing bounded contract repair path for older
                    # plans that genuinely lack a root or atomic role.
                    pass
            try:
                return self._complete_registered_action_contract(
                    data={
                        "action": forced_action,
                        "binding_reason": (
                            "new container creation requires the dedicated "
                            "creation capability"
                        ),
                        "resolved_from_evidence": [],
                    },
                    semantic_step=step,
                    grounded_context=grounded_context,
                    initial_error=(
                        "bind the frozen new-container creation capability"
                    ),
                    plan_resources=plan.resources,
                )
            except (KeyError, TypeError, ValueError, OSError) as exc:
                raise ExecutionBindingError(
                    "required action contract failed for %s: %s"
                    % (forced_action, exc),
                    replan_recommended=True,
                    category="capability_contract_invalid",
                ) from exc
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
        shell_candidate_attempts = 0
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
                if _implementation_item_is_verification(
                    {"title": step.title, "objective": step.objective}
                ):
                    step.expected_changes = []
                    step.risk = "readonly"
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
                    selected_spec = self.action_registry.get(action)
                    if (
                        selected_spec is None
                        or action not in DIRECT_PRIVILEGED_ACTIONS
                    ):
                        raise ValueError(
                            "action_not_directly_registered=%s"
                            % (action or "<missing>")
                        )
                    if (
                        _step_is_verification(step)
                        and selected_spec.effects
                    ):
                        raise ImplementationRejected(
                            "registered_action:%s" % action,
                            "readonly verification cannot bind a mutating Action",
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
                            plan_resources=plan.resources,
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
                    if (
                        shell_candidate_attempts
                        >= MAX_SHELL_CANDIDATE_ATTEMPTS
                    ):
                        raise ExecutionBindingError(
                            "Shell 候选次数已耗尽，无法安全实现该语义步骤",
                            replan_recommended=True,
                            category="capability_mismatch",
                        )
                    shell_candidate_attempts += 1
                    self._progress(
                        "选择结论：步骤“%s”没有匹配的注册 Action；"
                        "进入一次性脚本合同阶段（候选 %s/%s）。"
                        % (
                            _progress_text(step.title or step.objective),
                            shell_candidate_attempts,
                            MAX_SHELL_CANDIDATE_ATTEMPTS,
                        )
                    )
                    try:
                        binding = self._complete_shell_contract(
                            selection=data,
                            semantic_step=step,
                            grounded_context=grounded_context,
                            plan_resources=plan.resources,
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
                    if (
                        not _step_is_verification(step)
                        or step.risk != "readonly"
                        or step.expected_changes
                    ):
                        raise ImplementationRejected(
                            "verification_only",
                            "verification_only cannot replace a mutation-shaped objective",
                        )
                    try:
                        binding = self._complete_verification_only_contract(
                            selection=data,
                            semantic_step=step,
                            grounded_context=grounded_context,
                            plan_resources=plan.resources,
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
                    blocker_category = str(
                        data.get("shell_blocker_category") or ""
                    ).strip().lower()
                    hard_shell_blocker = (
                        blocker_category in HARD_SHELL_BLOCKER_CATEGORIES
                    )
                    if (
                        shell_candidate_attempts == 0
                        and not shell_fallback_reviewed
                    ):
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
                    if (
                        shell_candidate_attempts
                        < MAX_SHELL_CANDIDATE_ATTEMPTS
                        and not hard_shell_blocker
                    ):
                        self._progress(
                            "实现结论：仅尝试了 %s/%s 个 Shell 候选，"
                            "且未提供硬性阻断；拒绝提前 blocked，正在要求"
                            "一个实质不同的候选。"
                            % (
                                shell_candidate_attempts,
                                MAX_SHELL_CANDIDATE_ATTEMPTS,
                            )
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "A blocked result is premature. Only %s of %s"
                                    " distinct shell candidates have been attempted."
                                    " Select shell_artifact and design a materially"
                                    " different bounded implementation, or return"
                                    " blocked with one truthful hard category:"
                                    " hard_policy, unverifiable, or no_safe_command."
                                    " A previous candidate validation failure is not"
                                    " itself a hard blocker. Previous reason: %s"
                                    % (
                                        shell_candidate_attempts,
                                        MAX_SHELL_CANDIDATE_ATTEMPTS,
                                        reason[:1000],
                                    )
                                ),
                            }
                        )
                        continue
                    self._progress(
                        "实现结论：当前证据下无法安全实现步骤“%s”"
                        "（Shell 候选=%s/%s%s）。"
                        % (
                            _progress_text(step.title or step.objective),
                            shell_candidate_attempts,
                            MAX_SHELL_CANDIDATE_ATTEMPTS,
                            (
                                "，硬性阻断=%s" % blocker_category
                                if hard_shell_blocker
                                else "，候选已耗尽"
                            ),
                        )
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
                if exc.implementation == "shell_artifact":
                    if (
                        shell_candidate_attempts
                        >= MAX_SHELL_CANDIDATE_ATTEMPTS
                    ):
                        raise ExecutionBindingError(
                            "所有 Shell 候选均无法安全完成语义步骤：%s"
                            % "；".join(rejected_implementations),
                            replan_recommended=True,
                            category="capability_mismatch",
                        ) from exc
                    self._progress(
                        "实现结论：Shell 候选 %s/%s 校验失败（%s）；"
                        "正在要求实质不同的 Shell 候选。"
                        % (
                            shell_candidate_attempts,
                            MAX_SHELL_CANDIDATE_ATTEMPTS,
                            _progress_text(str(exc), 180),
                        )
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Shell candidate %s of %s failed contract or"
                                " safety validation: %s. Select shell_artifact"
                                " again with a materially different implementation;"
                                " do not repeat the rejected script or merely repair"
                                " its formatting. You may return blocked early only"
                                " for a truthful hard_policy, unverifiable, or"
                                " no_safe_command blocker."
                                % (
                                    shell_candidate_attempts,
                                    MAX_SHELL_CANDIDATE_ATTEMPTS,
                                    str(exc)[:1000],
                                )
                            ),
                        }
                    )
                    continue
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
            "frozen_plan_resources": _resource_manifest_payload(
                plan.resources
            ),
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
        plan_resources: list[PlanResource] | None = None,
    ) -> ExecutionBinding:
        action = str(data.get("action") or "").strip()
        spec = self.action_registry.get(action)
        if spec is None or action not in DIRECT_PRIVILEGED_ACTIONS:
            raise ValueError(
                "action_not_directly_registered=%s"
                % (action or "<missing>")
            )
        if (
            _step_is_verification(semantic_step)
            and spec.effects
        ):
            raise ValueError(
                "readonly_verification_cannot_bind_mutating_action=%s"
                % action
            )
        args = _json_object_value(data.get("args"))
        if args is None:
            raise ValueError(
                "action=%s args must be an object, got=%s"
                % (action, type(data.get("args")).__name__)
            )
        semantic_text = " ".join(
            [semantic_step.title, semantic_step.objective, *semantic_step.expected_changes]
        )
        if (
            action == "edit_text_file"
            and str(args.get("operation") or "").strip().lower() == "replace_once"
            and str(args.get("anchor") or "")
            and re.search(r"\b(?:remove|delete)\b|移除|删除", semantic_text, re.I)
        ):
            action = "replace_text_in_file"
            args = {
                "path": args.get("path"),
                "old_text": args.get("anchor"),
                "new_text": "",
            }
            spec = self.action_registry.get(action)
            if spec is None or action not in DIRECT_PRIVILEGED_ACTIONS:
                raise ValueError("action_not_directly_registered=%s" % action)
        if (
            action == "edit_text_file"
            and str(args.get("operation") or "").strip().lower() == "replace_file"
            and str(args.get("path") or "").strip().endswith(".py")
            and re.search(r"\b(?:remove|delete)\b|移除|删除", semantic_text, re.I)
        ):
            exact_removal = _grounded_python_removal_from_semantics(
                semantic_text,
                grounded_context,
            )
            if exact_removal:
                action = "replace_text_in_file"
                args = {
                    "path": args.get("path"),
                    "old_text": exact_removal,
                    "new_text": "",
                }
                spec = self.action_registry.get(action)
                if spec is None or action not in DIRECT_PRIVILEGED_ACTIONS:
                    raise ValueError("action_not_directly_registered=%s" % action)
        if action == "replace_text_in_file" and not str(args.get("new_text") or ""):
            removes_definition_and_call = bool(
                re.search(r"\bfunction\s+definition\b|函数定义", semantic_text, re.I)
                and re.search(r"\b(?:unconditional\s+)?call\b|无条件调用|调用", semantic_text, re.I)
            )
            exact_semantic_removal = (
                _grounded_python_removal_from_semantics(
                    semantic_text, grounded_context,
                )
                if removes_definition_and_call else ""
            )
            args["old_text"] = exact_semantic_removal or (
                _expand_grounded_python_function_removal(
                    str(args.get("old_text") or ""),
                    grounded_context,
                )
            )
            if removes_definition_and_call and not re.search(
                r"(?m)^def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(",
                str(args.get("old_text") or ""),
            ):
                raise ValueError(
                    "python_function_and_call_removal_not_grounded"
                )
        args = _infer_structural_action_args(
            action,
            args,
            plan_resources or [],
        )
        args = _infer_semantic_action_args(action, args, semantic_step)
        # Semantic inference repairs model-supplied role/root values, but the
        # frozen manifest remains the final authority for the exact consumer
        # slot (notably instance-specific screen names).  Inject it last so a
        # deterministic fallback such as ``test_w`` cannot overwrite a
        # planned ``test_vemu_uestc_w`` resource.
        args = _inject_frozen_resource_args(
            semantic_step,
            args,
            plan_resources or [],
        )
        if action == "stop_klonet_component":
            semantic_text = " ".join(
                [semantic_step.title, semantic_step.objective, *semantic_step.expected_changes]
            )
            role = _atomic_runtime_role(semantic_text)
            root = _semantic_runtime_root(semantic_text)
            port_match = re.search(r"\b([1-9]\d{3,4})\b", semantic_text)
            pid = next(
                (
                    resource.value
                    for resource in plan_resources or []
                    if resource.status == "frozen"
                    and resource.kind == "identifier"
                    and "pid" in "%s %s" % (resource.name, resource.role)
                    and (not role or role in "%s %s" % (resource.name, resource.role))
                    and any(
                        semantic_step.step_id == str(consumer).rsplit(".", 1)[0]
                        for consumer in resource.consumers
                    )
                ),
                None,
            )
            if isinstance(pid, list):
                numeric_pids = []
                for value in pid:
                    try:
                        numeric_pids.append(int(value))
                    except (TypeError, ValueError):
                        continue
                pid = min(numeric_pids) if numeric_pids else None
            elif isinstance(pid, str) and pid.strip().startswith("["):
                try:
                    parsed_pids = json.loads(pid)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_pids = []
                numeric_pids = []
                if isinstance(parsed_pids, list):
                    for value in parsed_pids:
                        try:
                            numeric_pids.append(int(value))
                        except (TypeError, ValueError):
                            continue
                pid = min(numeric_pids) if numeric_pids else None
            if role:
                args["component"] = role
            if root:
                args["runtime_cwd"] = root
            if port_match:
                args["port"] = port_match.group(1)
            if pid is not None:
                args["pid"] = pid
        if action in {
            "set_python_class_attribute",
            "start_screen_component",
            "restart_screen_component",
        }:
            for port_role in ("master_port", "worker_port"):
                scoped_port = _scoped_role_port_value(
                    semantic_step,
                    plan_resources or [],
                    port_role,
                )
                if scoped_port is not None:
                    args[port_role] = scoped_port
        if action == "set_python_class_attribute":
            semantic_root = _semantic_runtime_root(
                " ".join([
                    semantic_step.title,
                    semantic_step.objective,
                    *semantic_step.expected_changes,
                ])
            )
            if not semantic_root or not Path(semantic_root).is_dir():
                semantic_root = _runtime_root_from_frozen_paths(
                    plan_resources or [],
                )
            if semantic_root:
                args["path"] = semantic_root.rstrip("/") + "/vemu_config/config.py"
            attribute = str(args.get("attribute") or "").strip()
            if (
                str(args.get("path") or "").endswith("/vemu_config/config.py")
                and attribute in {
                    "master_port", "worker_port", "public_port",
                    "web_terminal_port", "master_ip", "mysql_ip",
                    "rabbitmq_ip", "redis_port", "mysql_port",
                    "rabbitmq_port", "celery_redis_port_db",
                    "celery_rabbitmq_port_db",
                }
            ):
                # Resolve PROJ_CONFIG at execution time. A model-provided
                # instance label must never select a Python class to mutate.
                args.pop("class_name", None)
            scoped_value = _scoped_role_port_value(
                semantic_step,
                plan_resources or [],
                attribute,
            )
            if scoped_value is not None:
                args["value"] = scoped_value
            elif attribute and attribute in args:
                args["value"] = args[attribute]
        if action in {"start_screen_component", "restart_screen_component"}:
            session = str(args.get("screen_session") or "").strip()
            component = str(args.get("component") or "").strip()
            suffix = {
                "master": "_m",
                "worker": "_w",
                "celery": "_c",
                "web_terminal": "_web",
            }.get(component, "")
            semantic_root = _semantic_runtime_root(
                " ".join([
                    semantic_step.title,
                    semantic_step.objective,
                    *semantic_step.expected_changes,
                ])
            )
            if not semantic_root or not Path(semantic_root).is_dir():
                semantic_root = _runtime_root_from_frozen_paths(
                    plan_resources or [],
                )
            if semantic_root:
                root_path = Path(semantic_root)
                frozen_session = any(
                    resource.status == "frozen"
                    and resource.role == "screen_session"
                    and str(resource.value) == session
                    and suffix
                    and session.endswith(suffix)
                    for resource in plan_resources or []
                )
                platform_name = (
                    session[:-len(suffix)]
                    if frozen_session and suffix
                    else (
                        root_path.parent.name
                        if root_path.name == "vemu_uestc"
                        and root_path.parent.name == "test"
                        else root_path.name
                    )
                )
                args["platform"] = platform_name
                args["project_root"] = str(root_path.resolve())
                if suffix and not frozen_session:
                    args["screen_session"] = "%s%s" % (
                        platform_name, suffix,
                    )
                    session = str(args["screen_session"])
            if (
                suffix
                and "/test/" in semantic_root
                and not re.search(
                    r"(?:^|[_-])test(?:$|[_-])", session, re.I
                )
            ):
                args["platform"] = "test"
                args["screen_session"] = "test%s" % suffix
                session = str(args["screen_session"])
            if suffix and session.endswith(suffix):
                args["platform"] = session[:-len(suffix)]
            # The runtime canonicalizer may derive a convenient platform name
            # from the project directory or Screen session.  An instance
            # identifier frozen by the semantic plan is stronger evidence and
            # must win, especially when an atomic child step inherited the
            # parent step's resource consumer.
            frozen_platform = next(
                (
                    resource.value
                    for resource in plan_resources or []
                    if resource.status == "frozen"
                    and resource.role == "instance_identifier"
                    and any(
                        str(consumer).rsplit(".", 1)[-1] == "platform"
                        and (
                            semantic_step.step_id
                            == str(consumer).rsplit(".", 1)[0]
                            or semantic_step.step_id.startswith(
                                str(consumer).rsplit(".", 1)[0] + "__"
                            )
                        )
                        for consumer in resource.consumers
                    )
                ),
                None,
            )
            if frozen_platform is not None:
                args["platform"] = frozen_platform
                if suffix and not frozen_session:
                    args["screen_session"] = "%s%s" % (
                        frozen_platform, suffix,
                    )
        problem = _validate_action_evidence(action, args, grounded_context)
        if problem:
            raise ValueError(problem)
        missing = [
            key
            for key in REQUIRED_ACTION_ARGS.get(action, ())
            if key not in args
            or args.get(key) is None
            or (
                not args.get(key)
                and not (action == "replace_text_in_file" and key == "new_text")
            )
        ]
        if missing:
            raise ValueError(
                "action=%s missing_required_args=%s"
                % (action, ",".join(missing))
            )
        problem = self.action_registry.validate_args(spec, args)
        if problem:
            raise ValueError(problem)
        problem = _validate_mutating_action_paths(
            action,
            args,
            plan_resources or [],
        )
        if problem:
            raise ValueError(problem)
        problem = _validate_action_objective_fit(action, semantic_step)
        if problem:
            raise ValueError(problem)
        problem = _validate_action_contract_consistency(
            action, args, semantic_step
        )
        if problem:
            raise ValueError(problem)
        problem = _validate_action_semantics(action, args)
        if problem:
            raise ValueError(problem)
        if grounded_context is not None:
            problem = _validate_host_facts(action, args)
            if problem and not _host_fact_is_planned_future(
                action,
                args,
                semantic_step,
                problem,
                plan_resources or [],
            ):
                raise ValueError(problem)
        _validate_action_resource_bindings(
            semantic_step,
            action,
            args,
            plan_resources or [],
        )
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
        preconditions = self._valid_checks(data.get("preconditions"))
        preconditions = _canonical_action_preconditions(
            action, args, preconditions,
        )
        checks = self._valid_checks(data.get("postconditions"))
        checks = checks or _default_action_postconditions(action, args)
        checks = _canonical_action_postconditions(action, args, checks)
        if not checks:
            checks = [{"checker": "exit_code_zero", "args": {}}]
        problem = _validate_action_postcondition_fit(action, args, checks)
        if problem:
            raise ValueError(problem)
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
            preconditions=preconditions,
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
        plan_resources: list[PlanResource] | None = None,
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
            "optional_args": _optional_action_args(action),
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
            "frozen_plan_resources": _resource_manifest_payload(
                plan_resources or []
            ),
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
                    plan_resources,
                )
                self._progress(
                    "实现节点：注册 Action“%s”的参数合同已补全。"
                    % _progress_text(action)
                )
                return binding
            except (KeyError, TypeError, ValueError, OSError) as exc:
                last_error = str(exc)
                if attempt >= MAX_ACTION_CONTRACT_REPAIRS:
                    break
                self._progress(
                    "实现节点：Action 参数合同无效（%s），正在请求第 %s 次定向修复…"
                    % (_progress_text(last_error, 240), attempt)
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
        plan_resources: list[PlanResource] | None = None,
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
            "frozen_plan_resources": _resource_manifest_payload(
                plan_resources or []
            ),
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
                    plan_resources,
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
                    "实现节点：Shell 合同不完整（%s），正在请求第 %s 次定向修复…"
                    % (_progress_text(str(exc), 180), attempt)
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
        plan_resources: list[PlanResource] | None = None,
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
        allowed_future_cwds = list(_planned_output_paths(semantic_step))
        for resource in plan_resources or []:
            if resource.status != "frozen" or resource.kind != "path":
                continue
            if any(
                semantic_step.step_id == consumer.rsplit(".", 1)[0]
                or semantic_step.step_id.startswith(
                    consumer.rsplit(".", 1)[0] + "__"
                )
                for consumer in resource.consumers
            ):
                allowed_future_cwds.append(resource.value)
        problem = self.shell_policy.validate(
            artifact,
            allowed_future_cwds=tuple(allowed_future_cwds),
        )
        if problem:
            raise ValueError(problem)
        _validate_shell_resource_bindings(
            semantic_step,
            artifact,
            plan_resources or [],
        )
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
        plan_resources: list[PlanResource] | None = None,
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
            "frozen_plan_resources": _resource_manifest_payload(
                plan_resources or []
            ),
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
                problem = _validate_checker_resource_scope(
                    semantic_step,
                    checks,
                    plan_resources or [],
                )
                if problem:
                    raise ValueError(problem)
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
        return _merge_alternative_checker_contracts(result), errors

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
                    "shell_blocker_category": {
                        "type": "string",
                        "enum": [
                            "",
                            "hard_policy",
                            "unverifiable",
                            "no_safe_command",
                        ],
                    },
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
                        "maxItems": 16,
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
        optional = _optional_action_args(action)
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
    if name in {
        "packages",
        "argv",
        "sources",
        "entries",
        "port_bindings",
        "environment",
        "command",
    }:
        return {"type": "array", "items": {"type": "string"}}
    if name in {"patch", "credential_source"}:
        return {"type": "object", "additionalProperties": True}
    if name in {"pid", "expected_port"}:
        return {"type": "integer"}
    return {"type": "string"}


def _optional_action_args(action: str) -> list[str]:
    return {
        "install_nginx_config": ["source_path", "content"],
        "edit_text_file": ["anchor"],
        "upsert_python_class": ["base_class"],
        "set_python_config_assignment": ["assignment_name"],
        "set_python_class_attribute": ["class_name"],
        "create_docker_container": [
            "environment", "restart_policy", "command", "credential_source"
        ],
        "git_operation": [
            "url", "remote", "ref", "revision", "path", "tag", "create",
            "force_with_lease",
        ],
    }.get(action, [])


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


def _remove_outer_semantic_dependencies(
    items: list[dict[str, Any]],
    semantic_dependencies: list[str],
) -> None:
    """Keep the outer DAG edge out of the implementation-local namespace."""

    outer = {str(value) for value in semantic_dependencies}
    if not outer:
        return
    for item in items:
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list):
            continue
        item["depends_on"] = [
            value for value in dependencies if str(value) not in outer
        ]


def _collapse_redundant_container_starts(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Treat create_docker_container as the create-and-start atomic capability."""

    create_by_id: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if not re.search(r"\b(?:docker\s+)?container\b|容器", text, re.I):
            continue
        if not re.search(r"\b(?:create|provision)\b|创建|新建", text, re.I):
            continue
        service = next(
            (
                marker
                for marker in ("mysql", "redis", "rabbitmq")
                if marker in text.lower()
            ),
            "container",
        )
        create_by_id[item_id] = service
    replacements: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        primary = "%s %s" % (
            item.get("title") or "",
            item.get("objective") or "",
        )
        if not re.match(r"^\s*(?:start|launch|启动)", primary, re.I):
            continue
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            creator_id = _safe_implementation_step_id(dependency, fallback="")
            service = create_by_id.get(creator_id)
            if service is None:
                continue
            if service != "container" and service not in primary.lower():
                continue
            replacements[item_id] = creator_id
            break
    if not replacements:
        return items

    def resolve(item_id: str) -> str:
        seen = set()
        while item_id in replacements and item_id not in seen:
            seen.add(item_id)
            item_id = replacements[item_id]
        return item_id

    normalized = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        if item_id in replacements:
            continue
        dependencies = item.get("depends_on")
        if isinstance(dependencies, list):
            rewired = []
            for dependency in dependencies:
                target = resolve(
                    _safe_implementation_step_id(dependency, fallback="")
                )
                if target and target != item_id and target not in rewired:
                    rewired.append(target)
            item["depends_on"] = rewired
        normalized.append(item)
    return normalized


def _collapse_redundant_nginx_activations(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """One semantic Nginx activation compiles to exactly one reload action."""

    semantic_text = "%s %s" % (semantic_step.title, semantic_step.objective)
    if not (
        re.search(r"nginx", semantic_text, re.I)
        and re.search(r"\b(?:activate|reload|restart)\b|激活|重载|重新加载", semantic_text, re.I)
    ):
        return items
    activation_ids = []
    for index, item in enumerate(items, start=1):
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if re.search(r"nginx", text, re.I) and re.search(
            r"\b(?:reload|restart)\b|重载|重新加载", text, re.I
        ):
            activation_ids.append(
                _safe_implementation_step_id(
                    item.get("id"), fallback="step-%s" % index
                )
            )
    if len(activation_ids) <= 1:
        return items
    keeper = activation_ids[0]
    replacements = {item_id: keeper for item_id in activation_ids[1:]}
    normalized = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        if item_id in replacements:
            continue
        dependencies = item.get("depends_on")
        if isinstance(dependencies, list):
            rewired = []
            for dependency in dependencies:
                target = replacements.get(
                    _safe_implementation_step_id(dependency, fallback=""),
                    _safe_implementation_step_id(dependency, fallback=""),
                )
                if target and target != item_id and target not in rewired:
                    rewired.append(target)
            item["depends_on"] = rewired
        normalized.append(item)
    return normalized


def _collapse_redundant_container_policy_steps(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Creation already applies the audited default unless-stopped policy."""

    create_ids = set()
    for index, item in enumerate(items, start=1):
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if re.search(r"containers?\b|容器", text, re.I) and re.search(
            r"\b(?:create|provision)\b|创建|新建", text, re.I
        ):
            create_ids.add(
                _safe_implementation_step_id(
                    item.get("id"), fallback="step-%s" % index
                )
            )
    replacements = {}
    for index, item in enumerate(items, start=1):
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if not re.search(r"restart\s+policy|重启策略", text, re.I):
            continue
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list):
            continue
        creator = next(
            (
                _safe_implementation_step_id(value, fallback="")
                for value in dependencies
                if _safe_implementation_step_id(value, fallback="") in create_ids
            ),
            "",
        )
        if creator:
            replacements[
                _safe_implementation_step_id(
                    item.get("id"), fallback="step-%s" % index
                )
            ] = creator
    if not replacements:
        return items
    normalized = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        if item_id in replacements:
            continue
        dependencies = item.get("depends_on")
        if isinstance(dependencies, list):
            rewired = []
            for dependency in dependencies:
                target = replacements.get(
                    _safe_implementation_step_id(dependency, fallback=""),
                    _safe_implementation_step_id(dependency, fallback=""),
                )
                if target and target != item_id and target not in rewired:
                    rewired.append(target)
            item["depends_on"] = rewired
        normalized.append(item)
    return normalized


def _collapse_redundant_container_setup_steps(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Creation already resolves allowlisted Redis credentials atomically."""

    semantic_text = "%s %s" % (
        semantic_step.title, semantic_step.objective
    )
    if not (
        re.search(r"redis", semantic_text, re.I)
        and re.search(r"containers?\b|容器", semantic_text, re.I)
        and re.search(r"\b(?:create|provision)\b|创建|新建", semantic_text, re.I)
    ):
        return items
    creator = ""
    for index, item in enumerate(items, start=1):
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if re.search(r"redis", text, re.I) and re.search(
            r"\b(?:create|provision)\b|创建|新建", text, re.I
        ):
            creator = _safe_implementation_step_id(
                item.get("id"), fallback="step-%s" % index
            )
            break
    if not creator:
        return items
    replacements = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        if item_id == creator:
            continue
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if (
            re.search(r"redis", text, re.I)
            and re.search(r"password|credentials?|密码|凭据", text, re.I)
            and re.search(r"\b(?:configure|set|ensure)\b|配置|设置", text, re.I)
        ):
            replacements[item_id] = creator
    if not replacements:
        return items
    normalized = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        if item_id in replacements:
            continue
        dependencies = item.get("depends_on")
        if isinstance(dependencies, list):
            rewired = []
            for dependency in dependencies:
                target = replacements.get(
                    _safe_implementation_step_id(dependency, fallback=""),
                    _safe_implementation_step_id(dependency, fallback=""),
                )
                if target and target != item_id and target not in rewired:
                    rewired.append(target)
            item["depends_on"] = rewired
        normalized.append(item)
    return normalized


def _deterministic_klonet_config_items(
    plan: PrivilegedPlan,
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Compile the complete same-host WtxConfig contract from frozen resources."""

    text = " ".join(
        [
            semantic_step.title,
            semantic_step.objective,
            *semantic_step.expected_changes,
        ]
    )
    if (
        re.search(r"nginx", text, re.I)
        and not re.search(r"wtxconfig|config\.py", text, re.I)
    ):
        return []
    config_semantic = bool(
        re.search(r"wtxconfig", text, re.I)
        or (
            re.search(r"config\.py|\bconfig(?:uration)?\b|配置", text, re.I)
            and re.search(r"settings?|ports?|ips?|endpoints?|配置|设置|端口", text, re.I)
        )
    )
    if not (
        config_semantic
        and re.search(r"complete|isolated|完整|隔离", "%s %s" % (plan.goal, text), re.I)
    ):
        return []
    def scoped(resource: PlanResource) -> bool:
        return any(
            str(consumer).partition(".")[0] == semantic_step.step_id
            for consumer in resource.consumers
        )

    config_path = next(
        (
            str(resource.value)
            for resource in plan.resources
            if resource.status == "frozen"
            and resource.kind == "path"
            and str(resource.value).endswith("/vemu_config/config.py")
            and scoped(resource)
        ),
        "",
    )
    ports = {
        str(resource.role): int(resource.value)
        for resource in plan.resources
        if resource.status == "frozen"
        and resource.kind == "port"
        and scoped(resource)
        and str(resource.role) in {
            "master_port", "worker_port", "web_terminal_port", "public_port",
            "redis_port", "mysql_port", "rabbitmq_port",
        }
    }
    required = {
        "master_port", "worker_port", "web_terminal_port",
        "redis_port", "mysql_port", "rabbitmq_port",
    }
    if not config_path or not required <= set(ports):
        return []
    attributes: list[tuple[str, Any]] = [
        ("master_ip", "127.0.0.1"),
        ("mysql_ip", "127.0.0.1"),
        ("rabbitmq_ip", "127.0.0.1"),
        ("master_port", ports["master_port"]),
        ("worker_port", ports["worker_port"]),
        ("web_terminal_port", ports["web_terminal_port"]),
    ]
    if "public_port" in ports:
        attributes.append(("public_port", ports["public_port"]))
    attributes.extend(
        [
            ("redis_port", ports["redis_port"]),
            ("mysql_port", ports["mysql_port"]),
            ("rabbitmq_port", ports["rabbitmq_port"]),
            ("celery_redis_port_db", "%s/6" % ports["redis_port"]),
            ("celery_rabbitmq_port_db", "%s/7" % ports["redis_port"]),
        ]
    )
    items = []
    previous = ""
    for index, (attribute, value) in enumerate(attributes, start=1):
        item_id = "set-%s" % attribute.replace("_", "-")
        items.append(
            {
                "id": item_id,
                "title": "Set WtxConfig %s" % attribute,
                "objective": (
                    "Set class WtxConfig attribute %s to %r in %s"
                    % (attribute, value, config_path)
                ),
                "reason": "compile the frozen complete isolated runtime contract",
                "depends_on": [previous] if previous else [],
                "expected_changes": ["WtxConfig.%s becomes %r" % (attribute, value)],
                "success_criteria": ["WtxConfig.%s equals %r" % (attribute, value)],
                "risk_suggestion": "medium",
                "attribute": attribute,
                "value": value,
            }
        )
        previous = item_id
    items.append(
        {
            "id": "verify-proj-config",
            "title": "Verify PROJ_CONFIG remains WtxConfig",
            "objective": "Verify PROJ_CONFIG is an instance of WtxConfig",
            "reason": "prove active configuration selection",
            "depends_on": [previous],
            "expected_changes": [],
            "success_criteria": ["PROJ_CONFIG uses WtxConfig"],
            "risk_suggestion": "readonly",
        }
    )
    return items


def _deterministic_runtime_stop_items(
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Compile a scoped orphan-runtime stop without model-expanded targets."""

    text = " ".join(
        [semantic_step.title, semantic_step.objective, *semantic_step.expected_changes]
    )
    if not (
        re.match(
            r"^\s*(?:(?:precisely|safely|only|精确|安全|仅)\s*)?"
            r"(?:stop|terminate|停止|终止)",
            text,
            re.I,
        )
        and re.search(r"\bworker\b|工作进程", text, re.I)
        and re.search(r"\b(?:port|listener)\b|端口|监听", text, re.I)
    ):
        return []
    root = _semantic_runtime_root(text)
    port_match = re.search(r"\b([1-9]\d{3,4})\b", text)
    if not root or port_match is None:
        return []
    port = int(port_match.group(1))
    return [
        {
            "id": "stop-root-worker",
            "title": "Stop root-bound worker runtime",
            "objective": "Stop only worker processes for %s owning port %s"
            % (root, port),
            "reason": "release the conflicting listener with PID/cwd/role/port checks",
            "depends_on": [],
            "expected_changes": ["root-bound worker listener on %s stops" % port],
            "success_criteria": ["port %s is released by that root" % port],
            "risk_suggestion": "high",
        }
    ]


def _split_multi_attribute_config_items(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
    resources: list[PlanResource],
) -> list[dict[str, Any]]:
    """Compile plural scalar config edits into singular Action-sized steps."""

    fields = (
        "master_ip", "mysql_ip", "rabbitmq_ip", "master_port",
        "worker_port", "web_terminal_port", "public_port", "redis_port",
        "mysql_port", "rabbitmq_port", "celery_redis_port_db",
        "celery_rabbitmq_port_db",
    )
    semantic_id = semantic_step.step_id
    scoped_values = {
        str(resource.role or resource.name): resource.value
        for resource in resources
        if resource.status == "frozen"
        and any(
            consumer.rsplit(".", 1)[0] == semantic_id
            for consumer in resource.consumers
            if "." in consumer
        )
    }
    expanded: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("objective") or ""),
                *[str(value) for value in item.get("expected_changes", [])],
            ]
        )
        mentioned = [
            field
            for field in fields
            if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(field), text)
            and field in scoped_values
        ]
        if len(mentioned) < 2:
            expanded.append(dict(item))
            continue
        previous = ""
        original_dependencies = list(item.get("depends_on") or [])
        for field in mentioned:
            atomic_id = "%s-%s" % (item_id, field.replace("_", "-"))
            value = scoped_values[field]
            expanded.append(
                {
                    "id": atomic_id,
                    "title": "Set WtxConfig %s" % field,
                    "objective": "Set WtxConfig.%s to the frozen value %r" % (field, value),
                    "reason": str(item.get("reason") or "apply the frozen configuration contract"),
                    "depends_on": [previous] if previous else original_dependencies,
                    "expected_changes": ["WtxConfig.%s becomes %r" % (field, value)],
                    "success_criteria": ["WtxConfig.%s equals %r" % (field, value)],
                    "risk_suggestion": str(item.get("risk_suggestion") or "medium"),
                }
            )
            previous = atomic_id
        replacements[item_id] = previous
    if not replacements:
        return expanded
    for item in expanded:
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list):
            continue
        item["depends_on"] = [
            replacements.get(
                _safe_implementation_step_id(dependency, fallback=""),
                _safe_implementation_step_id(dependency, fallback=""),
            )
            for dependency in dependencies
        ]
    return expanded


def _split_multi_component_runtime_items(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Compile plural runtime starts into one component capability per step."""

    expanded: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    role_patterns = (
        ("master", r"(?<![A-Za-z0-9_])master(?![A-Za-z0-9_])"),
        ("celery", r"(?<![A-Za-z0-9_])celery(?![A-Za-z0-9_])"),
        ("web_terminal", r"web[_ -]?terminal"),
        ("worker", r"(?<![A-Za-z0-9_])worker(?![A-Za-z0-9_])"),
    )
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("objective") or ""),
                *[str(value) for value in item.get("expected_changes", [])],
            ]
        )
        roles = [role for role, pattern in role_patterns if re.search(pattern, text, re.I)]
        for role in re.findall(
            r"\bmanaged\s+component\s+([A-Za-z][A-Za-z0-9_-]{0,63})\b",
            text,
            re.I,
        ):
            normalized = role.lower().replace("-", "_")
            if normalized not in roles:
                roles.append(normalized)
        starts = re.search(r"\b(?:start|restart|restore|recover|launch)\b|启动|重启|恢复", text, re.I)
        semantic_roles = _semantic_runtime_components(semantic_step)
        if starts and len(semantic_roles) > len(roles) and re.search(
            r"platform|runtime|components?|roles?|平台|运行时|组件|角色",
            text,
            re.I,
        ):
            roles = semantic_roles
        if not starts or len(roles) < 2:
            expanded.append(dict(item))
            continue
        previous = ""
        original_dependencies = list(item.get("depends_on") or [])
        dispositions = _runtime_component_dispositions(semantic_step)
        for role in roles:
            atomic_id = "%s-%s" % (item_id, role.replace("_", "-"))
            verb = dispositions.get(role, "Start")
            expanded.append(
                {
                    "id": atomic_id,
                    "title": "%s %s screen component" % (verb, role),
                    "objective": "%s the %s role for %s" % (
                        verb, role,
                        semantic_step.objective or semantic_step.title,
                    ),
                    "reason": str(item.get("reason") or "restore the runtime role"),
                    "depends_on": [previous] if previous else original_dependencies,
                    "expected_changes": ["%s role starts" % role],
                    "success_criteria": ["%s role is running" % role],
                    "risk_suggestion": str(item.get("risk_suggestion") or "medium"),
                }
            )
            previous = atomic_id
        replacements[item_id] = previous
    if not replacements:
        return expanded
    for item in expanded:
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list):
            continue
        item["depends_on"] = [
            replacements.get(
                _safe_implementation_step_id(dependency, fallback=""),
                _safe_implementation_step_id(dependency, fallback=""),
            )
            for dependency in dependencies
        ]
    return expanded


def _collapse_redundant_config_mutations(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one atomic WtxConfig write per attribute and rewire its users."""

    kept: list[dict[str, Any]] = []
    owner_by_attribute: dict[str, str] = {}
    replacements: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("objective") or ""),
                *[str(value) for value in item.get("expected_changes", [])],
            ]
        )
        attributes = [
            attribute
            for attribute in (
                "master_port", "worker_port", "public_port", "web_terminal_port",
                "redis_port", "mysql_port", "rabbitmq_port", "master_ip",
                "mysql_ip", "rabbitmq_ip", "celery_redis_port_db",
                "celery_rabbitmq_port_db",
            )
            if re.search(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(attribute),
                text,
            )
        ]
        config_write = bool(
            len(attributes) == 1
            and _contains_mutation_intent(text)
            and re.search(
                r"config|WtxConfig|配置|"
                r"\b(?:set|update|change)\b|设置|修改|更新|改为",
                text,
                re.I,
            )
        )
        if not config_write:
            kept.append(item)
            continue
        attribute = attributes[0]
        owner = owner_by_attribute.get(attribute)
        if owner is None:
            owner_by_attribute[attribute] = item_id
            kept.append(item)
            continue
        replacements[item_id] = owner
    if not replacements:
        return kept
    for item in kept:
        dependencies = []
        for raw in item.get("depends_on") or []:
            dependency = _safe_implementation_step_id(raw, fallback="")
            while dependency in replacements:
                dependency = replacements[dependency]
            current = _safe_implementation_step_id(item.get("id"), fallback="")
            if dependency and dependency != current and dependency not in dependencies:
                dependencies.append(dependency)
        item["depends_on"] = dependencies
    return kept


def _drop_unauthorized_config_mutations(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Drop scalar config writes that the semantic change never authorizes."""

    fields = (
        "master_port", "worker_port", "public_port", "web_terminal_port",
        "redis_port", "mysql_port", "rabbitmq_port", "master_ip",
        "mysql_ip", "rabbitmq_ip", "celery_redis_port_db",
        "celery_rabbitmq_port_db",
    )
    fragments = [
        semantic_step.title,
        semantic_step.objective,
        *semantic_step.expected_changes,
    ]
    semantic_text = " ".join(str(fragment) for fragment in fragments)
    if re.search(
        r"complete.{0,30}(?:config|contract)|(?:完整|全部).{0,20}配置",
        semantic_text,
        re.I,
    ):
        return items
    preservation = re.compile(
        r"\b(?:preserve|keep|remain|unchanged|do not (?:change|modify))\b|"
        r"保持|保留|不变|不修改",
        re.I,
    )
    authorized = {
        field
        for field in fields
        if any(
            re.search(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(field),
                str(fragment),
                re.I,
            )
            and not preservation.search(str(fragment))
            for fragment in fragments
        )
    }
    removed: dict[str, list[str]] = {}
    kept: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("objective") or ""),
                *[str(value) for value in item.get("expected_changes", [])],
            ]
        )
        mentioned = [
            field
            for field in fields
            if re.search(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(field),
                text,
                re.I,
            )
        ]
        config_write = bool(
            len(mentioned) == 1
            and _contains_mutation_intent(text)
            and re.search(
                r"config|WtxConfig|配置|\b(?:set|update|change|repair)\b|"
                r"设置|修改|更新|修复|改为",
                text,
                re.I,
            )
        )
        if config_write and mentioned[0] not in authorized:
            removed[item_id] = [
                _safe_implementation_step_id(value, fallback="")
                for value in item.get("depends_on") or []
            ]
            continue
        kept.append(item)
    if not removed:
        return kept

    def resolve(item_id: str, seen: set[str] | None = None) -> list[str]:
        if item_id not in removed:
            return [item_id] if item_id else []
        seen = set(seen or ())
        if item_id in seen:
            return []
        seen.add(item_id)
        result: list[str] = []
        for dependency in removed[item_id]:
            for resolved in resolve(dependency, seen):
                if resolved not in result:
                    result.append(resolved)
        return result

    for item in kept:
        item_id = _safe_implementation_step_id(item.get("id"), fallback="")
        dependencies: list[str] = []
        for raw in item.get("depends_on") or []:
            for dependency in resolve(_safe_implementation_step_id(raw, fallback="")):
                if dependency and dependency != item_id and dependency not in dependencies:
                    dependencies.append(dependency)
        item["depends_on"] = dependencies
    return kept


def _drop_unauthorized_source_mutations(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Do not turn observed repository metadata into a checkout mutation."""

    semantic_fragments = [
        semantic_step.title,
        semantic_step.objective,
        *semantic_step.expected_changes,
    ]
    source_terms = re.compile(r"\b(?:git|source|repository|revision|branch|commit)\b|源码|仓库|分支|版本", re.I)
    source_mutation = re.compile(
        r"\b(?:checkout|switch|pull|fetch|reset|clone|update|change)\b|"
        r"检出|切换|拉取|重置|克隆|更新|修改",
        re.I,
    )
    preservation = re.compile(
        r"\b(?:existing|current|preserv\w*|keep|remain\w*|unchanged|untouched|using)\b|"
        r"现有|当前|保持|保留|不变|沿用|使用",
        re.I,
    )
    authorized = any(
        source_terms.search(str(fragment))
        and source_mutation.search(str(fragment))
        and not preservation.search(str(fragment))
        for fragment in semantic_fragments
    )
    if authorized:
        return items

    removed: dict[str, list[str]] = {}
    kept: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if source_terms.search(text) and source_mutation.search(text):
            removed[item_id] = [
                _safe_implementation_step_id(value, fallback="")
                for value in item.get("depends_on") or []
            ]
            continue
        kept.append(item)
    if not removed:
        return kept

    def resolve(item_id: str) -> list[str]:
        if item_id not in removed:
            return [item_id] if item_id else []
        result: list[str] = []
        for dependency in removed[item_id]:
            for value in resolve(dependency):
                if value not in result:
                    result.append(value)
        return result

    for item in kept:
        item_id = _safe_implementation_step_id(item.get("id"), fallback="")
        dependencies: list[str] = []
        for raw in item.get("depends_on") or []:
            for dependency in resolve(_safe_implementation_step_id(raw, fallback="")):
                if dependency and dependency != item_id and dependency not in dependencies:
                    dependencies.append(dependency)
        item["depends_on"] = dependencies
    return kept


def _collapse_redundant_python_removal_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one atomic removal for the same evidenced Python symbol and file."""

    kept: list[dict[str, Any]] = []
    owner_by_key: dict[tuple[str, str], str] = {}
    replacements: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("objective") or ""),
                *[str(value) for value in item.get("expected_changes") or []],
            ]
        )
        if not re.search(r"\b(?:remove|delete)\w*\b|移除|删除", text, re.I):
            kept.append(item)
            continue
        symbols = sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]{4,}\b", text)))
        paths = re.findall(r"/[A-Za-z0-9._/-]+\.py\b", text)
        if len(symbols) != 1:
            kept.append(item)
            continue
        key = (paths[0] if paths else "", symbols[0])
        owner = owner_by_key.get(key)
        if owner is None:
            owner_by_key[key] = item_id
            kept.append(item)
            continue
        dependencies = [
            _safe_implementation_step_id(value, fallback="")
            for value in item.get("depends_on") or []
        ]
        replacements[item_id] = next(
            (value for value in reversed(dependencies) if value and value != owner),
            owner,
        )
    if not replacements:
        return kept
    for item in kept:
        item_id = _safe_implementation_step_id(item.get("id"), fallback="")
        dependencies: list[str] = []
        for raw in item.get("depends_on") or []:
            dependency = _safe_implementation_step_id(raw, fallback="")
            seen: set[str] = set()
            while dependency in replacements and dependency not in seen:
                seen.add(dependency)
                dependency = replacements[dependency]
            if dependency and dependency != item_id and dependency not in dependencies:
                dependencies.append(dependency)
        item["depends_on"] = dependencies
    return kept


def _drop_preservation_only_config_mutations(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Treat explicitly preserved config fields as assertions, not writes."""

    attributes = (
        "master_port", "worker_port", "public_port", "web_terminal_port",
        "redis_port", "mysql_port", "rabbitmq_port", "master_ip",
        "mysql_ip", "rabbitmq_ip", "celery_redis_port_db",
        "celery_rabbitmq_port_db",
    )
    semantic_fragments = [
        semantic_step.title,
        semantic_step.objective,
        *semantic_step.expected_changes,
    ]
    preserved: set[str] = set()
    preservation = re.compile(
        r"\b(?:preserve|keep|remain|unchanged|do not (?:change|modify))\b|"
        r"保持|保留|不变|不修改",
        re.I,
    )
    for attribute in attributes:
        mentions = [
            str(fragment)
            for fragment in semantic_fragments
            if re.search(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])"
                % re.escape(attribute),
                str(fragment),
                re.I,
            )
        ]
        if mentions and all(preservation.search(fragment) for fragment in mentions):
            preserved.add(attribute)
    if not preserved:
        return items

    kept: list[dict[str, Any]] = []
    replacement: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("objective") or ""),
                *[str(value) for value in item.get("expected_changes", [])],
            ]
        )
        matches = [
            attribute
            for attribute in preserved
            if re.search(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])"
                % re.escape(attribute),
                text,
                re.I,
            )
        ]
        config_write = bool(
            len(matches) == 1
            and _contains_mutation_intent(text)
            and re.search(r"config|WtxConfig|配置", text, re.I)
        )
        if config_write:
            dependencies = [
                _safe_implementation_step_id(value, fallback="")
                for value in item.get("depends_on") or []
            ]
            replacement[item_id] = next(
                (value for value in reversed(dependencies) if value), ""
            )
            continue
        kept.append(item)
    if not replacement:
        return kept
    for item in kept:
        rewired: list[str] = []
        for raw in item.get("depends_on") or []:
            dependency = _safe_implementation_step_id(raw, fallback="")
            seen: set[str] = set()
            while dependency in replacement and dependency not in seen:
                seen.add(dependency)
                dependency = replacement[dependency]
            current = _safe_implementation_step_id(item.get("id"), fallback="")
            if dependency and dependency != current and dependency not in rewired:
                rewired.append(dependency)
        item["depends_on"] = rewired
    return kept


def _order_runtime_migration_items(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Enforce stop -> configure -> start for an in-place runtime migration."""

    def text_of(item: dict[str, Any]) -> str:
        return "%s %s" % (item.get("title") or "", item.get("objective") or "")

    stops = [
        item for item in items
        if re.search(r"\b(?:stop|terminate)\b|停止|终止", text_of(item), re.I)
        and (
            re.search(
                r"\b(?:runtime|process|instance|pid|pgid)s?\b|运行时|进程|实例",
                text_of(item),
                re.I,
            )
            or _atomic_runtime_role(str(item.get("title") or ""))
        )
    ]
    configs = [
        item for item in items
        if (
            re.search(
                r"(?<![A-Za-z0-9_])(?:master_port|worker_port)(?![A-Za-z0-9_])",
                text_of(item),
                re.I,
            )
            and re.search(
                r"\b(?:set|update|change|edit|replace)\b|设置|更新|修改|改为",
                text_of(item),
                re.I,
            )
        )
    ]
    starts = [
        item for item in items
        if re.search(
            r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
            text_of(item),
            re.I,
        )
        and _atomic_runtime_role(text_of(item))
        and not re.search(
            r"\b(?:stop|terminate)\w*\b|停止|终止",
            text_of(item),
            re.I,
        )
    ]
    if not configs or not starts:
        return items
    selected_ids = {
        _safe_implementation_step_id(item.get("id"), fallback="")
        for item in [*stops, *configs, *starts]
    }
    remainder = [item for item in items if item not in stops + configs + starts]
    ordered = [*stops, *configs, *starts, *remainder]
    external_dependencies = []
    for item in ordered:
        for dependency in item.get("depends_on") or []:
            normalized = _safe_implementation_step_id(dependency, fallback="")
            if normalized and normalized not in selected_ids and normalized not in external_dependencies:
                external_dependencies.append(normalized)
    previous = ""
    for item in ordered:
        item["depends_on"] = [previous] if previous else list(external_dependencies)
        previous = _safe_implementation_step_id(item.get("id"), fallback="")
    return ordered


def _order_source_runtime_recovery_items(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Compile source repair recovery as edit -> exact stop -> start -> verify."""

    if not _required_runtime_recovery_roles(semantic_step):
        return items

    def text_of(item: dict[str, Any]) -> str:
        return " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("objective") or ""),
                *[str(value) for value in item.get("expected_changes") or []],
            ]
        )

    # Port/config migrations already have their own stop -> configure -> start
    # compiler and must not be reordered by this source-repair rule.
    if any(
        re.search(r"master_port|worker_port|WtxConfig|配置", text_of(item), re.I)
        and re.search(r"\b(?:set|update|change|edit)\b|设置|更新|修改", text_of(item), re.I)
        for item in items
    ):
        return items
    stops = [
        item for item in items
        if _atomic_runtime_role(text_of(item))
        and re.search(r"\b(?:stop|terminate)\w*\b|停止|终止", text_of(item), re.I)
    ]
    starts = [
        item for item in items
        if _atomic_runtime_role(text_of(item))
        and re.search(
            r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
            text_of(item),
            re.I,
        )
        and item not in stops
    ]
    if not stops or not starts:
        return items
    verifications = [item for item in items if _implementation_item_is_verification(item)]
    edits = [
        item for item in items
        if item not in stops + starts + verifications
        and re.search(r"\b(?:edit|remove|delete|replace)\w*\b|编辑|移除|删除|替换", text_of(item), re.I)
    ]
    remainder = [
        item for item in items
        if item not in edits + stops + starts + verifications
    ]
    ordered = [*edits, *stops, *starts, *remainder, *verifications]
    selected_ids = {
        _safe_implementation_step_id(item.get("id"), fallback="")
        for item in ordered
    }
    external: list[str] = []
    for item in ordered:
        for raw in item.get("depends_on") or []:
            dependency = _safe_implementation_step_id(raw, fallback="")
            if dependency and dependency not in selected_ids and dependency not in external:
                external.append(dependency)
    previous = ""
    for item in ordered:
        item["depends_on"] = [previous] if previous else list(external)
        previous = _safe_implementation_step_id(item.get("id"), fallback="")
    return ordered


def _ensure_runtime_role_recovery_items(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Require each evidence-derived unhealthy role to have a mutation step."""

    fragments = [semantic_step.objective, *semantic_step.expected_changes]
    semantic_text = " ".join(
        str(fragment)
        for fragment in fragments
        if not re.search(
            r"\b(?:preserv\w*|keep|remain\w*|unchanged|untouched|do not (?:start|restart|"
            r"change|modify))\b|\b(?:verify|confirm)\b.{0,40}\b(?:healthy|health)\b|"
            r"保持|保留|不变|不修改|不要重启|无需重启|仍健康|"
            r"验证.{0,40}健康|确认.{0,40}健康",
            str(fragment),
            re.I,
        )
    )
    if re.match(
        r"^\s*(?:(?:precisely|safely|only|精确|安全|仅)\s*)?"
        r"(?:stop|terminate|停止|终止)",
        str(semantic_step.title or ""),
        re.I,
    ):
        return items
    required: list[tuple[str, str]] = list(
        _runtime_component_dispositions(semantic_step).items()
    )
    for role in ("master", "worker"):
        if any(required_role == role for required_role, _ in required):
            continue
        exact = re.search(
            r"\b(start missing|restart unhealthy|restart requested)\s+%s\s+role\b" % role,
            semantic_text,
            re.I,
        )
        nearby = re.search(
            r"(?:\b(?:start|restart|restore|recover|launch|migrate)\w*\b|"
            r"启动|重启|恢复|迁移)[^.!?。！？]{0,80}\b%s\b|"
            r"\b%s\b[^.!?。！？]{0,80}(?:\b(?:start|restart|restore|recover|"
            r"launch|migrate)\w*\b|启动|重启|恢复|迁移)" % (role, role),
            semantic_text,
            re.I,
        )
        if exact or nearby:
            marker = exact.group(1) if exact else nearby.group(0)
            verb = (
                "Restart"
                if re.search(r"restart|重启|migrate|迁移", marker, re.I)
                else "Start"
            )
            required.append((role, verb))
    if not required:
        return items
    existing_roles = set()
    for item in items:
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if (
            _contains_mutation_intent(text)
            and re.search(
                r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
                text,
                re.I,
            )
            and not re.search(
                r"\b(?:stop|terminate)\w*\b|停止|终止",
                text,
                re.I,
            )
        ):
            role = _atomic_runtime_role(text)
            if role:
                existing_roles.add(role)
    additions = []
    previous = ""
    for role, verb in required:
        if role in existing_roles:
            continue
        item_id = "%s-recover-%s" % (semantic_step.step_id, role)
        additions.append(
            {
                "id": item_id,
                "title": "%s %s screen component" % (verb, role),
                "objective": "%s the %s role for %s" % (
                    verb, role, semantic_step.objective or semantic_step.title,
                ),
                "reason": "the running-platform evidence requires role recovery",
                "depends_on": [previous] if previous else [],
                "expected_changes": ["%s role is recovered" % role],
                "success_criteria": ["%s backend health succeeds" % role],
                "risk_suggestion": "medium",
            }
        )
        previous = item_id
    if not additions:
        return items

    # Recovery is an implementation consequence of the semantic change, not a
    # prerequisite for that change.  In particular, a synthesized restart must
    # run after any port/config mutations and before verification; prepending it
    # would launch the component with stale configuration.
    insertion_index = next(
        (
            index
            for index, item in enumerate(items)
            if _implementation_item_is_verification(item)
        ),
        len(items),
    )
    prior_id = ""
    for item in items[:insertion_index]:
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if _contains_mutation_intent(text):
            prior_id = _safe_implementation_step_id(item.get("id"), fallback="")

    previous = prior_id
    for addition in additions:
        addition["depends_on"] = [previous] if previous else []
        previous = _safe_implementation_step_id(addition.get("id"), fallback="")

    following = items[insertion_index:]
    if previous:
        for item in following:
            if not _implementation_item_is_verification(item):
                continue
            dependencies = [
                _safe_implementation_step_id(dependency, fallback="")
                for dependency in item.get("depends_on") or []
            ]
            dependencies = [
                dependency
                for dependency in dependencies
                if dependency and dependency != prior_id
            ]
            item["depends_on"] = [previous, *dependencies]

    return [*items[:insertion_index], *additions, *following]


def _ensure_runtime_migration_stop_item(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Make an old root-owned runtime stop explicit before port migration."""

    semantic_text = " ".join(
        [semantic_step.title, semantic_step.objective, *semantic_step.expected_changes]
    )
    explicit_stop = bool(
        re.search(r"\b(?:stop|terminate)\w*\b|停止|终止", semantic_text, re.I)
        and re.search(r"\bworker\b|工作进程", semantic_text, re.I)
        and re.search(r"\b(?:port|listener)\b|端口|监听", semantic_text, re.I)
    )
    if not re.search(
        r"\b(?:migrate|move)\w*\b|迁移|切换.{0,20}端口",
        semantic_text,
        re.I,
    ) and not explicit_stop:
        return items
    if semantic_step.depends_on:
        return items
    if any(
        re.search(
            r"\b(?:stop|terminate)\w*\b|停止|终止",
            "%s %s" % (item.get("title") or "", item.get("objective") or ""),
            re.I,
        )
        for item in items
    ):
        return items
    root = _semantic_runtime_root(semantic_text)
    role = next(
        (
            candidate
            for candidate in ("worker", "master")
            if re.search(
                r"\b%s\b[^.!?。！？]{0,60}(?:restart|migrate|start|stop|"
                r"重启|迁移|启动|停止)|"
                r"(?:restart|migrate|start|stop|重启|迁移|启动|停止)"
                r"[^.!?。！？]{0,60}\b%s\b"
                % (candidate, candidate),
                semantic_text,
                re.I,
            )
        ),
        "",
    )
    if not root or not role:
        return items
    stop_id = "%s-stop-old-%s" % (semantic_step.step_id, role)
    return [
        {
            "id": stop_id,
            "title": "Stop current %s runtime process" % role,
            "objective": "Stop the old %s runtime owned by %s before changing its port"
            % (role, root),
            "reason": "release the old instance-bound listener before configuration migration",
            "depends_on": [],
            "expected_changes": ["old %s process for %s stops" % (role, root)],
            "success_criteria": ["the old root-owned %s listener is released" % role],
            "risk_suggestion": "high",
        },
        *items,
    ]


def _collapse_redundant_runtime_role_mutations(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one start/restart mutation for each runtime role."""

    kept: list[dict[str, Any]] = []
    owner_by_key: dict[tuple[str, str], str] = {}
    replacements: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        title = str(item.get("title") or "")
        text = "%s %s" % (title, item.get("objective") or "")
        # Planner objectives often repeat the whole semantic goal and therefore
        # mention both master and worker.  The atomic item title is the role
        # authority; only fall back to the combined text when the title omits it.
        role = _atomic_runtime_role(title) or _atomic_runtime_role(text)
        starts = re.search(
            r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
            text,
            re.I,
        )
        stops = re.search(
            r"\b(?:stop|terminate)\w*\b|停止|终止",
            text,
            re.I,
        )
        if not role or (not starts and not stops):
            kept.append(item)
            continue
        key = ("stop" if stops else "start", role)
        owner = owner_by_key.get(key)
        if owner is None:
            owner_by_key[key] = item_id
            kept.append(item)
            continue
        dependencies = [
            _safe_implementation_step_id(raw, fallback="")
            for raw in item.get("depends_on") or []
        ]
        replacements[item_id] = next(
            (dependency for dependency in reversed(dependencies) if dependency),
            owner,
        )
    if not replacements:
        return kept
    for item in kept:
        dependencies = []
        for raw in item.get("depends_on") or []:
            dependency = _safe_implementation_step_id(raw, fallback="")
            while dependency in replacements:
                dependency = replacements[dependency]
            current = _safe_implementation_step_id(item.get("id"), fallback="")
            if dependency and dependency != current and dependency not in dependencies:
                dependencies.append(dependency)
        item["depends_on"] = dependencies
    return kept


def _normalize_runtime_role_recovery_verbs(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Compile evidence disposition (restart unhealthy/start missing) into Action verbs."""

    dispositions = _runtime_component_dispositions(semantic_step)
    if not dispositions:
        return items
    for item in items:
        title = str(item.get("title") or "")
        role = _atomic_runtime_role(title)
        if role not in dispositions or not re.search(
            r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
            title,
            re.I,
        ):
            continue
        item["title"] = "%s %s screen component" % (dispositions[role], role)
    return items


def _runtime_component_dispositions(
    semantic_step: PrivilegedStep,
) -> dict[str, str]:
    """Return the frozen start/restart decision for every managed component."""

    semantic_text = " ".join(
        [semantic_step.objective, *semantic_step.expected_changes]
    )
    dispositions: dict[str, str] = {}
    for disposition, role in re.findall(
        r"\b(restart (?:unhealthy|requested)|start missing)\s+"
        r"([A-Za-z][A-Za-z0-9_-]{0,63})\s+role\b",
        semantic_text,
        re.I,
    ):
        normalized = role.lower().replace("-", "_")
        dispositions[normalized] = (
            "Restart" if disposition.lower().startswith("restart") else "Start"
        )
    for disposition, role in re.findall(
        r"\b(restart requested|start missing)\s+managed\s+component\s+"
        r"([A-Za-z][A-Za-z0-9_-]{0,63})\b",
        semantic_text,
        re.I,
    ):
        normalized = role.lower().replace("-", "_")
        dispositions[normalized] = (
            "Restart" if disposition.lower().startswith("restart") else "Start"
        )
    return dispositions


def _expand_unhealthy_role_restarts(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Compile an unhealthy live role into an explicit exact stop then start."""

    semantic_text = " ".join(
        [semantic_step.objective, *semantic_step.expected_changes]
    )
    unhealthy_roles = {
        role
        for role in ("master", "worker")
        if re.search(
            r"\brestart unhealthy\s+%s\s+role\b" % role,
            semantic_text,
            re.I,
        )
    }
    if not unhealthy_roles:
        return items
    expanded = []
    for index, item in enumerate(items, start=1):
        title = str(item.get("title") or "")
        role = _atomic_runtime_role(title)
        if role not in unhealthy_roles or not re.search(
            r"\brestart\b|重启", title, re.I
        ):
            expanded.append(item)
            continue
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="restart-%s-%s" % (role, index)
        )
        stop_id = "%s-stop-current" % item_id
        expanded.append(
            {
                "id": stop_id,
                "title": "Stop current %s runtime process" % role,
                "objective": "Precisely stop the unhealthy %s role for %s"
                % (role, semantic_step.objective or semantic_step.title),
                "reason": "the role has a frozen PID and must not be restarted by name",
                "depends_on": list(item.get("depends_on") or []),
                "expected_changes": ["current %s process stops" % role],
                "success_criteria": ["the exact role-owned listener is released"],
                "risk_suggestion": "high",
            }
        )
        started = dict(item)
        started["id"] = item_id
        started["title"] = "启动 %s Screen 组件" % role
        started["depends_on"] = [stop_id]
        expanded.append(started)
    return expanded


def _filter_unauthorized_runtime_role_mutations(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Drop every role mutation not explicitly authorized by this semantic step."""

    allowed: set[str] = set()
    fragments = [
        "%s %s" % (semantic_step.title, semantic_step.objective),
        *semantic_step.expected_changes,
    ]
    for fragment in fragments:
        clauses = re.split(
            r"[.;。；！？]|\bthen\b|随后|然后|"
            r"\band\s+(?=verify|confirm|keep|preserve)|并(?=验证|确认|保持|保留)",
            str(fragment or ""),
            flags=re.I,
        )
        for text in clauses:
            if re.search(
                r"\b(?:preserv\w*|keep|remain\w*|unchanged|untouched)\b|"
                r"\b(?:verify|confirm)\b.{0,40}\b(?:healthy|health|interface|endpoint)\b|"
                r"保持|保留|不变|不修改|仍健康|验证|确认",
                text,
                re.I,
            ):
                continue
            if not re.search(
                r"\b(?:start|restart|restore|recover|launch|stop|terminate)\w*\b|"
                r"启动|重启|恢复|停止|终止",
                text,
                re.I,
            ):
                continue
            for role, pattern in (
                ("master", r"(?<![A-Za-z0-9_])master(?![A-Za-z0-9_])"),
                ("worker", r"(?<![A-Za-z0-9_])worker(?![A-Za-z0-9_])"),
                ("celery", r"(?<![A-Za-z0-9_])celery(?![A-Za-z0-9_])"),
                ("web_terminal", r"web[_ -]?terminal"),
            ):
                if re.search(pattern, text, re.I):
                    allowed.add(role)
    removed: dict[str, list[str]] = {}
    for index, item in enumerate(items, start=1):
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        role = _atomic_runtime_role(text)
        if not role or role in allowed or not re.search(
            r"\b(?:start|restart|restore|recover|launch|stop|terminate)\w*\b|"
            r"启动|重启|恢复|停止|终止",
            text,
            re.I,
        ):
            continue
        item_id = _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        removed[item_id] = [
            _safe_implementation_step_id(dep, fallback="")
            for dep in item.get("depends_on") or []
        ]
    if not removed:
        return items

    def resolve(item_id: str) -> list[str]:
        if item_id not in removed:
            return [item_id] if item_id else []
        result: list[str] = []
        for dependency in removed[item_id]:
            for resolved in resolve(dependency):
                if resolved not in result:
                    result.append(resolved)
        return result

    kept = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        if item_id in removed:
            continue
        dependencies: list[str] = []
        for dependency in item.get("depends_on") or []:
            for resolved in resolve(_safe_implementation_step_id(dependency, fallback="")):
                if resolved and resolved != item_id and resolved not in dependencies:
                    dependencies.append(resolved)
        item["depends_on"] = dependencies
        kept.append(item)
    return kept


def _drop_runtime_stops_covered_by_semantic_predecessor(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
    plan: PrivilegedPlan | None = None,
) -> list[dict[str, Any]]:
    """Do not broaden a migration stop already represented by a predecessor."""

    semantic_text = "%s %s" % (semantic_step.title, semantic_step.objective)
    has_preceding_stop = bool(semantic_step.depends_on)
    if not has_preceding_stop and plan is not None:
        root = _semantic_runtime_root(semantic_text)
        for candidate in plan.steps:
            if candidate.step_id == semantic_step.step_id:
                break
            candidate_text = " ".join(
                [candidate.title, candidate.objective, *candidate.expected_changes]
            )
            if (
                re.search(r"\b(?:stop|terminate)\w*\b|停止|终止", candidate_text, re.I)
                and root
                and root == _semantic_runtime_root(candidate_text)
            ):
                has_preceding_stop = True
                break
    if not has_preceding_stop or not re.search(
        r"\b(?:migrate|move)\w*\b|迁移|切换.{0,20}端口",
        semantic_text,
        re.I,
    ):
        return items
    removed: dict[str, list[str]] = {}
    for index, item in enumerate(items, start=1):
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if not (
            re.search(r"\b(?:stop|terminate)\w*\b|停止|终止", text, re.I)
            and re.search(r"\b(?:runtime|process|instance)\b|运行时|进程|实例", text, re.I)
        ):
            continue
        item_id = _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        removed[item_id] = [
            _safe_implementation_step_id(dep, fallback="")
            for dep in item.get("depends_on") or []
        ]
    if not removed:
        return items

    def resolve(item_id: str) -> list[str]:
        if item_id not in removed:
            return [item_id] if item_id else []
        result: list[str] = []
        for dependency in removed[item_id]:
            for resolved in resolve(dependency):
                if resolved not in result:
                    result.append(resolved)
        return result

    kept = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        if item_id in removed:
            continue
        dependencies: list[str] = []
        for raw in item.get("depends_on") or []:
            for dependency in resolve(_safe_implementation_step_id(raw, fallback="")):
                if dependency and dependency != item_id and dependency not in dependencies:
                    dependencies.append(dependency)
        item["depends_on"] = dependencies
        kept.append(item)
    return kept


def _drop_runtime_stops_for_roles_known_missing(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """A role proven missing has nothing left to stop before it is started."""

    semantic_text = " ".join(
        [semantic_step.objective, *semantic_step.expected_changes]
    )
    missing_roles = {
        role
        for role in ("master", "worker")
        if re.search(
            r"\bstart missing\s+%s\s+role\b" % role,
            semantic_text,
            re.I,
        )
    }
    if not missing_roles:
        return items
    removed: dict[str, list[str]] = {}
    for index, item in enumerate(items, start=1):
        item_text = "%s %s" % (
            item.get("title") or "", item.get("objective") or ""
        )
        role = _atomic_runtime_role(item_text)
        if role not in missing_roles or not re.search(
            r"\b(?:stop|terminate)\w*\b|停止|终止|清理",
            item_text,
            re.I,
        ):
            continue
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        removed[item_id] = [
            _safe_implementation_step_id(value, fallback="")
            for value in item.get("depends_on") or []
        ]
    if not removed:
        return items

    def resolve(item_id: str) -> list[str]:
        if item_id not in removed:
            return [item_id] if item_id else []
        result: list[str] = []
        for dependency in removed[item_id]:
            for value in resolve(dependency):
                if value not in result:
                    result.append(value)
        return result

    kept: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        if item_id in removed:
            continue
        dependencies: list[str] = []
        for raw in item.get("depends_on") or []:
            for dependency in resolve(_safe_implementation_step_id(raw, fallback="")):
                if dependency and dependency != item_id and dependency not in dependencies:
                    dependencies.append(dependency)
        item["depends_on"] = dependencies
        kept.append(item)
    return kept


def _drop_runtime_stops_covered_by_same_step_restart(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Avoid a broad pre-stop when a role restart already owns that lifecycle."""

    text_of = lambda item: "%s %s" % (
        item.get("title") or "", item.get("objective") or ""
    )
    lifecycle_roles = {
        role
        for item in items
        for role in (_atomic_runtime_role(str(item.get("title") or "")),)
        if role
        and re.search(r"\b(?:start|restart)\w*\b|启动|重启", text_of(item), re.I)
        and not re.search(r"\b(?:stop|terminate)\w*\b|停止|终止", text_of(item), re.I)
    }
    configured_roles = {
        role
        for item in items
        for role in ("master", "worker")
        if re.search(r"(?<![A-Za-z0-9_])%s_port(?![A-Za-z0-9_])" % role, text_of(item), re.I)
        and re.search(r"\b(?:set|update|change|edit|replace)\b|设置|更新|修改|改为", text_of(item), re.I)
    }
    if not lifecycle_roles:
        return items
    removed: dict[str, list[str]] = {}
    kept = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        role = _atomic_runtime_role(str(item.get("title") or ""))
        is_stop = bool(re.search(r"\b(?:stop|terminate)\w*\b|停止|终止", text_of(item), re.I))
        if is_stop and role in lifecycle_roles and role not in configured_roles:
            removed[item_id] = [
                _safe_implementation_step_id(value, fallback="")
                for value in item.get("depends_on") or []
            ]
            continue
        kept.append(item)
    if not removed:
        return kept
    for item in kept:
        dependencies = []
        for raw in item.get("depends_on") or []:
            dependency = _safe_implementation_step_id(raw, fallback="")
            seen = set()
            while dependency in removed and dependency not in seen:
                seen.add(dependency)
                dependency = next((value for value in reversed(removed[dependency]) if value), "")
            current = _safe_implementation_step_id(item.get("id"), fallback="")
            if dependency and dependency != current and dependency not in dependencies:
                dependencies.append(dependency)
        item["depends_on"] = dependencies
    return kept


def _ground_runtime_item_roots(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
    resources: list[PlanResource],
) -> list[dict[str, Any]]:
    """Carry the parent instance identity into every mutating atomic step."""

    parent_text = " ".join(
        [semantic_step.title, semantic_step.objective, *semantic_step.expected_changes]
    )
    root = _runtime_root_for_step(semantic_step, resources)
    if not root:
        return items
    for item in items:
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if not _contains_mutation_intent(text) or _semantic_runtime_root(text):
            continue
        if re.match(r"^\s*Set\s+WtxConfig\s+", str(item.get("title") or ""), re.I):
            suffix = "%s/vemu_config/config.py" % root.rstrip("/")
        else:
            suffix = root
        item["objective"] = "%s for %s" % (
            str(item.get("objective") or item.get("title") or "").rstrip(),
            suffix,
        )
    return items


def _ensure_runtime_entry_preparation_items(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
    resources: list[PlanResource],
) -> list[dict[str, Any]]:
    """Insert the existing explicit copy capability before Screen startup."""

    starts = [
        item for item in items
        if _atomic_runtime_role(
            "%s %s" % (item.get("title") or "", item.get("objective") or "")
        )
        and re.search(
            r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
            "%s %s" % (item.get("title") or "", item.get("objective") or ""),
            re.I,
        )
        and not re.search(
            r"\b(?:stop|terminate)\w*\b|停止|终止",
            "%s %s" % (item.get("title") or "", item.get("objective") or ""),
            re.I,
        )
    ]
    if not starts or any(
        re.match(
            r"^\s*(?:Prepare\s+project\s+root\s+entry\s+files|"
            r"准备项目根目录入口文件)\s*$",
            str(item.get("title") or ""),
            re.I,
        )
        for item in items
    ):
        return items
    root_text = _runtime_root_for_step(semantic_step, resources)
    if not root_text:
        return items
    root = Path(root_text)
    source = next(
        (
            candidate
            for candidate in (root / "mains", root / "vemu_uestc" / "mains")
            if all((candidate / name).is_file() for name in REQUIRED_ENTRY_FILES)
        ),
        None,
    )
    if source is None:
        return items
    start_ids = {
        _safe_implementation_step_id(item.get("id"), fallback="")
        for item in starts
    }
    dependencies: list[str] = []
    for item in starts:
        for raw in item.get("depends_on") or []:
            dependency = _safe_implementation_step_id(raw, fallback="")
            if (
                dependency
                and dependency not in start_ids
                and dependency not in dependencies
            ):
                dependencies.append(dependency)
    prepare_id = "prepare-runtime-entries"
    prepare = {
        "id": prepare_id,
        "title": "准备项目根目录入口文件",
        "objective": (
            "Copy canonical runtime entries before Screen startup; "
            "source_root=%s project_root=%s" % (source.resolve(), root.resolve())
        ),
        "depends_on": dependencies,
        "expected_changes": [
            "Project root entry files match the canonical mains sources",
        ],
        "success_criteria": [
            "All required runtime entry files exist in the project root",
        ],
        "risk_suggestion": "medium",
    }
    first_start_index = min(items.index(item) for item in starts)
    result = list(items)
    result.insert(first_start_index, prepare)
    for item in starts:
        existing = [
            _safe_implementation_step_id(raw, fallback="")
            for raw in item.get("depends_on") or []
        ]
        item["depends_on"] = [
            dependency
            for dependency in existing
            if dependency in start_ids
        ]
        if not item["depends_on"]:
            item["depends_on"] = [prepare_id]
    return result


def _runtime_root_for_step(
    semantic_step: PrivilegedStep,
    resources: list[PlanResource],
) -> str:
    text = " ".join(
        [semantic_step.title, semantic_step.objective, *semantic_step.expected_changes]
    )
    return _semantic_runtime_root(text) or _runtime_root_from_frozen_paths(resources)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _drop_redundant_implementation_verifications(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Keep pre/post checks on Action and semantic contracts, not as micro nodes."""

    semantic_mutates = bool(
        semantic_step.expected_changes
        or _contains_mutation_intent("%s %s" % (
            semantic_step.title, semantic_step.objective,
        ))
    )
    if not semantic_mutates or not semantic_step.postconditions:
        return items
    removed: dict[str, list[str]] = {}
    for index, item in enumerate(items, start=1):
        if not _implementation_item_is_verification(item):
            continue
        item_id = _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        removed[item_id] = [
            _safe_implementation_step_id(dep, fallback="")
            for dep in item.get("depends_on") or []
        ]
    if not removed:
        return items

    def resolve(item_id: str) -> list[str]:
        if item_id not in removed:
            return [item_id] if item_id else []
        result: list[str] = []
        for dependency in removed[item_id]:
            for resolved in resolve(dependency):
                if resolved not in result:
                    result.append(resolved)
        return result

    kept = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        if item_id in removed:
            continue
        dependencies: list[str] = []
        for raw in item.get("depends_on") or []:
            for dependency in resolve(_safe_implementation_step_id(raw, fallback="")):
                if dependency and dependency != item_id and dependency not in dependencies:
                    dependencies.append(dependency)
        item["depends_on"] = dependencies
        kept.append(item)
    return kept


def _drop_nginx_activation_from_prepare(
    items: list[dict[str, Any]],
    semantic_step: PrivilegedStep,
) -> list[dict[str, Any]]:
    """Keep Nginx file preparation separate from post-application activation."""

    primary = "%s %s" % (semantic_step.title, semantic_step.objective)
    if not (
        re.search(r"nginx", primary, re.I)
        and re.search(r"\b(?:create|write|install|prepare)\b|创建|写入|准备", primary, re.I)
        and not re.search(r"\b(?:activate|reload|restart)\b|激活|重载", primary, re.I)
    ):
        return items
    removed: dict[str, list[str]] = {}
    for index, item in enumerate(items, start=1):
        text = "%s %s" % (item.get("title") or "", item.get("objective") or "")
        if re.search(r"\b(?:reload|restart)\b|重载|重新加载", text, re.I):
            item_id = _safe_implementation_step_id(
                item.get("id"), fallback="step-%s" % index
            )
            dependencies = item.get("depends_on")
            removed[item_id] = [
                _safe_implementation_step_id(value, fallback="")
                for value in dependencies
            ] if isinstance(dependencies, list) else []
    if not removed:
        return items

    def expand(item_id: str) -> list[str]:
        if item_id not in removed:
            return [item_id]
        result = []
        for dependency in removed[item_id]:
            for expanded in expand(dependency):
                if expanded and expanded not in result:
                    result.append(expanded)
        return result

    normalized = []
    for index, item in enumerate(items, start=1):
        item_id = _safe_implementation_step_id(
            item.get("id"), fallback="step-%s" % index
        )
        if item_id in removed:
            continue
        dependencies = item.get("depends_on")
        rewired = []
        if isinstance(dependencies, list):
            for dependency in dependencies:
                for target in expand(
                    _safe_implementation_step_id(dependency, fallback="")
                ):
                    if target and target != item_id and target not in rewired:
                        rewired.append(target)
        item["depends_on"] = rewired
        normalized.append(item)
    return normalized


def _topologically_order_implementation_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compile a valid DAG emitted out of order into execution order."""

    identities = [
        _safe_implementation_step_id(item.get("id"), fallback="step-%s" % index)
        for index, item in enumerate(items, start=1)
    ]
    known = set(identities)
    dependencies: dict[str, list[str]] = {}
    for identity, item in zip(identities, items):
        dependencies[identity] = [
            _safe_implementation_step_id(value, fallback="")
            for value in item.get("depends_on", [])
        ] if isinstance(item.get("depends_on"), list) else []
        unknown = set(dependencies[identity]).difference(known)
        if unknown:
            raise ExecutionBindingError(
                "implementation dependency is unknown=%s" % sorted(unknown)[0],
                replan_recommended=False,
                category="implementation_contract_invalid",
            )
    remaining = list(zip(identities, items))
    emitted: set[str] = set()
    ordered: list[dict[str, Any]] = []
    while remaining:
        ready_index = next(
            (
                index
                for index, (identity, _) in enumerate(remaining)
                if set(dependencies[identity]) <= emitted
            ),
            None,
        )
        if ready_index is None:
            raise ExecutionBindingError(
                "implementation dependency cycle",
                replan_recommended=False,
                category="implementation_contract_invalid",
            )
        identity, item = remaining.pop(ready_index)
        ordered.append(item)
        emitted.add(identity)
    return ordered


def _is_discovery_only_implementation_step(
    item: dict[str, Any],
    *,
    risk: str,
) -> bool:
    """Reject internal argument discovery accidentally emitted as execution."""

    if risk != "readonly":
        return False
    text = " ".join(
        str(item.get(key) or "").lower()
        for key in ("title", "objective")
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


def _implementation_item_is_verification(item: dict[str, Any]) -> bool:
    """Normalize verifier-shaped micro steps despite inherited effect text."""

    title = str(item.get("title") or "").lower().strip()
    objective = str(item.get("objective") or "").lower().strip()
    text = title or objective
    if title and "只读" in title and re.search(
        r"验证|校验|确认|检查|复核|验收|verify\b|validate\b|check\b|confirm\b",
        title,
        re.I,
    ):
        return not bool(re.search(
            r"(?:并|且|然后).{0,24}(?:创建|写入|修改|修复|设置|切换|启动|停止|重启|迁移)",
            title,
            re.I,
        ))
    if title and re.match(
        r"^(?:验证|校验|确认|检查|验收|verify\b|validate\b|check\b|confirm\b|assert\b)",
        title,
        re.I,
    ):
        compound_mutation = re.search(
            r"(?:并|且|然后|and\b|then\b).{0,24}"
            r"(?:创建|新增|写入|修改|修复|设置|切换|启动|停止|重启|重载|迁移|"
            r"create\b|add\b|write\b|modify\b|repair\b|set\b|switch\b|"
            r"start\b|stop\b|restart\b|reload\b|migrate\b)",
            title,
            re.I,
        )
        return compound_mutation is None
    if _contains_mutation_intent(" ".join(part for part in (title, objective) if part)):
        return False
    verifier = re.search(
        r"验证|校验|确认|检查|验收|verify\b|validate\b|check\b|confirm\b|assert\b",
        text,
        re.IGNORECASE,
    )
    mutation = re.search(
        r"创建|新增|写入|修改|修复|设置|切换|启动|停止|重载|重新加载|安装|复制|同步|删除|"
        r"create\b|add\b|write\b|modify\b|insert\b|configure\b|deploy\b|"
        r"set\b|switch\b|start\b|stop\b|reload\b|repair\b|install\b|"
        r"copy\b|sync\b|remove\b|checkout\b|pin\b",
        text,
        re.IGNORECASE,
    )
    if not verifier or mutation:
        return False
    if title and re.search(
        r"^(?:请|需要|要|to\s+)?(?:(?:创建|新增|写入|插入|修改|修复|设置|"
        r"切换|启动|停止|重载|安装|复制|同步|删除|准备)|"
        r"(?:create|add|write|insert|modify|repair|configure|deploy|set|"
        r"switch|start|stop|reload|install|copy|sync|remove|prepare|checkout|pin)\b)",
        objective,
        re.IGNORECASE,
    ):
        return False
    return True


def _semantic_step_is_observational(step: PrivilegedStep) -> bool:
    """Classify the semantic goal by its primary verb, not prerequisite prose."""

    title = str(step.title or "").strip()
    objective = str(step.objective or "").strip()
    if _contains_mutation_intent(" ".join(part for part in (title, objective) if part)):
        return False
    if title:
        if not _implementation_item_is_verification({"title": title}):
            return False
        if re.search(
            r"^(?:请|需要|要|to\s+)?(?:(?:创建|新增|写入|插入|修改|修复|设置|"
            r"切换|启动|停止|重载|安装|复制|同步|删除|准备)|"
            r"(?:create|add|write|insert|modify|repair|configure|deploy|set|"
            r"switch|start|stop|reload|install|copy|sync|remove|prepare|checkout|pin)\b)",
            objective,
            re.IGNORECASE,
        ):
            return False
        return True
    return _implementation_item_is_verification(
        {"objective": step.objective}
    )


def _contains_mutation_intent(text: str) -> bool:
    return re.search(
        r"创建|新增|写入|插入|修改|修复|恢复|设置|切换|启动|停止|重启|重载|安装|复制|同步|删除|迁移|"
        r"\b(?:create|add|write|insert|modify|repair|recover|restore|configure|deploy|set|"
        r"switch|start|stop|restart|reload|install|copy|sync|remove|migrate|checkout|pin)\b",
        str(text or ""),
        re.IGNORECASE,
    ) is not None


def _step_is_verification(step: PrivilegedStep) -> bool:
    """Require both a readonly contract and an observational objective."""

    if step.risk != "readonly" or step.expected_changes:
        return False
    return _implementation_item_is_verification(
        {"title": step.title, "objective": step.objective}
    )


def _planned_output_paths(step: PrivilegedStep) -> tuple[Any, ...]:
    """Extract dependency-produced absolute paths for compile-time cwd checks."""

    paths = []
    for reference in step.evidence_refs:
        for raw in re.findall(r"/(?:[^\s|;,]+)", str(reference)):
            cleaned = raw.rstrip(".。:：)]}'\"")
            if cleaned and cleaned not in paths:
                paths.append(cleaned)
    return tuple(paths)


def _host_fact_is_planned_future(
    action: str,
    args: dict[str, Any],
    step: PrivilegedStep,
    problem: str,
    resources: list[PlanResource] | None = None,
) -> bool:
    """Allow compile-time absent paths explicitly produced by dependencies."""

    problem_to_arg = {
        "grounding_failed=nginx_source_not_file": "source_path",
        "grounding_failed=archive_not_found": "archive_path",
    }
    arg_name = problem_to_arg.get(problem)
    if arg_name is None:
        if problem.startswith("grounding_failed=install_script_not_found"):
            arg_name = "script_dir"
        elif problem.startswith("grounding_failed=project_root_missing_entries"):
            arg_name = "project_root"
        elif problem.startswith("grounding_failed=project_root_not_directory"):
            arg_name = "project_root"
    if arg_name is None:
        return False
    candidate_text = str(args.get(arg_name) or "").strip()
    if not candidate_text.startswith("/"):
        return False
    candidate = Path(candidate_text)
    for raw in _planned_output_paths(step):
        root_text = str(raw or "").strip()
        if not root_text.startswith("/"):
            continue
        root = Path(root_text)
        try:
            candidate.relative_to(root)
            if candidate == root or root in candidate.parents:
                return True
        except (OSError, ValueError):
            continue
    if any(
        re.search(r"planned (?:semantic )?predecessor", item)
        for item in step.evidence_refs
    ):
        for resource in resources or []:
            if (
                resource.status == "frozen"
                and (resource.role == "instance_root" or resource.name == "instance_root")
            ):
                root = Path(str(resource.value))
                try:
                    candidate.relative_to(root)
                    return True
                except (OSError, ValueError):
                    continue
    return False


def _validate_micro_plan_dependency_shape(
    plan: PrivilegedPlan,
    semantic_step: PrivilegedStep,
    micro_steps: list[PrivilegedStep],
) -> None:
    """Prevent local decomposition from violating outer semantic ordering."""

    step_by_id = {item.step_id: item for item in plan.steps}
    pending = list(semantic_step.depends_on)
    ancestors: set[str] = set()
    while pending:
        step_id = pending.pop()
        if step_id in ancestors:
            continue
        ancestors.add(step_id)
        predecessor = step_by_id.get(step_id)
        if predecessor is not None:
            pending.extend(predecessor.depends_on)
    backend_start_ids = {
        item.step_id for item in plan.steps
        if re.search(
            r"启动.{0,16}(?:平台|组件|服务)|"
            r"\bstart.{0,20}(?:platform|component|service)",
            "%s %s" % (item.title, item.objective),
            re.IGNORECASE,
        )
    }
    if not backend_start_ids:
        return
    if backend_start_ids.intersection(ancestors):
        return
    for micro_step in micro_steps:
        text = "%s %s %s" % (
            micro_step.title,
            micro_step.objective,
            " ".join(micro_step.success_criteria),
        )
        if re.search(
            r"curl|路由.{0,8}(?:可达|访问|响应)|"
            r"http.{0,16}(?:status|response|200|2\d\d|3\d\d)",
            text,
            re.IGNORECASE,
        ):
            raise ExecutionBindingError(
                "implementation_dependency_order_invalid=%s_http_health_"
                "requires_backend_start_semantic_predecessor"
                % micro_step.step_id,
                replan_recommended=True,
                category="implementation_plan_unavailable",
            )


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


def _validate_runtime_knowledge_contract(
    plan: PrivilegedPlan,
    grounded_context: GroundedPlanContext | None,
) -> None:
    """Require relevant retrieved runbook guidance for runtime mutations."""

    if grounded_context is None:
        return
    knowledge = str(grounded_context.knowledge_evidence or "")
    if "probe=klonet_knowledge" not in knowledge:
        return  # Backward-compatible contexts created outside the staged workflow.
    semantic = " ".join(
        [plan.goal]
        + [
            "%s %s" % (step.title, step.objective)
            for step in plan.steps
        ]
    )
    if not re.search(
        r"\b(?:start|restart|launch)\b|启动|重启",
        semantic,
        re.I,
    ):
        return
    if "retrieval unavailable" in knowledge.lower() or "未执行 Klonet RAG" in knowledge:
        raise ExecutionBindingError(
            "runtime_knowledge_evidence_unavailable",
            replan_recommended=False,
            category="knowledge_evidence_unavailable",
        )
    retrieval_status = re.search(
        r"(?im)^\s*-?\s*retrieval_status\s*:\s*([a-z_]+)\s*$",
        knowledge,
    )
    if retrieval_status is not None and retrieval_status.group(1).lower() != "reliable":
        raise ExecutionBindingError(
            "runtime_knowledge_evidence_not_reliable=%s"
            % retrieval_status.group(1).lower(),
            replan_recommended=False,
            category="knowledge_evidence_irrelevant",
        )
    required_groups = (
        ("<project_root>", "project_root"),
        ("mains", "source_root"),
        ("screen", "Screen"),
    )
    missing = [
        names[0] for names in required_groups
        if not any(name.lower() in knowledge.lower() for name in names)
    ]
    if missing:
        raise ExecutionBindingError(
            "runtime_knowledge_contract_incomplete=%s" % ",".join(missing),
            replan_recommended=False,
            category="knowledge_evidence_irrelevant",
        )


def _resource_manifest_payload(
    resources: list[PlanResource],
) -> list[dict[str, Any]]:
    return [resource.to_dict() for resource in resources]


def _validate_action_resource_bindings(
    step: PrivilegedStep,
    action: str,
    args: dict[str, Any],
    resources: list[PlanResource],
) -> None:
    """Reject per-step arguments that diverge from the plan-wide manifest."""

    if action == "create_docker_container":
        bindings = _strings(args.get("port_bindings"), 20)
        if any(
            re.fullmatch(r"127\.0\.0\.1:[1-9]\d{0,4}:[1-9]\d{0,4}", item)
            is None
            for item in bindings
        ):
            raise ValueError("container_port_binding_not_loopback")
        relevant_resources = [
            resource
            for resource in resources
            if resource.status == "frozen"
            and resource.kind == "port"
            and any(
                step.step_id == consumer.rsplit(".", 1)[0]
                or step.step_id.startswith(consumer.rsplit(".", 1)[0] + "__")
                for consumer in resource.consumers
            )
        ]
        step_text = "%s %s" % (step.title, step.objective)
        service = next(
            (
                marker
                for marker in ("mysql", "redis", "rabbitmq")
                if marker in step_text.lower()
            ),
            "",
        )
        credential_source = args.get("credential_source")
        if service in {"mysql", "redis"}:
            if not isinstance(credential_source, dict):
                raise ValueError("container_credential_source_required=%s" % service)
            source_path = str(credential_source.get("path") or "")
            if credential_source.get("service") != service:
                raise ValueError("container_credential_source_service_mismatch")
            frozen_config_paths = {
                str(resource.value)
                for resource in resources
                if resource.status == "frozen"
                and resource.kind == "path"
                and "config" in (
                    "%s %s" % (resource.name, resource.role)
                ).lower()
            }
            if source_path not in frozen_config_paths:
                raise ValueError("container_credential_source_not_frozen")
        service_resources = [
            resource
            for resource in relevant_resources
            if service
            and service in " ".join(
                [resource.name, resource.role, *resource.consumers]
            ).lower()
        ]
        allowed_ports = {
            int(resource.value)
            for resource in (service_resources or relevant_resources)
        }
        for binding in _strings(args.get("port_bindings"), 20):
            parts = binding.split(":")
            try:
                bound_ports = {int(parts[-2]), int(parts[-1])}
            except (IndexError, ValueError):
                raise ValueError("invalid_container_port_binding")
            unknown = bound_ports.difference(allowed_ports)
            if unknown:
                raise ValueError(
                    "unfrozen_container_port=%s" % min(unknown)
                )
    for resource in resources:
        for consumer in resource.consumers:
            semantic_id, arg_name = consumer.rsplit(".", 1)
            if not (
                step.step_id == semantic_id
                or step.step_id.startswith(semantic_id + "__")
            ):
                continue
            if resource.role == "screen_session":
                role = _atomic_runtime_role(
                    " ".join(
                        [step.title, step.objective, step.reason, *step.expected_changes]
                    )
                )
                suffix = {
                    "master": "_m",
                    "worker": "_w",
                    "celery": "_c",
                    "web_terminal": "_web",
                }.get(role, "")
                if not suffix or not str(resource.value).endswith(suffix):
                    continue
                semantic_root = _semantic_runtime_root(
                    " ".join(
                        [step.title, step.objective, step.reason, *step.expected_changes]
                    )
                )
                if (
                    "/test/" in semantic_root
                    and not re.search(
                        r"(?:^|[_-])test(?:$|[_-])", str(resource.value), re.I
                    )
                ):
                    continue
                observed = args.get("screen_session")
                if not _resource_values_equal(resource, observed):
                    raise ValueError(
                        "resource_binding_violation=%s.screen_session must use ${%s}"
                        % (semantic_id, resource.name)
                    )
                continue
            if arg_name not in args:
                continue
            if (
                resource.kind == "port"
                and resource.role in {"master_port", "worker_port"}
                and arg_name == resource.role
            ):
                scoped_value = _scoped_role_port_value(
                    step, resources, resource.role
                )
                if (
                    scoped_value is not None
                    and not _resource_values_equal(resource, scoped_value)
                ):
                    continue
            if resource.status == "deferred":
                raise ValueError(
                    "deferred_plan_resource_required=%s resolve_before=%s"
                    % (resource.name, resource.resolve_before)
                )
            observed = args.get(arg_name)
            if (
                action in {"start_screen_component", "restart_screen_component"}
                and arg_name == "project_root"
                and str(observed or "").rstrip("/") in {
                    str(resource.value).rstrip("/") + "/mains",
                    str(resource.value).rstrip("/") + "/vemu_uestc/mains",
                }
            ):
                continue
            if not _resource_values_equal(resource, observed):
                raise ValueError(
                    "resource_binding_violation=%s.%s must use ${%s}"
                    % (semantic_id, arg_name, resource.name)
                )


def _inject_frozen_resource_args(
    step: PrivilegedStep,
    args: dict[str, Any],
    resources: list[PlanResource],
) -> dict[str, Any]:
    """Compile authoritative resource consumers into Action arguments."""

    compiled = dict(args)
    runtime_role = _atomic_runtime_role(
        " ".join(
            [step.title, step.objective, step.reason, *step.expected_changes]
        )
    )
    role_suffix = {
        "master": "_m",
        "worker": "_w",
        "celery": "_c",
        "web_terminal": "_web",
    }.get(runtime_role, "")
    scoped_screen = next(
        (
            resource
            for resource in resources
            if resource.status == "frozen"
            and resource.role == "screen_session"
            and role_suffix
            and str(resource.value).endswith(role_suffix)
            and not (
                "/test/" in _semantic_runtime_root(
                    " ".join(
                        [step.title, step.objective, step.reason, *step.expected_changes]
                    )
                )
                and not re.search(
                    r"(?:^|[_-])test(?:$|[_-])", str(resource.value), re.I
                )
            )
            and any(
                step.step_id == str(consumer).rsplit(".", 1)[0]
                or step.step_id.startswith(
                    str(consumer).rsplit(".", 1)[0] + "__"
                )
                for consumer in resource.consumers
            )
        ),
        None,
    )
    if scoped_screen is not None:
        compiled["screen_session"] = scoped_screen.value
    for resource in resources:
        if resource.status != "frozen":
            continue
        for consumer in resource.consumers:
            semantic_id, arg_name = consumer.rsplit(".", 1)
            if step.step_id == semantic_id or step.step_id.startswith(
                semantic_id + "__"
            ):
                if resource.role == "screen_session":
                    continue
                compiled[arg_name] = resource.value
    return compiled


def _validate_mutating_action_paths(
    action: str,
    args: dict[str, Any],
    resources: list[PlanResource],
) -> str:
    """Keep generated file mutations inside the plan's frozen path scope."""

    arg_name = {
        "write_ops_file": "path",
        "replace_text_in_file": "path",
        "insert_text_before_anchor": "path",
        "edit_text_file": "path",
        "upsert_python_class": "path",
        "set_python_config_assignment": "path",
        "set_python_class_attribute": "path",
        "install_nginx_config": "source_path",
    }.get(action)
    if arg_name is None:
        return ""
    if action == "install_nginx_config" and not str(
        args.get("source_path") or ""
    ).strip():
        return ""
    candidate_text = str(args.get(arg_name) or "").strip()
    if not candidate_text.startswith("/"):
        return "action_path_not_absolute=%s" % arg_name
    candidate = Path(candidate_text)
    frozen_roots = [
        Path(str(resource.value))
        for resource in resources
        if resource.status == "frozen"
        and resource.kind == "path"
        and str(resource.value).startswith("/")
    ]
    if not frozen_roots:
        return ""
    for root in frozen_roots:
        try:
            candidate.relative_to(root)
            return ""
        except ValueError:
            continue
    return "action_path_outside_frozen_resources=%s:%s" % (
        arg_name,
        candidate,
    )


def _forced_registered_action_for_step(step: PrivilegedStep) -> str:
    """Freeze unambiguous capability choices before probabilistic selection."""

    if _step_is_verification(step):
        return ""
    primary = "%s %s" % (step.title, step.objective)
    if re.match(r"^\s*Set\s+WtxConfig\s+", step.title, re.I) or (
        re.search(
            r"(?<![A-Za-z0-9_])(?:master_port|worker_port)(?![A-Za-z0-9_])",
            primary,
            re.I,
        )
        and re.search(
            r"\b(?:set|update|change|edit|replace)\b|设置|更新|修改|改为",
            primary,
            re.I,
        )
        and re.search(r"config|配置", primary, re.I)
    ):
        return "set_python_class_attribute"
    if re.match(r"^\s*Start\s+[A-Za-z][A-Za-z0-9_-]{0,63}\s+screen\s+component\s*$", step.title, re.I):
        return "start_screen_component"
    if re.match(r"^\s*Restart\s+[A-Za-z][A-Za-z0-9_-]{0,63}\s+screen\s+component\s*$", step.title, re.I):
        return "restart_screen_component"
    if re.match(
        r"^\s*(?:Prepare\s+project\s+root\s+entry\s+files|"
        r"准备项目根目录入口文件)\s*$",
        step.title,
        re.I,
    ):
        return "prepare_project_files"
    runtime_role = _atomic_runtime_role(step.title)
    if runtime_role in {"master", "worker"} and re.match(
        r"^\s*(?:stop|terminate|停止|终止)", step.title, re.I,
    ) and re.search(
        r"\b(?:runtime|process|role)\b|运行时|进程|角色", primary, re.I,
    ):
        return "stop_klonet_component"
    if runtime_role and re.match(
        r"^\s*(?:restart|restore|recover|重启|恢复)", step.title, re.I,
    ) and re.search(r"\b(?:screen|component|process|role)\b|组件|进程|角色", primary, re.I):
        return "restart_screen_component"
    if runtime_role and re.match(
        r"^\s*(?:start|launch|启动)", step.title, re.I,
    ) and re.search(r"\b(?:screen|component|process|role)\b|组件|进程|角色", primary, re.I):
        return "start_screen_component"
    text = " ".join(
        [step.title, step.objective, step.reason, *step.expected_changes]
    ).lower()
    if (
        re.search(r"\b(?:stop|terminate)\b|停止|终止", primary, re.I)
        and re.search(r"\b(?:runtime|instance|process)\b|运行实例|进程", text, re.I)
        and re.search(r"/[A-Za-z0-9._/-]*vemu_uestc\b", text)
        and not re.search(r"\bscreen\s+(?:session|component)\b|screen\s*会话", primary, re.I)
    ):
        if runtime_role in {"master", "worker"}:
            return "stop_klonet_component"
        return "stop_klonet_runtime_instance"
    if re.search(r"\b(?:clone|checkout)\b|克隆|检出", primary, re.I) and re.search(
        r"\b(?:git|repository|source)\b|仓库|源码", primary, re.I
    ):
        return "git_operation"
    if re.match(r"^\s*(?:apply|set|update)\b|^\s*(?:应用|设置|更新)", primary, re.I) and re.search(
        r"restart\s+policy|重启策略", primary, re.I
    ) and re.search(
        r"container|容器", primary, re.I
    ) and not re.search(r"\b(?:create|new|provision)\b|创建|新建", primary, re.I):
        return "manage_container"
    mentions_container = bool(re.search(r"\b(?:docker\s+)?container\b|容器", text))
    creates_absent = bool(
        re.search(
            r"(?:\bcreate\b|\bnew\b|previously absent|\bprovision\b|"
            r"isolated|创建|新建|全新|隔离).{0,60}(?:\bcontainer\b|容器)|"
            r"(?:\bcontainer\b|容器).{0,60}(?:\bcreate\b|\bnew\b|"
            r"previously absent|\bprovision\b|isolated|创建|新建|全新|隔离)",
            text,
            re.IGNORECASE,
        )
    )
    if mentions_container and creates_absent:
        return "create_docker_container"
    return ""


def _validate_action_objective_fit(
    action: str,
    step: PrivilegedStep,
) -> str:
    """Reject structurally valid Actions that cannot cause the stated effect."""

    text = " ".join(
        [step.title, step.objective, step.reason, *step.expected_changes]
    ).lower()
    primary = "%s %s" % (step.title, step.objective)
    if action in {"stop_klonet_component", "stop_klonet_runtime_instance"} and re.match(
        r"^\s*(?:start|restart|restore|recover|launch|启动|重启|恢复)",
        primary,
        re.I,
    ):
        return "action=%s contradicts_start_or_restart_objective" % action
    if action in {"manage_container", "start_docker_container"} and re.search(
        r"(?:create|new|previously absent|创建|新建|全新).{0,40}(?:container|容器)|"
        r"(?:container|容器).{0,40}(?:create|new|previously absent|创建|新建|全新)",
        text,
        re.IGNORECASE,
    ):
        return "action=%s cannot_create_new_container" % action
    if action == "manual_checkpoint":
        if step.risk == "readonly" and not step.expected_changes:
            return ""
        if re.search(
            r"人工|手动|用户.{0,8}(?:决定|确认)|manual|checkpoint|"
            r"human.{0,8}(?:decision|approval)",
            text,
            re.IGNORECASE,
        ):
            return ""
        return "action=manual_checkpoint cannot_produce_automatic_state_change"
    if action == "write_ops_file" and re.search(
        r"sites-enabled|符号链接|软链接|\bsymlink\b|"
        r"(?:nginx|站点).{0,20}(?:启用|enable)|"
        r"(?:启用|enable).{0,20}(?:nginx|site|站点)",
        text,
        re.IGNORECASE,
    ):
        return "action=write_ops_file cannot_enable_nginx_site"
    if action != "set_python_config_assignment":
        return ""
    if re.search(
        r"激活|切换.{0,12}(?:配置|config)|活动配置|"
        r"(?:active|default).{0,12}config|"
        r"config.{0,12}(?:assignment|activate|switch|point)",
        text,
        re.IGNORECASE,
    ):
        return ""
    return "action=set_python_config_assignment objective_is_not_config_activation"


def _validate_action_postcondition_fit(
    action: str,
    args: dict[str, Any],
    checks: list[dict[str, Any]],
) -> str:
    """Require generated artifacts to contain every claimed literal."""

    if action != "install_nginx_config":
        return ""
    content = str(args.get("content") or "")
    if not content.strip():
        source = Path(str(args.get("source_path") or "")).expanduser()
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return "nginx_source_or_content_unreadable"
    destination = "/etc/nginx/sites-available/%s" % str(
        args.get("config_name") or ""
    ).strip()
    for check in checks:
        check_args = check.get("args") if isinstance(check, dict) else None
        if (
            check.get("checker") == "file_contains"
            and isinstance(check_args, dict)
            and str(check_args.get("path") or "") == destination
        ):
            expected = str(check_args.get("text") or "")
            if expected and expected not in content:
                return "nginx_source_missing_declared_content=%s" % expected
    return ""


def _validate_action_contract_consistency(
    action: str,
    args: dict[str, Any],
    step: PrivilegedStep,
) -> str:
    """Cross-check typed Action arguments against the frozen semantic objective."""

    if action in {"start_screen_component", "restart_screen_component"}:
        text = " ".join(
            [step.title, step.objective, step.reason, *step.expected_changes]
        )
        expected_role = _atomic_runtime_role(text)
        observed_role = str(args.get("component") or "").strip()
        if expected_role and observed_role != expected_role:
            return "screen_component_mismatch=%s expected=%s" % (
                observed_role or "<missing>", expected_role,
            )
        expected_root = _semantic_runtime_root(text)
        project_root = str(args.get("project_root") or "").rstrip("/")
        if expected_root and not (
            project_root == expected_root
            or project_root.startswith(expected_root + "/")
        ):
            return "screen_project_root_mismatch=%s expected_under=%s" % (
                project_root or "<missing>", expected_root,
            )
        if expected_root:
            root_path = Path(expected_root)
            observed_platform = str(args.get("platform") or "").strip()
            test_instance = (
                root_path.name == "vemu_uestc" and root_path.parent.name == "test"
            )
            expected_platform = "test-qualified" if test_instance else root_path.name
            expected_suffix = {
                "master": "m", "worker": "w", "celery": "c", "web_terminal": "web",
            }.get(expected_role, "")
            expected_session = (
                "%s_%s" % (observed_platform, expected_suffix)
                if expected_suffix else ""
            )
            observed_session = str(args.get("screen_session") or "").strip()
            platform_matches = (
                bool(re.search(r"(?:^|[_-])test(?:$|[_-])", observed_platform, re.I))
                if test_instance
                else observed_platform == expected_platform
            )
            if not platform_matches:
                return "screen_platform_mismatch=%s expected=%s" % (
                    observed_platform or "<missing>", expected_platform,
                )
            if expected_session and observed_session != expected_session:
                return "screen_session_mismatch=%s expected=%s" % (
                    observed_session or "<missing>", expected_session,
                )
        return ""
    if action == "stop_screen_component":
        text = " ".join([step.title, step.objective, *step.expected_changes])
        named_sessions = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9_.-]*_[mcwt]\b", text)
        observed = str(args.get("screen_session") or "").strip()
        if named_sessions and observed not in named_sessions:
            return "screen_session_mismatch=%s expected=%s" % (
                observed or "<missing>", named_sessions[0],
            )
        return ""
    if action != "create_docker_container":
        return ""
    text = " ".join(
        [step.title, step.objective, step.reason, *step.expected_changes]
    )
    service = next(
        (item for item in ("mysql", "redis", "rabbitmq") if item in text.lower()),
        "",
    )
    names = re.findall(
        r"\b[A-Za-z0-9][A-Za-z0-9_.-]{0,70}[-_]"
        r"(?:mysql|redis|rabbitmq)\b",
        text,
        re.I,
    )
    expected_name = names[0] if names else ""
    observed_name = str(args.get("name") or "")
    if expected_name and observed_name != expected_name:
        return "container_name_mismatch=%s expected=%s" % (
            observed_name or "<missing>", expected_name
        )
    image = str(args.get("image") or "").lower()
    if service and service not in image:
        return "container_image_service_mismatch=%s:%s" % (service, image)
    credential_source = args.get("credential_source")
    if service == "rabbitmq" and credential_source is not None:
        return "container_credential_source_not_allowed=rabbitmq"
    return ""


def _validate_action_evidence(
    action: str,
    args: dict[str, Any],
    grounded_context: GroundedPlanContext | None,
) -> str:
    """Require container images to come from the Docker image Discovery record."""

    if action != "create_docker_container" or grounded_context is None:
        return ""
    evidence = str(grounded_context.environment_evidence or "")
    if "inspect_docker_images" not in evidence:
        return "docker_image_evidence_missing"
    image = str(args.get("image") or "").strip()
    if not image:
        return "docker_image_missing"
    repository, separator, tag = image.rpartition(":")
    if not separator or "/" in tag:
        repository, tag = image, "latest"
    pattern = r"(?m)^%s\s+%s(?:\s|$)" % (
        re.escape(repository),
        re.escape(tag),
    )
    if re.search(pattern, evidence) is None:
        return "docker_image_not_observed=%s" % image
    return ""


def _normalize_resolved_resource_semantic_step(
    step: PrivilegedStep,
    resources: list[PlanResource],
) -> None:
    """Turn planning-time port selection into execution-time verification."""

    text = "%s %s" % (step.title, step.objective)
    if not re.search(
        r"自动.{0,8}选择.{0,8}(?:空闲)?端口|选择.{0,8}空闲端口|"
        r"(?:select|choose).{0,20}(?:available|free).{0,12}ports?|"
        r"scan.{0,20}(?:and|to).{0,12}(?:select|choose).{0,12}ports?",
        text,
        re.IGNORECASE,
    ):
        return
    frozen_ports = [
        (str(resource.role or resource.name), int(resource.value))
        for resource in resources
        if resource.status == "frozen"
        and (
            resource.kind == "port"
            or str(resource.role or resource.name).endswith("_port")
        )
        and str(resource.value).isdigit()
    ]
    if not frozen_ports:
        return
    manifest = "、".join(
        "%s=%s" % item for item in sorted(frozen_ports)
    )
    step.title = "验证已冻结端口仍可用"
    step.objective = "验证计划阶段已冻结的端口仍未被占用：%s" % manifest
    step.expected_changes = []
    step.risk = "readonly"
    step.success_criteria = ["所有已冻结端口在执行前仍未被监听"]


def _validate_semantic_destination_availability(
    step: PrivilegedStep,
    resources: list[PlanResource],
) -> None:
    """Block every implementation route when a new-instance target has data."""

    text = "%s %s" % (step.title, step.objective)
    if not re.search(
        r"复制|同步|克隆|copy|sync|clone|materialize",
        text,
        re.IGNORECASE,
    ):
        return
    target = next(
        (
            Path(str(resource.value)).expanduser()
            for resource in resources
            if resource.status == "frozen"
            and (resource.role == "instance_root" or resource.name == "instance_root")
            and str(resource.value).startswith("/")
        ),
        None,
    )
    if target is None or not target.is_dir():
        return
    try:
        occupied = next(target.iterdir(), None) is not None
    except OSError as exc:
        raise ExecutionBindingError(
            "instance_root_not_inspectable=%s" % target,
            replan_recommended=True,
            category="external_state_conflict",
        ) from exc
    if occupied:
        raise ExecutionBindingError(
            "instance_root_not_empty=%s" % target,
            replan_recommended=True,
            category="external_state_conflict",
        )


def _validate_semantic_resource_coverage(
    semantic_step: PrivilegedStep,
    micro_steps: list[PrivilegedStep],
    resources: list[PlanResource],
) -> None:
    """Ensure a backend port configuration consumes every frozen port field."""

    for step in micro_steps:
        binding = step.execution_binding
        action = step.action or (binding.action if binding is not None else "")
        args = step.args or (binding.args if binding is not None else {})
        if action != "stop_klonet_component":
            continue
        pid = args.get("pid")
        grounded = any(
            resource.status == "frozen"
            and resource.kind == "identifier"
            and "pid" in "%s %s" % (resource.name, resource.role)
            and any(
                semantic_step.step_id == str(consumer).rsplit(".", 1)[0]
                for consumer in resource.consumers
            )
            and (
                _resource_values_equal(resource, pid)
                or (
                    isinstance(resource.value, str)
                    and resource.value.strip().startswith("[")
                    and str(pid) in re.findall(r"\d+", resource.value)
                )
            )
            for resource in resources
        )
        if not grounded:
            raise ExecutionBindingError(
                "stop_component_pid_not_frozen_for_semantic_step=%s" % semantic_step.step_id,
                replan_recommended=False,
                category="implementation_contract_invalid",
            )

    text = "%s %s" % (semantic_step.title, semantic_step.objective)
    if not re.search(r"配置|端口|config|port", text, re.IGNORECASE):
        return
    requires_public_assignment = bool(
        re.search(r"\bpublic_port\b|public\s+port|公开端口", text, re.I)
        or (
            re.search(r"\b(?:deploy|provision|create)\b|部署|新建|创建", text, re.I)
            and re.search(
                r"\bpublic_port\b|public\s+port|公开端口",
                " ".join(semantic_step.expected_changes),
                re.I,
            )
        )
    )
    if not requires_public_assignment:
        return
    has_public_port = any(
        resource.status == "frozen"
        and (resource.role == "public_port" or resource.name == "public_port")
        for resource in resources
    )
    if not has_public_port:
        return
    attributes = set()
    for step in micro_steps:
        binding = step.execution_binding
        action = step.action or (binding.action if binding is not None else "")
        args = step.args or (binding.args if binding is not None else {})
        if action == "set_python_class_attribute":
            attributes.add(str(args.get("attribute") or "").strip())
    backend_fields = {"master_port", "worker_port", "web_terminal_port"}
    if len(attributes.intersection(backend_fields)) >= 2 and "public_port" not in attributes:
        raise ExecutionBindingError(
            "missing_public_port_assignment",
            replan_recommended=False,
            category="implementation_contract_invalid",
        )


def _validate_checker_resource_scope(
    step: PrivilegedStep,
    checks: list[dict[str, Any]],
    resources: list[PlanResource],
) -> str:
    """Reject port assertions unrelated to the frozen plan or stated objective."""

    frozen_ports = {
        int(resource.value)
        for resource in resources
        if resource.status == "frozen"
        and (
            resource.kind == "port"
            or str(resource.role or resource.name).endswith("_port")
        )
        and str(resource.value).isdigit()
    }
    if not frozen_ports:
        return ""
    text = " ".join(
        [
            step.title,
            step.objective,
            step.reason,
            *step.evidence_refs,
            *step.success_criteria,
        ]
    ).lower()
    shared_service_check = bool(
        re.search(r"redis|mysql|rabbitmq|共享服务|shared service", text)
    )
    for check in checks:
        if not isinstance(check, dict) or not str(
            check.get("checker") or ""
        ).startswith("port_"):
            continue
        args = check.get("args")
        raw_port = args.get("port") if isinstance(args, dict) else None
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            continue
        if port in frozen_ports or str(port) in text:
            continue
        if shared_service_check and port in {3307, 5672, 8368}:
            continue
        return "unscoped_port_check=%s" % port
    return ""


def _infer_structural_action_args(
    action: str,
    args: dict[str, Any],
    resources: list[PlanResource],
) -> dict[str, Any]:
    """Infer stable AST facts from the current or predecessor source tree."""

    compiled = dict(args)
    if action == "set_python_class_attribute":
        attribute = str(compiled.get("attribute") or "").strip()
        instance_root = str(compiled.get("instance_root") or "").rstrip("/")
        if instance_root:
            compiled["path"] = instance_root + "/vemu_config/config.py"
        frozen_ports = {
            str(resource.role or resource.name): int(resource.value)
            for resource in resources
            if resource.status == "frozen"
            and resource.kind == "port"
            and str(resource.value).isdigit()
        }
        if attribute and attribute in compiled:
            compiled["value"] = compiled[attribute]
        elif attribute in {"master_ip", "mysql_ip", "rabbitmq_ip"}:
            compiled["value"] = "127.0.0.1"
        elif attribute in frozen_ports:
            compiled["value"] = frozen_ports[attribute]
        elif attribute == "celery_redis_port_db" and "redis_port" in frozen_ports:
            compiled["value"] = "%s/6" % frozen_ports["redis_port"]
        elif attribute == "celery_rabbitmq_port_db" and "redis_port" in frozen_ports:
            compiled["value"] = "%s/7" % frozen_ports["redis_port"]
    if action == "git_operation":
        operation = str(compiled.get("operation") or "").strip().lower()
        if operation == "clone" and str(compiled.get("revision") or "").strip():
            compiled["operation"] = "clone_at_revision"
        if operation in {
            "clone+checkout",
            "clone_and_checkout",
            "clone-and-checkout",
            "clone_checkout",
        }:
            compiled["operation"] = "clone_at_revision"
    if action == "set_python_config_assignment" and str(
        compiled.get("class_name") or ""
    ).strip() == "WtxConfig":
        compiled["assignment_name"] = "PROJ_CONFIG"
    if action == "create_docker_container":
        identity = "%s %s" % (
            compiled.get("name") or "",
            compiled.get("image") or "",
        )
        service = next(
            (item for item in ("mysql", "redis") if item in identity.lower()),
            "",
        )
        config_path = next(
            (
                str(resource.value)
                for resource in resources
                if resource.status == "frozen"
                and resource.kind == "path"
                and str(resource.value).endswith("/vemu_config/config.py")
                and "config" in ("%s %s" % (
                    resource.name, resource.role
                )).lower()
            ),
            "",
        )
        if service and config_path:
            compiled["credential_source"] = {
                "path": config_path,
                "service": service,
            }
    if action == "install_nginx_config":
        content = str(compiled.get("content") or "")
        if content and "location = /healthz" not in content:
            health = (
                "    location = /healthz {\n"
                "        access_log off;\n"
                "        return 200;\n"
                "    }\n"
            )
            location = re.search(r"(?m)^\s*location\s+/\s*\{", content)
            if location is not None:
                content = content[:location.start()] + health + content[location.start():]
            else:
                closing = content.rfind("}")
                if closing >= 0:
                    content = content[:closing] + health + content[closing:]
            compiled["content"] = content
    if action in {
        "start_screen_component",
        "restart_screen_component",
        "stop_screen_component",
    }:
        session = str(compiled.get("screen_session") or "").strip()
        component_from_session = next(
            (
                component
                for component, component_suffix in {
                    "master": "m",
                    "celery": "c",
                    "web_terminal": "web",
                    "worker": "w",
                }.items()
                if session.endswith("_" + component_suffix)
            ),
            "",
        )
        component = component_from_session or {
            "web": "web_terminal",
            "webserver": "web_terminal",
            "controller": "celery",
            "message": "master",
            "manager": "master",
        }.get(
            str(compiled.get("component") or "").strip().lower(),
            str(compiled.get("component") or "").strip(),
        )
        compiled["component"] = component
        if component not in {"master", "celery", "web_terminal", "worker"}:
            component_resource = next(
                (
                    resource for resource in resources
                    if resource.status == "frozen"
                    and resource.role == "runtime_component_spec:%s" % component
                ),
                None,
            )
            if component_resource is not None:
                try:
                    component_spec = json.loads(str(component_resource.value))
                except (TypeError, ValueError, json.JSONDecodeError):
                    component_spec = {}
                if isinstance(component_spec, dict):
                    for key in (
                        "screen_suffix", "command_argv", "preflight_argv",
                        "ports", "health_checks", "start_after",
                    ):
                        if key in component_spec:
                            compiled[key] = component_spec[key]
                    ports = component_spec.get("ports")
                    if isinstance(ports, list) and len(ports) == 1:
                        compiled["%s_port" % component] = ports[0]
        suffix = {
            "master": "m",
            "celery": "c",
            "web_terminal": "web",
            "worker": "w",
        }.get(component) or str(compiled.get("screen_suffix") or "").strip()
        platform = str(
            compiled.get("session_prefix")
            or compiled.get("platform")
            or ""
        ).strip()
        resource_platform = next(
            (
                str(resource.value).strip()
                for resource in resources
                if resource.status == "frozen"
                and (
                    resource.role in {
                        "instance_identifier",
                        "platform_instance_name",
                        "screen_session_prefix",
                        "screen_session_name_prefix",
                    }
                    or (
                        "screen" in str(resource.role or "")
                        and "prefix" in str(resource.role or "")
                    )
                )
                and str(resource.value).strip()
            ),
            "",
        )
        if resource_platform:
            platform = resource_platform
        if suffix and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", platform):
            compiled["platform"] = platform
            compiled["screen_session"] = "%s_%s" % (platform, suffix)
    if action != "upsert_python_class":
        return compiled
    raw_body = textwrap.dedent(str(compiled.get("body") or "")).strip()
    class_name = str(compiled.get("class_name") or "").strip()
    if raw_body.startswith("class ") and class_name:
        try:
            wrapper = ast.parse(raw_body)
        except SyntaxError:
            wrapper = None
        if (
            wrapper is not None
            and len(wrapper.body) == 1
            and isinstance(wrapper.body[0], ast.ClassDef)
            and wrapper.body[0].name == class_name
            and wrapper.body[0].body
        ):
            class_node = wrapper.body[0]
            body_lines = raw_body.splitlines()
            first = class_node.body[0]
            last = class_node.body[-1]
            extracted = "\n".join(
                body_lines[
                    int(first.lineno) - 1:int(last.end_lineno or last.lineno)
                ]
            )
            compiled["body"] = textwrap.dedent(extracted).strip()
            if not compiled.get("base_class") and class_node.bases:
                base = class_node.bases[0]
                if isinstance(base, ast.Name):
                    compiled["base_class"] = base.id
                elif isinstance(base, ast.Attribute):
                    try:
                        compiled["base_class"] = ast.unparse(base)
                    except (AttributeError, ValueError):
                        pass
    if compiled.get("base_class"):
        return compiled
    target_text = str(compiled.get("path") or "").strip()
    if not target_text.startswith("/") or not class_name.endswith("Config"):
        return compiled
    target = Path(target_text)
    candidates = [target]
    instance_root = next(
        (
            Path(str(item.value)) for item in resources
            if item.status == "frozen"
            and (item.role == "instance_root" or item.name == "instance_root")
        ),
        None,
    )
    source_root = next(
        (
            Path(str(item.value)) for item in resources
            if item.status == "frozen" and item.role == "source_repo_root"
        ),
        None,
    )
    if instance_root is not None and source_root is not None:
        try:
            candidates.append(source_root / target.relative_to(instance_root))
        except ValueError:
            pass
    for candidate in candidates:
        try:
            tree = ast.parse(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        if any(
            isinstance(node, ast.ClassDef) and node.name == "CommonConfig"
            for node in tree.body
        ):
            compiled["base_class"] = "CommonConfig"
            break
    return compiled


def _canonical_action_preconditions(
    action: str,
    args: dict[str, Any],
    checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove global/model-invented guards from root-bound Screen actions."""

    if action == "set_python_class_attribute":
        path = str(args.get("path") or "").strip()
        return (
            [{"checker": "file_exists", "args": {"path": path}}]
            if path
            else []
        )
    if action == "restart_screen_component":
        # Restart supports both an existing Screen and an orphan runtime.  The
        # registered Action resolves the exact session and root itself.
        return []
    if action != "start_screen_component":
        return checks
    session = str(args.get("screen_session") or "").strip()
    component = str(args.get("component") or "").strip()
    result = []
    if session and not str(args.get("run_as_uid") or "").strip():
        result.append({
            "checker": "screen_session_absent",
            "args": {"session": session},
        })
    raw_port = args.get("%s_port" % component)
    if str(raw_port or "").isdigit():
        result.append({
            "checker": "port_not_listening",
            "args": {"port": int(raw_port)},
        })
    return result


def _canonical_action_postconditions(
    action: str,
    args: dict[str, Any],
    checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ground structural Action checks in the finalized, typed Action args."""

    if action in {
        "replace_text_in_file",
        "insert_text_before_anchor",
        "edit_text_file",
        "write_ops_file",
    }:
        path = str(args.get("path") or "").strip()
        if path.endswith(".py"):
            # Importing application entrypoints executes module-level code and
            # requires the application's complete dependency environment.  A
            # source-edit Action can prove only the file property here; actual
            # runtime correctness belongs to the later process/health checks.
            checks = [
                check
                for check in checks
                if str(check.get("checker") or "") != "python_import_succeeds"
            ]
            syntax_check = {
                "checker": "python_file_syntax_valid",
                "args": {"path": path},
            }
            if syntax_check not in checks:
                checks.append(syntax_check)

    if action == "stop_klonet_component":
        return [
            {
                "checker": "process_pid_absent",
                "args": {"pid": args.get("pid")},
            },
            {
                "checker": "port_not_listening",
                "args": {"port": args.get("port")},
            },
        ]
    if action in {"start_screen_component", "restart_screen_component"}:
        session = str(args.get("screen_session") or "").strip()
        component = str(args.get("component") or "").strip()
        checks = []
        if not str(args.get("run_as_uid") or "").strip():
            checks.append({
                "checker": "screen_session_exists",
                "args": {"session": session},
            })
        component_port = component_port_arg(args, component)
        if component_port is not None:
            checks.append(
                {
                    "checker": "port_listening",
                    "args": {
                        "port": component_port,
                        "host": "127.0.0.1",
                    },
                }
            )
            if str(args.get("project_root") or "").strip():
                checks.append({
                    "checker": "port_listener_project_root",
                    "args": {
                        "port": component_port,
                        "project_root": str(args.get("project_root") or ""),
                    },
                })
        elif str(args.get("project_root") or "").strip():
            checks.append({
                "checker": "component_process_project_root",
                "args": {
                    "component": component,
                    "project_root": str(args.get("project_root") or ""),
                },
            })
        if (
            action == "restart_screen_component"
            and str(args.get("project_root") or "").strip()
        ):
            identity_args = {
                "component": component,
                "project_root": str(args.get("project_root") or ""),
            }
            if str(raw_port or "").isdigit():
                identity_args["port"] = int(raw_port)
            checks.append({
                "checker": "component_restart_identity",
                "args": identity_args,
            })
        return checks
    if action == "reload_nginx":
        return [
            {"checker": "nginx_config_valid", "args": {}},
            {
                "checker": "process_running",
                "args": {"pattern": "^nginx: master process"},
            },
        ]
    if action != "set_python_class_attribute":
        return checks
    check_args: dict[str, Any] = {}
    path = Path(str(args.get("path") or ""))
    if path.suffix == ".py":
        check_args["module"] = "%s.%s" % (path.parent.name, path.stem)
    if path.is_absolute():
        check_args["cwd"] = str(path.parent.parent)
    attribute = str(args.get("attribute") or "").strip()
    class_name = str(args.get("class_name") or "").strip()
    check_args["attribute"] = (
        "%s.%s" % (class_name, attribute)
        if class_name
        else "PROJ_CONFIG.%s" % attribute
    )
    expected = args.get("value")
    if isinstance(expected, str):
        stripped = expected.strip()
        if re.fullmatch(r"-?(?:0|[1-9]\d*)", stripped):
            expected = int(stripped)
        elif stripped.lower() in {"true", "false"}:
            expected = stripped.lower() == "true"
        elif stripped.lower() in {"none", "null"}:
            expected = None
        else:
            expected = stripped
    check_args["expected"] = expected
    python = str(args.get("python_executable") or "").strip()
    if python:
        check_args["python_executable"] = python
    return [{"checker": "python_attribute_equals", "args": check_args}]


def _infer_semantic_action_args(
    action: str,
    args: dict[str, Any],
    step: PrivilegedStep,
) -> dict[str, Any]:
    """Compile identifiers and service policy stated by the semantic change."""

    compiled = dict(args)
    if action == "prepare_project_files":
        text = " ".join(
            [step.title, step.objective, step.reason, *step.expected_changes]
        )
        project_match = re.search(r"\bproject_root=(/[^\s;]+)", text)
        source_match = re.search(r"\bsource_root=(/[^\s;]+)", text)
        if project_match:
            compiled["project_root"] = project_match.group(1).rstrip("/")
        if source_match:
            compiled["source_root"] = source_match.group(1).rstrip("/")
        source = Path(str(compiled.get("source_root") or ""))
        if all((source / name).is_file() for name in REQUIRED_ENTRY_FILES):
            compiled["entry_sha256s"] = {
                name: _sha256_file(source / name)
                for name in REQUIRED_ENTRY_FILES
            }
            project_root = Path(str(compiled.get("project_root") or ""))
            compiled["target_sha256s"] = {
                name: _sha256_file(project_root / name)
                if (project_root / name).is_file() else "missing"
                for name in REQUIRED_ENTRY_FILES
            }
            compiled["overwrite_files"] = [
                name for name in REQUIRED_ENTRY_FILES
                if (project_root / name).is_file()
                and _sha256_file(project_root / name)
                != compiled["entry_sha256s"][name]
            ]
    if action in {"start_screen_component", "restart_screen_component"}:
        text = " ".join(
            [step.title, step.objective, step.reason, *step.expected_changes]
        )
        role = _atomic_runtime_role(text)
        root = _semantic_runtime_root(text)
        if role:
            compiled["component"] = role
        if root:
            compiled["project_root"] = root.rstrip("/")
        platform = str(compiled.get("platform") or "").strip()
        if root:
            path = Path(root)
            platform = (
                path.parent.name
                if path.name == "vemu_uestc" and path.parent.name == "test"
                else path.name
            )
            compiled["platform"] = platform
        suffix = {
            "master": "m",
            "worker": "w",
            "celery": "c",
            "web_terminal": "web",
        }.get(role)
        if platform and suffix:
            compiled["screen_session"] = "%s_%s" % (platform, suffix)
        return compiled
    if action == "stop_screen_component":
        text = " ".join([step.title, step.objective, *step.expected_changes])
        named_sessions = re.findall(
            r"\b[A-Za-z0-9][A-Za-z0-9_.-]*_[mcwt]\b", text
        )
        if named_sessions:
            compiled["screen_session"] = named_sessions[0]
        return compiled
    if action != "create_docker_container":
        return compiled
    text = " ".join(
        [step.title, step.objective, step.reason, *step.expected_changes]
    )
    service = next(
        (item for item in ("mysql", "redis", "rabbitmq") if item in text.lower()),
        "",
    )
    names = re.findall(
        r"\b[A-Za-z0-9][A-Za-z0-9_.-]{0,70}[-_]"
        r"(?:mysql|redis|rabbitmq)\b",
        text,
        re.I,
    )
    if names:
        compiled["name"] = names[0]
    bindings = compiled.get("port_bindings")
    if service and isinstance(bindings, list):
        normalized_bindings = []
        for binding in bindings:
            parts = str(binding).split(":")
            if len(parts) >= 2:
                normalized_bindings.append(
                    "127.0.0.1:%s:%s" % (parts[-2], parts[-1])
                )
            else:
                normalized_bindings.append(str(binding))
        compiled["port_bindings"] = normalized_bindings
    if service == "rabbitmq":
        compiled.pop("credential_source", None)
    return compiled


def _scoped_role_port_value(
    step: PrivilegedStep,
    resources: list[PlanResource],
    role: str,
) -> Any:
    """Prefer destination/new instance ports over source/old comparison ports."""

    semantic_root = _semantic_runtime_root(
        " ".join([step.title, step.objective, step.reason, *step.expected_changes])
    )
    root_hint = "test" if "/test/" in semantic_root else "formal"
    candidates: list[tuple[int, int, Any]] = []
    for index, resource in enumerate(resources):
        if resource.status != "frozen" or resource.kind != "port" or resource.role != role:
            continue
        relevant_consumers = [
            str(consumer)
            for consumer in resource.consumers
            if (
                step.step_id == str(consumer).rsplit(".", 1)[0]
                or step.step_id.startswith(str(consumer).rsplit(".", 1)[0] + "__")
            )
        ]
        if not relevant_consumers:
            continue
        identity = "%s %s" % (resource.name, resource.source)
        score = 0
        if re.search(r"(?:^|_)(?:new|target|destination)(?:_|$)", resource.name, re.I):
            score += 100
        if resource.source in {"planner_decision", "compiler_selected_from_checked_free_candidates"}:
            score += 60
        if any(consumer.rsplit(".", 1)[-1] == role for consumer in relevant_consumers):
            score += 40
        if root_hint == "test" and "test" in identity.lower():
            score += 50
        if root_hint == "formal" and re.search(r"formal|prod", identity, re.I):
            score += 50
        if re.search(r"(?:^|_)(?:old|source|current)(?:_|$)", resource.name, re.I):
            score -= 100
        candidates.append((score, -index, resource.value))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _required_runtime_recovery_roles(step: PrivilegedStep) -> set[str]:
    fragments = [
        "%s %s" % (step.title, step.objective),
        *step.expected_changes,
    ]
    preserved = {
        role
        for role in ("master", "worker")
        if any(
            re.search(
                r"\b(?:preserv\w*|keep|remain\w*|unchanged|untouched)\b"
                r"[^.!?。！？]{0,50}\b%s\b|"
                r"(?:保持|保留|不重启)[^。！？]{0,30}%s|"
                r"%s[^。！？]{0,30}(?:保持|保留|不重启)"
                % (role, role, role),
                str(fragment or ""),
                re.I,
            )
            for fragment in fragments
        )
    }
    required: set[str] = set()
    for fragment in fragments:
        text = str(fragment or "")
        if re.search(
            r"\b(?:preserv\w*|keep|remain\w*|unchanged|untouched)\b|"
            r"\b(?:verify|confirm)\b.{0,40}\b(?:healthy|health)\b|"
            r"保持|保留|不变|不修改|仍健康|验证.{0,40}健康|确认.{0,40}健康",
            text,
            re.I,
        ):
            continue
        if not re.search(
            r"\b(?:start|restart|restore|recover|migrate|move|healthy|health)\w*\b|"
            r"启动|重启|恢复|迁移|健康",
            text,
            re.I,
        ):
            continue
        for role in ("master", "worker"):
            if role in preserved:
                continue
            if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role, text, re.I):
                required.add(role)
    return required


def _normalize_semantic_backend_health_contract(
    step: PrivilegedStep,
    resources: list[PlanResource],
) -> None:
    """Compile exact localhost /server_health/ checks before authorization."""

    required = _required_runtime_recovery_roles(step)
    if not required:
        return
    role_ports: dict[str, int] = {}
    for role in required:
        port = _scoped_role_port_value(step, resources, "%s_port" % role)
        if str(port or "").isdigit():
            role_ports[role] = int(port)
    if not role_ports:
        return
    kept = []
    for check in step.postconditions:
        # A semantic repair step is root/role scoped.  Model-supplied HTTP
        # checks may point at another instance (especially while an old shared
        # port is being migrated), so discard all of them and compile only the
        # frozen ports consumed by this step below.
        if check.get("checker") in {"http_status", "backend_health"}:
            continue
        kept.append(check)
    for role in ("master", "worker"):
        if role not in role_ports:
            continue
        port = role_ports[role]
        kept.append({
            "checker": "backend_health",
            "args": {
                "url": "http://127.0.0.1:%s/server_health/" % port,
                "expected_code": 1,
            },
        })
    step.postconditions = kept


def _validate_runtime_recovery_action_coverage(
    semantic_step: PrivilegedStep,
    micro_steps: list[PrivilegedStep],
) -> None:
    """Reject a ready plan that promises a role recovery without an Action."""

    required = _required_runtime_recovery_roles(semantic_step)
    if not required:
        return
    actual: set[str] = set()
    for micro_step in micro_steps:
        binding = micro_step.execution_binding
        if binding is None or binding.action not in {
            "start_screen_component", "restart_screen_component",
        }:
            continue
        role = str(binding.args.get("component") or "").strip()
        if role:
            actual.add(role)
    missing = sorted(required.difference(actual))
    if missing:
        raise ExecutionBindingError(
            "runtime_recovery_action_missing=%s" % ",".join(missing),
            replan_recommended=False,
            category="implementation_contract_invalid",
        )


def _validate_source_mutation_action_coverage(
    semantic_step: PrivilegedStep,
    micro_steps: list[PrivilegedStep],
) -> None:
    """Reject plans that promise a Python file change but omit its Action."""

    required: set[str] = set()
    for fragment in [
        semantic_step.title,
        semantic_step.objective,
        *semantic_step.expected_changes,
    ]:
        text = str(fragment or "")
        if re.search(
            r"\b(?:preserv\w*|keep|remain\w*|unchanged|untouched)\b|"
            r"保持|保留|不变|不修改",
            text,
            re.I,
        ):
            continue
        if not re.search(
            r"\b(?:edit|remove|delete|replace|write|update|set)\w*\b|"
            r"编辑|移除|删除|替换|写入|更新|设置|修改",
            text,
            re.I,
        ):
            continue
        required.update(re.findall(r"/[A-Za-z0-9._/-]+\.py\b", text))
    if not required:
        return
    file_actions = {
        "write_ops_file", "replace_text_in_file", "insert_text_before_anchor",
        "edit_text_file", "upsert_python_class", "set_python_config_assignment",
        "set_python_class_attribute", "remove_python_package_entries",
    }
    actual = {
        str(binding.args.get("path") or binding.args.get("file_path") or "")
        for step in micro_steps
        for binding in [step.execution_binding]
        if binding is not None and binding.action in file_actions
    }
    missing = sorted(required.difference(actual))
    if missing:
        raise ExecutionBindingError(
            "source_mutation_action_missing=%s" % ",".join(missing),
            replan_recommended=False,
            category="implementation_contract_invalid",
        )


def _atomic_runtime_role(text: str) -> str:
    leading = re.search(
        r"^\s*(?:start|restart|restore|recover|launch|启动|重启|恢复)\w*"
        r"[^.!?。！？]{0,40}?\b([A-Za-z][A-Za-z0-9_-]{0,63})\b"
        r"(?=\s+(?:screen\s+component|component|role)\b)",
        str(text or ""),
        re.I,
    )
    if leading is not None:
        return leading.group(1).lower().replace("-", "_").replace(" ", "_")
    roles = []
    for role, pattern in (
        ("master", r"(?<![A-Za-z0-9_])master(?![A-Za-z0-9_])"),
        ("worker", r"(?<![A-Za-z0-9_])worker(?![A-Za-z0-9_])"),
        ("celery", r"(?<![A-Za-z0-9_])celery(?![A-Za-z0-9_])"),
        ("web_terminal", r"web[_ -]?terminal"),
    ):
        if re.search(pattern, str(text or ""), re.I):
            roles.append(role)
    return roles[0] if len(roles) == 1 else ""


def _semantic_runtime_components(step: PrivilegedStep) -> list[str]:
    text = " ".join([step.objective, *step.expected_changes])
    ordered: list[str] = []
    for role, pattern in (
        ("master", r"(?<![A-Za-z0-9_])master(?![A-Za-z0-9_])"),
        ("celery", r"(?<![A-Za-z0-9_])celery(?![A-Za-z0-9_])"),
        ("web_terminal", r"web[_ -]?terminal"),
        ("worker", r"(?<![A-Za-z0-9_])worker(?![A-Za-z0-9_])"),
    ):
        if re.search(pattern, text, re.I):
            ordered.append(role)
    for role in re.findall(
        r"\bmanaged\s+component\s+([A-Za-z][A-Za-z0-9_-]{0,63})\b",
        text,
        re.I,
    ):
        normalized = role.lower().replace("-", "_")
        if normalized not in ordered:
            ordered.append(normalized)
    return ordered


def _semantic_runtime_root(text: str) -> str:
    candidates = re.findall(r"/[A-Za-z0-9._/-]*vemu_uestc(?:/mains)?", str(text or ""))
    if not candidates:
        return ""
    candidate = candidates[0]
    if candidate.endswith("/mains"):
        candidate = candidate[:-len("/mains")]
    return candidate.rstrip("/")


def _runtime_root_from_frozen_paths(resources: list[PlanResource]) -> str:
    """Derive an existing instance root from typed paths, including entry files."""

    root_roles = {
        "instance_root", "project_root", "platform_root",
        "platform_runtime_root", "runtime_cwd",
    }
    for resource in resources:
        if (
            resource.status != "frozen"
            or resource.kind != "path"
            or not str(resource.value).startswith("/")
        ):
            continue
        value = Path(str(resource.value).rstrip("/"))
        identity = str(resource.name or resource.role)
        if identity in root_roles:
            candidate = value.parent if value.name == "mains" else value
        elif value.parent.name == "mains" and value.suffix == ".py":
            candidate = value.parent.parent
        else:
            continue
        if candidate.is_dir():
            return str(candidate)
    return ""


def _validate_shell_resource_bindings(
    step: PrivilegedStep,
    artifact: ShellArtifact,
    resources: list[PlanResource],
) -> None:
    for resource in resources:
        relevant_targets = set()
        for consumer in resource.consumers:
            semantic_id, target = consumer.rsplit(".", 1)
            if target not in {"script", "cwd", "declared_changes", "content"}:
                continue
            if step.step_id == semantic_id or step.step_id.startswith(
                semantic_id + "__"
            ):
                relevant_targets.add(target)
        if not relevant_targets:
            continue
        if resource.status == "deferred":
            if resource.name in {"git_remote", "repository_url"}:
                if not re.search(
                    r"(?:^|[;&|\n])\s*git\s+clone\b",
                    artifact.script,
                ):
                    continue
            raise ValueError(
                "deferred_plan_resource_required=%s resolve_before=%s"
                % (resource.name, resource.resolve_before)
            )
        materials = []
        if relevant_targets & {"script", "content"}:
            materials.append(artifact.script)
        if "cwd" in relevant_targets:
            materials.append(artifact.cwd)
        if "declared_changes" in relevant_targets:
            materials.extend(artifact.declared_changes)
        if not any(str(resource.value) in value for value in materials):
            raise ValueError(
                "resource_binding_violation=shell step %s must use ${%s}"
                % (step.step_id, resource.name)
            )


def _resource_values_equal(resource: PlanResource, observed: Any) -> bool:
    if resource.kind == "path":
        try:
            return os.path.abspath(
                os.path.expanduser(str(observed))
            ) == os.path.abspath(os.path.expanduser(str(resource.value)))
        except OSError:
            return False
    if resource.kind == "port":
        try:
            return int(observed) == int(resource.value)
        except (TypeError, ValueError):
            return bool(
                re.search(
                    r"(?<![0-9])%s(?![0-9])" % re.escape(str(resource.value)),
                    str(observed),
                )
            )
    return str(observed) == str(resource.value)


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
                :20000 if normalized in {"content", "anchor", "body"} else 500
            ]
    return result


def _merge_alternative_checker_contracts(
    checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Represent alternative HTTP outcomes as one deterministic OR check."""

    merged: list[dict[str, Any]] = []
    http_by_url: dict[str, dict[str, Any]] = {}
    for check in checks:
        args = check.get("args", {})
        if check.get("checker") != "http_status" or not isinstance(args, dict):
            merged.append(check)
            continue
        url = str(args.get("url") or "")
        existing = http_by_url.get(url)
        if existing is None:
            copied = {"checker": "http_status", "args": dict(args)}
            http_by_url[url] = copied
            merged.append(copied)
            continue
        statuses = []
        for candidate in (existing["args"], args):
            raw = candidate.get("statuses")
            if isinstance(raw, list):
                statuses.extend(raw)
            elif candidate.get("status") is not None:
                statuses.append(candidate.get("status"))
            else:
                statuses.append(200)
        existing["args"].pop("status", None)
        existing["args"]["statuses"] = sorted(
            {int(item) for item in statuses}
        )
    return merged


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


def _expand_grounded_python_function_removal(
    old_text: str,
    grounded_context: GroundedPlanContext | None,
) -> str:
    """Include an adjacent top-level call and only its local blank-line delta."""

    match = re.search(
        r"(?m)^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|$)",
        old_text,
    )
    function_name = match.group(1) if match is not None else ""
    if not function_name:
        identifier = str(old_text or "").strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
            function_name = identifier
    if not function_name or grounded_context is None:
        return old_text
    evidence = str(grounded_context.environment_evidence or "")
    definition_match = re.search(
        r"(?m)^def\s+%s\s*\([^\n]*\):\n(?:[ \t]+[^\n]*(?:\n|$))+"
        % re.escape(function_name),
        evidence,
    )
    if definition_match is None:
        return old_text
    definition = definition_match.group(0).rstrip("\n")
    candidates: set[str] = set()
    for occurrence in re.finditer(re.escape(definition), evidence):
        start = occurrence.start()
        search_from = occurrence.end()
        call = re.search(
            r"\A(?P<gap>\s*?)(?P<call>%s\(\))(?P<tail>\n+)"
            % re.escape(function_name),
            evidence[search_from:],
        )
        if call is None:
            continue
        # The call must be the next non-whitespace statement. Preserve the two
        # newlines before the injection and consume its trailing blank delta.
        preceding_newlines = 0
        cursor = start
        while cursor > 0 and evidence[cursor - 1] == "\n":
            preceding_newlines += 1
            cursor -= 1
        remove_start = start - max(0, preceding_newlines - 2)
        remove_end = search_from + call.end()
        while remove_end < len(evidence) and evidence[remove_end] == "\n":
            remove_end += 1
        candidates.add(evidence[remove_start:remove_end])
    return next(iter(candidates)) if len(candidates) == 1 else old_text


def _grounded_python_removal_from_semantics(
    semantic_text: str,
    grounded_context: GroundedPlanContext | None,
) -> str:
    """Compile a whole-file deletion proposal into one exact evidenced block."""

    if grounded_context is None:
        return ""
    evidence = str(grounded_context.environment_evidence or "")
    candidates = []
    for name in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", semantic_text):
        if name in candidates:
            continue
        if re.search(r"(?m)^def\s+%s\s*\(" % re.escape(name), evidence):
            candidates.append(name)
    if len(candidates) != 1:
        return ""
    expanded = _expand_grounded_python_function_removal(
        "def %s" % candidates[0],
        grounded_context,
    )
    if not re.search(
        r"(?m)^def\s+%s\s*\(" % re.escape(candidates[0]),
        expanded,
    ):
        return ""
    return expanded


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
    text = " ".join(str(value or "").split())
    replacements = (
        (r"^Start ([A-Za-z][A-Za-z0-9_-]*) screen component$", r"启动 \1 Screen 组件"),
        (r"^Restart ([A-Za-z][A-Za-z0-9_-]*) screen component$", r"重启 \1 Screen 组件"),
        (r"^Stop ([A-Za-z][A-Za-z0-9_-]*) backend component$", r"停止 \1 后端组件"),
    )
    for pattern, replacement in replacements:
        match = re.fullmatch(pattern, text, re.I)
        if match is not None:
            text = match.expand(replacement)
            break
    return text[:limit] or "未命名步骤"
