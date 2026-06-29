"""Deterministic Ops environment decision planning.

This module turns read-only tool observations into conservative start/skip/block
guidance. The model still writes the final answer, but these decisions remove
the riskiest "guess from prose" parts of routine Klonet operations.
"""

from __future__ import annotations

import re
from typing import Iterable


PORT_ASSIGNMENT_RE = re.compile(
    r"\b(?:master_port|worker_port|web_terminal_port|terminal_port|public_port|data_server_port)\s*=\s*['\"]?(\d+)"
)
LISTEN_PORT_RE = re.compile(r":(\d{2,5})(?!\d)")
SCREEN_SESSION_RE = re.compile(r"\b([A-Za-z0-9_-]+_(?:m|w|c|web|t))\b")


def build_ops_environment_plan(
    *,
    user_input: str,
    operation: str,
    tool_events: Iterable[dict],
) -> str:
    """Build a system-message friendly Ops plan from tool evidence."""

    if not _is_platform_start(user_input, operation):
        return ""

    events = list(tool_events or [])
    combined = "\n".join(str(event.get("result") or "") for event in events)
    target_name = _target_instance_name(user_input, combined)
    configured_ports = _configured_ports(combined)
    listening_ports = _listening_ports(combined)
    conflicting_ports = sorted(configured_ports & listening_ports)
    screens = set(SCREEN_SESSION_RE.findall(combined))
    expected_screens = _expected_screens(target_name)
    conflicting_screens = sorted(screens & expected_screens)

    decisions = [
        "【Ops deterministic environment plan】",
        "- 该计划由只读工具结果生成；最终回答必须遵守 action=block/skip/verify/proceed，不得给出相反启动步骤。",
    ]
    decisions.append(_service_decision("redis", combined, "Redis 已运行，只需确认 Klonet 配置可达。"))
    decisions.append(
        _docker_decision(combined)
    )
    if conflicting_ports:
        decisions.append(
            "step=ports action=block reason=配置端口已被当前监听占用，启动前必须换端口或停止冲突服务 ports="
            + ",".join(conflicting_ports)
        )
    elif configured_ports:
        decisions.append(
            "step=ports action=proceed reason=已配置端口未在当前监听端口中发现冲突 ports="
            + ",".join(sorted(configured_ports))
        )
    else:
        decisions.append(
            "step=ports action=verify reason=未从配置证据中提取到目标端口，启动前需要读取运行项目 config.py/Nginx/前端配置。"
        )

    if _has_gunicorn_path(combined):
        decisions.append(
            "step=gunicorn action=proceed reason=工具证据中已出现 gunicorn/celery/python 路径或版本。"
        )
    else:
        decisions.append(
            "step=gunicorn action=verify command=command -v gunicorn && command -v celery && command -v python3.8 reason=启动命令路径尚未由当前机器验证。"
        )

    if conflicting_screens:
        decisions.append(
            "step=screen action=block reason=已存在同名 screen session，不能重复 screen -S 创建 sessions="
            + ",".join(conflicting_screens)
        )
    elif target_name:
        decisions.append(
            "step=screen action=proceed reason=未发现目标实例同名 screen session expected="
            + ",".join(sorted(expected_screens))
        )
    else:
        decisions.append(
            "step=screen action=verify reason=未确认新平台实例名，无法判断 screen 名是否冲突。"
        )
    return "\n".join(decisions)


def _is_platform_start(user_input: str, operation: str) -> bool:
    text = user_input or ""
    return operation == "platform_start" or any(term in text for term in ("启动", "部署", "新 Klonet", "新平台"))


def _service_decision(step: str, evidence: str, running_reason: str) -> str:
    lowered = evidence.lower()
    if step in lowered and any(token in lowered for token in ("active", "running", "detected", "up ")):
        return f"step={step} action=skip reason={running_reason}"
    return f"step={step} action=verify reason=未确认 {step} 当前可用，启动前先检查状态。"


def _docker_decision(evidence: str) -> str:
    lowered = evidence.lower()
    if "docker" in lowered and any(token in lowered for token in (" up ", "active", "running", "detected")):
        return "step=docker action=skip reason=Docker daemon/容器已有运行证据，不要重复执行 docker_service.sh。"
    return "step=docker action=verify reason=未确认 Docker 当前可用，启动前先检查 docker ps/docker info。"


def _configured_ports(evidence: str) -> set[str]:
    return set(PORT_ASSIGNMENT_RE.findall(evidence or ""))


def _listening_ports(evidence: str) -> set[str]:
    ports = set()
    for line in (evidence or "").splitlines():
        lowered = line.lower()
        if "listen" not in lowered and "listening" not in lowered:
            continue
        ports.update(LISTEN_PORT_RE.findall(line))
    return ports


def _target_instance_name(user_input: str, evidence: str) -> str:
    path_match = re.search(r"/([A-Za-z0-9_-]{2,32})_project/", evidence or "")
    if path_match:
        return path_match.group(1)
    for text in (user_input or "",):
        match = re.search(r"\b([A-Za-z0-9_-]{2,32})_(?:m|w|c|web|t)\b", text)
        if match:
            return match.group(1)
    match = re.search(r"\b(?:instance|平台|项目|project)\s*[:：=]?\s*([A-Za-z0-9_-]{2,32})\b", user_input or "")
    return match.group(1) if match else ""


def _expected_screens(target_name: str) -> set[str]:
    if not target_name:
        return set()
    return {f"{target_name}_m", f"{target_name}_w", f"{target_name}_c", f"{target_name}_web", f"{target_name}_t"}


def _has_gunicorn_path(evidence: str) -> bool:
    lowered = (evidence or "").lower()
    return "gunicorn" in lowered and ("celery" in lowered or "python" in lowered)
