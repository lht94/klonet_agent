"""Controlled Ops operation plans.

This module is the phase-three safety substrate. It stores auditable operation
plans and confirmation state, and only executes an injected controlled recipe
runner for steps that explicitly bind a recipe_id. It never turns model text
into arbitrary shell.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional


_UTC8 = timezone(timedelta(hours=8))
VALID_OPERATIONS = {"deploy_platform", "restart_platform", "destroy_platform"}
VALID_PLAN_STATUS = {"pending", "approved", "aborted", "completed", "failed"}
VALID_STEP_STATUS = {"pending", "approved", "running", "completed", "failed", "blocked"}
RESTART_STEP_COMPONENTS = {
    "restart-master": ("master", "m"),
    "restart-worker": ("worker", "w"),
    "restart-celery": ("celery", "c"),
    "restart-web-terminal": ("web_terminal", "web"),
}
MAX_RECIPE_CONTENT_CHARS = 20000


@dataclass
class OperationStep:
    step_id: str
    title: str
    purpose: str
    risk: str = "normal"
    requires_step_confirmation: bool = False
    status: str = "pending"
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
    evidence: List[str] = field(default_factory=list)
    steps: List[OperationStep] = field(default_factory=list)


class OperationPlanStore:
    """Filesystem-backed OperationPlan store."""

    def __init__(self, root: Path, recipe_runner: Optional[Callable] = None):
        self.root = Path(root)
        self.recipe_runner = recipe_runner
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
            evidence=[_one_line(item, 300) for item in (evidence or [])[:12]],
            steps=_default_steps(operation),
        )
        _apply_default_recipe_bindings(plan)
        _apply_recipe_bindings(plan, recipe_bindings or {})
        self.save_plan(plan)
        return plan

    def save_plan(self, plan: OperationPlan) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
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
        return _plan_from_mapping(raw)

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
            return _render_step_execution(
                plan,
                step,
                result_status="completed",
                execution_result="step already completed; environment unchanged",
            )
        if step.status == "blocked":
            return _render_step_execution(
                plan,
                step,
                result_status="blocked",
                execution_result="step is blocked; resolve required action before continuing",
                next_required_action="inspect_runtime_or_update_plan",
            )
        if step.status == "failed":
            return _render_step_execution(
                plan,
                step,
                result_status="failed",
                execution_result="step already failed; create a new plan or repair state before continuing",
                next_required_action="create_new_plan_or_recover_manually",
            )
        if step.status == "running":
            step.status = "blocked"
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
        if not step.recipe_id:
            if plan.operation == "deploy_platform" and step.step_id == "precheck":
                step.status = "blocked"
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
            if not step.requires_step_confirmation and step.risk == "normal":
                step.status = "completed"
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
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                previous_status=previous_status,
                result_status="blocked",
                execution_result="no_recipe_attached; environment unchanged",
                next_required_action="attach a controlled recipe before executing this step",
            )
        if self.recipe_runner is None:
            step.status = "blocked"
            self.save_plan(plan)
            return _render_step_execution(
                plan,
                step,
                previous_status=previous_status,
                result_status="blocked",
                execution_result="recipe_runner_unavailable; environment unchanged",
                next_required_action="configure a controlled recipe runner for this environment",
            )
        step.status = "running"
        self.save_plan(plan)
        result = self._run_recipe(plan, step)
        step.status = _recipe_status(result.status)
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

    def _run_recipe(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        try:
            result = self.recipe_runner(plan, step)
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
        recipe = f" recipe={step.recipe_id}" if step.recipe_id else ""
        recipe_args = _render_recipe_args(step.recipe_args)
        lines.append(
            f"  - {step.step_id}: {step.title} "
            f"risk={step.risk}{confirm}{recipe}{recipe_args} status={step.status}"
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
    if next_required_action:
        lines.append(f"next_required_action={next_required_action}")
    return "\n".join(lines)


def _default_steps(operation: str) -> List[OperationStep]:
    if operation == "deploy_platform":
        return [
            OperationStep("precheck", "预检环境和冲突", "确认端口、screen、源码路径和共享服务状态"),
            OperationStep("prepare-files", "准备项目文件与配置", "复制入口文件并渲染配置", risk="controlled"),
            OperationStep("start-services", "启动后端与前端服务", "启动 screen、校验 nginx 并 reload", risk="controlled"),
        ]
    if operation == "destroy_platform":
        return [
            OperationStep("identify-owned-resources", "识别目标平台资源", "证明 screen、端口、目录和 nginx 片段属于目标平台"),
            OperationStep("stop-services", "停止目标平台服务", "停止目标 screen 和专属进程", risk="dangerous", requires_step_confirmation=True),
            OperationStep("cleanup-owned-resources", "清理本平台资源", "只清理由计划证明归属的资源", risk="dangerous", requires_step_confirmation=True),
        ]
    return [
        OperationStep("precheck-runtime", "读取当前运行态", "确认目标平台、screen、端口和日志来源"),
        OperationStep("restart-master", "重启 Master", "按已确认启动命令重启 master screen", risk="privileged", requires_step_confirmation=True),
        OperationStep("restart-worker", "重启 Worker", "按已确认启动命令重启 worker screen", risk="privileged", requires_step_confirmation=True),
        OperationStep("restart-celery", "重启 Celery", "按已确认启动命令重启 celery screen", risk="privileged", requires_step_confirmation=True),
        OperationStep("restart-web-terminal", "重启 Web Terminal", "按已确认启动命令重启 web terminal screen", risk="privileged", requires_step_confirmation=True),
        OperationStep("verify-health", "验证重启结果", "检查 screen、进程、端口和最新日志"),
    ]


def _find_step(plan: OperationPlan, step_id: str) -> OperationStep:
    for step in plan.steps:
        if step.step_id == step_id:
            return step
    raise ValueError(f"operation step not found: {step_id}")


def _apply_recipe_bindings(plan: OperationPlan, recipe_bindings: dict) -> None:
    if not isinstance(recipe_bindings, dict):
        return
    for step_id, binding in recipe_bindings.items():
        if not isinstance(binding, dict):
            continue
        try:
            step = _find_step(plan, str(step_id))
        except ValueError:
            continue
        previous_recipe_id = step.recipe_id
        previous_recipe_args = dict(step.recipe_args)
        recipe_id = _one_line(str(binding.get("recipe_id") or ""), 120)
        if recipe_id:
            step.recipe_id = recipe_id
        args = binding.get("args")
        if not isinstance(args, dict):
            args = binding.get("recipe_args")
        if not isinstance(args, dict):
            if step.recipe_id != previous_recipe_id:
                step.recipe_args = {}
            else:
                step.recipe_args = previous_recipe_args
            continue
        step.recipe_args = {}
        for key, value in args.items():
            if not key or value is None:
                continue
            normalized_key = _one_line(str(key), 80)
            if step.recipe_id == "write_ops_file" and normalized_key == "content":
                step.recipe_args[normalized_key] = str(value)[:MAX_RECIPE_CONTENT_CHARS]
                continue
            step.recipe_args[normalized_key] = _one_line(str(value), 300)


def _apply_default_recipe_bindings(plan: OperationPlan) -> None:
    if plan.operation == "restart_platform" and plan.target:
        project_root = str(plan.operation_args.get("project_root") or "").strip()
        if project_root:
            for step_id, (component, screen_suffix) in RESTART_STEP_COMPONENTS.items():
                try:
                    step = _find_step(plan, step_id)
                except ValueError:
                    continue
                step.recipe_id = "restart_screen_component"
                step.recipe_args = {
                    "platform": _one_line(plan.target, 120),
                    "component": component,
                    "screen_session": _one_line(f"{plan.target}_{screen_suffix}", 120),
                    "project_root": _one_line(project_root, 300),
                }
    if plan.operation == "deploy_platform" and plan.target:
        project_root = str(plan.operation_args.get("project_root") or "").strip()
        archive_path = str(plan.operation_args.get("archive_path") or "").strip()
        destination_dir = str(plan.operation_args.get("destination_dir") or "").strip()
        script_dir = str(plan.operation_args.get("script_dir") or "").strip()
        script_name = str(plan.operation_args.get("script_name") or "").strip()
        script_args = str(plan.operation_args.get("script_args") or "").strip()
        shared_services_script_dir = str(
            plan.operation_args.get("shared_services_script_dir")
            or plan.operation_args.get("docker_service_script_dir")
            or ""
        ).strip()
        if not shared_services_script_dir and script_name == "docker_service.sh":
            shared_services_script_dir = script_dir
        skip_shared_services = _truthy(plan.operation_args.get("skip_shared_services"))
        if not shared_services_script_dir:
            shared_services_script_dir = "/root/vemu_install_new_gen"
        if project_root:
            try:
                precheck_step = _find_step(plan, "precheck")
                precheck_step.recipe_id = "validate_project_files"
                precheck_step.recipe_args = {
                    "project_root": _one_line(project_root, 300),
                }
            except ValueError:
                pass
            try:
                prepare_step = _find_step(plan, "prepare-files")
                prepare_step.recipe_id = "prepare_project_files"
                prepare_step.recipe_args = {
                    "project_root": _one_line(project_root, 300),
                }
            except ValueError:
                pass
            try:
                step = _find_step(plan, "start-services")
            except ValueError:
                return
            step.recipe_id = "start_platform_screens"
            step.recipe_args = {
                "platform": _one_line(plan.target, 120),
                "project_root": _one_line(project_root, 300),
            }
            if not skip_shared_services:
                _bind_shared_services_step(plan, shared_services_script_dir)
            return
        try:
            prepare_step = _find_step(plan, "prepare-files")
        except ValueError:
            prepare_step = None
        if prepare_step and archive_path and destination_dir:
            prepare_step.recipe_id = "extract_archive"
            prepare_step.recipe_args = {
                "archive_path": _one_line(archive_path, 300),
                "destination_dir": _one_line(destination_dir, 300),
            }
            return
        if prepare_step and script_dir and script_name:
            prepare_step.recipe_id = "run_install_script"
            prepare_step.recipe_args = {
                "script_dir": _one_line(script_dir, 300),
                "script_name": _one_line(script_name, 120),
            }
            if script_args:
                prepare_step.recipe_args["script_args"] = _one_line(script_args, 300)
    if plan.operation == "destroy_platform" and plan.target:
        try:
            step = _find_step(plan, "stop-services")
        except ValueError:
            return
        step.recipe_id = "stop_platform_screens"
        step.recipe_args = {"platform": _one_line(plan.target, 120)}


def _clean_operation_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    return {
        _one_line(str(key), 80): _one_line(str(value), 300)
        for key, value in args.items()
        if key and value is not None
    }


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _bind_shared_services_step(plan: OperationPlan, script_dir: str) -> None:
    if not script_dir:
        return
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
                risk="controlled",
            ),
        )
    step = _find_step(plan, "start-shared-services")
    step.recipe_id = "ensure_shared_services"
    step.recipe_args = {
        "script_dir": _one_line(script_dir, 300),
    }


def _insert_step_before(plan: OperationPlan, before_step_id: str, step: OperationStep) -> None:
    for index, existing in enumerate(plan.steps):
        if existing.step_id == step.step_id:
            plan.steps[index] = step
            return
        if existing.step_id == before_step_id:
            plan.steps.insert(index, step)
            return
    plan.steps.append(step)


def _render_recipe_args(recipe_args: dict) -> str:
    if not isinstance(recipe_args, dict) or not recipe_args:
        return ""
    pairs = []
    for key in sorted(recipe_args)[:6]:
        if not key:
            continue
        if str(key) == "content":
            pairs.append(f"recipe_args.content=<{len(str(recipe_args[key]))} chars>")
            continue
        pairs.append(
            f"recipe_args.{_one_line(str(key), 40)}="
            f"{_one_line(str(recipe_args[key]), 120)}"
        )
    if len(recipe_args) > 6:
        pairs.append("recipe_args.omitted=...")
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
    steps = [
        OperationStep(**step)
        for step in raw.get("steps", [])
        if isinstance(step, dict)
    ]
    status = str(raw.get("status") or "pending")
    if status not in VALID_PLAN_STATUS:
        status = "pending"
    for step in steps:
        if step.status not in VALID_STEP_STATUS:
            step.status = "pending"
    return OperationPlan(
        plan_id=_safe_plan_id(str(raw.get("plan_id") or "")),
        operation=str(raw.get("operation") or "restart_platform"),
        target=str(raw.get("target") or ""),
        objective=str(raw.get("objective") or ""),
        status=status,
        created_at=str(raw.get("created_at") or ""),
        constraints=str(raw.get("constraints") or ""),
        operation_args=_clean_operation_args(raw.get("operation_args") or {}),
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
