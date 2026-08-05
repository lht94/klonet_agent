"""Top-level routing for the staged Ops-Privilege V4 workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    ) -> None:
        self.classifier = classifier
        self.discovery = discovery
        self.synthesis = synthesis
        self.response = response
        self.mutation_workflow = mutation_workflow

    def handle(
        self,
        text: str,
        *,
        conversation_context: str = "",
    ) -> V4WorkflowResult:
        decision = self.classifier.classify(
            text,
            conversation_context=conversation_context,
        )
        if decision.intent == "conversation":
            return V4WorkflowResult(False, "conversation")
        bundle = self.discovery.collect(
            text,
            command=str(getattr(decision, "command", "") or ""),
            conversation_context=conversation_context,
        )
        conclusion = self.synthesis.synthesize(text, bundle)
        if decision.intent == "readonly_action":
            return V4WorkflowResult(
                True,
                "completed",
                self.response.render_readonly(text, conclusion),
                evidence=bundle,
            )
        return self.mutation_workflow.submit(
            text,
            evidence_bundle=bundle,
            evidence_conclusion=conclusion,
            conversation_context=conversation_context,
        )
