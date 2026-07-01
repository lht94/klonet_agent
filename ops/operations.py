"""Controlled Ops operation plans.

This module is the phase-three safety substrate. It stores auditable operation
plans and confirmation state, but deliberately does not execute shell commands.
Concrete deployment/restart/destroy recipes can be attached later step by step.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional


_UTC8 = timezone(timedelta(hours=8))
VALID_OPERATIONS = {"deploy_platform", "restart_platform", "destroy_platform"}
VALID_PLAN_STATUS = {"pending", "approved", "aborted", "completed", "failed"}
VALID_STEP_STATUS = {"pending", "approved", "running", "completed", "failed", "blocked"}


@dataclass
class OperationStep:
    step_id: str
    title: str
    purpose: str
    risk: str = "normal"
    requires_step_confirmation: bool = False
    status: str = "pending"


@dataclass
class OperationPlan:
    plan_id: str
    operation: str
    target: str
    objective: str
    status: str = "pending"
    created_at: str = ""
    constraints: str = ""
    evidence: List[str] = field(default_factory=list)
    steps: List[OperationStep] = field(default_factory=list)


class OperationPlanStore:
    """Filesystem-backed OperationPlan store."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create_plan(
        self,
        *,
        operation: str,
        target: str,
        evidence: Optional[List[str]] = None,
        objective: str = "",
        constraints: str = "",
    ) -> OperationPlan:
        operation = operation if operation in VALID_OPERATIONS else "restart_platform"
        plan = OperationPlan(
            plan_id=_new_plan_id(operation),
            operation=operation,
            target=_one_line(target, 120),
            objective=_one_line(objective or _default_objective(operation, target), 260),
            created_at=datetime.now(_UTC8).isoformat(timespec="seconds"),
            constraints=str(constraints or "").strip(),
            evidence=[_one_line(item, 300) for item in (evidence or [])[:12]],
            steps=_default_steps(operation),
        )
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
        step.status = "approved"
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
        if step.requires_step_confirmation and step.status != "approved":
            return (
                "Error: step requires explicit confirm-step "
                f"{plan.plan_id} {step.step_id} before execution."
            )
        previous_status = step.status
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
        lines.append(
            f"  - {step.step_id}: {step.title} "
            f"risk={step.risk}{confirm} status={step.status}"
        )
    lines.append(f"approve_plan_command=confirm {plan.plan_id}")
    lines.append(
        "approve_step_command=confirm-step "
        f"{plan.plan_id} <step_id>"
    )
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
            OperationStep("prepare-files", "准备项目文件与配置", "复制入口文件并渲染配置", risk="privileged", requires_step_confirmation=True),
            OperationStep("start-services", "启动后端与前端服务", "启动 screen、校验 nginx 并 reload", risk="privileged", requires_step_confirmation=True),
        ]
    if operation == "destroy_platform":
        return [
            OperationStep("identify-owned-resources", "识别目标平台资源", "证明 screen、端口、目录和 nginx 片段属于目标平台"),
            OperationStep("stop-services", "停止目标平台服务", "停止目标 screen 和专属进程", risk="dangerous", requires_step_confirmation=True),
            OperationStep("cleanup-owned-resources", "清理本平台资源", "只清理由计划证明归属的资源", risk="dangerous", requires_step_confirmation=True),
        ]
    return [
        OperationStep("precheck-runtime", "读取当前运行态", "确认目标平台、screen、端口和日志来源"),
        OperationStep("restart-master", "重启目标平台核心进程", "按已确认启动命令重启相关 screen", risk="privileged", requires_step_confirmation=True),
        OperationStep("verify-health", "验证重启结果", "检查 screen、进程、端口和最新日志"),
    ]


def _find_step(plan: OperationPlan, step_id: str) -> OperationStep:
    for step in plan.steps:
        if step.step_id == step_id:
            return step
    raise ValueError(f"operation step not found: {step_id}")


def _next_step_id(plan: OperationPlan) -> str:
    for step in plan.steps:
        if step.status not in {"completed", "blocked", "failed"}:
            return step.step_id
    return "none"


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
