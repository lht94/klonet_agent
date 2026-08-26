"""Tool-less semantic intent classification for Ops-Privilege turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from klonet_agent.ops.privileged.action_contracts import _parse_json_object
from klonet_agent.ops.privileged.context import klonet_domain_context
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
GOAL_RELATIONS = {
    "new", "continue_previous", "refine_previous", "supersede_previous",
}
GOAL_KINDS = {
    "conversation", "execution", "health_check", "causal_diagnosis",
}
OPERATIONS = {"none", "restart", "repair", "start", "stop", "inspect"}
SCOPES = {"none", "platform", "component"}

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
- Use resume_plan only for explicit references to an earlier unfinished plan,
  such as "继续上次部署", "恢复刚才的任务", "接着之前的计划", or
  "刚才的计划执行完了吗".
  Set plan_reference to an explicit priv-* id when supplied or when exactly
  one matching id is visible in the unfinished-plan context; otherwise use
  "latest". Never turn resume_plan into a new deploy, restart, or repair goal.
  Use operation=inspect when the user only asks to inspect that plan.
- A conversational recap such as "刚才我们进行到哪一步了", "刚才聊了什么",
  or "前面说到哪里了" is conversation, not resume_plan, unless the current
  wording explicitly refers to a change plan, its approval, or its execution.
- A request to actually repeat a previous inspection, such as "重新检查一次"
  or "再试一次刚才的检查", keeps the previous action intent and uses
  goal_relation=continue_previous. Never label a recap as an action, and never
  rely on Coordinator to promote conversation into an action.
- clarification_question must be a concise Chinese question and is required only
  for goal_clarity=missing.
- goal_relation describes how the current request relates to the persisted
  operational goal shown in recent context: new, continue_previous,
  refine_previous, or supersede_previous. Referential follow-ups such as
  "这个报错是为什么",
  "那具体原因呢", and "继续查到根因" refine the previous goal rather than
  starting a new one.
- continue_previous means replaying the same requested result without adding
  a new fact, predicate, comparison, scope, or expected output.  If the user
  keeps the previous target but asks a new question about it, classify the
  turn as refine_previous even when goal_kind and operation remain unchanged.
  For example, after listing runtime platforms, asking which of those
  platforms are healthy is a refinement: the target is inherited but the
  requested result has changed.
- A follow-up that challenges a previous operational conclusion with the
  user's own runtime observation is operational inspection, not a concept
  question. If it asks why the observations differ, use readonly_action,
  refine_previous, and causal_diagnosis so the discrepancy is checked against
  current evidence.
- When an awaiting-confirmation plan exists, a user who identifies incorrect
  resources, missing acceptance checks, contradictory effects, or components
  that must be added/removed is requesting a replacement plan: use
  mutating_action + refine_previous.  Critiquing a plan does not approve it,
  and the ordinary Answerer must never draft the replacement.
- For a component-scoped refinement, components contains only the roles the
  replacement plan is authorized to change.  Do not include a component that
  the user explicitly excludes merely because it appears in the discussion.
- Use supersede_previous when the user rejects or corrects the previous goal,
  including when the same turn supplies a replacement goal. A self-contained
  current request does not inherit the previous goal merely because historical
  context exists.
- goal_kind is one of conversation, execution, health_check, or
  causal_diagnosis. It describes the user's request, never wrapper text added
  by another workflow stage.
- operation is one of none, restart, repair, start, stop, or inspect.
- scope is one of none, platform, or component. components contains explicitly
  named application components; leave it empty for a platform-wide operation.
- "重启平台" means restart the complete managed application-component set.
  It is not a repair request and healthy components must not be preserved.
- Moving existing Klonet application roles under Screen management (for example
  "全部角色收编为 Screen 管理") is mutating_action + operation=restart: the
  requested invariant requires replacing any non-Screen-owned runtime with a
  Screen-owned runtime.  This does not approve execution; the resulting exact
  plan still requires user confirmation.  In contrast, asking which Screen
  sessions exist is readonly_action + operation=inspect.
- Questions about a previous plan use resume_plan + operation=inspect + a
  non-empty plan_reference. Questions about server, platform, process, port, or
  component runtime state use readonly_action + health_check + operation=inspect
  and must leave plan_reference empty.
- A question such as "我之前不是修订过这个计划了吗" asks the plan domain to
  reconcile conversation history with persisted plan state. Use resume_plan,
  operation=inspect, goal_relation=refine_previous. It is not a request to dump
  the old plan and does not itself authorize a replacement.
- The persisted operational goal is historical context only. Inherit it only
  when the current request explicitly continues, refines, or supersedes it.

Examples:
- "帮我部署一个平台" -> mutating_action, discoverable
- "检查 Klonet 为什么没启动" -> readonly_action, discoverable
- "查看 Python 版本" -> readonly_action, clear
- "什么是 tc qdisc" -> conversation, clear
- "继续上次的部署计划" -> resume_plan, clear, operation=none,
  plan_reference="latest"
- "刚才的重启计划执行完了吗" -> resume_plan, clear, operation=inspect,
  plan_reference="latest"
- "刚刚我们进行到哪一步了" -> conversation, clear,
  goal_relation=continue_previous, operation=none, plan_reference=""
- "重新检查一次刚才的平台状态" -> readonly_action, discoverable,
  goal_relation=continue_previous, goal_kind=health_check, operation=inspect,
  plan_reference=""
- "这些平台里哪些现在是健康的" after a runtime inventory ->
  readonly_action, discoverable, goal_relation=refine_previous,
  goal_kind=health_check, operation=inspect, plan_reference=""
- "看看服务器上有哪些平台在运行" -> readonly_action, clear,
  goal_relation=new, goal_kind=health_check, operation=inspect,
  plan_reference=""
- "不是重启，只检查运行平台" with a persisted restart goal ->
  readonly_action, clear, goal_relation=supersede_previous,
  goal_kind=health_check, operation=inspect, plan_reference=""
- "不要重启了" with a persisted restart goal -> conversation, clear,
  goal_relation=supersede_previous, plan_reference=""
- "这个计划的 worker 环境错了，并且必须检查 Screen，请按这些修正" with
  an awaiting plan -> mutating_action, discoverable,
  goal_relation=refine_previous, goal_kind=execution, operation=restart
- "把所有平台的所有应用角色收编为 Screen 管理" -> mutating_action,
  discoverable, goal_relation=new, goal_kind=execution, operation=restart,
  scope=platform, components=[]
- "我之前不是修订过这个计划了吗" -> resume_plan, clear,
  goal_relation=refine_previous, operation=inspect, plan_reference="latest"
- "把它删掉" with no referent in recent context -> ambiguous, missing
- "帮我处理一下" -> ambiguous, missing

Return one JSON object only with intent, goal_clarity, goal_relation, goal_kind,
operation, scope, components, requires_execution, command, confidence, reason,
and clarification_question. Never execute tools.
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
    goal_relation: str = "new"
    goal_kind: str = "conversation"
    operation: str = "none"
    scope: str = "none"
    components: tuple[str, ...] = ()

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
            {
                "role": "system",
                "content": INTENT_CLASSIFIER_SYSTEM_PROMPT
                + "\n\n" + klonet_domain_context("intent"),
            },
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
        goal_relation = str(data.get("goal_relation") or "new").strip().lower()
        if goal_relation not in GOAL_RELATIONS:
            raise ValueError("invalid goal_relation: %s" % goal_relation)
        default_kind = (
            "execution" if intent == "mutating_action"
            else "health_check" if intent == "readonly_action"
            else "conversation"
        )
        goal_kind = str(data.get("goal_kind") or default_kind).strip().lower()
        if goal_kind not in GOAL_KINDS:
            raise ValueError("invalid goal_kind: %s" % goal_kind)
        operation = str(data.get("operation") or "none").strip().lower()
        if operation not in OPERATIONS:
            raise ValueError("invalid operation: %s" % operation)
        if intent == "conversation" and operation != "none":
            raise ValueError("conversation cannot request an operation")
        scope = str(data.get("scope") or "none").strip().lower()
        if scope not in SCOPES:
            raise ValueError("invalid scope: %s" % scope)
        raw_components = data.get("components") or []
        if not isinstance(raw_components, list):
            raise ValueError("components must be an array")
        components = tuple(
            str(item).strip().lower().replace("-", "_").replace(" ", "_")
            for item in raw_components
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,63}", str(item).strip())
        )
        plan_reference = str(data.get("plan_reference") or "").strip()[:500]
        if intent == "resume_plan" and not plan_reference:
            raise ValueError("resume_plan requires plan_reference")
        if intent != "resume_plan" and plan_reference:
            # ``plan_reference`` is routing authority only for resume_plan.
            # Some providers redundantly echo "latest" while correctly
            # classifying a refinement as mutating_action/readonly_action.
            # Discarding that powerless field preserves the valid semantic
            # classification without allowing it to select or resume a plan;
            # persisted Coordinator state remains the sole plan authority.
            plan_reference = ""
        if intent == "resume_plan" and operation == "inspect":
            goal_kind = "conversation"
        if goal_kind == "health_check" and intent != "readonly_action":
            raise ValueError("health_check requires readonly_action")
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
            plan_reference=plan_reference,
            goal_relation=goal_relation,
            goal_kind=goal_kind,
            operation=operation,
            scope=scope,
            components=components,
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
