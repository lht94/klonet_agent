"""Grounded context assembled before privileged planning."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from klonet_agent.config import (
    KLONET_UPSTREAM_SOURCE_ROOT,
)
from klonet_agent.knowledge.rag import KNOWLEDGE_BASE
from klonet_agent.ops.actions import (
    OpsActionRegistry,
    configured_ops_action_registry,
)
from klonet_agent.ops.privileged.action_runner import (
    DIRECT_PRIVILEGED_ACTIONS,
)
from klonet_agent.ops.privileged.environment_facts import (
    EnvironmentFactCollector,
    UnifiedEnvironmentFacts,
)
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.planner_schema import normalize_semantic_risk
from klonet_agent.tools.environment import (
    inspect_install_scripts,
    inspect_klonet_runtime,
    inspect_nginx_routes,
    inspect_platform_instances,
    inspect_platform_health,
    inspect_process_detail,
    inspect_screen_session,
    inspect_service_health,
    inspect_system_environment,
    read_klonet_logs,
    read_ops_file,
)


_EXCLUDED_PLANNER_ACTIONS: set[str] = set()

_RECOVERY_PROBES = {
    spec.name: spec.handler for spec in DEFAULT_READONLY_PROBES.describe()
}


def _omit_knowledge_absolute_paths(value: str) -> str:
    """Prevent documentation paths from becoming frozen server resources."""

    return re.sub(
        r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9._~+@-]+/)*[A-Za-z0-9._~+@-]+/?",
        "[knowledge-path omitted; resolve from server evidence]",
        str(value or ""),
    )


@dataclass(frozen=True)
class GroundedPlanContext:
    knowledge_evidence: str
    environment_evidence: str
    action_catalog: str
    knowledge_status: str = "available"
    environment_status: str = "available"
    facts: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        environment_model = self.facts.get("environment_model")
        model_section = ""
        if environment_model:
            model_section = (
                "## Structured environment facts (authoritative; secrets are metadata only)\n"
                f"{json.dumps(environment_model, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
            )
        return (
            "## Klonet knowledge evidence (procedural only; absolute paths are"
            " not host facts)\n"
            f"{_omit_knowledge_absolute_paths(self.knowledge_evidence)}\n\n"
            f"{model_section}"
            "## Registered read-only probes\n"
            f"{DEFAULT_READONLY_PROBES.render()}\n\n"
            "## Read-only server evidence\n"
            f"{self.environment_evidence}\n\n"
            "## Available execution capability summary\n"
            "This summary describes feasibility only. The semantic Planner must"
            " not select implementation names.\n"
            f"{self.action_catalog}"
        )
    def audit_summary(self) -> dict[str, Any]:
        summary = {
            "knowledge_status": self.knowledge_status,
            "environment_status": self.environment_status,
            "context_policy": (
                "knowledge+readonly_environment+execution_capability_summary"
            ),
        }
        fingerprint = self.facts.get("environment_fingerprint")
        if fingerprint:
            summary["environment_fingerprint"] = str(fingerprint)
            summary["environment_schema_version"] = 1
        return summary

    def planning_blocker(self) -> str:
        if self.knowledge_status != "available":
            return "Klonet 知识检索当前不可用"
        if self.environment_status != "available":
            return "服务器只读环境探测当前不可用"
        if (
            "未检索到可靠 Klonet 证据" in self.knowledge_evidence
            or "未执行 Klonet RAG" in self.knowledge_evidence
        ):
            return "没有检索到足以支撑操作计划的 Klonet 证据"
        return ""


class PrivilegedPlanContextBuilder:
    """Collect Klonet RAG and bounded read-only host facts before planning."""

    def __init__(
        self,
        *,
        knowledge_search: Callable[..., str] | None = None,
        environment_inspector: Callable[[dict], str] | None = None,
        fact_collector: EnvironmentFactCollector | None = None,
        action_registry: OpsActionRegistry | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.knowledge_search = knowledge_search or KNOWLEDGE_BASE.search_knowledge
        self.environment_inspector = (
            environment_inspector or self._inspect_environment
        )
        self.fact_collector = fact_collector or EnvironmentFactCollector()
        self.action_registry = action_registry or configured_ops_action_registry()
        self.on_progress = on_progress
        self._probe_cache: dict[str, str] | None = None
        self._discovered_project_roots: list[str] = []

    def begin_probe_session(self) -> None:
        """Cache identical read-only probes only within one planning submission."""

        self._probe_cache = {}
        self._discovered_project_roots = []

    def end_probe_session(self) -> None:
        self._probe_cache = None
        self._discovered_project_roots = []

    def build(
        self,
        goal: str,
        *,
        supplemental_environment_context: str = "",
    ) -> GroundedPlanContext:
        knowledge_status = "available"
        environment_status = "available"
        knowledge_query = str(goal or "").strip()
        if supplemental_environment_context:
            knowledge_query += (
                "\n\nCurrent read-only/failure evidence:\n"
                + _bounded(supplemental_environment_context, 6000)
            )
        self._progress("证据节点 1/3：正在检索 Klonet 知识库…")
        try:
            knowledge = self.knowledge_search(
                knowledge_query,
                top_k=6,
                task_type=_knowledge_task_type(goal),
            )
            self._progress("关键证据：Klonet 知识检索完成。")
        except Exception as exc:
            knowledge_status = "unavailable"
            knowledge = "Klonet knowledge retrieval unavailable: %s" % _one_line(exc)
            self._progress("关键证据：Klonet 知识检索不可用。")

        self._progress("证据节点 2/3：正在识别工程布局和服务配置…")
        project_roots = self._candidate_project_roots()
        environment_facts: UnifiedEnvironmentFacts
        try:
            environment_facts = self.fact_collector.collect(project_roots)
        except Exception:
            environment_facts = UnifiedEnvironmentFacts(
                warnings=("structured_environment_collection_unavailable",)
            )
        self._progress(_environment_fact_progress(environment_facts))
        self._progress("证据节点 3/3：正在执行服务器只读状态检查…")
        try:
            environment = self.environment_inspector(
                {"project_roots": project_roots}
            )
            self._progress("关键证据：服务器只读状态检查完成。")
        except Exception as exc:
            environment_status = "unavailable"
            environment = "Read-only environment inspection unavailable: %s" % _one_line(exc)
            self._progress("关键证据：服务器只读状态检查不可用。")
        if supplemental_environment_context:
            environment += (
                "\n\n## Supplemental caller evidence\n"
                + str(supplemental_environment_context).strip()
            )

        return GroundedPlanContext(
            knowledge_evidence=_bounded(knowledge, 18000),
            environment_evidence=_bounded(environment, 12000),
            action_catalog=self._action_catalog(),
            knowledge_status=knowledge_status,
            environment_status=environment_status,
            facts={
                "candidate_project_roots": project_roots,
                "environment_model": environment_facts.to_dict(),
                "environment_fingerprint": environment_facts.fingerprint,
            },
        )

    def _progress(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def _action_catalog(self) -> str:
        lines = [
            "An independent Execution Agent may use registered capabilities or"
            " propose a separately confirmed one-time shell artifact."
        ]
        for spec in self.action_registry.describe():
            if (
                spec.name in _EXCLUDED_PLANNER_ACTIONS
                or spec.name not in DIRECT_PRIVILEGED_ACTIONS
            ):
                continue
            parts = [
                f"- category={spec.category}",
                "risk=%s" % normalize_semantic_risk(spec.risk, "medium"),
                f"description={spec.description or spec.category}",
            ]
            if spec.preconditions:
                parts.append("preconditions=" + ",".join(spec.preconditions))
            if spec.postconditions:
                parts.append("postconditions=" + ",".join(spec.postconditions))
            lines.append(" ".join(parts))
        return "\n".join(dict.fromkeys(lines))

    @staticmethod
    def recovery_probe_catalog() -> str:
        return DEFAULT_READONLY_PROBES.render()

    def current_environment_fingerprint(self) -> str:
        facts = self.fact_collector.collect(
            self._candidate_project_roots()
        )
        return facts.fingerprint

    def run_recovery_diagnostics(
        self,
        requests: list[dict[str, Any]],
    ) -> str:
        """Execute only named, read-only probes requested by recovery analysis."""

        roots = [Path(item).resolve() for item in self._candidate_project_roots()]
        sections = []
        for index, request in enumerate(requests[:8], start=1):
            if not isinstance(request, dict):
                continue
            name = str(request.get("probe") or "").strip()
            purpose = _one_line(request.get("purpose"))
            args = request.get("args")
            args = dict(args) if isinstance(args, dict) else {}
            probe = _RECOVERY_PROBES.get(name)
            if probe is None:
                sections.append(
                    "probe_%s name=%s status=refused reason=probe_not_allowlisted"
                    % (index, name or "missing")
                )
                continue
            visible_purpose = _localized_probe_purpose(name, args, purpose)
            self._progress(
                "只读补证 %s/%s：正在检查 %s%s…"
                % (
                    index,
                    min(len(requests), 8),
                    name,
                    "（%s）" % visible_purpose if visible_purpose else "",
                )
            )
            path_problem = _recovery_probe_path_problem(name, args, roots)
            if path_problem:
                sections.append(
                    "probe_%s name=%s status=refused reason=%s"
                    % (index, name, path_problem)
                )
                continue
            cache_key = json.dumps(
                {"probe": name, "args": args},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if self._probe_cache is not None and cache_key in self._probe_cache:
                result = self._probe_cache[cache_key]
                self._progress("关键证据：复用本次规划中已取得的 %s 检查结果。" % name)
            else:
                try:
                    result = probe(args)
                except Exception as exc:
                    result = "probe unavailable: %s" % _one_line(exc)
                if self._probe_cache is not None:
                    self._probe_cache[cache_key] = str(result)
            self._learn_project_roots_from_probe(name, str(result))
            roots = [Path(item).resolve() for item in self._candidate_project_roots()]
            # Batch inventories are typed domain evidence consumed by multiple
            # workflow stages.  Truncating them can preserve the declared
            # counts while dropping later instance/code-only rows, producing a
            # self-contradictory result.  LLM prompts are bounded separately;
            # the canonical EvidenceRecord must remain complete.
            result_limit = (
                200000
                if name in {"running_platforms", "platform_instances"}
                else 7000
            )
            sections.append(
                "## recovery_probe_%s name=%s purpose=%s\n%s"
                % (
                    index, name, purpose or "补充失败诊断证据",
                    _bounded(result, result_limit),
                )
            )
        if not sections:
            return "No adaptive recovery probes were requested."
        self._progress("关键证据：本轮只读补证完成。")
        return "\n\n".join(sections)

    def _candidate_project_roots(self) -> list[str]:
        roots = []
        # The bundled knowledge/klonet_source tree is a searchable source
        # snapshot, not a deployment target. Only the configured upstream
        # source root is advertised as a candidate; running instances are
        # discovered independently from process/screen evidence.
        for candidate in (KLONET_UPSTREAM_SOURCE_ROOT,):
            path = Path(candidate).expanduser()
            if path.exists() and path.is_dir():
                resolved = str(path.resolve())
                if resolved not in roots:
                    roots.append(resolved)
        for candidate in self._discovered_project_roots:
            if candidate not in roots:
                roots.append(candidate)
        return roots

    def _learn_project_roots_from_probe(self, probe: str, output: str) -> None:
        if probe not in {
            "running_platforms", "platform_instances", "platform_health",
            "screen", "screen_session", "process", "process_detail",
            "process_tree", "process_logs", "git_repository", "project_layout",
        }:
            return
        candidates: list[str] = []
        text = str(output or "")
        for pattern in (
            r"\bproject_root=(/[^\s,]+)",
            r"\bgit_root(?:s)?=(/[^\s,]+)",
            r"\brepository=(/[^\s,]+)",
            r"\bcwd=(/[^\s,]+)",
        ):
            candidates.extend(re.findall(pattern, text))
        for raw in candidates:
            try:
                path = Path(raw).expanduser().resolve()
            except OSError:
                continue
            if not path.is_dir():
                continue
            if path.name == "mains" and path.parent.is_dir():
                path = path.parent
            value = str(path)
            if value not in self._discovered_project_roots:
                self._discovered_project_roots.append(value)

    @staticmethod
    def _inspect_environment(args: dict) -> str:
        roots = list(args.get("project_roots") or [])
        sections = [
            inspect_system_environment(
                {
                    "checks": [
                        "os",
                        "python",
                        "disk",
                        "command_paths",
                    ],
                    "commands": [
                        "python",
                        "screen",
                        "docker",
                        "nginx",
                        "redis-server",
                    ],
                }
            ),
            inspect_platform_instances({"project_roots": roots}),
            inspect_service_health({}),
        ]
        if roots:
            sections.append(
                "candidate_project_roots:\n"
                + "\n".join("- %s" % root for root in roots)
            )
        return "\n\n".join(sections)


def _knowledge_task_type(goal: str) -> str:
    lowered = str(goal or "").lower()
    recovery_markers = (
        "failure packet",
        "失败",
        "故障",
        "异常",
        "报错",
        "修复",
        "恢复",
        "诊断",
        "排查",
        "failed",
        "error",
        "recover",
        "diagnose",
        "troubleshoot",
    )
    return (
        "troubleshooting"
        if any(marker in lowered for marker in recovery_markers)
        else "deployment"
    )


def _environment_fact_progress(facts: UnifiedEnvironmentFacts) -> str:
    """Render a concise, secret-free evidence summary for interactive progress."""

    projects = []
    for item in facts.projects[:6]:
        name = Path(item.backend_package_root).name or Path(item.platform_root).name
        projects.append("%s:%s" % (name, item.readiness))
    services = []
    for label, ports in (
        ("Redis", facts.redis.ports),
        ("MySQL", facts.mysql.ports),
        ("RabbitMQ", facts.rabbitmq.ports),
    ):
        if ports:
            services.append("%s=%s" % (label, ",".join(str(port) for port in ports)))
    capabilities = [
        name
        for name, available in (
            ("nginx", bool(facts.nginx.binary)),
            ("docker", bool(facts.capabilities.docker_binary)),
            ("libvirt", bool(facts.capabilities.libvirt_binary)),
            ("ovs", bool(facts.capabilities.ovs_binary)),
            ("screen", bool(facts.capabilities.screen_binary)),
            ("kvm", facts.capabilities.kvm_device_present),
        )
        if available
    ]
    return (
        "关键证据：工程布局=%s；服务端口=%s；可用组件=%s。"
        % (
            "、".join(projects) if projects else "未识别",
            "、".join(services) if services else "未识别",
            "、".join(capabilities) if capabilities else "未识别",
        )
    )


def _localized_probe_purpose(
    probe: str,
    args: dict[str, Any],
    model_purpose: str,
) -> str:
    """Render a stable Chinese purpose instead of leaking model language."""

    purposes = {
        "running_platforms": "按项目根目录核验后端角色、端口和健康接口",
        "platform_instances": "发现并区分服务器上的平台实例根目录",
        "platform_health": "核验目标实例的后端健康状态",
        "screen": "核对 Screen 会话及其运行目录",
        "screen_session": "核对指定 Screen 会话和进程",
        "process": "核对目标进程的 PID、命令和工作目录",
        "process_detail": "核对端口或 PID 对应的进程归属",
        "process_tree": "核对目标进程树和进程组",
        "process_logs": "读取目标进程所属实例的日志",
        "ports": "核对目标端口监听状态和进程归属",
        "port_owner": "核对监听端口的 PID 和工作目录",
        "project_layout": "确认项目布局、入口文件和运行目录",
        "git_repository": "确认目标项目的 Git 根目录和版本状态",
        "klonet_config_consistency": "确认活动配置类和后端端口",
        "ops_file": "读取并脱敏检查目标运维文件",
        "logs": "读取并脱敏检查目标日志",
        "python_import": "验证目标解释器和模块导入路径",
        "http_endpoint": "验证目标 HTTP 健康接口",
        "path_permissions": "核对目标路径的属主和权限",
        "privilege_capabilities": "确认本会话可用的受控权限通道",
    }
    if probe in purposes:
        return purposes[probe]
    # Unknown probes still get a Chinese boundary; the model text remains in
    # trace/evidence but never becomes user-facing progress.
    return "收集该检查项所需的只读事实"


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... grounded context truncated ..."


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())[:300]


def _recovery_probe_path_problem(
    probe: str,
    args: dict[str, Any],
    roots: list[Path],
) -> str:
    path_fields = {
        "ops_file": ("path",),
        "logs": ("path",),
        "platform_health": ("project_root",),
        "install_scripts": ("script_dir",),
        "project_layout": ("project_roots",),
        "git_repository": ("repository",),
        "python_import": ("python_executable", "cwd"),
        "path_permissions": ("paths",),
        "process_logs": ("project_root",),
        "file_integrity": ("paths",),
        "json_file": ("path",),
        "archive_inventory": ("path",),
        "klonet_config_consistency": ("project_root",),
    }.get(probe, ())
    for field in path_fields:
        raw_value = args.get(field)
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for item in values:
            raw = str(item or "").strip()
            if not raw:
                continue
            try:
                resolved = Path(raw).expanduser().resolve()
            except OSError:
                return "invalid_path"
            if roots and not any(
                resolved == root
                or root in resolved.parents
                or resolved in root.parents
                for root in roots
            ):
                return "path_outside_grounded_project_roots"
    return ""
