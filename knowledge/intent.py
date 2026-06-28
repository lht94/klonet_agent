"""模型生成的结构化意图及其信任边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ALLOWED_SCOPES = {"klonet", "general", "mixed"}
ALLOWED_TASK_TYPES = {
    "concept",
    "deployment_preparation",
    "deployment_guidance",
    "credential_boundary",
    "operation_guide",
    "troubleshooting",
    "code_lookup",
    "development",
    "project_progress",
    "general",
}
ALLOWED_OPERATIONS = {
    "unknown",
    "environment_setup",
    "dependency_install",
    "platform_start",
    "platform_stop",
    "platform_restart",
    "topology_deploy",
    "acceptance_check",
}
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
_ENVIRONMENT_DIAGNOSIS_TERMS = {
    "address_already_in_use",
    "celery",
    "connection_refused",
    "docker",
    "gunicorn",
    "kvm",
    "libvirt",
    "mysql",
    "nginx",
    "onos",
    "ovs",
    "port",
    "port_conflict",
    "rabbitmq",
    "redis",
    "screen",
    "terminal",
    "topology",
    "web_terminal",
    "worker",
    "端口",
    "报错",
    "启动失败",
}


@dataclass(frozen=True)
class QueryIntent:
    """经代码校验后的单轮用户意图。"""

    scope: str = "klonet"
    task_type: str = "concept"
    operation: str = "unknown"
    target: str = ""
    symptom: str = ""
    excluded_intents: tuple[str, ...] = ()
    prerequisites: tuple[str, ...] = ()
    requires_retrieval: bool = True
    requires_environment_diagnosis: bool = False
    clarification_required: bool = False
    clarification_question: str = ""
    is_correction: bool = False
    confidence: float = 0.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "QueryIntent":
        """清洗不可信的模型工具参数，非法枚举使用稳定默认值。"""

        raw = value if isinstance(value, Mapping) else {}
        scope = _allowed_value(raw.get("scope"), ALLOWED_SCOPES, "klonet")
        task_type = _allowed_value(
            raw.get("task_type"),
            ALLOWED_TASK_TYPES,
            "concept",
        )
        operation = _allowed_value(
            raw.get("operation"),
            ALLOWED_OPERATIONS,
            "unknown",
        )
        requires_environment_diagnosis = raw.get("requires_environment_diagnosis") is True
        if not requires_environment_diagnosis:
            requires_environment_diagnosis = _looks_like_environment_diagnosis(
                task_type=task_type,
                operation=operation,
                target=str(raw.get("target") or ""),
                symptom=str(raw.get("symptom") or ""),
            )
        return cls(
            scope=scope,
            task_type=task_type,
            operation=operation,
            target=str(raw.get("target") or "").strip(),
            symptom=str(raw.get("symptom") or "").strip(),
            excluded_intents=_string_tuple(
                raw.get("excluded_intents"),
                allowed=ALLOWED_OPERATIONS - {"unknown"},
            ),
            prerequisites=_string_tuple(raw.get("prerequisites")),
            requires_retrieval=raw.get("requires_retrieval") is not False,
            requires_environment_diagnosis=requires_environment_diagnosis,
            clarification_required=raw.get("clarification_required") is True,
            clarification_question=str(raw.get("clarification_question") or "").strip(),
            is_correction=raw.get("is_correction") is True,
            confidence=_confidence(raw.get("confidence")),
        )


def build_retrieval_plan(
    query: str,
    intent: QueryIntent | None,
    top_k: int,
) -> tuple[str, int]:
    """把有限业务意图转换成 BM25 查询扩展和证据预算。"""

    if intent is None or intent.operation == "unknown":
        return query, top_k
    terms = _OPERATION_QUERY_TERMS.get(intent.operation, "")
    enriched_query = f"{query} {terms}".strip()
    return enriched_query, max(
        top_k,
        _OPERATION_MIN_TOP_K.get(intent.operation, top_k),
    )


def _allowed_value(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def _string_tuple(value: Any, *, allowed: set[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for item in value:
        normalized = str(item or "").strip().lower()
        if not normalized or (allowed is not None and normalized not in allowed):
            continue
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(1.0, max(0.0, number)), 4)


def _looks_like_environment_diagnosis(
    *,
    task_type: str,
    operation: str,
    target: str,
    symptom: str,
) -> bool:
    if task_type != "troubleshooting":
        return False
    text = f"{operation} {target} {symptom}".lower()
    return any(term in text for term in _ENVIRONMENT_DIAGNOSIS_TERMS)
