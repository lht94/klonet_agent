"""Confirmation-gated mutation workflow for Ops-Privilege."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterable

from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.ops.privileged.execution_agent import (
    _canonical_action_postconditions,
)
from klonet_agent.ops.privileged.workflow.coordinator import WorkflowResult
from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
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
    ) -> WorkflowResult:
        outcome = self.planner.plan(goal, evidence_bundle, evidence_conclusion)
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
                    outcome.probe_requests,
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
                    outcome = self.planner.plan(
                        goal,
                        evidence_bundle,
                        evidence_conclusion,
                    )
                continue
            occupied_candidate = (
                outcome.status == "blocked"
                and str(outcome.reason or "").startswith(
                    "candidate ports became occupied:"
                )
            )
            if occupied_candidate and candidate_replans < self.max_candidate_replans:
                candidate_replans += 1
                outcome = self.planner.plan(
                    goal,
                    evidence_bundle,
                    evidence_conclusion,
                    binding_feedback=(
                        "%s. Select different ports that are not reported as occupied, "
                        "freeze them, and preserve every other grounded decision."
                        % outcome.reason
                    ),
                )
                continue
            break
        if outcome.status != "ready" or outcome.plan is None:
            return WorkflowResult(
                True,
                outcome.status,
                outcome.reason or "change planning did not produce an executable plan",
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
            outcome = self.planner.plan(
                goal,
                evidence_bundle,
                evidence_conclusion,
                binding_feedback=str(exc),
            )
            if outcome.status != "ready" or outcome.plan is None:
                return WorkflowResult(
                    True,
                    "blocked",
                    outcome.reason or "binding replan did not produce a ready plan",
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
                return WorkflowResult(
                    True,
                    "blocked",
                    "binding replan budget exhausted: %s" % final_error,
                    plan=outcome.plan,
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
        """Expose only collected read-only evidence to the capability binder."""

        records = getattr(evidence_bundle, "records", None)
        if not isinstance(records, list):
            return None
        sections = []
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
            sections.append(
                "[%s probe=%s]\n%s"
                % (evidence_id, probe or "unknown", _head_tail(output, 8000))
            )
        if not sections:
            return None
        return GroundedPlanContext(
            knowledge_evidence="Change Binding uses the frozen semantic plan contract.",
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
        match = re.fullmatch(
            r"confirm-priv-plan\s+(priv-ops-[A-Za-z0-9_-]{1,64})\s+([0-9a-f]{64})",
            str(text or "").strip(),
        )
        if match is None:
            return None
        return self.confirm(match.group(1), match.group(2))

    def _execute(self, plan: ChangePlan) -> WorkflowResult:
        if not plan.is_authorized:
            return WorkflowResult(
                True, "confirmation_rejected", "plan is not exactly authorized", plan=plan
            )
        verification_plan = self._verification_plan(plan)
        for change in plan.steps:
            for step in self._execution_steps(change):
                if step.status in {"completed", "skipped"}:
                    continue
                if step.status == "execution_unknown":
                    change.status = plan.status = "paused"
                    self.store.save(plan)
                    return WorkflowResult(True, "paused", step.observation, plan=plan)
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
                    return WorkflowResult(True, "paused", "execution outcome unknown", plan=plan)
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
                    return WorkflowResult(
                        True,
                        "paused",
                        step.observation,
                        plan=plan,
                    )
                if decision.status != "passed":
                    step.status = "paused"
                    step.observation = str(getattr(decision, "reason", "verification failed"))
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
                step.observation = str(getattr(decision, "reason", "passed"))
                self.store.save(plan)
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
                    return WorkflowResult(
                        True,
                        "paused",
                        change.observation,
                        plan=plan,
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
                    return WorkflowResult(
                        True,
                        "paused",
                        change.observation,
                        plan=plan,
                        verification=semantic_decision,
                    )
                change.implementation_plan.status = "completed"
            change.status = "completed"
            change.observation = "all executable changes passed verification"
            self.store.save(plan)
        plan.status = "completed"
        self.store.save(plan)
        return WorkflowResult(True, "completed", "变更计划已执行并通过验证。", plan=plan)

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
            lines.append("冻结资源：")
            for resource in plan.resources:
                lines.append(
                    "- %s (%s/%s): %s"
                    % (resource.name, resource.kind, resource.status, resource.value)
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
                        "  执行绑定：已注册动作 %s 参数=%s"
                        % (binding.action, _redacted_binding_args(binding.args))
                    )
                    continue
                artifact = binding.shell_artifact
                lines.append(
                    "  执行绑定：受控脚本 %s sha256=%s"
                    % (
                        artifact.artifact_id if artifact is not None else "missing",
                        artifact.sha256 if artifact is not None else "missing",
                    )
                )
                if artifact is not None:
                    lines.extend(
                        [
                            "  脚本：",
                            "```bash",
                            artifact.script,
                            "```",
                        ]
                    )
        lines.extend(
            [
                "精确计划哈希：%s" % plan.content_hash,
                "请使用以下命令确认这份精确计划：",
                "confirm-priv-plan %s %s" % (plan.plan_id, plan.content_hash),
            ]
        )
        return "\n".join(lines)


def _localized_plan_text(value: Any) -> str:
    """Translate deterministic runtime phrases at the presentation boundary."""

    text = str(value or "")
    substitutions = (
        (r"\bStart master screen component\b", "启动 master Screen 组件"),
        (r"\bStart worker screen component\b", "启动 worker Screen 组件"),
        (r"\bRestart master screen component\b", "重启 master Screen 组件"),
        (r"\bRestart worker screen component\b", "重启 worker Screen 组件"),
        (r"start missing (master|worker) role at (\d+) and backend health succeeds",
         r"启动缺失的 \1 角色（端口 \2），并确认后端健康"),
        (r"restart requested (master|worker) role at (\d+) and backend health succeeds",
         r"按要求重启 \1 角色（端口 \2），并确认后端健康"),
        (r"recover (master|worker) for (/[^;\s]+)", r"恢复 \1（实例 \2）"),
    )
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


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
