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
from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
from klonet_agent.ops.privileged.contracts import (
    ExecutionBinding,
    PrivilegedPlan,
    PrivilegedStep,
    RISK_LEVELS,
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
""".strip()

MAX_BINDING_PROBE_ROUNDS = 2


class ExecutionBindingError(Exception):
    pass


class PrivilegedExecutionAgent:
    def __init__(
        self,
        llm: Any,
        *,
        action_registry: OpsActionRegistry | None = None,
        probe_runner: Callable[[list[dict[str, Any]]], str] | None = None,
        shell_policy: ShellArtifactPolicy | None = None,
    ) -> None:
        self.llm = llm
        self.action_registry = (
            action_registry or configured_ops_action_registry()
        )
        self.probe_runner = probe_runner
        self.shell_policy = shell_policy or ShellArtifactPolicy()
        self.checkers = DefaultCheckerRegistry()

    def prepare_plan(
        self,
        plan: PrivilegedPlan,
        *,
        grounded_context: GroundedPlanContext | None,
    ) -> PrivilegedPlan:
        for step in plan.steps:
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
                "content": json.dumps(
                    {
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
                        "grounded_context": (
                            grounded_context.render()
                            if grounded_context is not None
                            else "(no structured context)"
                        ),
                        "registered_action_catalog": self._action_catalog(),
                        "registered_probe_catalog": (
                            DEFAULT_READONLY_PROBES.render()
                        ),
                    },
                    ensure_ascii=False,
                )[:30000],
            },
        ]
        probe_round = 0
        invalid_repair = False
        while True:
            response = self._complete(messages)
            content = response.choices[0].message.content or ""
            try:
                data = _parse_json_object(content)
                status = str(data.get("status") or "").strip()
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
                    return self._registered_binding(
                        data,
                        step,
                        grounded_context,
                    )
                if status == "shell_artifact":
                    return self._shell_binding(
                        data,
                        step,
                        grounded_context,
                    )
                if status == "blocked":
                    raise ExecutionBindingError(
                        str(data.get("reason") or "Execution Agent 无法实现该步骤")
                    )
                raise ValueError("invalid execution binding status")
            except ExecutionBindingError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                if invalid_repair:
                    raise ExecutionBindingError(
                        "Execution Agent 返回了无效实现：%s" % exc
                    ) from exc
                invalid_repair = True
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair the implementation binding JSON without"
                            " changing the semantic objective. Error: %s" % exc
                        ),
                    }
                )

    def _registered_binding(
        self,
        data: dict[str, Any],
        semantic_step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> ExecutionBinding:
        action = str(data.get("action") or "").strip()
        args = data.get("args")
        if not isinstance(args, dict):
            raise ValueError("registered action args must be an object")
        spec = self.action_registry.get(action)
        if spec is None or action not in DIRECT_PRIVILEGED_ACTIONS:
            raise ValueError("action_not_directly_registered=%s" % action)
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

    def _shell_binding(
        self,
        data: dict[str, Any],
        semantic_step: PrivilegedStep,
        grounded_context: GroundedPlanContext | None,
    ) -> ExecutionBinding:
        postconditions = self._valid_checks(data.get("postconditions"))
        if not postconditions:
            raise ValueError("shell artifact requires registered postconditions")
        fingerprint = ""
        if grounded_context is not None:
            fingerprint = str(
                grounded_context.facts.get("environment_fingerprint") or ""
            )
        artifact = create_shell_artifact(
            artifact_id="shell-" + uuid.uuid4().hex[:12],
            script=str(data.get("script") or ""),
            cwd=str(data.get("cwd") or ""),
            run_as=str(data.get("run_as") or ""),
            timeout=int(data.get("timeout") or 120),
            environment_fingerprint=fingerprint,
            declared_changes=_strings(data.get("declared_changes"), 20),
            rollback=str(data.get("rollback") or "").strip()[:2000],
            nonce=uuid.uuid4().hex,
        )
        problem = self.shell_policy.validate(artifact)
        if problem:
            raise ValueError(problem)
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

    def _valid_checks(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result = []
        known = set(self.checkers.names)
        for item in value[:20]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("checker") or "").strip()
            args = item.get("args")
            if name not in known or not isinstance(args, dict):
                continue
            result.append({"checker": name, "args": args})
        return result

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
