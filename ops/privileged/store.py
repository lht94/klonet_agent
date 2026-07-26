"""高权限计划的会话隔离持久化与恢复。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from klonet_agent.ops.privileged.contracts import PrivilegedPlan, utc_now


class PrivilegedPlanStore:
    def __init__(
        self,
        memory_root: Path,
        *,
        user_id: str,
        project_id: str,
    ) -> None:
        self.plan_dir = (
            Path(memory_root)
            / "sessions"
            / _safe_component(user_id)
            / _safe_component(project_id)
            / "privileged_ops_plans"
        )

    def save(self, plan: PrivilegedPlan) -> None:
        _validate_plan_id(plan.plan_id)
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        plan.updated_at = utc_now()
        destination = self.plan_dir / ("%s.json" % plan.plan_id)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(destination))

    def load(self, plan_id: str) -> PrivilegedPlan:
        _validate_plan_id(plan_id)
        path = self.plan_dir / ("%s.json" % plan_id)
        if not path.is_file():
            raise KeyError("unknown privileged plan: %s" % plan_id)
        return PrivilegedPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[PrivilegedPlan]:
        if not self.plan_dir.exists():
            return []
        plans = [
            PrivilegedPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in self.plan_dir.glob("*.json")
        ]
        return sorted(plans, key=lambda item: item.updated_at, reverse=True)

    def recover(self, plan_id: str) -> PrivilegedPlan:
        plan = self.load(plan_id)
        interrupted = False
        for step in plan.steps:
            if step.status in {"running", "verifying"}:
                step.status = "execution_unknown"
                step.observation = (
                    "process restarted while step was active; verify current state and "
                    "never auto-reexecute this command"
                )
                interrupted = True
        if interrupted or plan.status in {"executing", "verifying"}:
            plan.status = "blocked"
        self.save(plan)
        return plan


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned or "default"


def _validate_plan_id(plan_id: str) -> None:
    if not re.fullmatch(r"priv-[A-Za-z0-9_-]{1,64}", str(plan_id or "")):
        raise ValueError("invalid privileged plan id")
