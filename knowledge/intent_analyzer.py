"""Front-loaded structured intent analysis for Mentor turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from klonet_agent.knowledge.intent_cases import (
    IntentCase,
    IntentCaseRetriever,
    build_default_intent_case_retriever,
    build_intent_case_query,
)
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.models import QueryRoute
from klonet_agent.knowledge.rag import route_query
from klonet_agent.knowledge.semantic_understanding import (
    IntentDecision,
    SemanticDecisionPlanner,
    SemanticFrame,
    SemanticState,
)


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
- requires_environment_diagnosis: 是否需要读取本机只读环境状态来诊断故障
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
6. Klonet 启动失败、端口占用、screen 报错、nginx/Docker/Redis/RabbitMQ/MySQL/OVS/KVM/libvirt/Worker/拓扑进度卡住等运维故障，task_type=troubleshooting 且 requires_environment_diagnosis=true。
"""


INTENT_ANALYSIS_PROMPT += """

语义理解字段（优先输出这些字段，再由代码决定路由和是否追问）：
- user_role: learner | operator | developer | admin | unknown
- perspective: asking_about_own_pc | operating_target_machine | using_platform | debugging_runtime | unknown
- machine_role: operator_local_pc | target_server | target_vm | host_machine | klonet_master | klonet_worker | unspecified
- deployment_phase: local_tool_preparation | environment_setup | platform_startup | platform_shutdown | platform_restart | topology_deploy | platform_usage | troubleshooting | unknown
- action_goal: prepare_tools | install_dependencies | start_services | stop_services | restart_services | deploy_topology | inspect_error | use_feature | explain_concept | unknown
- target_component: 用户正在操作或询问的对象
- excluded_meanings: 用户明确否定的方向
- context_refs: 第一种、上面那个、继续等上下文指代
- evidence_spans: 支撑字段判断的原文片段
- ambiguity: {"level":"low|medium|high","candidates":[],"defaultable":true|false}

判断原则：
1. “电脑”默认先判断是不是操作者自己的电脑；只有出现服务器、虚拟机、部署机等证据时才当作目标机器。
2. “部署平台/怎么部署 Klonet”没有首次安装证据时，可默认理解为启动已安装平台，歧义标为 medium 且 defaultable=true。
3. “部署环境/安装依赖/首次部署/base_requ_setup/Docker Redis MySQL”属于 environment_setup。
4. “部署拓扑/创建拓扑/进度条/TopoDeployAPI”属于 topology_deploy，不要混成平台启动。
5. 如果上文给出 A/B 或场景一/场景二选项，当前输入只有 “A”“B”“第一种”“第二种” 时，必须解析 context_refs 并结合上文，不要当作全新问题。
6. 如果当前输入只是“klonet平台/这个平台/平台”等短补充，而上文正在确认“使用哪个平台/普通用户使用/浏览器使用”，应理解为补充上一轮对象，优先解析为 platform_usage，不要重新追问首次安装还是启动平台。
7. 当用户目标是了解目标机器的真实运行状态、故障现场或当前环境事实，而不是询问标准安装/启动步骤时，使用 perspective=debugging_runtime 或 action_goal=inspect_error，并设置 requires_environment_diagnosis=true。
8. 只输出 JSON，不要输出 Markdown，不要解释。
"""


@dataclass(frozen=True)
class IntentAnalysis:
    intent: QueryIntent
    token_usage: int = 0
    used_model: bool = False
    raw_content: str = ""
    semantic_frame: SemanticFrame | None = None
    decision: IntentDecision | None = None


class IntentAnalyzer:
    """Use an LLM to parse the user's request before retrieval/tool routing."""

    def __init__(self, llm, intent_case_retriever: IntentCaseRetriever | None = None):
        self.llm = llm
        self.intent_case_retriever = (
            intent_case_retriever or build_default_intent_case_retriever()
        )

    def analyze(
        self,
        user_input: str,
        *,
        recent_history: list[dict] | None = None,
    ) -> IntentAnalysis:
        history = recent_history or []
        user_message = _build_analysis_user_message(user_input, history)
        intent_cases = self.intent_case_retriever.search_for_prompt(
            build_intent_case_query(user_input, history),
            top_k=4,
            min_score=0.1,
        )
        user_message = _append_intent_cases(user_message, intent_cases)
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
        raw = _parse_json_object(content)
        frame = _semantic_frame_from_mapping(raw)
        if frame is not None:
            decision = SemanticDecisionPlanner().plan(
                user_input,
                frame,
                SemanticState.from_history(recent_history or []),
            )
            return IntentAnalysis(
                intent=decision.intent,
                token_usage=token_usage,
                used_model=True,
                raw_content=content,
                semantic_frame=frame,
                decision=decision,
            )
        return IntentAnalysis(
            intent=QueryIntent.from_mapping(raw),
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


def _semantic_frame_from_mapping(raw: dict[str, Any]) -> SemanticFrame | None:
    semantic = raw.get("semantic_frame")
    if isinstance(semantic, dict):
        return SemanticFrame.from_mapping(semantic)
    semantic_keys = {
        "user_role",
        "perspective",
        "machine_role",
        "deployment_phase",
        "action_goal",
        "target_component",
        "excluded_meanings",
        "context_refs",
        "evidence_spans",
        "ambiguity",
    }
    if semantic_keys.intersection(raw):
        return SemanticFrame.from_mapping(raw)
    return None


def _append_intent_cases(user_message: str, cases: tuple[IntentCase, ...]) -> str:
    if not cases:
        return user_message
    blocks = [
        "下面是与当前输入相似的历史意图样例。样例只用于理解意图和槽位；如果样例与当前用户明确条件冲突，以当前用户输入为准。"
    ]
    for index, case in enumerate(cases, start=1):
        blocks.append(
            "\n".join(
                [
                    f"[Intent Case {index}]",
                    f"case_id: {case.case_id}",
                    f"tag: {case.tag}",
                    f"retrieval_mode: {case.retrieval_mode}",
                    f"score: {case.score}",
                    f"keyword_score: {case.keyword_score}",
                    f"semantic_score: {case.semantic_score}",
                    f"history_pattern: {case.history_pattern}",
                    f"latest_query: {case.latest_query}",
                    f"handling_rule: {case.handling_rule}",
                    "semantic_frame: "
                    + json.dumps(case.semantic_frame or {}, ensure_ascii=False),
                    "intent: " + json.dumps(case.intent or {}, ensure_ascii=False),
                    "slots: " + json.dumps(case.slots or {}, ensure_ascii=False),
                    "safety: " + json.dumps(case.safety or {}, ensure_ascii=False),
                ]
            )
        )
    return user_message + "\n\n" + "\n\n".join(blocks)


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
