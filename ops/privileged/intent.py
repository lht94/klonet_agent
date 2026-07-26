"""Tool-less semantic intent classification for Ops-Privilege turns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from klonet_agent.ops.privileged.planner import _parse_json_object


INTENTS = {
    "conversation",
    "readonly_action",
    "mutating_action",
    "ambiguous",
}
MIN_INTENT_CONFIDENCE = 0.6

INTENT_CLASSIFIER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Intent Classifier.
Classify one user turn as exactly one of:
- conversation: explanation or discussion that does not request real execution
- readonly_action: requests real inspection without changing machine state
- mutating_action: requests any real state change
- ambiguous: execution intent or target is not clear enough to act safely

Return one JSON object only with intent, requires_execution, command, confidence,
and reason. For readonly_action, provide one concrete shell command only when it
is explicit or can be derived safely. Never execute tools. Do not classify risk.
""".strip()


@dataclass(frozen=True)
class PrivilegedIntentDecision:
    intent: str
    requires_execution: bool
    command: str = ""
    confidence: float = 0.0
    reason: str = ""


class PrivilegedIntentClassifier:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def classify(self, text: str) -> PrivilegedIntentDecision:
        messages = [
            {"role": "system", "content": INTENT_CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        for attempt in range(2):
            try:
                response = self.llm.complete(messages=messages, tools=None)
                content = response.choices[0].message.content or ""
                data = _parse_json_object(content)
                return self._decision(data)
            except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content if "content" in locals() else ""})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Repair the invalid response. Return one valid JSON object. "
                                "Error: %s" % exc
                            ),
                        }
                    )
        return PrivilegedIntentDecision(
            intent="ambiguous",
            requires_execution=False,
            confidence=0.0,
            reason="intent classifier returned invalid structured output",
        )

    @staticmethod
    def _decision(data: dict) -> PrivilegedIntentDecision:
        intent = str(data.get("intent") or "").strip().lower()
        if intent not in INTENTS:
            raise ValueError("invalid privileged intent: %s" % intent)
        requires_execution = intent in {"readonly_action", "mutating_action"}
        confidence = max(0.0, min(float(data.get("confidence") or 0.0), 1.0))
        if confidence < MIN_INTENT_CONFIDENCE:
            return PrivilegedIntentDecision(
                intent="ambiguous",
                requires_execution=False,
                confidence=confidence,
                reason="low confidence intent classification",
            )
        return PrivilegedIntentDecision(
            intent=intent,
            requires_execution=requires_execution,
            command=str(data.get("command") or "").strip(),
            confidence=confidence,
            reason=str(data.get("reason") or "").strip(),
        )
