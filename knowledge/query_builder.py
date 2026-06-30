"""Deterministic retrieval query planning from intent and dialogue state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from klonet_agent.knowledge.conversation_state import ConversationState
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.semantic_understanding import IntentDecision, SemanticFrame


_OPERATION_QUERY_TERMS = {
    "platform_start": "启动顺序 Redis Master Gunicorn Celery Web Terminal Worker Nginx 标准命令",
    "platform_stop": "停止顺序 Master Celery Web Terminal Worker 标准命令",
    "platform_restart": "重启顺序 Master Celery Web Terminal Worker Nginx 验证",
    "topology_deploy": "拓扑部署 TopoDeployAPI Celery Redis Worker Docker OVS KVM 进度条",
    "environment_setup": "首次安装 依赖 Docker Redis MySQL RabbitMQ 镜像仓库 环境验收",
    "dependency_install": "依赖安装 Python Docker OVS Redis MySQL RabbitMQ",
}
_OPERATION_MIN_TOP_K = {
    "platform_start": 8,
    "platform_stop": 5,
    "platform_restart": 6,
    "topology_deploy": 8,
    "environment_setup": 5,
    "dependency_install": 5,
}
_PHASE_QUERY_TERMS = {
    "local_tool_preparation": "操作者个人电脑 本机工具 SSH SFTP SCP VS Code 浏览器 数据库客户端 Redis 客户端",
    "platform_usage": "浏览器访问 普通用户 使用平台 登录 页面 功能入口",
}


@dataclass(frozen=True)
class RetrievalQueryPlan:
    """Prepared retrieval request before hitting the knowledge retriever."""

    query: str
    top_k: int
    task_type: str
    domains: tuple[str, ...] = ()
    layers: tuple[str, ...] | None = None
    intent_operation: str = "unknown"
    excluded_intents: tuple[str, ...] = ()
    debug_parts: tuple[str, ...] = ()


class QueryBuilder:
    """Build focused retrieval queries without an additional LLM call."""

    def build(
        self,
        original_user_input: str,
        *,
        intent: QueryIntent | None = None,
        semantic_frame: SemanticFrame | None = None,
        decision: IntentDecision | None = None,
        conversation_state: ConversationState | None = None,
        top_k: int = 3,
        task_type: str | None = None,
        domains: tuple[str, ...] | None = None,
        layers: tuple[str, ...] | None = None,
    ) -> RetrievalQueryPlan:
        if intent is not None and intent.confidence < 0.6:
            intent = None

        state = conversation_state or ConversationState()
        has_structured_context = (
            intent is not None
            or semantic_frame is not None
            or _has_state_signal(state)
            or decision is not None
        )
        if not has_structured_context:
            return RetrievalQueryPlan(
                query=original_user_input.strip(),
                top_k=top_k,
                task_type=task_type or "auto",
                domains=domains or (),
                layers=layers,
                debug_parts=("original_user_input",),
            )

        operation = intent.operation if intent is not None else "unknown"
        task = intent.task_type if intent is not None else task_type or "auto"
        plan_top_k = max(top_k, _OPERATION_MIN_TOP_K.get(operation, top_k))

        parts = [f"raw_query:{original_user_input.strip()}"]
        debug_parts = ["original_user_input"]
        if intent is not None:
            _append(parts, debug_parts, "task_type", intent.task_type)
            _append(parts, debug_parts, "operation", intent.operation)
            _append(parts, debug_parts, "target", intent.target)
            _append(parts, debug_parts, "symptom", intent.symptom)
            for prerequisite in intent.prerequisites:
                _append(parts, debug_parts, "prerequisite", prerequisite)
            operation_terms = _OPERATION_QUERY_TERMS.get(intent.operation)
            if operation_terms:
                _append(parts, debug_parts, "operation_terms", operation_terms)

        if semantic_frame is not None:
            _append(parts, debug_parts, "user_role", semantic_frame.user_role)
            _append(parts, debug_parts, "machine_role", semantic_frame.machine_role)
            _append(parts, debug_parts, "deployment_phase", semantic_frame.deployment_phase)
            _append(parts, debug_parts, "action_goal", semantic_frame.action_goal)
            _append(parts, debug_parts, "target_component", semantic_frame.target_component)
            phase_terms = _PHASE_QUERY_TERMS.get(semantic_frame.deployment_phase)
            if phase_terms:
                _append(parts, debug_parts, "phase_terms", phase_terms)

        _append(parts, debug_parts, "state_topic", state.current_topic)
        _append(parts, debug_parts, "state_machine_role", state.machine_role)
        _append(parts, debug_parts, "state_phase", state.deployment_phase)
        for key, value in state.confirmed_slots.items():
            _append(parts, debug_parts, "confirmed_slot", f"{key}:{value}")
        state_phase_terms = _PHASE_QUERY_TERMS.get(state.deployment_phase)
        if state_phase_terms:
            _append(parts, debug_parts, "state_phase_terms", state_phase_terms)

        if decision is not None and decision.soft_note:
            _append(parts, debug_parts, "soft_note", decision.soft_note)

        excluded = _merge_exclusions(intent, semantic_frame, state)
        return RetrievalQueryPlan(
            query=" ".join(part for part in parts if part).strip(),
            top_k=plan_top_k,
            task_type=task,
            domains=domains or (),
            layers=layers,
            intent_operation=operation,
            excluded_intents=excluded,
            debug_parts=tuple(debug_parts),
        )


def _append(parts: list[str], debug_parts: list[str], label: str, value: Any):
    text = str(value or "").strip()
    if not text or text in {"unknown", "unspecified"}:
        return
    parts.append(f"{label}:{text}")
    debug_parts.append(label)


def _merge_exclusions(
    intent: QueryIntent | None,
    semantic_frame: SemanticFrame | None,
    state: ConversationState,
) -> tuple[str, ...]:
    values: list[str] = []
    if intent is not None:
        values.extend(intent.excluded_intents)
    if semantic_frame is not None:
        values.extend(semantic_frame.excluded_meanings)
    values.extend(state.excluded_meanings)
    return tuple(dict.fromkeys(item for item in values if item))


def _has_state_signal(state: ConversationState) -> bool:
    return bool(
        state.current_topic
        or state.user_role not in {"", "unknown"}
        or state.machine_role not in {"", "unspecified"}
        or state.deployment_phase not in {"", "unknown"}
        or state.confirmed_slots
        or state.excluded_meanings
        or state.last_option_map
    )
