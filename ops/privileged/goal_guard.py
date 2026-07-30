"""Raw-goal safety boundary that runs before semantic classification."""

from __future__ import annotations

from dataclasses import dataclass

from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy


@dataclass(frozen=True)
class GoalSafetyDecision:
    denied: bool
    reason: str = ""


class GoalSafetyGuard:
    def __init__(self, policy: PrivilegedRiskPolicy | None = None) -> None:
        self.policy = policy or PrivilegedRiskPolicy()

    def check(self, goal: str) -> GoalSafetyDecision:
        normalized = (goal or "").translate(
            str.maketrans({"。": " ", "；": " ", "，": " ", "！": " ", "？": " "})
        )
        risk, reason = self.policy.classify_command(normalized)
        if risk == "destructive":
            return GoalSafetyDecision(True, "hard-denied raw goal: " + reason)
        return GoalSafetyDecision(False)
