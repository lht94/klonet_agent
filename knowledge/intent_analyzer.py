"""Front-loaded structured intent analysis for Mentor turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.models import QueryRoute
from klonet_agent.knowledge.rag import route_query


INTENT_ANALYSIS_PROMPT = """
你是 Klonet Mentor 的前置意图解析器。你的任务不是回答用户问题，
而是把用户原始输入解析为一个稳定 JSON 对象，供后续路由、检索和回答策略使用。

只输出 JSON，不要输出 Markdown，不要解释。

字段：
- scope: klonet | general | mixed
- task_type: concept | deployment_preparation | deployment_guidance | credential_boundary | operation_guide | troubleshooting | code_lookup | development | project_progress | general
- operation: unknown | environment_setup | dependency_install | platform_start | platform_stop | platform_restart | acceptance_check
- target: 用户要处理的对象，例如 klonet_platform、web_terminal、redis、topology
- symptom: 故障现象，例如 address_already_in_use、import_error、port_conflict
- excluded_intents: 用户明确否定的方向数组
- prerequisites: 用户已说明的前提数组
- requires_retrieval: 是否需要 Klonet 知识库证据
- clarification_required: 是否应该先追问用户
- clarification_question: 需要追问时的一句话问题
- is_correction: 用户是否在纠正上一轮回答
- confidence: 0 到 1

判断原则：
1. “部署 Klonet”如果无法判断是安装基础环境还是启动平台服务，clarification_required=true。
2. “安装环境、依赖、base_requ_setup、docker_service”属于 deployment_preparation / environment_setup。
3. “启动 Klonet、启动平台、web-terminal、gunicorn、celery、nginx”属于 deployment_guidance 或 troubleshooting / platform_start。
4. 用户明确说不需要 Klonet 时，scope=general，requires_retrieval=false。
5. 不要补造 Klonet 架构；不确定时降低 confidence 或要求澄清。
"""


@dataclass(frozen=True)
class IntentAnalysis:
    intent: QueryIntent
    token_usage: int = 0
    used_model: bool = False
    raw_content: str = ""


class IntentAnalyzer:
    """Use an LLM to parse the user's request before retrieval/tool routing."""

    def __init__(self, llm):
        self.llm = llm

    def analyze(
        self,
        user_input: str,
        *,
        recent_history: list[dict] | None = None,
    ) -> IntentAnalysis:
        user_message = _build_analysis_user_message(user_input, recent_history or [])
        response = self.llm.complete(
            messages=[
                {"role": "system", "content": INTENT_ANALYSIS_PROMPT},
                {"role": "user", "content": user_message},
            ],
            tools=None,
            stream=False,
        )
        content = response.choices[0].message.content or ""
        token_usage = getattr(getattr(response, "usage", None), "total_tokens", 0)
        return IntentAnalysis(
            intent=QueryIntent.from_mapping(_parse_json_object(content)),
            token_usage=token_usage,
            used_model=True,
            raw_content=content,
        )


def route_from_intent(user_input: str, intent: QueryIntent) -> QueryRoute:
    """Convert front-loaded intent into the route object expected downstream."""

    if intent.confidence <= 0:
        return route_query(user_input)

    return QueryRoute(
        scope=intent.scope,
        confidence=intent.confidence,
        task_type=intent.task_type,
        domains=_domains_from_intent(intent),
        reasons=("model_intent",),
        hard_disable_rag=(intent.scope == "general" and not intent.requires_retrieval),
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _domains_from_intent(intent: QueryIntent) -> tuple[str, ...]:
    text = f"{intent.target} {intent.operation} {intent.symptom}".lower()
    domains: list[str] = []
    if any(term in text for term in ("terminal", "vm", "kvm", "web_terminal")):
        domains.append("vm")
    if any(term in text for term in ("topology", "topo")):
        domains.append("topology")
    if any(term in text for term in ("redis", "nginx", "gunicorn", "celery", "platform")):
        domains.append("runtime")
    return tuple(domains)


def _build_analysis_user_message(user_input: str, recent_history: list[dict]) -> str:
    context_lines = []
    for message in recent_history:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        context_lines.append(f"{role}: {content[:800]}")

    if not context_lines:
        return user_input

    return (
        "最近对话上下文：\n"
        + "\n".join(context_lines[-6:])
        + "\n\n当前用户输入：\n"
        + user_input
        + "\n\n如果当前输入包含“第一种/第二种/场景一/场景二/上面那个/你刚说的/继续”等指代，"
        "必须结合最近对话解析，不得直接追问这些指代是什么意思。"
    )
