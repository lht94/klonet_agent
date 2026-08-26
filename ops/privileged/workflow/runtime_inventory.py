"""Typed runtime inventory shared by Discovery, Verifier, Planner and Response.

The running-platform probe is a machine-readable domain record.  Parsing it in
one place prevents each workflow stage from independently guessing instance
identity, counts, ports and component state from truncated prose.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import re
from typing import Any, Iterable

from klonet_agent.ops.privileged.workflow.contracts import (
    ResolvedPlatformIdentity,
    normalize_instance_alias,
)


_RUNTIME_TERMS = (
    "平台", "实例", "后端", "健康接口", "端口", "进程", "启动",
    "重启", "screen", "master", "worker", "celery", "web terminal",
    "web_terminal", "server_health", "platform", "backend", "runtime",
)

_RUNTIME_OUTCOME_TERMS = (
    "正常", "健康", "状态", "运行", "没启动", "未启动", "挂了", "挂了吗",
    "报错", "故障", "404", "重启", "启动", "停止", "修复", "恢复",
    "归属", "冲突", "healthy", "status", "running", "restart", "start",
    "stop", "repair", "recover", "conflict",
)


def _field(line: str, name: str, default: str = "") -> str:
    match = re.search(r"(?:^|\s)%s=([^\s]+)" % re.escape(name), line)
    return match.group(1) if match else default


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item for item in str(value or "").split(",") if item and item != "none")


@dataclass(frozen=True)
class RuntimeRoleBinding:
    """Authoritative configured-port to runtime-process-group identity."""

    role: str
    configured_port: int | None
    status: str
    listener_pid: int | None = None
    listener_pgid: int | None = None
    listener_pids: tuple[int, ...] = ()
    observed_role: str = "unknown"
    runtime_root: str = "unknown"
    code_root: str = "unknown"

    @classmethod
    def from_dict(cls, value: Any) -> "RuntimeRoleBinding | None":
        if not isinstance(value, dict):
            return None
        role = str(value.get("role") or "").strip()
        status = str(value.get("status") or "").strip()
        if not role or not status:
            return None

        def optional_int(raw: Any) -> int | None:
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        return cls(
            role=role,
            configured_port=optional_int(value.get("configured_port")),
            status=status,
            listener_pid=optional_int(value.get("listener_pid")),
            listener_pgid=optional_int(value.get("listener_pgid")),
            listener_pids=tuple(
                parsed
                for item in value.get("listener_pids") or []
                if (parsed := optional_int(item)) is not None
            ),
            observed_role=str(value.get("observed_role") or "unknown"),
            runtime_root=str(value.get("runtime_root") or "unknown"),
            code_root=str(value.get("code_root") or "unknown"),
        )


def _decode_role_bindings(value: str) -> dict[str, RuntimeRoleBinding]:
    if not value or value == "none":
        return {}
    try:
        padding = "=" * (-len(value) % 4)
        raw = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for role, item in raw.items():
        parsed = RuntimeRoleBinding.from_dict(item)
        if parsed is not None and parsed.role == str(role):
            result[str(role)] = parsed
    return result


def _decode_screen_sessions(value: str) -> dict[str, tuple[str, ...]]:
    if not value or value == "none":
        return {}
    try:
        padding = "=" * (-len(value) % 4)
        raw = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for role, sessions in raw.items():
        if not isinstance(sessions, list):
            continue
        valid = tuple(dict.fromkeys(
            str(session)
            for session in sessions
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(session or ""))
        ))
        if valid:
            result[str(role)] = valid
    return result


@dataclass(frozen=True)
class RuntimeInstance:
    platform: str
    project_root: str
    backend_status: str
    roles: tuple[str, ...] = ()
    missing_roles: tuple[str, ...] = ()
    pids: tuple[int, ...] = ()
    configured_ports: dict[str, int] = field(default_factory=dict)
    endpoints: dict[str, str] = field(default_factory=dict)
    role_bindings: dict[str, RuntimeRoleBinding] = field(default_factory=dict)
    screen_sessions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""
    evidence_id: str = ""

    @classmethod
    def parse(cls, line: str, *, evidence_id: str = "") -> "RuntimeInstance | None":
        fields = dict(re.findall(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=([^\s]+)", line))
        root = _field(line, "project_root")
        platform = _field(line, "platform")
        if not root or not platform or not root.startswith("/"):
            return None
        ports = {
            key: int(value)
            for key, value in re.findall(
                r"(master_port|worker_port|public_port|web_terminal_port):(\d{1,5})",
                _field(line, "configured_ports"),
            )
        }
        for key in ("master_port", "worker_port", "web_terminal_port"):
            value = _field(line, key)
            if value.isdigit():
                ports[key] = int(value)
        endpoints = {
            role: _field(line, role + "_endpoint", "unknown")
            for role in ("master", "worker")
        }
        return cls(
            platform=platform,
            project_root=root.rstrip("/") or "/",
            backend_status=_field(line, "backend_status", "unknown"),
            roles=_csv(_field(line, "roles")),
            missing_roles=_csv(_field(line, "missing_roles")),
            pids=tuple(
                int(item) for item in _csv(_field(line, "pids"))
                if item.isdigit()
            ),
            configured_ports=ports,
            endpoints=endpoints,
            role_bindings=_decode_role_bindings(_field(line, "role_bindings_b64")),
            screen_sessions=_decode_screen_sessions(
                _field(line, "screen_sessions_b64")
            ),
            fields=fields,
            raw_line=line.strip(),
            evidence_id=evidence_id,
        )

    def role_binding(self, role: str) -> RuntimeRoleBinding | None:
        return self.role_bindings.get(str(role or "").strip().lower())

    def screen_session(self, role: str) -> str:
        """Return one root-bound role session, never guess among duplicates."""

        normalized = str(role or "").strip().lower().replace("-", "_")
        sessions = self.screen_sessions.get(normalized, ())
        suffix = {
            "master": "_m", "celery": "_c",
            "web_terminal": "_web", "worker": "_w",
        }.get(normalized, "")
        expected = "%s%s" % (self.platform, suffix) if suffix else ""
        exact = [session for session in sessions if session == expected]
        if len(exact) == 1:
            return exact[0]
        return sessions[0] if len(sessions) == 1 else ""

    @property
    def aliases(self) -> tuple[str, ...]:
        values = [self.platform, Path(self.project_root).name]
        return tuple(dict.fromkeys(item for item in values if item))

    def component_state(self, component: str) -> str:
        normalized = component.lower().replace("-", "_").replace(" ", "_")
        if normalized in {"master", "worker"}:
            endpoint = self.endpoints.get(normalized, "unknown")
            if normalized not in self.roles:
                return "未检测到运行进程"
            if endpoint == "healthy":
                return "运行中，健康接口正常"
            return "运行异常，健康接口=%s" % endpoint
        return "运行中" if normalized in self.roles else "未检测到运行证据"


def _overlay_port_owner_bindings(
    by_root: dict[str, RuntimeInstance],
    records: Iterable[Any],
) -> dict[str, RuntimeInstance]:
    """Merge later port-owner evidence into the one runtime inventory model."""

    updated = dict(by_root)
    for record in records:
        request = getattr(record, "request", None)
        if (
            getattr(request, "probe", "") != "port_owner"
            or str(getattr(record, "status", "available")) != "available"
        ):
            continue
        for line in str(getattr(record, "output", "") or "").splitlines():
            port_match = re.search(r"\bport=(\d{1,5})\b", line)
            pid_match = re.search(r"\b(?:tree_root_pid|pid)=(\d+)\b", line)
            if port_match is None or pid_match is None:
                continue
            port = int(port_match.group(1))
            pid = int(pid_match.group(1))
            pgid_match = re.search(r"\bpgid=(\d+)\b", line)
            cwd_match = re.search(r"\bcwd=([^\s]+)", line)
            command_match = re.search(r"\bcmd=(.*?)\s+cwd=", line)
            cwd = cwd_match.group(1).rstrip("/") if cwd_match else "unknown"
            command = command_match.group(1) if command_match else ""
            observed_role = _runtime_role_from_command(command)
            listener_match = re.search(r"\blistener_pids=([0-9,]+)", line)
            listener_pids = tuple(
                int(item)
                for item in (listener_match.group(1).split(",") if listener_match else [str(pid)])
                if item.isdigit()
            )
            candidates = []
            for root, instance in updated.items():
                for key, expected_role in (
                    ("master_port", "master"), ("worker_port", "worker"),
                ):
                    if instance.configured_ports.get(key) == port:
                        candidates.append((root, instance, expected_role))
            if not candidates:
                continue
            exact = [
                item for item in candidates
                if cwd in {item[0], item[0] + "/mains"}
            ]
            selected = exact if exact else candidates if len(candidates) == 1 else []
            for root, instance, expected_role in selected:
                code_root = _runtime_code_root(command) or "unknown"
                if observed_role and observed_role != expected_role:
                    status = "role_conflict"
                elif cwd in {root, root + "/mains"}:
                    status = "confirmed"
                elif cwd.startswith("/"):
                    status = "runtime_conflict"
                else:
                    status = "owner_ambiguous"
                binding = RuntimeRoleBinding(
                    role=expected_role,
                    configured_port=port,
                    status=status,
                    listener_pid=pid,
                    listener_pgid=(int(pgid_match.group(1)) if pgid_match else pid),
                    listener_pids=listener_pids,
                    observed_role=observed_role or "unknown",
                    runtime_root=cwd,
                    code_root=code_root,
                )
                role_bindings = dict(instance.role_bindings)
                role_bindings[expected_role] = binding
                updated[root] = replace(instance, role_bindings=role_bindings)
    return updated


def _runtime_role_from_command(command: str) -> str:
    lowered = str(command or "").lower()
    if "worker_main" in lowered or "worker_gun.py" in lowered:
        return "worker"
    if "master_main" in lowered or re.search(r"(?:^|\s)-c\s+\S*gun\.py", lowered):
        return "master"
    return ""


def _runtime_code_root(command: str) -> str:
    match = re.search(
        r"((?:/[A-Za-z0-9._-]+)+)/mains(?:/|\b)", command or "",
    )
    return match.group(1).strip().rstrip("/") if match else ""


@dataclass(frozen=True)
class RuntimeInventory:
    instances: tuple[RuntimeInstance, ...] = ()
    code_only_roots: tuple[str, ...] = ()
    declared_runtime_count: int | None = None
    declared_healthy_count: int | None = None
    declared_abnormal_count: int | None = None
    declared_code_only_count: int | None = None
    evidence_ids: tuple[str, ...] = ()

    @classmethod
    def from_records(cls, records: Iterable[Any]) -> "RuntimeInventory":
        records = tuple(records)
        instances: list[RuntimeInstance] = []
        code_only: list[str] = []
        evidence_ids: list[str] = []
        counts: dict[str, int] = {}
        for record in records:
            request = getattr(record, "request", None)
            if getattr(request, "probe", "") != "running_platforms":
                continue
            if str(getattr(record, "status", "available")) != "available":
                continue
            evidence_id = str(getattr(record, "evidence_id", "") or "")
            if evidence_id:
                evidence_ids.append(evidence_id)
            for line in str(getattr(record, "output", "") or "").splitlines():
                for key in (
                    "runtime_candidate_count", "healthy_count", "abnormal_count",
                    "code_only_count",
                ):
                    match = re.fullmatch(r"%s=(\d+)" % key, line.strip())
                    if match:
                        counts[key] = int(match.group(1))
                if line.startswith("platform="):
                    parsed = RuntimeInstance.parse(line, evidence_id=evidence_id)
                    if parsed is not None:
                        instances.append(parsed)
                elif line.startswith("code_only_root="):
                    root = line.partition("=")[2].strip().rstrip("/")
                    if root and root not in code_only:
                        code_only.append(root)
        by_root = {item.project_root: item for item in instances}
        by_root = _overlay_port_owner_bindings(by_root, records)
        return cls(
            instances=tuple(by_root[root] for root in sorted(by_root)),
            code_only_roots=tuple(sorted(code_only)),
            declared_runtime_count=counts.get("runtime_candidate_count"),
            declared_healthy_count=counts.get("healthy_count"),
            declared_abnormal_count=counts.get("abnormal_count"),
            declared_code_only_count=counts.get("code_only_count"),
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
        )

    @classmethod
    def from_bundle(cls, bundle: Any) -> "RuntimeInventory":
        return cls.from_records(getattr(bundle, "records", ()) or ())

    @property
    def complete(self) -> bool:
        return bool(self.evidence_ids) and all(
            value is not None for value in (
                self.declared_runtime_count, self.declared_healthy_count,
                self.declared_abnormal_count, self.declared_code_only_count,
            )
        )

    @property
    def healthy(self) -> tuple[RuntimeInstance, ...]:
        return tuple(item for item in self.instances if item.backend_status == "healthy")

    @property
    def abnormal(self) -> tuple[RuntimeInstance, ...]:
        return tuple(item for item in self.instances if item.backend_status != "healthy")

    def instance_for_root(self, project_root: str) -> RuntimeInstance | None:
        """Resolve one instance by normalized, exact project-root identity."""

        normalized = str(project_root or "").rstrip("/") or "/"
        matches = [
            item for item in self.instances
            if item.project_root == normalized
        ]
        return matches[0] if len(matches) == 1 else None

    def matching(self, text: str) -> tuple[RuntimeInstance, ...]:
        raw = str(text or "")
        explicit_roots = {
            candidate.rstrip("/")
            for candidate in re.findall(r"/[A-Za-z0-9._/-]+", raw)
        }
        if explicit_roots:
            matches = [
                item for item in self.instances
                if item.project_root in explicit_roots
                or any(root == item.project_root for root in explicit_roots)
            ]
            if matches:
                return tuple(matches)
        compact_goal = normalize_instance_alias(raw)
        matches = []
        for item in self.instances:
            aliases = {
                normalize_instance_alias(alias) for alias in item.aliases
                if normalize_instance_alias(alias)
            }
            if any(
                alias in compact_goal
                and (
                    len(alias) >= 4
                    or re.search(r"(?<!\d)%s(?!\d)" % re.escape(alias), raw, re.I)
                )
                for alias in aliases
            ):
                matches.append(item)
        return tuple(matches)

    def resolve_identity(self, text: str) -> ResolvedPlatformIdentity | None:
        matches = self.matching(text)
        if len(matches) != 1:
            return None
        instance = matches[0]
        aliases = tuple(sorted(instance.aliases))
        primary = next(
            (item for item in aliases if not item.startswith("klonet_")),
            aliases[0],
        )
        return ResolvedPlatformIdentity(
            project_root=instance.project_root,
            primary_alias=primary,
            aliases=aliases,
            evidence_refs=(instance.evidence_id,),
        )


def looks_like_runtime_goal(goal: str) -> bool:
    text = str(goal or "").lower()
    has_subject = any(term in text for term in _RUNTIME_TERMS) or bool(
        re.search(r"/[A-Za-z0-9._/-]*(?:vemu|klonet)[A-Za-z0-9._/-]*", text)
    )
    return has_subject and any(term in text for term in _RUNTIME_OUTCOME_TERMS)


def runtime_inventory_answers_goal(goal: str, inventory: RuntimeInventory) -> bool:
    """Whether the complete inventory directly settles this read-only goal."""

    if not inventory.complete:
        return False
    targets = inventory.matching(goal)
    if not targets and not looks_like_runtime_goal(goal):
        return False
    if not targets:
        # A runtime question without an instance identity is inventory-wide.
        # Do not infer this semantic scope from enumeration keywords.
        return True
    text = str(goal or "").lower()
    # A complete row settles backend health, configured ports, discovered
    # components, and whether Screen absence alone implies a stopped backend.
    readonly_markers = (
        "正常", "状态", "是不是", "健康", "挂", "404", "没启动",
        "运行", "核对", "归属", "screen", "status", "healthy",
    )
    return any(marker in text for marker in readonly_markers)


def render_runtime_goal(goal: str, inventory: RuntimeInventory) -> str:
    text = str(goal or "").lower()
    targets = inventory.matching(goal)
    if not targets:
        lines = [
            "当前发现 %s 个有后端运行证据的 Klonet 实例：%s 个正常，%s 个异常。"
            % (len(inventory.instances), len(inventory.healthy), len(inventory.abnormal))
        ]
        lines.append("\n运行正常")
        lines.extend(_render_inventory_instance(item) for item in inventory.healthy)
        if not inventory.healthy:
            lines.append("- 无")
        lines.append("\n运行异常")
        lines.extend(_render_inventory_instance(item) for item in inventory.abnormal)
        if not inventory.abnormal:
            lines.append("- 无")
        if inventory.code_only_roots:
            lines.append("\n仅发现代码、没有后端运行证据")
            lines.extend("- `%s`" % root for root in inventory.code_only_roots)
        return "\n".join(lines)
    if len(targets) > 1:
        lines = ["这些根目录对应独立平台实例，不会按名称合并："]
        lines.extend(_render_inventory_instance(item) for item in targets)
        return "\n".join(lines)
    if not targets:
        return ""
    item = targets[0]
    lines = []
    if "404" in text or re.search(r"(?:访问|get)\s*`?/`?", text, re.I):
        lines.append(
            "`/` 返回 404 不能证明后端故障；Klonet 后端以 "
            "`/server_health/` 为健康判据。"
        )
    if "screen" in text and any(marker in text for marker in ("没有", "不存在", "没启动")):
        lines.append("Screen 只是启动方式证据，不是后端健康的必要条件。")
    lines.append(
        "实例 `%s`（%s）后端状态：%s。"
        % (item.platform, item.project_root, item.backend_status)
    )
    for component, label in (
        ("master", "Master"), ("worker", "Worker"),
        ("celery", "Celery"), ("web_terminal", "Web Terminal"),
    ):
        port = item.configured_ports.get(component + "_port")
        suffix = "（端口 %s）" % port if port is not None else ""
        lines.append("- %s%s：%s" % (label, suffix, item.component_state(component)))
    return "\n".join(lines)


def _render_inventory_instance(item: RuntimeInstance) -> str:
    status = "正常" if item.backend_status == "healthy" else "异常"
    lines = [
        "- `%s`：%s" % (item.platform, status),
        "  - 项目目录：`%s`" % item.project_root,
    ]
    if item.roles:
        lines.append(
            "  - 已检测组件：%s"
            % "、".join(_role_label(role) for role in item.roles)
        )
    if item.missing_roles:
        lines.append(
            "  - 未检测到：%s"
            % "、".join(_role_label(role) for role in item.missing_roles)
        )
    for role, label in (("master", "Master"), ("worker", "Worker")):
        port = item.configured_ports.get(role + "_port")
        endpoint = item.endpoints.get(role, "unknown")
        if port is None and role not in item.roles and role not in item.missing_roles:
            continue
        port_text = "，端口 %s" % port if port is not None else ""
        lines.append(
            "  - %s：%s%s" % (label, _endpoint_label(endpoint), port_text)
        )
    return "\n".join(lines)


def _role_label(role: str) -> str:
    return {
        "master": "Master",
        "worker": "Worker",
        "celery": "Celery",
        "web_terminal": "Web Terminal",
        "topology_store": "Topology Store",
    }.get(str(role or ""), str(role or "未知组件"))


def _endpoint_label(status: str) -> str:
    return {
        "healthy": "健康检查通过",
        "unreachable": "健康接口不可达",
        "not_checked": "未检测到运行进程",
        "unknown": "状态未知",
    }.get(str(status or ""), "状态未知")
