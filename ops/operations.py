"""Controlled Ops operation plans.

This module is the phase-three safety substrate. It stores auditable operation
plans and confirmation state, and only executes an injected controlled recipe
runner for steps that explicitly bind a recipe_id. It never turns model text
into arbitrary shell.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from klonet_agent.ops.actions import (
    DEFAULT_OPS_ACTION_REGISTRY,
    default_action_bindings,
)
from klonet_agent.ops.command_policy import decide_ops_command


_UTC8 = timezone(timedelta(hours=8))
VALID_OPERATIONS = {"deploy_platform", "restart_platform", "destroy_platform"}
VALID_PLAN_STATUS = {"pending", "approved", "aborted", "completed", "failed"}
VALID_STEP_STATUS = {"pending", "approved", "running", "completed", "failed", "blocked"}
MAX_RECIPE_CONTENT_CHARS = 20000
MAX_PLAN_STEPS = 20
SENSITIVE_ACTION_CONTENT = re.compile(
    r"(?i)\b[A-Za-z0-9_-]*(?:password|passwd|pwd|api[_-]?key|secret|token)[A-Za-z0-9_-]*\s*[:=]"
)


@dataclass
class OperationStep:
    step_id: str
    title: str
    purpose: str
    action_type: str = ""
    risk: str = "normal"
    permission: str = ""
    requires_step_confirmation: bool = False
    status: str = "pending"
    observation: str = ""
    action: str = ""
    args: dict = field(default_factory=dict)
    # Deprecated compatibility fields for plans created before action/args.
    recipe_id: str = ""
    recipe_args: dict = field(default_factory=dict)


@dataclass
class RecipeExecutionResult:
    status: str
    output: str
    next_required_action: str = ""


@dataclass
class OperationPlan:
    plan_id: str
    operation: str
    target: str
    objective: str
    status: str = "pending"
    created_at: str = ""
    constraints: str = ""
    operation_args: dict = field(default_factory=dict)
    steps_source: str = "default"
    evidence: List[str] = field(default_factory=list)
    steps: List[OperationStep] = field(default_factory=list)


class OperationPlanStore:
    """Filesystem-backed OperationPlan store."""

    def __init__(
        self,
        root: Path,
        action_runner: Optional[Callable] = None,
        recipe_runner: Optional[Callable] = None,
    ):
        self.root = Path(root)
        self.action_runner = action_runner or recipe_runner
        self.recipe_runner = self.action_runner
        self.root.mkdir(parents=True, exist_ok=True)

    def create_plan(
        self,
        *,
        operation: str,
        target: str,
        evidence: Optional[List[str]] = None,
        objective: str = "",
        constraints: str = "",
        operation_args: Optional[dict] = None,
        steps: Optional[List[dict]] = None,
        action_bindings: Optional[dict] = None,
        recipe_bindings: Optional[dict] = None,
    ) -> OperationPlan:
        operation = operation if operation in VALID_OPERATIONS else "restart_platform"
        plan = OperationPlan(
            plan_id=_new_plan_id(operation),
            operation=operation,
            target=_one_line(target, 120),
            objective=_one_line(objective or _default_objective(operation, target), 260),
            created_at=datetime.now(_UTC8).isoformat(timespec="seconds"),
            constraints=str(constraints or "").strip(),
            operation_args=_clean_operation_args(operation_args or {}),
            steps_source="custom" if steps else "default",
            evidence=[_one_line(item, 300) for item in (evidence or [])[:12]],
            steps=_custom_steps(steps) if steps else _default_steps(operation),
        )
        if not steps:
            _apply_default_action_bindings(plan)
        _apply_action_bindings(plan, action_bindings or recipe_bindings or {})
        _normalize_plan_steps(plan)
        self.save_plan(plan)
        return plan

    def save_plan(self, plan: OperationPlan) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _normalize_plan_steps(plan)
        self._path(plan.plan_id).write_text(
            json.dumps(_plan_to_mapping(plan), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_plan(self, plan_id: str) -> OperationPlan:
        normalized = _safe_plan_id(plan_id)
        path = self._path(normalized)
        if not path.exists():
            raise ValueError(f"operation plan not found: {normalized}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        plan = _plan_from_mapping(raw)
        _normalize_plan_steps(plan)
        return plan

    def list_plans(
        self,
        limit: int = 10,
        status: str = "",
        operation: str = "",
        target: str = "",
    ) -> List[OperationPlan]:
        plans = []
        for path in self.root.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                plans.append(_plan_from_mapping(raw))
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                continue
        status_filter = str(status or "").strip()
        if status_filter in VALID_PLAN_STATUS:
            plans = [plan for plan in plans if plan.status == status_filter]
        operation_filter = str(operation or "").strip()
        if operation_filter in VALID_OPERATIONS:
            plans = [plan for plan in plans if plan.operation == operation_filter]
        target_filter = str(target or "").strip()
        if target_filter:
            plans = [plan for plan in plans if plan.target == target_filter]
        plans.sort(key=lambda item: item.created_at, reverse=True)
        bounded_limit = max(1, min(int(limit or 10), 50))
        return plans[:bounded_limit]

    def approve_plan(self, plan_id: str) -> OperationPlan:
        plan = self.load_plan(plan_id)
        plan.status = "approved"
        self.save_plan(plan)
        return plan

    def approve_step(self, plan_id: str, step_id: str) -> OperationPlan:
        plan = self.load_plan(plan_id)
        if plan.status != "approved":
            raise ValueError("plan must be approved before approving a step")
        step = _find_step(plan, step_id)
        if step.status == "blocked":
            raise ValueError(
                "blocked step must be resolved with resolve_ops_blocked_step before confirm-step"
            )
        if step.status == "running":
            raise ValueError(
                "running step must be inspected and resolved before confirm-step"
            )
        if step.status == "failed":
            raise ValueError("failed step cannot be approved; create a new plan or recover manually")
        step.status = "approved"
        self.save_plan(plan)
        return plan

    def resolve_blocked_step(self, plan_id: str, step_id: str, resolution_evidence: str) -> OperationPlan:
        plan = self.load_plan(plan_id)
        step = _find_step(plan, step_id)
        if step.status != "blocked":
            raise ValueError(f"operation step is not blocked: {step_id}")
        evidence = _one_line(resolution_evidence, 300)
        if not evidence:
            raise ValueError("resolution_evidence is required")
        step.status = "pending"
        if plan.status == "failed":
            plan.status = "approved"
        plan.evidence.append(evidence)
        plan.evidence = plan.evidence[-12:]
        self.save_plan(plan)
        return plan

    def execute_step(self, plan_id: str, step_id: str) -> str:
        plan = self.load_plan(plan_id)
        if plan.status != "approved":
            return "Error: plan must be approved before execution. Use confirm <plan_id>."
        step = _find_step(plan, step_id)
        if step.status == "completed":
            _record_observation(step, "step already completed; environment unchanged")
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                result_status="completed",
                execution_result="step already completed; environment unchanged",
            )
        if step.status == "blocked":
            _record_observation(step, "step is blocked; resolve required action before continuing")
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                result_status="blocked",
                execution_result="step is blocked; resolve required action before continuing",
                next_required_action="inspect_runtime_or_update_plan",
            )
        if step.status == "failed":
            _record_observation(step, "step already failed; create a new plan or repair state before continuing")
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                result_status="failed",
                execution_result="step already failed; create a new plan or repair state before continuing",
                next_required_action="create_new_plan_or_recover_manually",
            )
        if step.status == "running":
            step.status = "blocked"
            _record_observation(
                step,
                "previous execution left this step running; environment state unknown; inspect runtime",
            )
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                previous_status="running",
                result_status="blocked",
                execution_result=(
                    "previous execution left this step running; environment state unknown; "
                    "inspect runtime, logs and processes before retrying"
                ),
                next_required_action="inspect_runtime",
            )
        if step.requires_step_confirmation and step.status != "approved":
            return (
                "Error: step requires explicit confirm-step "
                f"{plan.plan_id} {step.step_id} before execution.\n"
                f"next_required_action=confirm-step {plan.plan_id} {step.step_id}"
            )
        previous_incomplete = _previous_incomplete_step(plan, step.step_id)
        if previous_incomplete:
            return f"Error: previous step must be completed first: {previous_incomplete.step_id}"
        previous_status = step.status
        if not step.action:
            if (
                plan.steps_source == "default"
                and plan.operation == "deploy_platform"
                and step.step_id == "precheck"
            ):
                step.status = "blocked"
                _record_observation(
                    step,
                    "deploy_precheck_requires_project_root_or_recipe; environment unchanged",
                )
                self.save_plan(plan)
                return _render_step_execution(
                    plan,
                    step,
                    previous_status=previous_status,
                    result_status="blocked",
                    execution_result=(
                        "deploy_precheck_requires_project_root_or_recipe; environment unchanged"
                    ),
                    next_required_action=(
                        "provide operation_args.project_root or bind a readonly precheck recipe"
                    ),
                )
            if _unbound_step_looks_mutating(plan, step):
                step.status = "blocked"
                _record_observation(
                    step,
                    "mutating_checkpoint_requires_action_binding; environment unchanged",
                )
                self.save_plan(plan)
                return _render_step_execution(
                    plan,
                    step,
                    previous_status=previous_status,
                    result_status="blocked",
                    execution_result=(
                        "mutating_checkpoint_requires_action_binding; environment unchanged"
                    ),
                    next_required_action="attach an allowlisted action before retrying",
                )
            if not step.requires_step_confirmation and step.risk == "normal":
                step.status = "completed"
                _record_observation(
                    step,
                    "readonly_or_manual_checkpoint_completed; environment unchanged",
                )
                if _all_steps_completed(plan):
                    plan.status = "completed"
                self.save_plan(plan)
                return _render_step_execution(
                    plan,
                    step,
                    previous_status=previous_status,
                    result_status="completed",
                    execution_result="readonly_or_manual_checkpoint_completed; environment unchanged",
                )
            step.status = "blocked"
            _record_observation(step, "no_recipe_attached; environment unchanged")
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                previous_status=previous_status,
                result_status="blocked",
                execution_result="no_recipe_attached; environment unchanged",
                next_required_action="attach an allowlisted action before executing this step",
            )
        if self.action_runner is None:
            step.status = "blocked"
            _record_observation(step, "recipe_runner_unavailable; environment unchanged")
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                previous_status=previous_status,
                result_status="blocked",
                execution_result="recipe_runner_unavailable; environment unchanged",
                next_required_action="configure a controlled action runner for this environment",
            )
        step.status = "running"
        self.save_plan(plan)
        result = self._run_action(plan, step)
        step.status = _recipe_status(result.status)
        _record_observation(step, result.output)
        if step.status == "failed":
            plan.status = "failed"
        elif _all_steps_completed(plan):
            plan.status = "completed"
        self.save_plan(plan)
        return _render_step_execution(
            plan,
            step,
            previous_status=previous_status,
            result_status=step.status,
            execution_result=result.output,
            next_required_action=result.next_required_action,
        )

    def execute_next_step(self, plan_id: str) -> str:
        plan = self.load_plan(plan_id)
        next_step_id = _next_step_id(plan)
        if next_step_id == "none":
            return (
                "ops_operation_execution\n"
                f"plan_id={plan.plan_id}\n"
                f"operation={plan.operation}\n"
                f"target={plan.target or 'unknown'}\n"
                f"plan_status={plan.status}\n"
                "execute_step=none\n"
                "result_status=completed\n"
                "execution_result=no remaining steps; environment unchanged"
            )
        return self.execute_step(plan_id, next_step_id)

    def execute_until_blocked(self, plan_id: str) -> str:
        """Run approved steps until completion, blockage, or step confirmation."""

        outputs = []
        max_steps = len(self.load_plan(plan_id).steps) + 1
        for _ in range(max_steps):
            plan = self.load_plan(plan_id)
            next_step_id = _next_step_id(plan)
            if next_step_id == "none":
                outputs.append(
                    "ops_operation_execution\n"
                    f"plan_id={plan.plan_id}\n"
                    f"operation={plan.operation}\n"
                    f"target={plan.target or 'unknown'}\n"
                    f"plan_status={plan.status}\n"
                    "execute_step=none\n"
                    "result_status=completed\n"
                    "execution_result=no remaining steps; environment unchanged"
                )
                break
            step = _find_step(plan, next_step_id)
            if step.requires_step_confirmation and step.status != "approved":
                outputs.append(
                    "ops_operation_execution\n"
                    f"plan_id={plan.plan_id}\n"
                    f"operation={plan.operation}\n"
                    f"target={plan.target or 'unknown'}\n"
                    f"plan_status={plan.status}\n"
                    f"execute_step={step.step_id}\n"
                    f"step_title={step.title}\n"
                    f"action_type={step.action_type or 'unknown'}\n"
                    f"permission={step.permission or _default_permission(step)}\n"
                    f"step_status={step.status}\n"
                    "result_status=waiting_for_confirmation\n"
                    "execution_result=step requires explicit confirmation; environment unchanged\n"
                    f"next_required_action=confirm-step {plan.plan_id} {step.step_id}"
                )
                break
            result = self.execute_step(plan_id, next_step_id)
            outputs.append(result)
            latest = self.load_plan(plan_id)
            latest_step = _find_step(latest, next_step_id)
            if result.startswith("Error:") or latest_step.status in {"blocked", "failed", "running"}:
                break
            if latest.status in {"completed", "failed", "aborted"}:
                break
        return "\n---\n".join(outputs)

    def _run_action(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        try:
            result = self.action_runner(plan, step)
        except Exception as exc:
            return RecipeExecutionResult("failed", f"recipe_exception={exc}")
        if isinstance(result, RecipeExecutionResult):
            return result
        return RecipeExecutionResult("failed", "recipe_runner_returned_invalid_result")

    def _path(self, plan_id: str) -> Path:
        return self.root / f"{_safe_plan_id(plan_id)}.json"


def render_plan(plan: OperationPlan) -> str:
    """Render a compact plan for the model and CLI."""

    lines = [
        "ops_operation_plan",
        f"plan_id={plan.plan_id}",
        f"operation={plan.operation}",
        f"target={plan.target or 'unknown'}",
        f"status={plan.status}",
        f"steps_source={plan.steps_source}",
        f"objective={plan.objective}",
    ]
    if plan.constraints:
        lines.append("constraints:")
        lines.extend(f"  {line}" for line in plan.constraints.splitlines()[:12])
    if plan.evidence:
        lines.append("evidence:")
        lines.extend(f"  - {item}" for item in plan.evidence[:8])
    lines.append("execution_state:")
    lines.append(f"  total_steps={len(plan.steps)}")
    for status in sorted(VALID_STEP_STATUS):
        count = sum(1 for step in plan.steps if step.status == status)
        if count:
            lines.append(f"  {status}={count}")
    lines.append(f"  next_step={_next_step_id(plan)}")
    lines.append(
        "  execution_order="
        + " -> ".join(step.step_id for step in plan.steps)
    )
    lines.append("steps:")
    for step in plan.steps:
        confirm = " step-confirm" if step.requires_step_confirmation else ""
        action = f" action={step.action_type}" if step.action_type else ""
        permission = f" permission={step.permission}" if step.permission else ""
        lines.append(
            f"  - {step.step_id}: {step.title} "
            f"risk={step.risk}{permission}{confirm}{action} status={step.status}"
        )
        if step.observation:
            lines.append(f"    observation={_one_line(step.observation, 180)}")
    bindings = [
        step
        for step in plan.steps
        if step.action or step.args
    ]
    if bindings:
        lines.append("execution_bindings:")
        for step in bindings:
            action = f" action={step.action_type}" if step.action_type else ""
            legacy_recipe = f" recipe={step.recipe_id}" if step.recipe_id else ""
            action_id = f" action_id={step.action}" if step.action else ""
            action_args = _render_action_args(step.args)
            lines.append(
                f"  - {step.step_id}:{action}{legacy_recipe}{action_id}{action_args}"
            )
    lines.append(f"approve_plan_command=confirm {plan.plan_id}")
    lines.append(
        "approve_step_command=confirm-step "
        f"{plan.plan_id} <step_id>"
    )
    return "\n".join(lines)


def render_plan_list(plans: List[OperationPlan]) -> str:
    lines = [
        "ops_operation_plan_list",
        f"count={len(plans)}",
    ]
    for plan in plans:
        lines.append(
            "plan "
            f"plan_id={plan.plan_id} "
            f"operation={plan.operation} "
            f"target={plan.target or 'unknown'} "
            f"status={plan.status} "
            f"next_step={_next_step_id(plan)} "
            f"created_at={plan.created_at or 'unknown'}"
        )
    return "\n".join(lines)


def render_step_resolution(plan: OperationPlan, step_id: str, resolution_evidence: str) -> str:
    step = _find_step(plan, step_id)
    lines = [
        "ops_operation_resolution",
        f"plan_id={plan.plan_id}",
        f"operation={plan.operation}",
        f"target={plan.target or 'unknown'}",
        f"plan_status={plan.status}",
        f"resolved_step={step.step_id}",
        f"step_status={step.status}",
        "result_status=resolved",
        f"resolution_evidence={_one_line(resolution_evidence, 300)}",
        f"next_step={_next_step_id(plan)}",
    ]
    if step.requires_step_confirmation:
        lines.append(f"next_required_action=confirm-step {plan.plan_id} {step.step_id}")
    else:
        lines.append(f"next_required_action=execute_ops_next_step {plan.plan_id}")
    return "\n".join(lines)


def execute_step_preview(plan: OperationPlan, step_id: str) -> str:
    """Return the current execution decision for a step.

    Real recipes are intentionally not wired yet. This prevents the new tools
    from becoming a disguised arbitrary command runner.
    """

    if plan.status != "approved":
        return "Error: plan must be approved before execution. Use confirm <plan_id>."
    _find_step(plan, step_id)
    return (
        "Error: 尚未接入真实执行 recipe；当前底座只支持生成、保存和授权计划，"
        "不会修改服务器环境。"
    )


def _render_step_execution(
    plan: OperationPlan,
    step: OperationStep,
    *,
    result_status: str,
    execution_result: str,
    previous_status: str = "",
    next_required_action: str = "",
) -> str:
    lines = [
        "ops_operation_execution",
        f"plan_id={plan.plan_id}",
        f"operation={plan.operation}",
        f"target={plan.target or 'unknown'}",
        f"plan_status={plan.status}",
        f"execute_step={step.step_id}",
        f"step_title={step.title}",
        f"action_type={step.action_type or 'unknown'}",
        f"permission={step.permission or _default_permission(step)}",
    ]
    if previous_status:
        lines.append(f"previous_step_status={previous_status}")
    lines.extend(
        [
            f"step_status={step.status}",
            f"result_status={result_status}",
            f"execution_result={execution_result}",
        ]
    )
    if step.observation:
        lines.append(f"observation={_one_line(step.observation, 300)}")
    if next_required_action:
        lines.append(f"next_required_action={next_required_action}")
    return "\n".join(lines)


def _default_steps(operation: str) -> List[OperationStep]:
    if operation == "deploy_platform":
        return [
            OperationStep("precheck", "预检环境和冲突", "确认端口、screen、源码路径和共享服务状态", action_type="validate_project_files"),
            OperationStep("prepare-files", "准备项目文件与配置", "复制入口文件并渲染配置", action_type="prepare_project_files", risk="controlled"),
            OperationStep("start-services", "启动后端与前端服务", "启动 screen、校验 nginx 并 reload", action_type="start_platform", risk="controlled"),
        ]
    if operation == "destroy_platform":
        return [
            OperationStep("identify-owned-resources", "识别目标平台资源", "证明 screen、端口、目录和 nginx 片段属于目标平台", action_type="identify_owned_resources"),
            OperationStep("stop-services", "停止目标平台服务", "停止目标 screen 和专属进程", action_type="stop_platform", risk="dangerous", requires_step_confirmation=True),
            OperationStep("cleanup-owned-resources", "清理本平台资源", "只清理由计划证明归属的资源", action_type="cleanup_owned_resources", risk="dangerous", requires_step_confirmation=True),
        ]
    return [
        OperationStep("precheck-runtime", "读取当前运行态", "确认目标平台、screen、端口和日志来源", action_type="inspect_runtime"),
        OperationStep("restart-master", "重启 Master", "按已确认启动命令重启 master screen", action_type="restart_component", risk="privileged", requires_step_confirmation=True),
        OperationStep("restart-worker", "重启 Worker", "按已确认启动命令重启 worker screen", action_type="restart_component", risk="privileged", requires_step_confirmation=True),
        OperationStep("restart-celery", "重启 Celery", "按已确认启动命令重启 celery screen", action_type="restart_component", risk="privileged", requires_step_confirmation=True),
        OperationStep("restart-web-terminal", "重启 Web Terminal", "按已确认启动命令重启 web terminal screen", action_type="restart_component", risk="privileged", requires_step_confirmation=True),
        OperationStep("verify-health", "验证重启结果", "检查 screen、进程、端口和最新日志", action_type="verify_health"),
    ]


def _custom_steps(raw_steps: List[dict]) -> List[OperationStep]:
    """Validate LLM-authored task steps without accepting commands."""

    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("steps must be a non-empty array")
    if len(raw_steps) > MAX_PLAN_STEPS:
        raise ValueError(f"steps exceeds maximum of {MAX_PLAN_STEPS}")
    result = []
    seen = set()
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"step {index} must be an object")
        step_id = _safe_step_id(raw.get("step_id") or f"step-{index}")
        if step_id in seen:
            raise ValueError(f"duplicate step_id: {step_id}")
        seen.add(step_id)
        if "command" in raw or "shell" in raw:
            raise ValueError(f"step {step_id} must use action + args, not command")
        requested_action = _one_line(str(raw.get("action") or ""), 120)
        action = ""
        if requested_action:
            action = DEFAULT_OPS_ACTION_REGISTRY.canonical_name(requested_action)
            if not action:
                raise ValueError(f"action_not_allowlisted={requested_action}")
        action_args = raw.get("args") or {}
        if not isinstance(action_args, dict):
            raise ValueError(f"step {step_id} args must be an object")
        cleaned_args = _clean_action_args(action, action_args)
        problem = _validate_plan_action_args(action, cleaned_args)
        if problem:
            raise ValueError(f"step {step_id} {problem}")
        spec = DEFAULT_OPS_ACTION_REGISTRY.get(action)
        command_decision = decide_ops_command(cleaned_args) if action == "run_ops_command" else None
        if command_decision and not command_decision.allowed:
            raise ValueError(f"step {step_id} {command_decision.reason}")
        if command_decision and command_decision.allowed:
            risk = command_decision.risk
        else:
            risk = spec.risk if spec else _one_line(str(raw.get("risk") or "normal"), 40)
        requires_step_confirmation = bool(
            spec and spec.confirmation_scope == "step"
        )
        if command_decision and command_decision.requires_step_confirmation:
            requires_step_confirmation = True
        result.append(
            OperationStep(
                step_id=step_id,
                title=_one_line(str(raw.get("title") or step_id), 160),
                purpose=_one_line(str(raw.get("purpose") or raw.get("title") or step_id), 260),
                action_type=_action_type_for_recipe(action) if action else "checkpoint",
                risk=risk or "normal",
                requires_step_confirmation=requires_step_confirmation,
                action=action,
                args=cleaned_args,
                recipe_id=action,
                recipe_args=dict(cleaned_args),
            )
        )
    return result


def _clean_action_args(action: str, args: dict) -> dict:
    cleaned = {}
    for key, value in args.items():
        normalized_key = _one_line(str(key), 80)
        if not normalized_key or value is None:
            continue
        if action == "write_ops_file" and normalized_key == "content":
            cleaned[normalized_key] = str(value)[:MAX_RECIPE_CONTENT_CHARS]
        elif isinstance(value, list):
            cleaned[normalized_key] = [
                _one_line(str(item), 300)
                for item in value[:40]
                if item is not None
            ]
        elif isinstance(value, dict):
            cleaned[normalized_key] = {
                _one_line(str(item_key), 80): _one_line(str(item_value), 300)
                for item_key, item_value in list(value.items())[:40]
                if item_key is not None and item_value is not None
            }
        else:
            cleaned[normalized_key] = _one_line(str(value), 300)
    return cleaned


def _safe_step_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        raise ValueError("invalid step_id")
    if any(not (char.isalnum() or char in "-_") for char in text):
        raise ValueError(f"invalid step_id: {text}")
    return text


def _normalize_plan_steps(plan: OperationPlan) -> None:
    for step in plan.steps:
        requested_action = step.action or step.recipe_id
        if step.recipe_id and step.recipe_id != step.action:
            requested_action = step.recipe_id
        canonical_action = DEFAULT_OPS_ACTION_REGISTRY.canonical_name(requested_action)
        if canonical_action:
            step.action = canonical_action
            step.recipe_id = canonical_action
        elif requested_action:
            # Preserve custom runners and old extension points.
            step.action = requested_action
            step.recipe_id = requested_action
        spec = DEFAULT_OPS_ACTION_REGISTRY.get(step.action)
        if spec:
            if step.risk == "normal" and spec.risk != "normal":
                step.risk = spec.risk
            if spec.confirmation_scope == "step":
                step.requires_step_confirmation = True
        if step.action == "run_ops_command":
            command_decision = decide_ops_command(step.args or step.recipe_args)
            if command_decision.allowed:
                step.risk = command_decision.risk
                if command_decision.requires_step_confirmation:
                    step.requires_step_confirmation = True
        if step.args:
            step.recipe_args = dict(step.args)
        elif step.recipe_args:
            step.args = dict(step.recipe_args)
        if not step.action_type:
            step.action_type = _default_action_type(plan.operation, step.step_id, step.action)
        if not step.permission:
            step.permission = _default_permission(step)
        if step.observation:
            step.observation = _one_line(step.observation, 300)


def _default_action_type(operation: str, step_id: str, recipe_id: str = "") -> str:
    if recipe_id:
        return _action_type_for_recipe(recipe_id)
    defaults = {
        "deploy_platform": {
            "precheck": "validate_project_files",
            "prepare-files": "prepare_project_files",
            "start-shared-services": "ensure_shared_services",
            "start-services": "start_platform",
        },
        "destroy_platform": {
            "identify-owned-resources": "identify_owned_resources",
            "stop-services": "stop_platform",
            "cleanup-owned-resources": "cleanup_owned_resources",
        },
        "restart_platform": {
            "precheck-runtime": "inspect_runtime",
            "restart-master": "restart_component",
            "restart-worker": "restart_component",
            "restart-celery": "restart_component",
            "restart-web-terminal": "restart_component",
            "verify-health": "verify_health",
        },
    }
    return defaults.get(operation, {}).get(step_id, step_id.replace("-", "_"))


def _action_type_for_recipe(recipe_id: str) -> str:
    spec = DEFAULT_OPS_ACTION_REGISTRY.get(recipe_id)
    if not spec:
        return recipe_id
    return spec.aliases[0] if spec.aliases else spec.name


def _unbound_step_looks_mutating(plan: OperationPlan, step: OperationStep) -> bool:
    if plan.steps_source != "custom" or plan.operation != "deploy_platform":
        return False
    text = " ".join([step.step_id, step.title, step.purpose]).lower()
    mutating_markers = (
        "install",
        "安装",
        "copy",
        "复制",
        "config",
        "configure",
        "配置",
        "write",
        "写",
        "修改",
        "生成",
        "install_nginx",
        "nginx",
        "reload",
        "重载",
        "start",
        "启动",
        "clone",
        "克隆",
    )
    readonly_markers = (
        "verify",
        "验证",
        "check",
        "检查",
        "inspect",
        "只读",
        "precheck",
        "预检",
    )
    return any(marker in text for marker in mutating_markers) and not any(
        marker in text for marker in readonly_markers
    )


def _default_permission(step: OperationStep) -> str:
    if step.requires_step_confirmation:
        return "step_confirm_required"
    if step.risk in {"dangerous", "destructive"}:
        return "step_confirm_required"
    if step.risk in {"controlled", "privileged"}:
        return "plan_confirmed"
    return "readonly_or_plan_confirmed"


def _record_observation(step: OperationStep, text: str) -> None:
    step.observation = _one_line(text, 300)


def _find_step(plan: OperationPlan, step_id: str) -> OperationStep:
    for step in plan.steps:
        if step.step_id == step_id:
            return step
    raise ValueError(f"operation step not found: {step_id}")


def _apply_action_bindings(plan: OperationPlan, action_bindings: dict) -> None:
    if not isinstance(action_bindings, dict):
        return
    for step_id, binding in action_bindings.items():
        if not isinstance(binding, dict):
            continue
        try:
            step = _find_step(plan, str(step_id))
        except ValueError:
            continue
        previous_action = step.action or step.recipe_id
        previous_args = dict(step.args or step.recipe_args)
        requested_action = _one_line(
            str(binding.get("action") or binding.get("recipe_id") or ""),
            120,
        )
        canonical_action = DEFAULT_OPS_ACTION_REGISTRY.canonical_name(requested_action)
        if requested_action and not canonical_action:
            step.action = requested_action
            step.recipe_id = requested_action
        elif canonical_action:
            step.action = canonical_action
            step.recipe_id = canonical_action
            step.action_type = _one_line(
                str(binding.get("action_type") or _action_type_for_recipe(canonical_action)),
                120,
            )
        elif binding.get("action_type"):
            step.action_type = _one_line(str(binding.get("action_type")), 120)
            routed_action = DEFAULT_OPS_ACTION_REGISTRY.canonical_name(step.action_type)
            if routed_action:
                step.action = routed_action
                step.recipe_id = routed_action
        args = binding.get("args")
        if not isinstance(args, dict):
            args = binding.get("recipe_args")
        if not isinstance(args, dict):
            if step.action != previous_action:
                step.args = {}
                step.recipe_args = {}
            else:
                step.args = previous_args
                step.recipe_args = dict(previous_args)
            continue
        step.args = {}
        step.recipe_args = {}
        effective_action = step.action or DEFAULT_OPS_ACTION_REGISTRY.canonical_name(step.action_type)
        for key, value in args.items():
            if not key or value is None:
                continue
            normalized_key = _one_line(str(key), 80)
            if effective_action == "write_ops_file" and normalized_key == "content":
                step.args[normalized_key] = str(value)[:MAX_RECIPE_CONTENT_CHARS]
                continue
            step.args[normalized_key] = _one_line(str(value), 300)
        problem = _validate_plan_action_args(effective_action, step.args)
        if problem:
            raise ValueError(f"step {step.step_id} {problem}")
        step.recipe_args = dict(step.args)


def _apply_default_action_bindings(plan: OperationPlan) -> None:
    bindings, add_shared_services = default_action_bindings(
        plan.operation,
        plan.target,
        plan.operation_args,
    )
    if add_shared_services:
        _ensure_shared_services_step(plan)
    _apply_action_bindings(plan, bindings)


def _clean_operation_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    return {
        _one_line(str(key), 80): _one_line(str(value), 300)
        for key, value in args.items()
        if key and value is not None
    }


def _validate_plan_action_args(action: str, args: dict) -> str:
    if action != "write_ops_file" or not isinstance(args, dict):
        return ""
    path = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if _is_direct_nginx_write_path(path):
        return "nginx_config_requires_install_nginx_config"
    if SENSITIVE_ACTION_CONTENT.search(content):
        return "sensitive_content_not_allowed"
    return ""


def _is_direct_nginx_write_path(path: str) -> bool:
    if not path:
        return False
    normalized = str(path).replace("\\", "/")
    return normalized == "/etc/nginx" or normalized.startswith("/etc/nginx/")


def _ensure_shared_services_step(plan: OperationPlan) -> None:
    try:
        _find_step(plan, "start-shared-services")
    except ValueError:
        _insert_step_before(
            plan,
            "start-services",
            OperationStep(
                "start-shared-services",
                "启动共享基础服务",
                "检查 Redis/MySQL/RabbitMQ；缺失时运行 docker_service.sh",
                action_type="ensure_shared_services",
                risk="controlled",
            ),
        )


def _insert_step_before(plan: OperationPlan, before_step_id: str, step: OperationStep) -> None:
    for index, existing in enumerate(plan.steps):
        if existing.step_id == step.step_id:
            plan.steps[index] = step
            return
        if existing.step_id == before_step_id:
            plan.steps.insert(index, step)
            return
    plan.steps.append(step)


def _render_action_args(action_args: dict) -> str:
    if not isinstance(action_args, dict) or not action_args:
        return ""
    pairs = []
    for key in sorted(action_args)[:6]:
        if not key:
            continue
        if str(key) == "content":
            pairs.append(f"args.content=<{len(str(action_args[key]))} chars>")
            continue
        pairs.append(
            f"args.{_one_line(str(key), 40)}="
            f"{_one_line(str(action_args[key]), 120)} "
            f"recipe_args.{_one_line(str(key), 40)}="
            f"{_one_line(str(action_args[key]), 120)}"
        )
    if len(action_args) > 6:
        pairs.append("args.omitted=...")
    return " " + " ".join(pairs) if pairs else ""


def _next_step_id(plan: OperationPlan) -> str:
    for step in plan.steps:
        if step.status != "completed":
            return step.step_id
    return "none"


def _previous_incomplete_step(plan: OperationPlan, step_id: str) -> Optional[OperationStep]:
    for step in plan.steps:
        if step.step_id == step_id:
            return None
        if step.status != "completed":
            return step
    return None


def _recipe_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"completed", "failed", "blocked"}:
        return normalized
    return "failed"


def _all_steps_completed(plan: OperationPlan) -> bool:
    return bool(plan.steps) and all(step.status == "completed" for step in plan.steps)


def _plan_to_mapping(plan: OperationPlan) -> dict:
    return asdict(plan)


def _plan_from_mapping(raw: dict) -> OperationPlan:
    step_fields = {item.name for item in fields(OperationStep)}
    steps = [
        OperationStep(**{key: value for key, value in step.items() if key in step_fields})
        for step in raw.get("steps", [])
        if isinstance(step, dict)
    ]
    status = str(raw.get("status") or "pending")
    if status not in VALID_PLAN_STATUS:
        status = "pending"
    for step in steps:
        if step.status not in VALID_STEP_STATUS:
            step.status = "pending"
    steps_source = str(raw.get("steps_source") or "").strip()
    if steps_source not in {"default", "custom"}:
        default_ids = {step.step_id for step in _default_steps(str(raw.get("operation") or "restart_platform"))}
        actual_ids = {step.step_id for step in steps}
        steps_source = "default" if actual_ids == default_ids else "custom"
    return OperationPlan(
        plan_id=_safe_plan_id(str(raw.get("plan_id") or "")),
        operation=str(raw.get("operation") or "restart_platform"),
        target=str(raw.get("target") or ""),
        objective=str(raw.get("objective") or ""),
        status=status,
        created_at=str(raw.get("created_at") or ""),
        constraints=str(raw.get("constraints") or ""),
        operation_args=_clean_operation_args(raw.get("operation_args") or {}),
        steps_source=steps_source,
        evidence=[str(item) for item in raw.get("evidence", []) if item],
        steps=steps,
    )


def _new_plan_id(operation: str) -> str:
    prefix = {
        "deploy_platform": "deploy",
        "restart_platform": "restart",
        "destroy_platform": "destroy",
    }.get(operation, "ops")
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _safe_plan_id(plan_id: str) -> str:
    cleaned = "".join(ch for ch in str(plan_id or "") if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise ValueError("empty operation plan id")
    return cleaned[:80]


def _default_objective(operation: str, target: str) -> str:
    target_text = target or "目标平台"
    if operation == "deploy_platform":
        return f"部署 {target_text}"
    if operation == "destroy_platform":
        return f"销毁 {target_text}"
    return f"重启 {target_text}"


def _one_line(text: str, limit: int) -> str:
    compacted = " ".join(str(text or "").split())
    if len(compacted) > limit:
        return compacted[: limit - 3] + "..."
    return compacted
