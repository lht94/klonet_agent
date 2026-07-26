"""Single control-plane entry for every Ops-Privilege user turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from klonet_agent.ops.privileged.goal_guard import GoalSafetyGuard


@dataclass
class SupervisorResult:
    handled: bool
    kind: str
    message: str = ""
    workflow_result: Any | None = None


class PrivilegedOpsSupervisor:
    def __init__(
        self,
        *,
        workflow: Any,
        classifier: Any,
        goal_guard: GoalSafetyGuard | None = None,
    ) -> None:
        self.workflow = workflow
        self.classifier = classifier
        self.goal_guard = goal_guard or GoalSafetyGuard()

    def handle(self, text: str, *, environment_context: str = "") -> SupervisorResult:
        normalized = " ".join((text or "").split())
        if self.workflow.is_control_command(normalized):
            result = self.workflow.handle_command(normalized)
            return self._handled(result)

        safety = self.goal_guard.check(normalized)
        if safety.denied:
            return SupervisorResult(True, "denied", "Denied: %s" % safety.reason)

        decision = self.classifier.classify(normalized)
        if decision.intent == "conversation":
            return SupervisorResult(False, "conversation")
        if decision.intent == "ambiguous":
            return SupervisorResult(
                True,
                "clarification",
                "Please clarify the exact operation or state you want inspected; "
                "nothing was executed.",
            )
        if decision.intent == "readonly_action":
            if not decision.command:
                return SupervisorResult(
                    True,
                    "clarification",
                    "Please clarify the exact read-only command or target; nothing was executed.",
                )
            return self._handled(
                self.workflow.submit_readonly(normalized, decision.command)
            )
        return self._handled(
            self.workflow.submit(
                normalized,
                environment_context=environment_context,
            )
        )

    @staticmethod
    def _handled(result: Any) -> SupervisorResult:
        return SupervisorResult(
            handled=True,
            kind=result.kind,
            message=result.message,
            workflow_result=result,
        )
