"""Tool-less semantic intent classification for Ops-Privilege turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from klonet_agent.ops.privileged.planner import _parse_json_object
from klonet_agent.tools.environment import redact_sensitive_text


INTENTS = {
    "conversation",
    "readonly_action",
    "mutating_action",
    "resume_plan",
    "ambiguous",
    "classifier_error",
}
GOAL_CLARITIES = {"clear", "discoverable", "missing"}
ACTION_INTENTS = {"readonly_action", "mutating_action"}

INTENT_CLASSIFIER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Intent Classifier.
Classify the user's operational goal, without executing tools.

intent must be exactly one of:
- conversation: explanation or discussion that does not request real execution
- readonly_action: requests real inspection without changing machine state
- mutating_action: requests any real state change
- resume_plan: asks to continue, recover, inspect, retry, or otherwise return
  to a previously persisted privileged plan; this intent does not itself
  authorize execution
- ambiguous: the desired outcome or target is genuinely missing

Separately classify goal_clarity:
- clear: enough information exists to answer or create an auditable plan
- discoverable: missing details can be recovered from recent conversation or
  safe read-only inspection; do not ask the user for these details
- missing: a material choice or target cannot be inferred or safely discovered

Important:
- A request may be actionable even when implementation details are unknown.
  Deployment, installation, restart, diagnosis and inspection requests normally
  enter planning; the planner can inspect the environment before changing it.
- Use ambiguous only when neither recent context nor read-only discovery can
  recover the essential goal or target.
- Low confidence is telemetry, not by itself a reason to ask the user.
- For goal_clarity=discoverable, return readonly_action or mutating_action, not
  ambiguous.
- For readonly_action, command is optional. Provide it only when one concrete,
  deterministically read-only command follows safely from the request.
- Use resume_plan for natural-language references to an earlier unfinished
  plan, such as "继续上次部署", "恢复刚才的任务", or "接着之前的计划".
  Set plan_reference to an explicit priv-* id when supplied or when exactly
  one matching id is visible in the unfinished-plan context; otherwise use
  "latest". Never turn resume_plan into a new deploy, restart, or repair goal.
- clarification_question must be a concise Chinese question and is required only
  for goal_clarity=missing.

Examples:
- "帮我部署一个平台" -> mutating_action, discoverable
- "检查 Klonet 为什么没启动" -> readonly_action, discoverable
- "查看 Python 版本" -> readonly_action, clear
- "什么是 tc qdisc" -> conversation, clear
- "继续上次的部署计划" -> resume_plan, clear, plan_reference="latest"
- "把它删掉" with no referent in recent context -> ambiguous, missing
- "帮我处理一下" -> ambiguous, missing

Return one JSON object only with intent, goal_clarity, requires_execution,
command, confidence, reason, and clarification_question. Never execute tools.
Also return plan_reference; use an empty string for non-resume intents. Do not
classify command risk.
""".strip()


@dataclass(frozen=True)
class PrivilegedIntentDecision:
    intent: str
    requires_execution: bool
    command: str = ""
    confidence: float = 0.0
    reason: str = ""
    goal_clarity: str = "clear"
    clarification_question: str = ""
    classifier_status: str = "ok"
    plan_reference: str = ""

    @property
    def should_clarify(self) -> bool:
        return self.goal_clarity == "missing" or self.intent == "ambiguous"


class PrivilegedIntentClassifier:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def classify(
        self,
        text: str,
        *,
        conversation_context: str = "",
    ) -> PrivilegedIntentDecision:
        messages = [
            {"role": "system", "content": INTENT_CLASSIFIER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Recent conversation:\n%s\n\nCurrent request:\n%s"
                    % (conversation_context or "(none)", text)
                ),
            },
        ]
        failure_status = "invalid_output"
        failure_reason = "意图分类器连续两次返回了无效的结构化结果"
        for attempt in range(2):
            content = ""
            try:
                response = self._complete(messages)
                content = response.choices[0].message.content or ""
                data = _parse_json_object(content)
                return self._decision(data)
            except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                failure_reason = (
                    "意图分类器返回无效 JSON 或字段：%s"
                    % _safe_classifier_error(exc)
                )
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Repair the invalid response. Return one valid JSON object. "
                                "Error: %s" % exc
                            ),
                        }
                    )
            except Exception as exc:
                # Provider/network failures are internal failures. A retry may recover,
                # but they must never be presented as if the user had described the
                # goal poorly.
                failure_status = "provider_error"
                failure_reason = "模型服务请求失败：%s" % _safe_classifier_error(
                    exc
                )
                continue
        return PrivilegedIntentDecision(
            intent="classifier_error",
            requires_execution=False,
            confidence=0.0,
            reason=failure_reason,
            goal_clarity="clear",
            classifier_status=failure_status,
        )

    def _complete(self, messages: list[dict[str, str]]) -> Any:
        try:
            return self.llm.complete(
                messages=messages,
                tools=None,
                temperature=0,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
        except TypeError:
            # Keep compatibility with simple test doubles and older local clients.
            return self.llm.complete(messages=messages, tools=None)

    @staticmethod
    def _decision(data: dict) -> PrivilegedIntentDecision:
        intent = str(data.get("intent") or "").strip().lower()
        if intent not in INTENTS:
            raise ValueError("invalid privileged intent: %s" % intent)
        if intent == "classifier_error":
            raise ValueError("classifier_error is reserved for internal failures")
        requires_execution = intent in ACTION_INTENTS
        confidence = max(0.0, min(float(data.get("confidence") or 0.0), 1.0))
        default_clarity = "missing" if intent == "ambiguous" else "clear"
        goal_clarity = str(data.get("goal_clarity") or default_clarity).strip().lower()
        if goal_clarity not in GOAL_CLARITIES:
            raise ValueError("invalid goal_clarity: %s" % goal_clarity)
        if intent == "ambiguous":
            goal_clarity = "missing"
        return PrivilegedIntentDecision(
            intent=intent,
            requires_execution=requires_execution,
            command=str(data.get("command") or "").strip(),
            confidence=confidence,
            reason=str(data.get("reason") or "").strip(),
            goal_clarity=goal_clarity,
            clarification_question=str(
                data.get("clarification_question") or ""
            ).strip(),
            plan_reference=str(data.get("plan_reference") or "").strip()[:500],
        )


def _safe_classifier_error(exc: Exception) -> str:
    """Return a useful provider/schema failure without leaking credentials."""

    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
        elif error:
            message = str(error).strip()
        if not message:
            message = str(body.get("message") or "").strip()
    if not message:
        message = str(exc or "").strip()
    message = redact_sensitive_text(message)
    message = re.sub(
        r"\b(?:sk|ds|ak)-[A-Za-z0-9_-]{8,}\b",
        "[REDACTED]",
        message,
        flags=re.IGNORECASE,
    )
    message = " ".join(message.split())[:300]
    error_name = type(exc).__name__
    if status:
        return "HTTP %s%s" % (
            status,
            "：%s" % message if message else "",
        )
    if "timeout" in error_name.lower() or "timed out" in message.lower():
        return "请求超时%s" % ("：%s" % message if message else "")
    if "connect" in error_name.lower():
        return "连接失败%s" % ("：%s" % message if message else "")
    return "%s%s" % (
        error_name,
        "：%s" % message if message else "",
    )
