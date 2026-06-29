"""Semantic frame understanding and decision planning for Mentor routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from klonet_agent.knowledge.intent import QueryIntent


ALLOWED_USER_ROLES = {"learner", "operator", "developer", "admin", "unknown"}
ALLOWED_PERSPECTIVES = {
    "asking_about_own_pc",
    "operating_target_machine",
    "using_platform",
    "debugging_runtime",
    "unknown",
}
ALLOWED_MACHINE_ROLES = {
    "operator_local_pc",
    "target_server",
    "target_vm",
    "host_machine",
    "klonet_master",
    "klonet_worker",
    "unspecified",
}
ALLOWED_DEPLOYMENT_PHASES = {
    "local_tool_preparation",
    "environment_setup",
    "platform_startup",
    "platform_shutdown",
    "platform_restart",
    "topology_deploy",
    "platform_usage",
    "troubleshooting",
    "unknown",
}
ALLOWED_ACTION_GOALS = {
    "prepare_tools",
    "install_dependencies",
    "start_services",
    "stop_services",
    "restart_services",
    "deploy_topology",
    "inspect_error",
    "use_feature",
    "explain_concept",
    "unknown",
}
ALLOWED_CLARIFICATION_ACTIONS = {"none", "soft_note", "ask_before_answer"}
ALLOWED_RISK_LEVELS = {"low", "medium", "high"}


@dataclass(frozen=True)
class SemanticFrame:
    """A domain semantic frame extracted from a user turn before routing."""

    scope: str = "klonet"
    user_role: str = "unknown"
    perspective: str = "unknown"
    machine_role: str = "unspecified"
    deployment_phase: str = "unknown"
    action_goal: str = "unknown"
    target_component: str = ""
    symptom: str = ""
    excluded_meanings: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    evidence_spans: tuple[str, ...] = ()
    ambiguity: Mapping[str, Any] | None = None
    confidence: float = 0.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "SemanticFrame":
        raw = value if isinstance(value, Mapping) else {}
        return cls(
            scope=_allowed(raw.get("scope"), {"klonet", "general", "mixed"}, "klonet"),
            user_role=_allowed(raw.get("user_role"), ALLOWED_USER_ROLES, "unknown"),
            perspective=_allowed(
                raw.get("perspective"),
                ALLOWED_PERSPECTIVES,
                "unknown",
            ),
            machine_role=_allowed(
                raw.get("machine_role"),
                ALLOWED_MACHINE_ROLES,
                "unspecified",
            ),
            deployment_phase=_allowed(
                raw.get("deployment_phase"),
                ALLOWED_DEPLOYMENT_PHASES,
                "unknown",
            ),
            action_goal=_allowed(
                raw.get("action_goal"),
                ALLOWED_ACTION_GOALS,
                "unknown",
            ),
            target_component=str(raw.get("target_component") or "").strip(),
            symptom=str(raw.get("symptom") or "").strip(),
            excluded_meanings=_string_tuple(raw.get("excluded_meanings")),
            context_refs=_string_tuple(raw.get("context_refs")),
            evidence_spans=_string_tuple(raw.get("evidence_spans")),
            ambiguity=raw.get("ambiguity") if isinstance(raw.get("ambiguity"), Mapping) else {},
            confidence=_confidence(raw.get("confidence")),
        )


@dataclass(frozen=True)
class SemanticState:
    """Small dialogue state used to resolve references before deciding."""

    referenced_phase: str = "unknown"
    referenced_perspective: str = "unknown"
    referenced_machine_role: str = "unspecified"

    @classmethod
    def from_history(cls, history: list[dict] | None) -> "SemanticState":
        text = "\n".join(
            str(message.get("content") or "")
            for message in (history or [])[-6:]
            if message.get("role") == "assistant"
        )
        if "场景一" in text and "浏览器" in text and "普通用户" in text:
            return cls(
                referenced_phase="platform_usage",
                referenced_perspective="using_platform",
                referenced_machine_role="unspecified",
            )
        if "B：" in text and "已经有 Klonet" in text and "没启动" in text:
            return cls(
                referenced_phase="platform_startup",
                referenced_perspective="operating_target_machine",
                referenced_machine_role="target_server",
            )
        return cls()


@dataclass(frozen=True)
class IntentDecision:
    """Decision-plan result consumed by legacy intent routing and answers."""

    intent: QueryIntent
    clarification_action: str = "none"
    answer_mode: str = "general"
    soft_note: str = ""
    risk_level: str = "low"
    defaulted_assumptions: tuple[str, ...] = ()


class SemanticDecisionPlanner:
    """Convert semantic frames into safe routing and clarification decisions."""

    def plan(
        self,
        user_input: str,
        frame: SemanticFrame,
        state: SemanticState,
    ) -> IntentDecision:
        frame = _resolve_context_reference(user_input, frame, state)
        phase = _infer_phase(user_input, frame)
        task_type = _task_type_for(frame, phase)
        operation = _operation_for(phase, frame)
        target = _target_for(frame, phase)
        requires_environment_diagnosis = _requires_environment_diagnosis(
            user_input,
            frame,
            task_type,
            target,
        )
        if requires_environment_diagnosis and task_type == "concept":
            task_type = "troubleshooting"
        clarification_action = _clarification_action_for(frame, phase)
        soft_note = (
            "这里先按启动已安装好的 Klonet 平台服务理解；如果你指首次安装环境，流程不同。"
            if clarification_action == "soft_note"
            else ""
        )
        intent = QueryIntent.from_mapping(
            {
                "scope": frame.scope,
                "task_type": task_type,
                "operation": operation,
                "target": target,
                "symptom": frame.symptom,
                "excluded_intents": _excluded_intents_for(frame, phase),
                "requires_retrieval": frame.scope != "general",
                "requires_environment_diagnosis": requires_environment_diagnosis,
                "clarification_required": clarification_action == "ask_before_answer",
                "clarification_question": _clarification_question_for(frame, phase),
                "confidence": max(frame.confidence, 0.7 if phase != "unknown" else 0.0),
            }
        )
        return IntentDecision(
            intent=intent,
            clarification_action=clarification_action,
            answer_mode=_answer_mode_for(phase),
            soft_note=soft_note,
            risk_level=_risk_level_for(frame, clarification_action),
            defaulted_assumptions=(
                ("platform_startup",) if clarification_action == "soft_note" else ()
            ),
        )


def _resolve_context_reference(
    user_input: str,
    frame: SemanticFrame,
    state: SemanticState,
) -> SemanticFrame:
    text = user_input.lower()
    refs = set(frame.context_refs)
    reference_terms = {"第一种", "场景一", "b", "B"}
    if (
        "第一种" not in text
        and "场景一" not in text
        and text.strip() != "b"
        and not refs.intersection(reference_terms)
    ):
        return frame
    if state.referenced_phase == "unknown":
        return frame
    return SemanticFrame(
        scope=frame.scope,
        user_role=frame.user_role,
        perspective=state.referenced_perspective,
        machine_role=state.referenced_machine_role,
        deployment_phase=state.referenced_phase,
        action_goal=frame.action_goal,
        target_component=frame.target_component,
        symptom=frame.symptom,
        excluded_meanings=frame.excluded_meanings,
        context_refs=frame.context_refs,
        evidence_spans=frame.evidence_spans,
        ambiguity={"level": "low", "resolved_by_context": True},
        confidence=max(frame.confidence, 0.75),
    )


def _infer_phase(user_input: str, frame: SemanticFrame) -> str:
    if frame.deployment_phase != "unknown":
        return frame.deployment_phase
    text = user_input.lower()
    if "拓扑" in text or "topology" in text or "topo" in text:
        return "topology_deploy"
    if "电脑" in text and "服务器" not in text and "虚拟机" not in text:
        return "local_tool_preparation"
    if any(term in text for term in ("环境", "安装", "依赖", "base_requ", "docker")):
        return "environment_setup"
    if frame.action_goal == "start_services" or "部署平台" in text or "启动平台" in text:
        return "platform_startup"
    if frame.action_goal == "use_feature":
        return "platform_usage"
    return "unknown"


def _task_type_for(frame: SemanticFrame, phase: str) -> str:
    if frame.symptom or frame.action_goal == "inspect_error":
        return "troubleshooting"
    if phase in {"local_tool_preparation", "environment_setup"}:
        return "deployment_preparation"
    if phase in {"platform_startup", "platform_shutdown", "platform_restart"}:
        return "deployment_guidance"
    if phase in {"topology_deploy", "platform_usage"}:
        return "operation_guide"
    return "concept"


def _requires_environment_diagnosis(
    user_input: str,
    frame: SemanticFrame,
    task_type: str,
    target: str,
) -> bool:
    text = f"{user_input} {frame.target_component} {frame.symptom} {target}".lower()
    if frame.action_goal == "inspect_error" or frame.perspective == "debugging_runtime":
        return True
    if task_type == "troubleshooting" and any(
        term in text
        for term in (
            "nginx",
            "docker",
            "redis",
            "rabbitmq",
            "mysql",
            "ovs",
            "kvm",
            "libvirt",
            "screen",
            "worker",
            "端口",
            "报错",
            "启动失败",
        )
    ):
        return True
    return False


def _operation_for(phase: str, frame: SemanticFrame) -> str:
    if phase == "environment_setup":
        return "environment_setup"
    if phase == "platform_startup":
        return "platform_start"
    if phase == "platform_shutdown":
        return "platform_stop"
    if phase == "platform_restart":
        return "platform_restart"
    if phase == "topology_deploy":
        return "topology_deploy"
    return "unknown"


def _target_for(frame: SemanticFrame, phase: str) -> str:
    if frame.machine_role == "operator_local_pc":
        return "operator_local_pc"
    if frame.target_component:
        return frame.target_component
    if phase == "platform_startup":
        return "klonet_platform"
    if phase == "topology_deploy":
        return "topology"
    return ""


def _clarification_action_for(frame: SemanticFrame, phase: str) -> str:
    ambiguity = frame.ambiguity or {}
    if ambiguity.get("defaultable") is True and phase == "platform_startup":
        return "soft_note"
    if ambiguity.get("level") == "high":
        return "ask_before_answer"
    return "none"


def _clarification_question_for(frame: SemanticFrame, phase: str) -> str:
    if _clarification_action_for(frame, phase) != "ask_before_answer":
        return ""
    return "你是想首次安装 Klonet 环境，还是启动已经安装好的平台服务？"


def _excluded_intents_for(frame: SemanticFrame, phase: str) -> tuple[str, ...]:
    if "environment_setup" in frame.excluded_meanings:
        return ("environment_setup",)
    if phase == "local_tool_preparation":
        return ("environment_setup", "dependency_install")
    if phase == "platform_usage":
        return (
            "environment_setup",
            "dependency_install",
            "platform_start",
            "topology_deploy",
        )
    return ()


def _answer_mode_for(phase: str) -> str:
    if phase in {"local_tool_preparation", "platform_usage"}:
        return phase
    if phase == "platform_startup":
        return "platform_start"
    return phase if phase != "unknown" else "general"


def _risk_level_for(frame: SemanticFrame, clarification_action: str) -> str:
    if clarification_action == "ask_before_answer":
        return "high"
    if (frame.ambiguity or {}).get("level") == "medium":
        return "medium"
    return "low"


def _allowed(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(1.0, max(0.0, number)), 4)
