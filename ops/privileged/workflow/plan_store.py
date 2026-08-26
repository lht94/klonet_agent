"""Session-isolated persistence for Ops-Privilege change plans."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from klonet_agent.ops.privileged.workflow.contracts import (
    ChangePlan, FailureRecord, _utc_now,
)


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned or "default"


class ChangePlanStore:
    def __init__(self, memory_root: Path, *, user_id: str, project_id: str) -> None:
        self.plan_dir = (
            Path(memory_root)
            / "sessions"
            / _safe_component(user_id)
            / _safe_component(project_id)
            / "privileged_ops_plans"
        )
        self.failure_dir = self.plan_dir.parent / "privileged_failures"

    def save(self, plan: ChangePlan) -> None:
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

    def load(self, plan_id: str) -> ChangePlan:
        self._validate_id(plan_id)
        path = self.plan_dir / (plan_id + ".json")
        if not path.is_file():
            raise KeyError("unknown privileged plan: %s" % plan_id)
        plan = ChangePlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if self._repair_inconsistent_completion(plan):
            self.save(plan)
        return plan

    def list(self) -> list[ChangePlan]:
        if not self.plan_dir.exists():
            return []
        plans = []
        for path in self.plan_dir.glob("priv-ops-*.json"):
            try:
                plan = ChangePlan.from_dict(json.loads(
                    path.read_text(encoding="utf-8")
                ))
                if self._repair_inconsistent_completion(plan):
                    self.save(plan)
                plans.append(plan)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # Historical drafts are not an authority over the current
                # workflow. A malformed or now-invalid draft remains available
                # for explicit forensic inspection, but cannot crash plan
                # routing or masquerade as the latest usable plan.
                continue
        return sorted(plans, key=lambda item: item.updated_at, reverse=True)

    def save_failure(self, failure: FailureRecord) -> None:
        if not re.fullmatch(r"failure-[A-Za-z0-9_-]{1,64}", failure.failure_id):
            raise ValueError("invalid privileged failure id")
        self.failure_dir.mkdir(parents=True, exist_ok=True)
        destination = self.failure_dir / (failure.failure_id + ".json")
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(failure.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(destination))

    def load_failure(self, failure_id: str) -> FailureRecord:
        if not re.fullmatch(r"failure-[A-Za-z0-9_-]{1,64}", str(failure_id or "")):
            raise ValueError("invalid privileged failure id")
        path = self.failure_dir / (failure_id + ".json")
        if not path.is_file():
            raise KeyError("unknown privileged failure: %s" % failure_id)
        failure = FailureRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if failure is None:
            raise ValueError("invalid privileged failure")
        return failure

    def list_failures(self) -> list[FailureRecord]:
        if not self.failure_dir.exists():
            return []
        failures = []
        for path in self.failure_dir.glob("failure-*.json"):
            try:
                failure = FailureRecord.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if failure is not None:
                failures.append((failure, path.stat().st_mtime_ns))
        ordered = sorted(
            failures,
            key=lambda item: (item[0].created_at, item[1]),
            reverse=True,
        )
        return [item[0] for item in ordered]

    def recover(self, plan_id: str) -> ChangePlan:
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
        if not re.fullmatch(r"priv-ops-[A-Za-z0-9_-]{1,64}", str(plan_id or "")):
            raise ValueError("invalid privileged plan id")

    @staticmethod
    def _repair_inconsistent_completion(plan: ChangePlan) -> bool:
        """Demote historical outer-completed/inner-incomplete plan state."""

        if plan.status != "completed" or plan.execution_is_complete:
            return False
        plan.status = "paused"
        return True
