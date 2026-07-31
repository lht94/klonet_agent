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


EXECUTION_BINDER_PROMPT = """
You are the Klonet Ops-Privilege Execution Agent.
You receive one semantic plan step, grounded environment evidence, and an exact
registered Action catalog. You choose an implementation; you do not change the
step objective, dependencies, expected effects, or success criteria.

Return one JSON object with status exactly:
- registered_action: include action, args, binding_reason,
  resolved_from_evidence, and optional registered checker postconditions.
- shell_artifact: only when no registered Action can implement the objective;
  include script, cwd, run_as, timeout, declared_changes, rollback,
  binding_reason, resolved_from_evidence, and postconditions.
- need_evidence: include at most 3 registered read-only probe_requests.
- blocked: include reason.

Prefer a registered Action whenever it actually covers the objective. Never
misuse a vaguely related Action merely to avoid shell review. All paths, ports,
services, users, process IDs, file contents and command arguments must be
grounded in supplied evidence. Never include passwords or secret values.

A shell artifact must implement only this one semantic step, use ordinary bash,
be non-interactive, and have observable postconditions. Do not use eval, source,
command substitution, background execution, dynamic download-and-execute,
unbounded deletion, or changes to sudoers/SSH/Agent security policy.
For shell_artifact, postconditions are required and every checker name and its
arguments must come from the supplied registered checker catalog.
""".strip()

MAX_BINDING_PROBE_ROUNDS = 2
MAX_BINDING_INVALID_REPAIRS = 2
MAX_ACTION_CONTRACT_REPAIRS = 2
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
            {"role": "system", "content": EXECUTION_BINDER_PROMPT},
            {
                "role": "user",
                "content": self._binding_request_content(
                    plan,
                    step,
                    grounded_context,
                ),
            },
        ]
        probe_round = 0
        invalid_repairs = 0
        while True:
            response = self._complete(messages)
            content = response.choices[0].message.content or ""
            data: dict[str, Any] = {}
            try:
                data = _parse_json_object(content)
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
                    messages.append({"role": "assistant", "content": content})
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
                    try:
                        binding = self._registered_binding(
                            data,
                            step,
                            grounded_context,
                        )
                    except ValueError as exc:
                        action = str(data.get("action") or "").strip()
                        if (
                            self.action_registry.get(action) is None
                            or action not in DIRECT_PRIVILEGED_ACTIONS
                        ):
                            raise
                        binding = self._complete_registered_action_contract(
                            data=data,
                            semantic_step=step,
                            grounded_context=grounded_context,
                            initial_error=str(exc),
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
                    binding = self._shell_binding(
                        data,
                        step,
                        grounded_context,
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
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair only the implementation binding JSON without"
                            " changing the semantic objective. Return one complete"
                            " JSON object. status must be exactly one of"
                            " registered_action, shell_artifact, need_evidence,"
                            " or blocked; Planner statuses such as ready are invalid."
                            " Error: %s" % exc
                        ),
                    }
                )

    def _progress(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def _binding_request_content(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> str:
        """Build valid JSON without truncating action/probe protocol catalogs."""

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
            "grounded_context": _binding_grounded_context(grounded_context),
            # These protocol catalogs deliberately come from independent fields
            # and are never cut by a final string slice.
            "registered_action_catalog": self._action_catalog(),
            "registered_probe_catalog": DEFAULT_READONLY_PROBES.render(),
            "registered_checker_catalog": self.checkers.render_catalog(),
            "required_status_values": [
                "registered_action",
                "shell_artifact",
                "need_evidence",
                "blocked",
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

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
            response = self._complete(messages)
            content = response.choices[0].message.content or ""
            try:
                result = _parse_json_object(content)
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
                messages.append({"role": "assistant", "content": content})
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
            response = self._complete(messages)
            content = response.choices[0].message.content or ""
            try:
                result = _parse_json_object(content)
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
                messages.append({"role": "assistant", "content": content})
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

    def _complete(self, messages: list[dict[str, str]]) -> Any:
        try:
            return self.llm.complete(
                messages=messages,
                tools=None,
                reasoning_effort="high",
                temperature=0,
                response_format={"type": "json_object"},
            )
        except TypeError:
            return self.llm.complete(messages=messages, tools=None)


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
