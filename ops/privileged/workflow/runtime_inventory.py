"""Typed runtime inventory shared by Discovery, Verifier, Planner and Response.

The running-platform probe is a machine-readable domain record.  Parsing it in
one place prevents each workflow stage from independently guessing instance
identity, counts, ports and component state from truncated prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
class RuntimeInstance:
    platform: str
    project_root: str
    backend_status: str
    roles: tuple[str, ...] = ()
    missing_roles: tuple[str, ...] = ()
    pids: tuple[int, ...] = ()
    configured_ports: dict[str, int] = field(default_factory=dict)
    endpoints: dict[str, str] = field(default_factory=dict)
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
            fields=fields,
            raw_line=line.strip(),
            evidence_id=evidence_id,
        )

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

    if not inventory.complete or not looks_like_runtime_goal(goal):
        return False
    text = str(goal or "").lower()
    if any(marker in text for marker in ("多少", "几个", "数量", "哪些", "how many", "which")):
        return True
    targets = inventory.matching(goal)
    if not targets:
        return False
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
    if any(marker in text for marker in ("多少", "几个", "数量", "哪些", "how many", "which")):
        lines = ["正常运行实例（%s）：" % len(inventory.healthy)]
        lines.extend(_render_instance(item) for item in inventory.healthy)
        if not inventory.healthy:
            lines.append("- 无")
        lines.append("后端异常的运行候选（%s）：" % len(inventory.abnormal))
        lines.extend(_render_instance(item) for item in inventory.abnormal)
        if not inventory.abnormal:
            lines.append("- 无")
        lines.append("只有代码、没有后端运行证据的目录（%s）：" % len(inventory.code_only_roots))
        lines.extend("- %s" % root for root in inventory.code_only_roots)
        if not inventory.code_only_roots:
            lines.append("- 无")
        return "\n".join(lines)
    if len(targets) > 1:
        lines = ["这些根目录对应独立平台实例，不会按名称合并："]
        lines.extend(_render_instance(item) for item in targets)
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


def _render_instance(item: RuntimeInstance) -> str:
    return (
        "- project_root=%s；platform=%s；backend_status=%s；"
        "master_port=%s，master_endpoint=%s；worker_port=%s，worker_endpoint=%s"
        % (
            item.project_root, item.platform, item.backend_status,
            item.configured_ports.get("master_port", "unknown"),
            item.endpoints.get("master", "unknown"),
            item.configured_ports.get("worker_port", "unknown"),
            item.endpoints.get("worker", "unknown"),
        )
    )
