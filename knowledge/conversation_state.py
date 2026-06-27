"""Compact dialogue state for intent parsing and retrieval planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.semantic_understanding import IntentDecision, SemanticFrame


@dataclass(frozen=True)
class ConversationState:
    """Small, structured state carried across one mentor turn."""

    current_topic: str = ""
    user_role: str = "unknown"
    machine_role: str = "unspecified"
    deployment_phase: str = "unknown"
    confirmed_slots: Mapping[str, str] = field(default_factory=dict)
    excluded_meanings: tuple[str, ...] = ()
    last_option_map: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ConversationState":
        raw = value if isinstance(value, Mapping) else {}
        return cls(
            current_topic=str(raw.get("current_topic") or "").strip(),
            user_role=str(raw.get("user_role") or "unknown").strip() or "unknown",
            machine_role=str(raw.get("machine_role") or "unspecified").strip()
            or "unspecified",
            deployment_phase=str(raw.get("deployment_phase") or "unknown").strip()
            or "unknown",
            confirmed_slots=_string_mapping(raw.get("confirmed_slots")),
            excluded_meanings=_string_tuple(raw.get("excluded_meanings")),
            last_option_map=_string_mapping(raw.get("last_option_map")),
        )

    def to_tool_args(self) -> dict[str, Any]:
        """Return JSON-safe state for internal tool argument injection."""

        return {
            "current_topic": self.current_topic,
            "user_role": self.user_role,
            "machine_role": self.machine_role,
            "deployment_phase": self.deployment_phase,
            "confirmed_slots": dict(self.confirmed_slots),
            "excluded_meanings": list(self.excluded_meanings),
            "last_option_map": dict(self.last_option_map),
        }


class ConversationStateManager:
    """Derive compact state from recent dialogue and semantic analysis."""

    def from_turn(
        self,
        user_input: str,
        *,
        recent_history: list[dict] | None = None,
        semantic_frame: SemanticFrame | None = None,
        intent: QueryIntent | None = None,
        decision: IntentDecision | None = None,
        previous_state: ConversationState | None = None,
    ) -> ConversationState:
        base = previous_state or ConversationState()
        option_map = _option_map_from_history(recent_history or [])
        selected_option = _selected_option(user_input)
        late_usage_supplement = _is_late_platform_usage_supplement(
            user_input,
            recent_history or [],
        )

        frame_phase = semantic_frame.deployment_phase if semantic_frame else "unknown"
        intent_operation = intent.operation if intent else "unknown"
        phase = _phase_from_operation(intent_operation) or _known(frame_phase)
        machine_role = _known(semantic_frame.machine_role if semantic_frame else "") or base.machine_role
        user_role = _known(semantic_frame.user_role if semantic_frame else "") or base.user_role

        confirmed_slots = dict(base.confirmed_slots)
        if selected_option:
            confirmed_slots["selected_option"] = selected_option
            phase = option_map.get(selected_option, phase)
        if late_usage_supplement:
            confirmed_slots["platform_name"] = "klonet"
            phase = "platform_usage"
            machine_role = "unspecified"
        if intent is not None and intent.prerequisites:
            for item in intent.prerequisites:
                confirmed_slots[item] = "true"

        if phase == "platform_startup" and machine_role in {"", "unspecified"}:
            machine_role = "target_server"

        excluded_meanings = list(_excluded_meanings(semantic_frame, intent, base))
        if late_usage_supplement:
            excluded_meanings.extend(
                ("deployment_preparation", "environment_setup", "platform_startup")
            )

        return ConversationState(
            current_topic=_topic_for(phase, intent),
            user_role=user_role or "unknown",
            machine_role=machine_role or "unspecified",
            deployment_phase=phase or base.deployment_phase,
            confirmed_slots=confirmed_slots,
            excluded_meanings=tuple(
                dict.fromkeys(item for item in excluded_meanings if item)
            ),
            last_option_map=option_map or dict(base.last_option_map),
        )


def _option_map_from_history(history: list[dict]) -> dict[str, str]:
    text = "\n".join(
        str(message.get("content") or "")
        for message in history[-6:]
        if message.get("role") == "assistant"
    )
    result: dict[str, str] = {}
    if "A：" in text and ("首次" in text or "全新" in text or "环境部署" in text):
        result["A"] = "environment_setup"
    if "B：" in text and "已经有 Klonet" in text and ("没启动" in text or "启动" in text):
        result["B"] = "platform_startup"
    if "场景一" in text and "浏览器" in text and "普通用户" in text:
        result["第一种"] = "platform_usage"
        result["场景一"] = "platform_usage"
    if "场景二" in text and ("部署" in text or "管理员" in text):
        result["第二种"] = "platform_startup"
        result["场景二"] = "platform_startup"
    return result


def _is_late_platform_usage_supplement(user_input: str, history: list[dict]) -> bool:
    text = user_input.strip().lower().replace(" ", "")
    if text not in {"klonet", "klonet平台", "平台", "这个平台", "那个平台"}:
        return False
    recent_text = "\n".join(
        str(message.get("content") or "")
        for message in history[-6:]
        if message.get("role") in {"user", "assistant"}
    ).lower()
    usage_signals = ("使用平台", "浏览器", "普通用户", "场景一", "电脑里需要下载")
    deploy_signals = ("场景二", "部署运行", "管理员", "开发者")
    return any(term in recent_text for term in usage_signals) and any(
        term in recent_text for term in deploy_signals
    )


def _selected_option(user_input: str) -> str:
    text = user_input.strip()
    lowered = text.lower()
    if text == "B" or lowered == "b":
        return "B"
    if text == "A" or lowered == "a":
        return "A"
    if "第一种" in text or "场景一" in text:
        return "第一种"
    if "第二种" in text or "场景二" in text:
        return "第二种"
    return ""


def _phase_from_operation(operation: str) -> str:
    return {
        "environment_setup": "environment_setup",
        "dependency_install": "environment_setup",
        "platform_start": "platform_startup",
        "platform_stop": "platform_shutdown",
        "platform_restart": "platform_restart",
        "topology_deploy": "topology_deploy",
    }.get(operation, "")


def _topic_for(phase: str, intent: QueryIntent | None) -> str:
    if phase == "platform_startup":
        return "klonet_platform_start"
    if phase == "environment_setup":
        return "klonet_environment_setup"
    if phase == "topology_deploy":
        return "klonet_topology_deploy"
    if phase == "platform_usage":
        return "klonet_platform_usage"
    if intent is not None and intent.target:
        return intent.target
    return ""


def _excluded_meanings(
    frame: SemanticFrame | None,
    intent: QueryIntent | None,
    base: ConversationState,
) -> tuple[str, ...]:
    values: list[str] = list(base.excluded_meanings)
    if frame is not None:
        values.extend(frame.excluded_meanings)
    if intent is not None:
        values.extend(intent.excluded_intents)
    if frame is not None and frame.deployment_phase == "platform_usage":
        values.extend(("deployment_preparation", "environment_setup", "platform_startup"))
    return tuple(dict.fromkeys(item for item in values if item))


def _known(value: str) -> str:
    return "" if value in {"", "unknown", "unspecified"} else value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }
