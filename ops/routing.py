"""Lightweight Ops routing and slot extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


PORT_RE = re.compile(r"(?<![\d.])([1-9]\d{1,4})(?![\d.])")
PATH_RE = re.compile(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+/?")
COMPONENT_RE = re.compile(
    r"\b(?:web_terminal_main\.py|master_main\.py|worker_main\.py|celery_worker\.py|gunicorn|celery|nginx|redis|mysql|rabbitmq|docker)\b",
    re.IGNORECASE,
)


@dataclass
class OpsRoute:
    goal: str
    mode: str
    ports: List[int] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    action: str = "inspect"
    risk: str = "low"
    recommended_tools: List[str] = field(default_factory=list)

    def summary(self) -> str:
        clues = []
        if self.ports:
            clues.append("port=" + ",".join(str(port) for port in self.ports))
        if self.components:
            clues.append("component=" + ",".join(self.components))
        if self.paths:
            clues.append("path=" + ",".join(self.paths[:2]))
        if self.action and self.action != "inspect":
            clues.append(f"action={self.action}")
        clue_text = "；线索：" + ", ".join(clues) if clues else ""
        return f"目标：{self.goal}{clue_text}；模式：{self.mode}"


def route_ops_request(user_input: str) -> OpsRoute:
    """Extract operational goal and slots without using Mentor task_type labels."""

    text = user_input or ""
    lowered = text.lower()
    ports = _extract_ports(text)
    paths = _dedupe(PATH_RE.findall(text))
    components = _dedupe(match.group(0) for match in COMPONENT_RE.finditer(text))
    action = _action_for(lowered)

    if action in {"restart", "deploy", "destroy", "stop"}:
        return OpsRoute(
            goal="受控操作请求",
            mode="需要 OperationPlan",
            ports=ports,
            paths=paths,
            components=components,
            action=action,
            risk="high",
            recommended_tools=["create_ops_operation_plan"],
        )

    if ports and any(term in lowered for term in ("address already in use", "占用", "pid", "cwd", "端口")):
        return OpsRoute(
            goal="端口占用诊断",
            mode="只读诊断",
            ports=ports,
            paths=paths,
            components=components,
            recommended_tools=["inspect_process_detail", "inspect_klonet_runtime"],
        )

    if any(term in lowered for term in ("traceback", "日志", "error.log", "报错")):
        return OpsRoute(
            goal="日志故障诊断",
            mode="只读诊断",
            ports=ports,
            paths=paths,
            components=components,
            recommended_tools=["inspect_screen_session", "read_klonet_logs"],
        )

    if any(term in lowered for term in ("有哪些平台", "运行", "screen", "服务", "进程")):
        return OpsRoute(
            goal="运行态盘点",
            mode="只读诊断",
            ports=ports,
            paths=paths,
            components=components,
            recommended_tools=["inspect_klonet_runtime", "inspect_ops_context"],
        )

    return OpsRoute(
        goal="运维诊断",
        mode="只读诊断",
        ports=ports,
        paths=paths,
        components=components,
        recommended_tools=["inspect_klonet_runtime"],
    )


def _extract_ports(text: str) -> List[int]:
    path_spans = [match.span() for match in PATH_RE.finditer(text or "")]
    ports = []
    for match in PORT_RE.finditer(text or ""):
        if any(start <= match.start() < end for start, end in path_spans):
            continue
        port = int(match.group(1))
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _action_for(lowered: str) -> str:
    if any(term in lowered for term in ("销毁", "destroy", "删除平台")):
        return "destroy"
    if any(term in lowered for term in ("部署", "新平台", "deploy")):
        return "deploy"
    if any(term in lowered for term in ("重启", "restart")):
        return "restart"
    if any(term in lowered for term in ("kill", "停止", "停掉", "stop")):
        return "stop"
    return "inspect"


def _dedupe(items) -> List[str]:
    result = []
    for item in items:
        value = str(item).strip()
        if value and value not in result:
            result.append(value)
    return result
