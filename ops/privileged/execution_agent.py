"""Execution Agent: bind semantic objectives to Actions or frozen shell artifacts."""

from __future__ import annotations

import json
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
    ShellArtifactPolicy,
    create_shell_artifact,
)


EXECUTION_SELECTION_PROMPT = """
You are stage 1 of the Klonet Ops-Privilege Execution Agent. Select exactly one
implementation capability for one frozen semantic step. Do not generate Action
arguments, shell code, checker contracts, or a revised semantic plan.

Return one JSON object with status exactly:
- registered_action:
  {"status":"registered_action","action":"<exact allowed name>",
   "selection_reason":"...","resolved_from_evidence":[]}
- shell_artifact, only when no registered Action covers the objective:
  {"status":"shell_artifact","selection_reason":"...",
   "resolved_from_evidence":[]}
- need_evidence: include at most 3 registered read-only probe_requests.
- blocked: {"status":"blocked","reason":"..."}, only when neither a
  registered Action nor a safe one-time shell artifact can implement the
  unchanged semantic objective.

Prefer a registered Action only when its declared capability actually covers
the objective. Never invent Action names. All implementation parameters will
be generated and validated by a separate stage 2 call.
""".strip()

MAX_BINDING_PROBE_ROUNDS = 2
MAX_BINDING_INVALID_REPAIRS = 2
MAX_ACTION_CONTRACT_REPAIRS = 2
MAX_SHELL_CONTRACT_REPAIRS = 2
MAX_SHELL_VERIFICATION_REPAIRS = 2
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
You are stage 2 of the Klonet Ops-Privilege Execution Agent. The semantic step
and the decision to use a one-time shell artifact are frozen. Generate only the
complete shell execution contract for this one step.

Return exactly one JSON object with status:
- ready: include script, cwd, run_as, timeout, declared_changes, rollback,
  binding_reason, resolved_from_evidence, and registered postconditions.
- blocked: include reason if a safe, grounded and verifiable shell contract
  cannot implement the frozen objective.

The script must use ordinary bash, be non-interactive, and produce observable
state. Do not use eval, source, command substitution, background execution,
dynamic download-and-execute, unbounded deletion, or changes to sudoers, SSH,
or Agent security policy. Every checker and required argument must come from
the supplied checker catalog. Do not return an Action or alter the objective.
""".strip()


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


class PrivilegedExecutionAgent:
    def __init__(
        self,
        llm: Any,
        *,
        action_registry: OpsActionRegistry | None = None,
        probe_runner: Callable[[list[dict[str, Any]]], str] | None = None,
        shell_policy: ShellArtifactPolicy | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.action_registry = (
            action_registry or configured_ops_action_registry()
        )
        self.probe_runner = probe_runner
        self.shell_policy = shell_policy or ShellArtifactPolicy()
        self.checkers = DefaultCheckerRegistry()
        self.on_progress = on_progress

    def prepare_plan(
        self,
        plan: PrivilegedPlan,
        *,
        grounded_context: GroundedPlanContext | None,
    ) -> PrivilegedPlan:
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

    def prepare_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        *,
        grounded_context: GroundedPlanContext | None,
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
                        initial_error="stage 2 Action arguments are not bound yet",
                    )
                    self._progress(
                        "实现结论：步骤“%s”将使用注册 Action：%s。"
                        % (
                            _progress_text(step.title or step.objective),
                            _progress_text(binding.action),
                        )
                    )
                    return binding
                if status == "shell_artifact":
                    self._progress(
                        "选择结论：步骤“%s”没有匹配的注册 Action；"
                        "进入一次性脚本合同阶段。"
                        % _progress_text(step.title or step.objective)
                    )
                    binding = self._complete_shell_contract(
                        selection=data,
                        semantic_step=step,
                        grounded_context=grounded_context,
                    )
                    self._progress(
                        "实现结论：没有合适的注册 Action，步骤“%s”需要一次性脚本并单独确认。"
                        % _progress_text(step.title or step.objective)
                    )
                    return binding
                if status == "blocked":
                    self._progress(
                        "实现结论：当前证据下无法安全实现步骤“%s”。"
                        % _progress_text(step.title or step.objective)
                    )
                    raise ExecutionBindingError(
                        str(data.get("reason") or "Execution Agent 无法实现该步骤")
                    )
                raise ValueError(
                    "invalid execution binding status=%s"
                    % (status or "<missing>")
                )
            except ExecutionBindingError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                if invalid_repairs >= MAX_BINDING_INVALID_REPAIRS:
                    raise ExecutionBindingError(
                        "Execution Agent 实现合同修复耗尽：%s" % exc,
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
                            " or blocked; stage 2 status ready is invalid here."
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
            "semantic_step": {
                "step_id": semantic_step.step_id,
                "objective": semantic_step.objective,
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
                            " frozen. Return status ready or blocked. Error: %s"
                            % exc
                        ),
                    }
                )
        raise ExecutionBindingError(
            "Execution Agent Shell 合同修复耗尽：%s" % last_error,
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
        problem = self.shell_policy.validate(artifact)
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

    def _action_contract_function_tool(
        self,
        action: str,
    ) -> dict[str, Any]:
        required = list(REQUIRED_ACTION_ARGS.get(action, ()))
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
                            for name in required
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
                    "script": {"type": "string"},
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
    }
    normalized = str(result.get("status") or "").strip().lower()
    result["status"] = aliases.get(normalized, normalized)
    return result


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
            result[normalized] = str(item)[:20000 if normalized == "content" else 500]
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
