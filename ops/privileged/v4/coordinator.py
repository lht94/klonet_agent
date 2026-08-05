"""Top-level routing for the staged Ops-Privilege V4 workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from klonet_agent.ops.privileged.goal_guard import GoalSafetyGuard


@dataclass
class V4WorkflowResult:
    handled: bool
    kind: str
    message: str = ""
    plan: Any | None = None
    evidence: Any | None = None
    verification: Any | None = None


class PrivilegedOpsV4Coordinator:
    def __init__(
        self,
        *,
        classifier: Any,
        discovery: Any,
        synthesis: Any,
        response: Any,
        mutation_workflow: Any,
        goal_guard: GoalSafetyGuard | None = None,
    ) -> None:
        self.classifier = classifier
        self.discovery = discovery
        self.synthesis = synthesis
        self.response = response
        self.mutation_workflow = mutation_workflow
        self.goal_guard = goal_guard or GoalSafetyGuard()

    def handle(
        self,
        text: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> V4WorkflowResult:
        normalized = str(text or "").lstrip("\ufeff\u200b").strip()
        safety = self.goal_guard.check(normalized)
        if safety.denied:
            return V4WorkflowResult(
                True,
                "denied",
                "Denied: %s" % safety.reason,
            )
        handle_control = getattr(self.mutation_workflow, "handle_control", None)
        if handle_control is not None:
            control = handle_control(normalized)
            if control is not None:
                return control
        decision = self.classifier.classify(
            normalized,
            conversation_context=conversation_context,
        )
        if decision.intent == "classifier_error":
            return V4WorkflowResult(
                True,
                "blocked",
                "Intent classification failed safely; nothing was executed: %s"
                % str(getattr(decision, "reason", "unknown error")),
            )
        if bool(getattr(decision, "should_clarify", False)):
            return V4WorkflowResult(
                True,
                "clarification",
                str(getattr(decision, "clarification_question", "") or "Please clarify the goal."),
            )
        if decision.intent == "conversation":
            return V4WorkflowResult(False, "conversation")
        bundle = self.discovery.collect(
            normalized,
            command=str(getattr(decision, "command", "") or ""),
            conversation_context=conversation_context,
        )
        conclusion = self.synthesis.synthesize(normalized, bundle)
        if decision.intent == "readonly_action":
            return V4WorkflowResult(
                True,
                "completed",
                self.response.render_readonly(normalized, conclusion),
                evidence=bundle,
            )
        return self.mutation_workflow.submit(
            normalized,
            evidence_bundle=bundle,
            evidence_conclusion=conclusion,
            conversation_context=conversation_context,
        )

    def handle_with_context(
        self,
        text: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> V4WorkflowResult:
        return self.handle(
            text,
            environment_context=environment_context,
            conversation_context=conversation_context,
        )
