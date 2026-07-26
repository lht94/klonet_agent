"""独立、无工具权限的高权限任务 Planner。"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from klonet_agent.ops.privileged.checkers import ensure_postconditions
from klonet_agent.ops.privileged.contracts import (
    PrivilegedPlan,
    PrivilegedStep,
    RISK_LEVELS,
)
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy


PLANNER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Planner.
Return one JSON object only. Decompose the requested operational goal into the
smallest auditable shell steps. Every step must include step_id, title, command,
risk, timeout, expected_changes, preconditions, postconditions, and rollback.
Never include passwords. Mark destructive operations honestly. Prefer explicit
postconditions that prove the requested state, not merely a zero exit code.
You have no tools and cannot execute commands.
""".strip()


class PrivilegedPlannerAgent:
    def __init__(self, llm: Any, policy: PrivilegedRiskPolicy | None = None) -> None:
        self.llm = llm
        self.policy = policy or PrivilegedRiskPolicy()

    def plan(
        self,
        goal: str,
        *,
        environment_context: str = "",
    ) -> PrivilegedPlan:
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Goal:\n%s\n\nRead-only environment evidence:\n%s"
                    % (goal, environment_context or "(none)")
                ),
            },
        ]
        data = None
        last_error = ""
        for attempt in range(2):
            response = self.llm.complete(messages=messages, tools=None)
            content = response.choices[0].message.content or ""
            try:
                data = _parse_json_object(content)
                steps = self._build_steps(data)
                planner_risk = str(data.get("risk") or "medium").lower()
                if planner_risk not in RISK_LEVELS:
                    raise ValueError("invalid top-level risk: %s" % planner_risk)
                for step in steps:
                    if RISK_LEVELS.index(planner_risk) > RISK_LEVELS.index(step.risk):
                        step.risk = planner_risk
                break
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Repair the invalid response. Return one valid JSON object "
                                "matching the required schema. Error: %s" % last_error
                            ),
                        }
                    )
        else:
            raise ValueError("Planner did not return a valid privileged plan: %s" % last_error)

        decision = self.policy.evaluate(goal, steps)
        if decision.denied:
            raise PermissionError(decision.reason)

        for step in steps:
            detected, _ = self.policy.classify_command(step.command)
            if RISK_LEVELS.index(detected) > RISK_LEVELS.index(step.risk):
                step.risk = detected
            if decision.requires_step_confirmation:
                step.approval_scope = "step"
                step.status = "awaiting_confirmation"
            checks, level = ensure_postconditions(step.command, step.postconditions)
            step.postconditions = checks
            if level == "partial":
                step.observation = "verification limited to exit code"

        plan = PrivilegedPlan(
            plan_id="priv-" + uuid.uuid4().hex[:10],
            goal=str(data.get("goal") or goal).strip(),
            risk=decision.risk,
            steps=steps,
            verification_level=(
                "partial"
                if any(step.observation == "verification limited to exit code" for step in steps)
                else "full"
            ),
            status="awaiting_confirmation",
        )
        if decision.auto_authorized:
            plan.authorize()
            for step in plan.steps:
                step.status = "approved"
        return plan

    @staticmethod
    def _build_steps(data: dict[str, Any]) -> list[PrivilegedStep]:
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("steps must be a non-empty array")
        steps = []
        seen = set()
        for index, item in enumerate(raw_steps, start=1):
            if not isinstance(item, dict):
                raise ValueError("step must be an object")
            step_id = str(item.get("step_id") or "step-%s" % index).strip()
            command = str(item.get("command") or "").strip()
            if not command:
                raise ValueError("step command is required")
            if step_id in seen:
                raise ValueError("duplicate step_id: %s" % step_id)
            seen.add(step_id)
            steps.append(
                PrivilegedStep(
                    step_id=step_id,
                    title=str(item.get("title") or step_id).strip(),
                    command=command,
                    cwd=str(item.get("cwd") or "").strip(),
                    risk=str(item.get("risk") or data.get("risk") or "medium").lower(),
                    timeout=int(item.get("timeout") or 120),
                    expected_changes=_string_list(item.get("expected_changes")),
                    preconditions=_check_list(item.get("preconditions")),
                    postconditions=_check_list(item.get("postconditions")),
                    rollback=str(item.get("rollback") or "").strip(),
                )
            )
        return steps


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("response does not contain JSON")
        text = match.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response JSON must be an object")
    return data


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _check_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and item.get("checker")]
