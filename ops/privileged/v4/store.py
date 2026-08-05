"""Session-isolated persistence for Ops-Privilege V4 change plans."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, _utc_now


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned or "default"


class V4PlanStore:
    def __init__(self, memory_root: Path, *, user_id: str, project_id: str) -> None:
        self.plan_dir = (
            Path(memory_root)
            / "sessions"
            / _safe_component(user_id)
            / _safe_component(project_id)
            / "privileged_ops_plans_v4"
        )

    def save(self, plan: ChangePlanV4) -> None:
        self._validate_id(plan.plan_id)
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        plan.updated_at = _utc_now()
        destination = self.plan_dir / (plan.plan_id + ".json")
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(destination))

    def load(self, plan_id: str) -> ChangePlanV4:
        self._validate_id(plan_id)
        path = self.plan_dir / (plan_id + ".json")
        if not path.is_file():
            raise KeyError("unknown V4 privileged plan: %s" % plan_id)
        return ChangePlanV4.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[ChangePlanV4]:
        if not self.plan_dir.exists():
            return []
        plans = [
            ChangePlanV4.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in self.plan_dir.glob("priv-v4-*.json")
        ]
        return sorted(plans, key=lambda item: item.updated_at, reverse=True)

    def recover(self, plan_id: str) -> ChangePlanV4:
        plan = self.load(plan_id)
        interrupted = False
        for step in plan.steps:
            if step.status in {"running", "verifying"}:
                step.status = "paused" if step.implementation_plan is not None else "execution_unknown"
                step.observation = (
                    "process restarted while the change was active; verify current "
                    "state and never auto-reexecute"
                )
                interrupted = True
            if step.implementation_plan is not None:
                for micro_step in step.implementation_plan.steps:
                    if micro_step.status not in {"running", "verifying"}:
                        continue
                    micro_step.status = "execution_unknown"
                    micro_step.observation = (
                        "process restarted while the change was active; verify "
                        "current state and never auto-reexecute"
                    )
                    step.implementation_plan.status = "paused"
                    interrupted = True
        if interrupted or plan.status in {"executing", "verifying"}:
            plan.status = "paused"
        self.save(plan)
        return plan

    @staticmethod
    def _validate_id(plan_id: str) -> None:
        if not re.fullmatch(r"priv-v4-[A-Za-z0-9_-]{1,64}", str(plan_id or "")):
            raise ValueError("invalid V4 privileged plan id")
