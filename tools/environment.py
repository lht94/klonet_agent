"""Read-only environment inspection tools for Klonet operations diagnosis."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence


MAX_LOG_CHARS = 8000
MAX_SCREEN_CHARS = 8000
PROBE_TIMEOUT_SECONDS = 5
STATUS_DETECTED = "detected"
STATUS_MISSING = "missing"
STATUS_UNCHECKED = "unchecked"
OPS_BASELINE_CHECKS = (
    "os_release",
    "kernel",
    "arch",
    "cpu",
    "memory",
    "disk",
    "virtualization",
    "python",
    "system_python",
    "rust",
    "docker_version",
    "compose_version",
    "ovs",
    "kvm",
    "libvirt",
)
OPS_RUNTIME_CHECKS = (
    "ports",
    "services",
    "screen",
    "processes",
    "docker_containers",
    "docker_images",
    "docker_networks",
    "redis",
    "mysql",
    "rabbitmq",
    "nginx",
)
OPS_SERVICE_HEALTH_CHECKS = (
    "docker_containers",
    "redis",
    "mysql",
    "rabbitmq",
    "nginx",
)
INSTALL_SCRIPT_ALLOWLIST = {
    "base_requ_setup.sh": ("NORMAL",),
    "docker_service.sh": (),
}
INSTALL_SCRIPT_RISK_MARKERS = (
    "apt-get",
    "yum",
    "docker",
    "systemctl",
    "service ",
    "redis-server",
    "mysql",
    "rabbitmq",
    "ovs-",
    "modprobe",
)
OPS_CONTEXT_SECTIONS = ("baseline", "runtime", "assets")
DEPLOYMENT_ASSET_NAMES = {
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "dockerfile",
    "gun.py",
    "worker_gun.py",
    "master_main.py",
    "worker_main.py",
    "web_terminal_main.py",
    "celery_worker.py",
    "config.py",
    "nginx.conf",
}
DEPLOYMENT_ASSET_SUFFIXES = {".service", ".conf", ".ini", ".yml", ".yaml", ".toml"}
KLONET_PORT_KEYS = (
    "master_port",
    "worker_port",
    "public_port",
    "web_terminal_port",
)

_SENSITIVE_NAME_PARTS = (
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "private_key",
    "secret",
    "token",
    "credential",
    "password",
)
_SAFE_LOG_SUFFIXES = {".log", ".txt", ".out", ".err"}
_SAFE_OPS_FILE_SUFFIXES = {
    ".py",
    ".conf",
    ".cfg",
    ".ini",
    ".json",
    ".js",
    ".service",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_SAFE_SCREEN_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SAFE_PLATFORM_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SAFE_SERVER_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_SAFE_FRONTEND_ALIAS = re.compile(r"^/[A-Za-z0-9_./:-]{1,120}$")
_SAFE_COMMAND_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")
_SAFE_REGISTRY_ENDPOINT = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)((?:[A-Za-z0-9_.]+\.)?config\[\s*['\"][^'\"]*(?:password|passwd|pwd|api[_-]?key|secret|token)[^'\"]*['\"]\s*\]\s*=\s*)([^\r\n]+)"
    ),
    re.compile(
        r"(?i)((?:['\"]?[A-Za-z0-9_-]*(?:password|passwd|pwd|api[_-]?key|secret|token)[A-Za-z0-9_-]*['\"]?\s*[:=]\s*))([^\r\n,}]+)"
    ),
    re.compile(
        r"(?i)((?:['\"]?authorization['\"]?\s*:\s*['\"]?\s*bearer\s+))([^'\"\s,}]+)"
    ),
    re.compile(
        r"(?i)\b([A-Za-z0-9_-]*(?:password|passwd|pwd|api[_-]?key|secret|token)[A-Za-z0-9_-]*)\s*[:=]\s*([^\s]+)"
    ),
    re.compile(
        r"(?i)(--(?:password|passwd|pwd|api-key|api_key|secret|token)(?:=|\s+))([^\s]+)"
    ),
    re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+([^\s]+)"),
    re.compile(r"(?i)\b(cookie\s*:\s*)(.+)$", re.MULTILINE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


@dataclass(frozen=True)
class ProbeResult:
    """A single read-only environment check result."""

    name: str
    status: str
    detail: str

    def render(self) -> str:
        return f"- {self.name}: {self.status} - {self.detail}"


def inspect_system_environment(args: Optional[dict] = None) -> str:
    """Inspect basic local system facts without modifying the host."""

    requested = _requested_checks(args, default=("os", "python", "disk", "virtualization"))
    results = []
    if "os" in requested:
        results.append(
            ProbeResult(
                "os",
                STATUS_DETECTED,
                redact_sensitive_text(
                    f"{platform.system()} {platform.release()} "
                    f"{platform.machine()} ({platform.platform()})"
                ),
            )
        )
    if "python" in requested:
        results.append(ProbeResult("python", STATUS_DETECTED, platform.python_version()))
    if "system_python" in requested:
        results.append(run_read_only_probe("system_python"))
    if "command_paths" in requested:
        results.append(_inspect_command_paths(args or {}))
    if "disk" in requested:
        results.append(_disk_usage_probe())
    if "virtualization" in requested:
        results.append(run_read_only_probe("virtualization"))
    return _render_tool_result("inspect_system_environment", results)


def inspect_klonet_runtime(args: Optional[dict] = None) -> str:
    """Inspect local runtime hints relevant to Klonet troubleshooting."""

    args = args or {}
    requested = _requested_checks(
        args,
        default=(
            "ports",
            "screen",
            "processes",
            "nginx",
            "docker",
            "redis",
            "rabbitmq",
            "mysql",
        ),
    )
    results = []
    for check in requested:
        if check == "port_owner":
            results.extend(_inspect_port_owners(args))
        elif check == "process_details":
            results.extend(_inspect_process_details(args))
        else:
            results.append(run_read_only_probe(check))
    return _render_tool_result("inspect_klonet_runtime", results)


def inspect_process_detail(args: Optional[dict] = None) -> str:
    """Inspect precise process ownership evidence for ports, PIDs or keywords."""

    args = args or {}
    results = []
    if args.get("ports"):
        results.extend(_inspect_port_owners(args))
    if args.get("pids") or args.get("process_keywords"):
        results.extend(_inspect_process_details(args))
    if not results:
        results.append(
            ProbeResult(
                "process_detail",
                STATUS_UNCHECKED,
                "ports, pids or process_keywords is required",
            )
        )
    return _render_tool_result("inspect_process_detail", results)


def inspect_ops_context(args: Optional[dict] = None) -> str:
    """Collect Ops baseline, runtime and deployment-asset context in one pass."""

    args = args or {}
    sections = _requested_sections(args)
    lines = ["inspect_ops_context"]
    if "baseline" in sections:
        lines.append("## baseline")
        lines.extend(result.render() for result in _ops_probe_many(OPS_BASELINE_CHECKS))
    if "runtime" in sections:
        lines.append("## runtime")
        lines.extend(result.render() for result in _ops_probe_many(OPS_RUNTIME_CHECKS))
    if "assets" in sections:
        lines.append("## assets")
        lines.extend(_scan_deployment_assets(args))
    return "\n".join(lines)


def inspect_service_health(args: Optional[dict] = None) -> str:
    """Summarize shared service health and reuse/start guidance."""

    args = args or {}
    requested = _requested_service_health_checks(args)
    results = [run_read_only_probe(service) for service in requested]
    lines = ["inspect_service_health"]
    has_reusable = False
    has_missing = False
    has_unchecked = False
    for result in results:
        recommendation = _service_health_recommendation(result)
        if recommendation == "reuse":
            has_reusable = True
        elif recommendation == "start_candidate":
            has_missing = True
        else:
            has_unchecked = True
        lines.append(
            (
                f"- service={result.name} "
                f"status={result.status} "
                f"recommendation={recommendation} "
                f"evidence={_single_line(result.detail, max_chars=420)}"
            )
        )
    if has_reusable and not has_missing:
        lines.append("docker_service_action=skip")
    else:
        lines.append("docker_service_action=inspect_before_run")
    if has_missing:
        lines.append("missing_services_require_plan=true")
    if has_unchecked:
        lines.append("unchecked_services_require_more_evidence=true")
    lines.append("environment unchanged")
    return "\n".join(lines)


def inspect_install_scripts(args: Optional[dict] = None) -> str:
    """Inspect allowlisted Klonet install scripts without executing them."""

    args = args or {}
    raw_dir = str(args.get("script_dir") or "").strip()
    if not raw_dir:
        return "Error: script_dir is required"
    script_dir = Path(raw_dir).expanduser()
    if _is_sensitive_path(script_dir):
        return "\n".join(
            ["inspect_install_scripts", f"refused_sensitive_path={script_dir.name}", "environment unchanged"]
        )
    if not script_dir.exists() or not script_dir.is_dir():
        return "\n".join(["inspect_install_scripts", f"script_dir_missing={raw_dir}", "environment unchanged"])
    raw_scripts = args.get("scripts")
    if isinstance(raw_scripts, list) and raw_scripts:
        scripts = [
            str(item).strip()
            for item in raw_scripts
            if str(item).strip() in INSTALL_SCRIPT_ALLOWLIST
        ]
    else:
        scripts = list(INSTALL_SCRIPT_ALLOWLIST)
    if not scripts:
        scripts = list(INSTALL_SCRIPT_ALLOWLIST)
    lines = [
        "inspect_install_scripts",
        f"script_dir={script_dir.resolve()}",
    ]
    blocked = False
    for script_name in scripts:
        path = script_dir / script_name
        if not path.is_file():
            blocked = True
            lines.append(f"- script={script_name} status=missing recommendation=do_not_bind_recipe")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            blocked = True
            lines.append(
                f"- script={script_name} status=unchecked error={_single_line(str(exc), max_chars=220)}"
            )
            continue
        shebang = _script_shebang(text)
        executable = _is_executable_file(path)
        risk_markers = _script_risk_markers(text)
        allowed_args = INSTALL_SCRIPT_ALLOWLIST[script_name]
        lines.append(
            (
                f"- script={script_name} status=detected "
                f"executable={str(executable).lower()} "
                f"shebang={shebang or 'missing'} "
                f"recommended_recipe=run_install_script "
                f"allowed_args={','.join(allowed_args) if allowed_args else 'none'} "
                f"risk_markers={','.join(risk_markers) if risk_markers else 'none'}"
            )
        )
    lines.append(f"preflight_status={'blocked' if blocked else 'ready'}")
    lines.append("environment unchanged")
    return "\n".join(lines)


def render_klonet_config(args: Optional[dict] = None) -> str:
    """Render Klonet deployment config drafts without writing files."""

    args = args or {}
    platform_name = str(args.get("platform") or "").strip()
    server_name = str(args.get("server_name") or "_").strip()
    frontend_alias = _normalize_frontend_alias(str(args.get("frontend_alias") or "/VEMU2/").strip())
    frontend_path = _normalize_frontend_path(str(args.get("frontend_path") or "").strip())
    master_port = _safe_port(args.get("master_port"))
    worker_port = _safe_port(args.get("worker_port"))
    public_port = _safe_port(args.get("public_port"))
    terminal_port = _safe_port(args.get("terminal_port") or args.get("web_terminal_port"))
    frontend_config_path = str(args.get("frontend_config_path") or "").strip()

    problem = _validate_render_config_inputs(
        platform_name,
        server_name,
        master_port,
        worker_port,
        public_port,
        terminal_port,
        frontend_alias,
        frontend_path,
    )
    if problem:
        return "\n".join(["render_klonet_config", problem, "environment unchanged"])

    frontend_source_path = ""
    frontend_source_text = ""
    if frontend_config_path:
        frontend_source_path, frontend_source_text, frontend_problem = _load_frontend_config_source(
            frontend_config_path
        )
        if frontend_problem:
            return "\n".join(["render_klonet_config", frontend_problem, "environment unchanged"])

    nginx_block = _render_nginx_server_block(
        server_name=server_name,
        master_port=master_port,
        public_port=public_port,
        frontend_alias=frontend_alias,
        frontend_path=frontend_path,
    )
    frontend_config = _render_frontend_config_js(
        server_name=server_name,
        public_port=public_port,
        terminal_port=terminal_port,
        source_text=frontend_source_text,
    )
    backend_config = _render_backend_config_py(
        master_port=master_port,
        worker_port=worker_port,
        public_port=public_port,
        terminal_port=terminal_port,
    )
    web_terminal_hint = _render_web_terminal_main_patch_hint(terminal_port)
    lines = [
        "render_klonet_config",
        f"platform={platform_name}",
        "template_status=draft",
        "environment unchanged",
        "next_recipes=write_ops_file,reload_nginx",
        "## nginx_server_block",
        nginx_block,
        "## backend_config_py",
        backend_config,
        "## web_terminal_main_py_patch_hint",
        web_terminal_hint,
    ]
    if frontend_source_path:
        lines.extend(
            [
                "## frontend_config_js_patch_draft",
                f"frontend_config_source={frontend_source_path}",
                frontend_config,
            ]
        )
    else:
        lines.extend(["## frontend_config_js", frontend_config])
    return "\n".join(lines)


def inspect_frontend_config(args: Optional[dict] = None) -> str:
    """Validate frontend config.js and optional Nginx alias evidence."""

    args = args or {}
    raw_path = str(args.get("frontend_config_path") or "").strip()
    lines = ["inspect_frontend_config"]
    blockers = []
    unchecked = []
    if not raw_path:
        return "\n".join(["inspect_frontend_config", "frontend_config_status=missing", "environment unchanged"])

    source_path, source_text, problem = _load_frontend_config_source(raw_path)
    if problem:
        status = "missing" if problem.startswith("frontend_config_missing") else "blocked"
        return "\n".join(
            [
                "inspect_frontend_config",
                problem,
                f"frontend_config_status={status}",
                f"overall_status={status}",
                "environment unchanged",
            ]
        )

    lines.append(f"frontend_config_source={source_path}")
    expected = _frontend_expected_values(args)
    assignments = _parse_frontend_assignments(source_text)
    matches = []
    mismatches = []
    for assignment in assignments:
        kind = _frontend_assignment_kind(assignment["name"])
        if kind not in expected:
            continue
        actual = assignment["value"]
        wanted = expected[kind]
        status = "matched" if actual == wanted else "mismatch"
        if status == "matched":
            matches.append(assignment["name"])
        else:
            mismatches.append(assignment["name"])
        lines.append(
            f"field={assignment['name']} actual={actual} expected={wanted} status={status}"
        )
    missing_kinds = sorted(set(expected) - {_frontend_assignment_kind(item["name"]) for item in assignments})
    if mismatches:
        blockers.append("frontend_config")
        lines.append("frontend_config_status=blocked")
    elif missing_kinds:
        unchecked.append("frontend_config")
        lines.append("frontend_config_status=unchecked missing_expected=" + ",".join(missing_kinds))
    elif matches:
        lines.append("frontend_config_status=aligned")
    else:
        unchecked.append("frontend_config")
        lines.append("frontend_config_status=unchecked")

    nginx_status = _frontend_nginx_alias_status(args)
    if nginx_status:
        lines.append(nginx_status)
        if nginx_status.endswith("mismatch") or nginx_status.endswith("missing"):
            blockers.append("nginx_alias")
        elif nginx_status.endswith("unchecked"):
            unchecked.append("nginx_alias")

    if blockers:
        overall = "blocked"
    elif unchecked:
        overall = "unchecked"
    else:
        overall = "ready"
    lines.append(f"overall_status={overall}")
    lines.append("environment unchanged")
    return "\n".join(lines)


def inspect_platform_instances(args: Optional[dict] = None) -> str:
    """Inspect running Klonet-like platform instances from screen, processes and configs."""

    args = args or {}
    instances = {}
    evidence = []
    for row in _screen_instance_rows():
        entry = _platform_entry(instances, row["platform"])
        entry["roles"].add(row["role"])
        entry["screen_sessions"].append(row["session"])
        entry["sources"].add("screen")
    for row in _process_instance_rows():
        entry = _platform_entry(instances, row["platform"])
        entry["roles"].add(row["role"])
        entry["pids"].append(row["pid"])
        if row["cwd"] and row["cwd"] != "?":
            entry["project_roots"].add(row["cwd"])
        entry["sources"].add("process")
    for root in _requested_project_roots(args):
        platform_name = _platform_from_project_root(root)
        entry = _platform_entry(instances, platform_name)
        entry["project_roots"].add(str(root))
        entry["sources"].add("config")
        ports = _read_config_ports_from_root(root)
        entry["ports"].update(ports)
    max_instances = _safe_int(args.get("max_instances"), 50)
    lines = ["inspect_platform_instances"]
    if not instances:
        return _render_tool_result(
            "inspect_platform_instances",
            [ProbeResult("platform_instances", STATUS_MISSING, "no screen/process/config evidence found")],
        )
    for name in sorted(instances)[:max_instances]:
        entry = instances[name]
        detail_parts = [
            f"platform={name}",
            f"source={','.join(sorted(entry['sources'])) or 'unchecked'}",
            f"roles={','.join(sorted(entry['roles'])) or 'unchecked'}",
        ]
        if entry["screen_sessions"]:
            detail_parts.append("screen_sessions=" + ",".join(sorted(entry["screen_sessions"])))
        if entry["pids"]:
            detail_parts.append("pids=" + ",".join(str(pid) for pid in sorted(set(entry["pids"]))))
        if entry["project_roots"]:
            detail_parts.append("project_roots=" + ",".join(sorted(entry["project_roots"])))
        if entry["ports"]:
            detail_parts.append(
                "ports="
                + ",".join(f"{key}:{entry['ports'][key]}" for key in _ordered_ports(entry["ports"]))
            )
        evidence.append(ProbeResult("platform_instance", STATUS_DETECTED, " ".join(detail_parts)))
    lines.extend(item.render() for item in evidence)
    return "\n".join(lines)


def inspect_platform_health(args: Optional[dict] = None) -> str:
    """Verify a Klonet platform after start/restart without modifying the host."""

    args = args or {}
    raw_platform = str(args.get("platform") or "").strip()
    raw_root = str(args.get("project_root") or "").strip()
    project_root = Path(raw_root).expanduser() if raw_root else None
    platform_name = raw_platform or (_platform_from_project_root(project_root) if project_root else "")
    lines = ["inspect_platform_health", f"platform={platform_name or 'missing'}"]
    blockers = []
    unchecked = []

    if not platform_name or not _SAFE_PLATFORM_NAME.match(platform_name):
        blockers.append("platform")
        lines.append(f"platform_status=invalid value={platform_name or 'missing'}")
    if project_root is None:
        blockers.append("project_root")
        lines.append("project_root_status=missing")
    elif _is_sensitive_path(project_root):
        blockers.append("project_root")
        lines.append(f"project_root_status=refused path={project_root.name}")
    elif not project_root.exists() or not project_root.is_dir():
        blockers.append("project_root")
        lines.append(f"project_root={project_root}")
        lines.append("project_root_status=missing")
    else:
        lines.append(f"project_root={project_root}")
        lines.append("project_root_status=detected")

    required_roles = _requested_platform_roles(args)
    screen_roles = _roles_for_platform(_screen_instance_rows(), platform_name)
    _append_role_health(lines, "screen", required_roles, screen_roles, blockers)

    process_rows = _process_rows_for_platform(platform_name, project_root)
    process_roles = {row["role"] for row in process_rows if row.get("role") and row.get("role") != "unknown"}
    _append_role_health(lines, "process", required_roles, process_roles, blockers)
    if process_rows:
        pids = sorted({int(row["pid"]) for row in process_rows if str(row.get("pid", "")).isdigit()})
        if pids:
            lines.append("process_pids=" + ",".join(str(pid) for pid in pids))

    ports = _read_config_ports_from_root(project_root) if project_root and project_root.exists() else {}
    if ports:
        lines.append("config_ports=" + ",".join(f"{key}:{ports[key]}" for key in _ordered_ports(ports)))
        port_numbers = sorted({_safe_port(value) for value in ports.values() if _safe_port(value) is not None})
        port_results = _inspect_port_owners({"ports": port_numbers})
        missing_or_unchecked = [result for result in port_results if result.status != STATUS_DETECTED]
        if missing_or_unchecked:
            blockers.append("ports")
            lines.append("port_status=blocked ports=" + ",".join(str(port) for port in port_numbers))
        else:
            lines.append("port_status=ready ports=" + ",".join(str(port) for port in port_numbers))
        lines.extend(result.render() for result in port_results)
    else:
        unchecked.append("ports")
        lines.append("config_ports=missing")
        lines.append("port_status=unchecked")

    nginx_paths = args.get("nginx_paths")
    if isinstance(nginx_paths, list) and nginx_paths:
        nginx_result = inspect_nginx_routes({"paths": nginx_paths})
        if "nginx_route: detected" in nginx_result:
            lines.append("nginx_status=detected")
        elif "nginx_route" in nginx_result:
            unchecked.append("nginx")
            lines.append("nginx_status=unchecked")
        else:
            unchecked.append("nginx")
            lines.append("nginx_status=missing")
        lines.append(_single_line(nginx_result, max_chars=900))
    else:
        unchecked.append("nginx")
        lines.append("nginx_status=unchecked")

    if blockers:
        overall = "blocked"
    elif unchecked:
        overall = "unchecked"
    else:
        overall = "ready"
    lines.append(f"overall_status={overall}")
    lines.append("environment unchanged")
    return "\n".join(lines)


def read_klonet_logs(args: Optional[dict] = None) -> str:
    """Read a safe tail of a whitelisted log file and redact sensitive values."""

    args = args or {}
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "Error: path is required"
    path = Path(raw_path).expanduser()
    if _is_sensitive_path(path):
        return f"Error: refused to read sensitive file: {path.name}"
    if path.suffix.lower() not in _SAFE_LOG_SUFFIXES:
        return f"Error: refused to read non-log file: {path.name}"
    if not path.exists() or not path.is_file():
        return _render_tool_result(
            "read_klonet_logs",
            [ProbeResult(str(path), STATUS_UNCHECKED, "file does not exist or is not a file")],
        )
    resolved_path = path.resolve()
    stat = path.stat()
    max_chars = _safe_int(args.get("max_chars"), MAX_LOG_CHARS)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _render_tool_result(
            "read_klonet_logs",
            [ProbeResult(str(path), STATUS_UNCHECKED, str(exc))],
        )
    tail = text[-max_chars:]
    return "\n".join(
        [
            "read_klonet_logs",
            ProbeResult(
                str(resolved_path),
                STATUS_DETECTED,
                (
                    f"resolved_path={resolved_path} "
                    f"mtime={_format_mtime(stat.st_mtime)} "
                    f"size_bytes={stat.st_size} "
                    f"showing last {len(tail)} chars"
                ),
            ).render(),
            redact_sensitive_text(tail),
        ]
    )


def read_ops_file(args: Optional[dict] = None) -> str:
    """Read a safe operational config/source file and redact sensitive values."""

    args = args or {}
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "Error: path is required"
    path = Path(raw_path).expanduser()
    if _is_sensitive_path(path):
        return f"Error: refused to read sensitive file: {path.name}"
    if not _is_safe_ops_file_path(path):
        return f"Error: refused to read unsupported ops file: {path.name}"
    if not path.exists() or not path.is_file():
        return _render_tool_result(
            "read_ops_file",
            [ProbeResult(str(path), STATUS_UNCHECKED, "file does not exist or is not a file")],
        )
    resolved_path = path.resolve()
    stat = path.stat()
    max_chars = _safe_int(args.get("max_chars"), MAX_LOG_CHARS)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _render_tool_result(
            "read_ops_file",
            [ProbeResult(str(path), STATUS_UNCHECKED, str(exc))],
        )
    snippet = text[-max_chars:]
    return "\n".join(
        [
            "read_ops_file",
            ProbeResult(
                str(resolved_path),
                STATUS_DETECTED,
                (
                    f"resolved_path={resolved_path} "
                    f"mtime={_format_mtime(stat.st_mtime)} "
                    f"size_bytes={stat.st_size} "
                    f"showing last {len(snippet)} chars"
                ),
            ).render(),
            redact_sensitive_text(snippet),
        ]
    )


def inspect_nginx_routes(args: Optional[dict] = None) -> str:
    """Parse safe nginx config files into route evidence."""

    args = args or {}
    raw_paths = args.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raw_paths = ["/etc/nginx/sites-available/default"]
    max_files = _safe_int(args.get("max_files"), 20)
    results = []
    for raw_path in raw_paths[:max_files]:
        path = Path(str(raw_path or "").strip()).expanduser()
        if _is_sensitive_path(path):
            results.append(ProbeResult("nginx_routes", STATUS_UNCHECKED, f"refused_sensitive_path={path.name}"))
            continue
        if not _is_safe_ops_file_path(path):
            results.append(ProbeResult("nginx_routes", STATUS_UNCHECKED, f"refused_unsupported_path={path.name}"))
            continue
        if not path.exists() or not path.is_file():
            results.append(ProbeResult("nginx_routes", STATUS_UNCHECKED, f"source_path={path} file does not exist or is not a file"))
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            results.append(ProbeResult("nginx_routes", STATUS_UNCHECKED, f"source_path={path} {exc}"))
            continue
        resolved_path = path.resolve()
        routes = _parse_nginx_routes(text, str(resolved_path))
        if routes:
            results.extend(routes)
        else:
            results.append(ProbeResult("nginx_routes", STATUS_MISSING, f"source_path={resolved_path} no routes found"))
    return _render_tool_result("inspect_nginx_routes", results)


def inspect_archive(args: Optional[dict] = None) -> str:
    """Inspect an archive without extracting it."""

    args = args or {}
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "Error: path is required"
    path = Path(raw_path).expanduser()
    if _is_sensitive_path(path):
        return "\n".join(["inspect_archive", f"refused_sensitive_path={path.name}", "environment unchanged"])
    if not path.exists() or not path.is_file():
        return "\n".join(["inspect_archive", f"archive_missing={raw_path}", "environment unchanged"])
    max_members = _safe_int(args.get("max_members"), 50)
    try:
        archive_type, members = _read_archive_members(path)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return "\n".join(
            [
                "inspect_archive",
                f"archive_unreadable={_single_line(str(exc), max_chars=300)}",
                "environment unchanged",
            ]
        )
    unsafe_members = _unsafe_archive_members(members)
    preview = members[:max_members]
    lines = [
        "inspect_archive",
        f"resolved_path={path.resolve()}",
        f"archive_type={archive_type}",
        f"member_count={len(members)}",
        f"unsafe_members={','.join(unsafe_members[:20]) if unsafe_members else 'none'}",
        "members:",
    ]
    lines.extend(f"  - {_single_line(member, max_chars=260)}" for member in preview)
    if len(members) > len(preview):
        lines.append(f"  - omitted={len(members) - len(preview)}")
    lines.append("environment unchanged")
    return "\n".join(lines)


def render_docker_daemon_config(args: Optional[dict] = None) -> str:
    """Render a Docker daemon.json merge draft without writing files."""

    args = args or {}
    raw_path = str(args.get("path") or "/etc/docker/daemon.json").strip()
    registry = str(args.get("registry") or "").strip()
    if not raw_path:
        return "Error: path is required"
    path = Path(raw_path).expanduser()
    if _is_sensitive_path(path):
        return "\n".join(
            ["render_docker_daemon_config", f"refused_sensitive_path={path.name}", "environment unchanged"]
        )
    if path.suffix.lower() != ".json":
        return "\n".join(
            ["render_docker_daemon_config", f"refused_non_json_path={path.name}", "environment unchanged"]
        )
    if not registry or not _SAFE_REGISTRY_ENDPOINT.match(registry) or ":" not in registry:
        return "\n".join(
            [
                "render_docker_daemon_config",
                f"invalid_registry={registry or 'missing'}",
                "environment unchanged",
            ]
        )
    source_status = "missing"
    config = {}
    if path.exists() and path.is_file():
        source_status = "detected"
        try:
            config = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            return "\n".join(
                [
                    "render_docker_daemon_config",
                    f"invalid_source_json={_single_line(str(exc), max_chars=300)}",
                    "environment unchanged",
                ]
            )
        if not isinstance(config, dict):
            return "\n".join(
                [
                    "render_docker_daemon_config",
                    "invalid_source_json=root_must_be_object",
                    "environment unchanged",
                ]
            )
    registries = config.get("insecure-registries")
    if not isinstance(registries, list):
        registries = []
    merged_registries = [str(item) for item in registries if str(item).strip()]
    if registry not in merged_registries:
        merged_registries.append(registry)
    config["insecure-registries"] = merged_registries
    draft = json.dumps(config, ensure_ascii=False, indent=2)
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path
    return "\n".join(
        [
            "render_docker_daemon_config",
            f"source_path={resolved_path}",
            f"source_status={source_status}",
            "template_status=draft",
            "environment unchanged",
            "next_recipes=write_ops_file",
            "## daemon_json_patch_draft",
            redact_sensitive_text(draft),
        ]
    )


def inspect_screen_session(args: Optional[dict] = None) -> str:
    """Capture a read-only snapshot of a detached screen session scrollback."""

    args = args or {}
    session = str(args.get("session") or "").strip()
    if not session:
        return "Error: session is required"
    if not _SAFE_SCREEN_NAME.match(session):
        return f"Error: unsafe screen session name: {session!r}"
    if os.name == "nt":
        return "Error: screen is not available on Windows"
    if shutil.which("screen") is None:
        return _render_tool_result(
            "inspect_screen_session",
            [ProbeResult(session, STATUS_UNCHECKED, "screen not found")],
        )

    max_chars = _safe_int(args.get("max_chars"), MAX_SCREEN_CHARS)
    snapshot_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="klonet-screen-",
            suffix=".log",
            delete=False,
        ) as handle:
            snapshot_path = Path(handle.name)

        completed = subprocess.run(
            ["screen", "-S", session, "-X", "hardcopy", str(snapshot_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            output = redact_sensitive_text((completed.stderr or completed.stdout or "").strip())
            return _render_tool_result(
                "inspect_screen_session",
                [ProbeResult(session, STATUS_UNCHECKED, output or f"exit {completed.returncode}")],
            )

        text = snapshot_path.read_text(encoding="utf-8", errors="replace")
        tail = text[-max_chars:]
        return "\n".join(
            [
                "inspect_screen_session",
                ProbeResult(
                    session,
                    STATUS_DETECTED,
                    (
                        "evidence_type=screen_scrollback "
                        "current_state=false "
                        f"hardcopy snapshot; showing last {len(tail)} chars"
                    ),
                ).render(),
                redact_sensitive_text(tail),
            ]
        )
    except subprocess.TimeoutExpired:
        return _render_tool_result(
            "inspect_screen_session",
            [ProbeResult(session, STATUS_UNCHECKED, "screen hardcopy timed out")],
        )
    except OSError as exc:
        return _render_tool_result(
            "inspect_screen_session",
            [ProbeResult(session, STATUS_UNCHECKED, str(exc))],
        )
    finally:
        if snapshot_path is not None:
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                pass


def _platform_entry(instances: dict, platform: str) -> dict:
    normalized = platform or "unknown"
    if normalized not in instances:
        instances[normalized] = {
            "roles": set(),
            "screen_sessions": [],
            "pids": [],
            "project_roots": set(),
            "ports": {},
            "sources": set(),
        }
    return instances[normalized]


def _screen_instance_rows() -> list:
    if os.name == "nt" or shutil.which("screen") is None:
        return []
    try:
        completed = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    rows = []
    for raw_line in (completed.stdout or "").splitlines():
        token = raw_line.strip().split(None, 1)[0] if raw_line.strip() else ""
        if "." not in token:
            continue
        logical_name = token.split(".", 1)[1]
        parsed = _platform_role_from_name(logical_name)
        if parsed:
            platform, role = parsed
            rows.append({"session": token, "platform": platform, "role": role})
    return rows


def _process_instance_rows() -> list:
    command = _probe_command("processes")
    if command is None:
        return []
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    rows = []
    for raw_line in (completed.stdout or "").splitlines():
        match = re.search(r"\bpid=(\d+)\s+cwd=(\S+)\s+cmd=(.*)$", raw_line)
        if not match:
            continue
        pid = int(match.group(1))
        cwd = match.group(2)
        cmd = match.group(3)
        role = _role_from_command(cmd)
        platform = _platform_from_cwd(cwd)
        if role or platform != "unknown":
            rows.append(
                {
                    "pid": pid,
                    "cwd": cwd,
                    "cmd": redact_sensitive_text(_single_line(cmd, max_chars=300)),
                    "platform": platform,
                    "role": role or "unknown",
                }
            )
    return rows


def _requested_project_roots(args: dict) -> list:
    roots = args.get("project_roots")
    if not isinstance(roots, list):
        return []
    result = []
    for raw_root in roots:
        path = Path(str(raw_root or "")).expanduser()
        if path.exists() and path.is_dir() and not _is_sensitive_path(path):
            result.append(path)
    return result


def _platform_role_from_name(name: str) -> Optional[tuple]:
    suffix_roles = (
        ("_web", "web_terminal"),
        ("_t", "web_terminal"),
        ("_m", "master"),
        ("_w", "worker"),
        ("_c", "celery"),
    )
    for suffix, role in suffix_roles:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], role
    return None


def _role_from_command(command: str) -> str:
    lowered = (command or "").lower()
    if "web_terminal_main.py" in lowered:
        return "web_terminal"
    if "worker_main" in lowered or "worker_gun.py" in lowered:
        return "worker"
    if "master_main" in lowered or "gun.py" in lowered:
        return "master"
    if "celery" in lowered:
        return "celery"
    return ""


def _platform_from_cwd(cwd: str) -> str:
    if not cwd or cwd == "?":
        return "unknown"
    return _platform_from_project_root(Path(cwd))


def _platform_from_project_root(root: Path) -> str:
    for part in reversed(root.parts):
        lower = part.lower()
        if lower.endswith("_project") and len(part) > len("_project"):
            return part[: -len("_project")]
    return root.name or "unknown"


def _read_config_ports(config_path: Path) -> dict:
    if not config_path.exists() or not config_path.is_file() or _is_sensitive_path(config_path):
        return {}
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    ports = {}
    for key in KLONET_PORT_KEYS:
        match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*['\"]?(\d+)['\"]?", text)
        if match:
            ports[key] = match.group(1)
    return ports


def _read_config_ports_from_root(root: Path) -> dict:
    candidates = (
        root / "config.py",
        root / "vemu_uestc" / "config.py",
        root / "mains" / "config.py",
    )
    for config_path in candidates:
        ports = _read_config_ports(config_path)
        if ports:
            return ports
    return {}


def _ordered_ports(ports: dict) -> list:
    ordered = [key for key in KLONET_PORT_KEYS if key in ports]
    ordered.extend(sorted(key for key in ports if key not in ordered))
    return ordered


def _requested_platform_roles(args: dict) -> set:
    raw_roles = args.get("required_roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        return {"master", "worker", "celery", "web_terminal"}
    roles = {str(role or "").strip() for role in raw_roles}
    return {role for role in roles if role} or {"master", "worker", "celery", "web_terminal"}


def _roles_for_platform(rows: list, platform_name: str) -> set:
    return {
        str(row.get("role") or "")
        for row in rows
        if str(row.get("platform") or "") == platform_name and str(row.get("role") or "")
    }


def _process_rows_for_platform(platform_name: str, project_root: Optional[Path]) -> list:
    rows = []
    expected_root = str(project_root) if project_root else ""
    for row in _process_instance_rows():
        row_platform = str(row.get("platform") or "")
        row_cwd = str(row.get("cwd") or "")
        if row_platform == platform_name or (expected_root and row_cwd == expected_root):
            rows.append(row)
    return rows


def _append_role_health(lines: list, label: str, required_roles: set, detected_roles: set, blockers: list) -> None:
    roles_text = ",".join(sorted(detected_roles)) if detected_roles else "none"
    missing = sorted(required_roles - detected_roles)
    if not detected_roles:
        blockers.append(label)
        lines.append(f"{label}_status=missing roles=none missing=" + ",".join(missing))
    elif missing:
        blockers.append(label)
        lines.append(f"{label}_status=partial roles={roles_text} missing=" + ",".join(missing))
    else:
        lines.append(f"{label}_status=ready roles={roles_text}")


def run_read_only_probe(name: str) -> ProbeResult:
    """Run one fixed allowlisted read-only probe."""

    normalized = (name or "").strip().lower()
    command = _probe_command(normalized)
    if command is None:
        return ProbeResult(normalized or "unknown", STATUS_UNCHECKED, "not allowlisted")
    if command and shutil.which(command[0]) is None:
        return ProbeResult(normalized, STATUS_UNCHECKED, f"{command[0]} not found")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(normalized, STATUS_UNCHECKED, "probe timed out")
    except OSError as exc:
        return ProbeResult(normalized, STATUS_UNCHECKED, str(exc))

    output = redact_sensitive_text((result.stdout or result.stderr or "").strip())
    if result.returncode != 0:
        return ProbeResult(normalized, STATUS_UNCHECKED, output or f"exit {result.returncode}")
    if not output:
        return ProbeResult(normalized, STATUS_MISSING, "no output")
    max_chars = 1800 if normalized == "processes" else 600
    return ProbeResult(normalized, STATUS_DETECTED, _single_line(output, max_chars=max_chars))


def _inspect_command_paths(args: dict) -> ProbeResult:
    commands = []
    for raw_command in args.get("commands", []):
        command = str(raw_command or "").strip()
        if command and _SAFE_COMMAND_NAME.match(command) and command not in commands:
            commands.append(command)
    if not commands:
        return ProbeResult("command_paths", STATUS_UNCHECKED, "commands is required")
    if os.name == "nt":
        shell_lines = []
        for command in commands[:20]:
            shell_lines.append(f"Write-Output '## {command}'")
            shell_lines.append(f"where.exe {command} 2>$null")
            shell_lines.append(f"{command} --version 2>$null | Select-Object -First 2")
        probe = ["powershell", "-NoProfile", "-Command", "; ".join(shell_lines)]
    else:
        shell_lines = []
        for command in commands[:20]:
            shell_lines.append(
                "printf '## %s\\n' "
                f"{command}; command -v {command} 2>/dev/null || true; "
                f"{command} --version 2>&1 | head -2 || true"
            )
        probe = ["sh", "-c", "; ".join(shell_lines)]
    if probe and shutil.which(probe[0]) is None:
        return ProbeResult("command_paths", STATUS_UNCHECKED, f"{probe[0]} not found")
    try:
        result = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult("command_paths", STATUS_UNCHECKED, "probe timed out")
    except OSError as exc:
        return ProbeResult("command_paths", STATUS_UNCHECKED, str(exc))
    output = redact_sensitive_text((result.stdout or result.stderr or "").strip())
    if result.returncode != 0:
        return ProbeResult("command_paths", STATUS_UNCHECKED, output or f"exit {result.returncode}")
    detail = f"accepted_commands={','.join(commands[:20])}"
    if output:
        detail = f"{detail} {_single_line(output, max_chars=1800)}"
    return ProbeResult("command_paths", STATUS_DETECTED, detail)


def _read_archive_members(path: Path) -> tuple:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as handle:
            members = [name for name in handle.namelist() if name and not name.endswith("/")]
        return "zip", members
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as handle:
            members = [member.name for member in handle.getmembers() if member.name and member.isfile()]
        return "tar", members
    raise OSError(f"unsupported_archive={path.name}")


def _unsafe_archive_members(members: list) -> list:
    unsafe = []
    for member in members:
        member_path = Path(str(member))
        if member_path.is_absolute() or ".." in member_path.parts:
            unsafe.append(str(member))
    return unsafe


def _inspect_port_owners(args: dict) -> list:
    ports = _requested_ports(args)
    if not ports:
        return [ProbeResult("port_owner", STATUS_UNCHECKED, "ports is required")]
    if os.name == "nt":
        return [ProbeResult("port_owner", STATUS_UNCHECKED, "port_owner is not implemented on Windows")]
    if shutil.which("ss") is None:
        return [ProbeResult("port_owner", STATUS_UNCHECKED, "ss not found")]

    results = []
    for port in ports:
        try:
            completed = subprocess.run(
                ["ss", "-ltnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            results.append(ProbeResult("port_owner", STATUS_UNCHECKED, f"port={port} probe timed out"))
            continue
        except OSError as exc:
            results.append(ProbeResult("port_owner", STATUS_UNCHECKED, f"port={port} {exc}"))
            continue
        output = redact_sensitive_text((completed.stdout or completed.stderr or "").strip())
        if completed.returncode != 0:
            results.append(ProbeResult("port_owner", STATUS_UNCHECKED, f"port={port} {output or 'ss failed'}"))
            continue
        pid = _pid_from_ss_output(output)
        if pid:
            results.append(_process_owner_result(port, pid))
        elif _port_is_listening(output, port):
            results.append(ProbeResult("port_owner", STATUS_DETECTED, f"port={port} pid=unchecked reason=ss did not expose pid"))
        else:
            results.append(ProbeResult("port_owner", STATUS_MISSING, f"port={port} not listening"))
    return results


def _inspect_process_details(args: dict) -> list:
    pids = _requested_pids(args)
    keywords = [str(item) for item in args.get("process_keywords", []) if str(item).strip()]
    if not pids and not keywords:
        return [ProbeResult("process_details", STATUS_UNCHECKED, "pids or process_keywords is required")]
    if os.name == "nt":
        return [ProbeResult("process_details", STATUS_UNCHECKED, "process_details is not implemented on Windows")]
    selected_pids = list(pids)
    if keywords:
        selected_pids.extend(_pids_for_keywords(keywords))
    selected_pids = _dedupe_ints(selected_pids)
    if not selected_pids:
        return [ProbeResult("process_details", STATUS_MISSING, "no matching process")]
    return [_process_detail_result(pid) for pid in selected_pids[:20]]


def _process_owner_result(port: int, pid: int) -> ProbeResult:
    detail = _process_detail(pid)
    fields = [f"port={port}", f"pid={pid}"]
    fields.extend(_detail_fields(detail))
    return ProbeResult("port_owner", STATUS_DETECTED, " ".join(fields))


def _process_detail_result(pid: int) -> ProbeResult:
    detail = _process_detail(pid)
    fields = [f"pid={pid}"]
    fields.extend(_detail_fields(detail))
    return ProbeResult("process_details", STATUS_DETECTED, " ".join(fields))


def _process_detail(pid: int) -> dict:
    ps = _ps_detail(pid)
    cmd = _read_proc_text(f"/proc/{pid}/cmdline").replace("\x00", " ").strip()
    cwd = _read_proc_link(f"/proc/{pid}/cwd")
    if not cmd:
        cmd = ps.get("cmd", "")
    return {
        "ppid": ps.get("ppid", ""),
        "user": ps.get("user", ""),
        "cmd": redact_sensitive_text(_single_line(cmd, max_chars=300)) if cmd else "unchecked",
        "cwd": cwd or "unchecked",
    }


def _detail_fields(detail: dict) -> list:
    fields = []
    for key in ("ppid", "user", "cmd", "cwd"):
        value = str(detail.get(key) or "unchecked")
        fields.append(f"{key}={value}")
    return fields


def _ps_detail(pid: int) -> dict:
    if shutil.which("ps") is None:
        return {}
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,ppid=,user=,args="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    line = (completed.stdout or "").strip()
    match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
    if not match:
        return {}
    return {"pid": match.group(1), "ppid": match.group(2), "user": match.group(3), "cmd": match.group(4)}


def _pids_for_keywords(keywords: list) -> list:
    if shutil.which("pgrep") is None:
        return []
    pattern = "|".join(re.escape(item) for item in keywords if item)
    if not pattern:
        return []
    try:
        completed = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(item) for item in re.findall(r"\b\d+\b", completed.stdout or "")]


def _pid_from_ss_output(output: str) -> Optional[int]:
    match = re.search(r"\bpid=(\d+)\b", output or "")
    return int(match.group(1)) if match else None


def _port_is_listening(output: str, port: int) -> bool:
    return bool(re.search(rf":{port}\b", output or ""))


def _requested_ports(args: dict) -> list:
    return _dedupe_ints(args.get("ports", []))


def _requested_pids(args: dict) -> list:
    return _dedupe_ints(args.get("pids", []))


def _dedupe_ints(values) -> list:
    result = []
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result


def _read_proc_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_proc_link(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def redact_sensitive_text(text: str) -> str:
    """Redact common secret shapes from logs and config snippets."""

    redacted = text or ""
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def _redact_match(match: re.Match) -> str:
    if match.lastindex:
        return f"{match.group(1)} [REDACTED]"
    return "[REDACTED]"


def _validate_render_config_inputs(
    platform_name: str,
    server_name: str,
    master_port: Optional[int],
    worker_port: Optional[int],
    public_port: Optional[int],
    terminal_port: Optional[int],
    frontend_alias: str,
    frontend_path: str,
) -> str:
    if not _SAFE_PLATFORM_NAME.match(platform_name):
        return f"invalid_platform={platform_name or 'missing'}"
    if not _SAFE_SERVER_NAME.match(server_name):
        return f"invalid_server_name={server_name or 'missing'}"
    for name, port in (
        ("master_port", master_port),
        ("worker_port", worker_port),
        ("public_port", public_port),
        ("terminal_port", terminal_port),
    ):
        if port is None:
            return f"invalid_{name}=missing"
    if len({master_port, worker_port, public_port, terminal_port}) != 4:
        return "invalid_ports=duplicate"
    if not _SAFE_FRONTEND_ALIAS.match(frontend_alias) or not frontend_alias.endswith("/"):
        return f"invalid_frontend_alias={frontend_alias or 'missing'}"
    if not frontend_path or _looks_unsafe_ops_path(frontend_path):
        return f"invalid_frontend_path={frontend_path or 'missing'}"
    return ""


def _normalize_frontend_alias(value: str) -> str:
    if not value:
        return "/VEMU2/"
    if not value.startswith("/"):
        return value
    return value if value.endswith("/") else f"{value}/"


def _normalize_frontend_path(value: str) -> str:
    return value.rstrip("/") if value else ""


def _safe_port(value) -> Optional[int]:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _looks_unsafe_ops_path(value: str) -> bool:
    if any(part in value for part in ("\x00", "\n", "\r")):
        return True
    return not (value.startswith("/") or Path(value).is_absolute())


def _load_frontend_config_source(raw_path: str) -> tuple:
    path = Path(raw_path).expanduser()
    if _is_sensitive_path(path):
        return "", "", f"refused_sensitive_path={path.name}"
    if not _is_safe_ops_file_path(path):
        return "", "", f"refused_unsupported_frontend_config={path.name}"
    if path.suffix.lower() != ".js":
        return "", "", f"refused_non_js_frontend_config={path.name}"
    if not path.exists() or not path.is_file():
        return "", "", f"frontend_config_missing={path}"
    try:
        resolved_path = str(path.resolve())
    except OSError:
        resolved_path = str(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", "", f"frontend_config_unreadable={exc}"
    return resolved_path, text[-MAX_LOG_CHARS:], ""


def _render_nginx_server_block(
    *,
    server_name: str,
    master_port: int,
    public_port: int,
    frontend_alias: str,
    frontend_path: str,
) -> str:
    frontend_alias = _normalize_frontend_alias(frontend_alias)
    frontend_path = _normalize_frontend_path(frontend_path)
    return "\n".join(
        [
            "server {",
            f"    listen {public_port};",
            f"    server_name {server_name};",
            "    index index.html index.htm index.nginx-debian.html;",
            "",
            "    location /file/dload/ {",
            f"        proxy_pass http://127.0.0.1:{master_port}/file/dload/;",
            "    }",
            "",
            "    location /file/uload/ {",
            f"        proxy_pass http://127.0.0.1:{master_port}/file/uload/;",
            "    }",
            "",
            "    location /reallyload/ {",
            f"        proxy_pass http://127.0.0.1:{master_port}/reallyload/;",
            "    }",
            "",
            "    location /download/ {",
            f"        proxy_pass http://127.0.0.1:{master_port}/download/;",
            "    }",
            "",
            "    location / {",
            f"        proxy_pass http://127.0.0.1:{master_port};",
            "    }",
            "",
            f"    location {frontend_alias} {{",
            f"        alias {frontend_path}/;",
            "    }",
            "}",
        ]
    )


def _render_frontend_config_js(
    *,
    server_name: str,
    public_port: int,
    terminal_port: int,
    source_text: str = "",
) -> str:
    if source_text:
        aligned = _align_frontend_config_js(
            source_text,
            server_name=server_name,
            public_port=public_port,
            terminal_port=terminal_port,
        )
        return redact_sensitive_text(aligned)
    return "\n".join(
        [
            f"var backend_ip = \"{server_name}\";",
            f"var backend_port = {public_port};",
            f"var web_terminal_port = {terminal_port};",
        ]
    )


def _align_frontend_config_js(
    text: str,
    *,
    server_name: str,
    public_port: int,
    terminal_port: int,
) -> str:
    changed = False
    lines = []
    for line in str(text or "").splitlines():
        updated, line_changed = _replace_frontend_config_assignment(
            line,
            server_name=server_name,
            public_port=public_port,
            terminal_port=terminal_port,
        )
        lines.append(updated)
        changed = changed or line_changed
    if changed:
        return "\n".join(lines)
    return "\n".join(
        [
            "// no recognizable existing frontend config fields; generic draft follows",
            f"var backend_ip = \"{server_name}\";",
            f"var backend_port = {public_port};",
            f"var web_terminal_port = {terminal_port};",
        ]
    )


def _replace_frontend_config_assignment(
    line: str,
    *,
    server_name: str,
    public_port: int,
    terminal_port: int,
) -> tuple:
    match = re.match(
        r"^(\s*(?:(?:var|let|const)\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*=\s*)([^;]+)(;.*)$",
        line,
    )
    if not match:
        return line, False
    prefix, name, value, suffix = match.groups()
    lowered = name.lower()
    if "terminal" in lowered and "port" in lowered:
        return f"{prefix}{terminal_port}{suffix}", True
    if "port" in lowered and "terminal" not in lowered:
        return f"{prefix}{public_port}{suffix}", True
    if any(key in lowered for key in ("ip", "host", "server")) and "port" not in lowered:
        quote = _assignment_quote(value)
        return f"{prefix}{quote}{server_name}{quote}{suffix}", True
    return line, False


def _frontend_expected_values(args: dict) -> dict:
    expected = {}
    server_name = str(args.get("server_name") or "").strip()
    public_port = _safe_port(args.get("public_port"))
    terminal_port = _safe_port(args.get("terminal_port") or args.get("web_terminal_port"))
    if server_name:
        expected["server"] = server_name
    if public_port is not None:
        expected["public_port"] = str(public_port)
    if terminal_port is not None:
        expected["terminal_port"] = str(terminal_port)
    return expected


def _parse_frontend_assignments(text: str) -> list:
    assignments = []
    for line in str(text or "").splitlines():
        match = re.match(
            r"^\s*(?:(?:var|let|const)\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*=\s*([^;]+)",
            line,
        )
        if not match:
            continue
        name = match.group(1)
        value = match.group(2).strip()
        assignments.append({"name": name, "value": _normalize_js_assignment_value(value)})
    return assignments


def _normalize_js_assignment_value(value: str) -> str:
    stripped = str(value or "").strip()
    if (stripped.startswith("\"") and stripped.endswith("\"")) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]
    return stripped


def _frontend_assignment_kind(name: str) -> str:
    lowered = str(name or "").lower()
    if "terminal" in lowered and "port" in lowered:
        return "terminal_port"
    if "port" in lowered:
        return "public_port"
    if any(key in lowered for key in ("ip", "host", "server")):
        return "server"
    return ""


def _frontend_nginx_alias_status(args: dict) -> str:
    nginx_paths = args.get("nginx_paths")
    frontend_alias = _normalize_frontend_alias(str(args.get("frontend_alias") or "").strip())
    frontend_path = _normalize_frontend_path(str(args.get("frontend_path") or "").strip())
    if not isinstance(nginx_paths, list) or not nginx_paths:
        return ""
    if not frontend_alias or not frontend_path:
        return "nginx_alias_status=unchecked"
    expected_alias = frontend_path.rstrip("/") + "/"
    saw_routes = False
    for raw_path in nginx_paths:
        path = Path(str(raw_path or "")).expanduser()
        if _is_sensitive_path(path) or not _is_safe_ops_file_path(path) or not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for route in _parse_nginx_routes(text, str(path.resolve())):
            saw_routes = True
            detail = route.detail
            if f"location={frontend_alias}" in detail:
                if f"alias={expected_alias}" in detail:
                    return "nginx_alias_status=matched"
                return "nginx_alias_status=mismatch"
    return "nginx_alias_status=missing" if saw_routes else "nginx_alias_status=unchecked"


def _assignment_quote(value: str) -> str:
    stripped = str(value or "").strip()
    if stripped.startswith("'") and stripped.endswith("'"):
        return "'"
    return "\""


def _render_backend_config_py(*, master_port: int, worker_port: int, public_port: int, terminal_port: int) -> str:
    return "\n".join(
        [
            f"master_port = {master_port}",
            f"worker_port = {worker_port}",
            f"public_port = {public_port}",
            f"web_terminal_port = {terminal_port}",
        ]
    )


def _render_web_terminal_main_patch_hint(terminal_port: int) -> str:
    return "\n".join(
        [
            "确认 mains/web_terminal_main.py 或已复制到项目根目录的 web_terminal_main.py 中监听端口一致：",
            f"WSGIServer(('0.0.0.0', {terminal_port}), app, ...)",
        ]
    )


def _parse_nginx_routes(text: str, source_path: str) -> list:
    clean = _strip_nginx_comments(text)
    results = []
    for block in _extract_named_blocks(clean, "server"):
        listens = _nginx_values(block, "listen") or ["unchecked"]
        server_names = _nginx_values(block, "server_name") or ["unchecked"]
        for location, location_body in _extract_location_blocks(block):
            proxy_passes = _nginx_values(location_body, "proxy_pass") or ["unchecked"]
            aliases = _nginx_values(location_body, "alias") or ["unchecked"]
            detail = (
                f"source_path={source_path} "
                f"listen={','.join(listens)} "
                f"server_name={','.join(server_names)} "
                f"location={location} "
                f"proxy_pass={','.join(proxy_passes)} "
                f"alias={','.join(aliases)}"
            )
            results.append(ProbeResult("nginx_route", STATUS_DETECTED, redact_sensitive_text(detail)))
    return results


def _strip_nginx_comments(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        head = line.split("#", 1)[0].strip()
        if head:
            lines.append(head)
    return "\n".join(lines)


def _extract_named_blocks(text: str, name: str) -> list:
    blocks = []
    pattern = re.compile(rf"\b{re.escape(name)}\s*\{{")
    for match in pattern.finditer(text or ""):
        open_brace = match.end() - 1
        end = _matching_brace(text, open_brace)
        if end > open_brace:
            blocks.append(text[open_brace + 1 : end])
    return blocks


def _extract_location_blocks(server_block: str) -> list:
    locations = []
    pattern = re.compile(r"\blocation\s+([^\s{]+)\s*\{")
    for match in pattern.finditer(server_block or ""):
        location = match.group(1).strip()
        open_brace = match.end() - 1
        end = _matching_brace(server_block, open_brace)
        if end > open_brace:
            locations.append((location, server_block[open_brace + 1 : end]))
    return locations


def _matching_brace(text: str, open_brace: int) -> int:
    depth = 0
    for index in range(open_brace, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _nginx_values(block: str, directive: str) -> list:
    values = []
    for match in re.finditer(rf"\b{re.escape(directive)}\s+([^;]+);", block or ""):
        value = " ".join(match.group(1).split())
        if directive == "listen":
            value = value.split()[0]
        if value and value not in values:
            values.append(value)
    return values


def _ops_probe_many(checks: Sequence[str]):
    for check in checks:
        if check == "disk":
            yield _disk_usage_probe()
        else:
            yield run_read_only_probe(check)


def _requested_sections(args: dict) -> tuple:
    raw_sections = args.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        return OPS_CONTEXT_SECTIONS
    sections = []
    for section in raw_sections:
        normalized = str(section or "").strip().lower()
        if normalized in OPS_CONTEXT_SECTIONS and normalized not in sections:
            sections.append(normalized)
    return tuple(sections) or OPS_CONTEXT_SECTIONS


def _requested_service_health_checks(args: dict) -> tuple:
    services = args.get("services")
    if not isinstance(services, list) or not services:
        return OPS_SERVICE_HEALTH_CHECKS
    result = []
    for service in services:
        normalized = str(service or "").strip().lower()
        if normalized in OPS_SERVICE_HEALTH_CHECKS and normalized not in result:
            result.append(normalized)
    return tuple(result) or OPS_SERVICE_HEALTH_CHECKS


def _service_health_recommendation(result: ProbeResult) -> str:
    detail = (result.detail or "").lower()
    if result.status == STATUS_UNCHECKED:
        return "inspect"
    if result.status == STATUS_DETECTED and not _service_detail_looks_inactive(detail):
        return "reuse"
    if result.status == STATUS_MISSING or _service_detail_looks_inactive(detail):
        return "start_candidate"
    return "inspect"


def _service_detail_looks_inactive(detail: str) -> bool:
    inactive_markers = (
        "inactive",
        "not found",
        "missing",
        "failed",
        "stopped",
        "exited",
        "no output",
    )
    return any(marker in detail for marker in inactive_markers)


def _script_shebang(text: str) -> str:
    lines = (text or "").splitlines()
    first_line = lines[0] if lines else ""
    return first_line.strip() if first_line.startswith("#!") else ""


def _script_risk_markers(text: str) -> list:
    lowered = (text or "").lower()
    result = []
    for marker in INSTALL_SCRIPT_RISK_MARKERS:
        normalized = marker.strip()
        if marker == "docker":
            found = re.search(r"(?m)^\s*(?:sudo\s+)?docker\b", lowered) is not None
        else:
            found = marker in lowered
        if found and normalized not in result:
            result.append(normalized)
    return result


def _is_executable_file(path: Path) -> bool:
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def _scan_deployment_assets(args: dict):
    roots = args.get("asset_roots")
    if not isinstance(roots, list) or not roots:
        roots = [str(Path.cwd())]
    max_assets = _safe_int(args.get("max_assets"), 100)
    shown = 0
    rows = []
    for raw_root in roots:
        root = Path(str(raw_root or "")).expanduser()
        if not root.exists() or not root.is_dir():
            rows.append(
                ProbeResult(
                    "asset_roots",
                    STATUS_UNCHECKED,
                    f"{root} does not exist or is not a directory",
                ).render()
            )
            continue
        matches = []
        for path in root.rglob("*"):
            if shown >= max_assets:
                break
            if not path.is_file() or _is_sensitive_path(path):
                continue
            if _is_deployment_asset(path):
                matches.append(str(path.relative_to(root)))
                shown += 1
        if matches:
            rows.append(
                ProbeResult(
                    "asset_roots",
                    STATUS_DETECTED,
                    f"{root}: " + ", ".join(matches[:max_assets]),
                ).render()
            )
        else:
            rows.append(
                ProbeResult(
                    "asset_roots",
                    STATUS_MISSING,
                    f"{root}: no deployment assets found",
                ).render()
            )
    return rows


def _is_deployment_asset(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name in DEPLOYMENT_ASSET_NAMES or lower_name.startswith("dockerfile"):
        return True
    return path.suffix.lower() in DEPLOYMENT_ASSET_SUFFIXES


def _requested_checks(args: Optional[dict], *, default: tuple) -> tuple:
    checks = (args or {}).get("checks")
    if not isinstance(checks, list) or not checks:
        return default
    result = []
    for check in checks:
        normalized = str(check or "").strip().lower()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result) or default


def _probe_command(name: str) -> Optional[list]:
    if os.name == "nt":
        return _windows_probe_command(name)
    return _posix_probe_command(name)


def _posix_probe_command(name: str) -> Optional[list]:
    commands = {
        "os_release": ["sh", "-c", "cat /etc/os-release 2>/dev/null || uname -a"],
        "kernel": ["uname", "-r"],
        "arch": ["uname", "-m"],
        "cpu": [
            "sh",
            "-c",
            (
                "lscpu 2>/dev/null | egrep 'Model name|CPU\\(s\\)|Architecture|Virtualization' "
                "|| grep -m1 'model name' /proc/cpuinfo 2>/dev/null || true"
            ),
        ],
        "memory": ["free", "-h"],
        "virtualization": ["sh", "-c", "egrep -c '(vmx|svm)' /proc/cpuinfo 2>/dev/null || true"],
        "python": [
            "sh",
            "-c",
            (
                "command -v python3 2>/dev/null; python3 --version 2>&1; "
                "command -v /usr/local/python3/bin/python3.8 2>/dev/null || true; "
                "/usr/local/python3/bin/python3.8 --version 2>&1 || true; "
                "command -v gunicorn 2>/dev/null || true; command -v celery 2>/dev/null || true"
            ),
        ],
        "system_python": [
            "sh",
            "-c",
            (
                "printf 'PATH python: '; command -v python 2>/dev/null || true; "
                "printf 'PATH python3: '; command -v python3 2>/dev/null || true; "
                "printf '/usr/bin/python: '; /usr/bin/python --version 2>&1 || true; "
                "printf '/usr/bin/python3: '; /usr/bin/python3 --version 2>&1 || true; "
                "ls -l /usr/bin/python /usr/bin/python3 /usr/bin/python3.* 2>/dev/null || true; "
                "dpkg-query -W -f='${Package} ${Version}\\n' 'python3*' 2>/dev/null | head -20 || true"
            ),
        ],
        "rust": [
            "sh",
            "-c",
            "command -v rustc 2>/dev/null && rustc --version; command -v cargo 2>/dev/null && cargo --version",
        ],
        "docker_version": ["sh", "-c", "docker version --format '{{.Server.Version}}' 2>/dev/null || true"],
        "compose_version": [
            "sh",
            "-c",
            "docker compose version 2>/dev/null || docker-compose --version 2>/dev/null || true",
        ],
        "ports": ["ss", "-ltnp"],
        "services": [
            "sh",
            "-c",
            (
                "systemctl --type=service --state=running --no-pager --no-legend 2>/dev/null "
                "| head -80 || service --status-all 2>/dev/null | head -80"
            ),
        ],
        "screen": ["screen", "-ls"],
        "processes": [
            "sh",
            "-c",
            (
                "for pid in $(pgrep -f 'vemu|klonet|gunicorn|celery|screen|"
                "master_main|worker_main|web_terminal' 2>/dev/null | head -80); do "
                "[ \"$pid\" = \"$$\" ] && continue; "
                "cwd=$(readlink /proc/$pid/cwd 2>/dev/null || echo '?'); "
                "cmd=$(tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null | "
                "sed 's/[[:space:]]\\+/ /g'); "
                "[ -n \"$cmd\" ] || cmd=$(ps -p \"$pid\" -o args= 2>/dev/null); "
                "printf 'pid=%s cwd=%s cmd=%s\\n' \"$pid\" \"$cwd\" \"$cmd\"; "
                "done"
            ),
        ],
        "nginx": ["systemctl", "is-active", "nginx"],
        "docker": ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        "docker_containers": ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        "docker_images": [
            "sh",
            "-c",
            "docker images --format '{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}' 2>/dev/null | head -100",
        ],
        "docker_networks": ["docker", "network", "ls", "--format", "{{.Name}}\t{{.Driver}}\t{{.Scope}}"],
        "redis": ["systemctl", "is-active", "redis"],
        "rabbitmq": ["systemctl", "is-active", "rabbitmq-server"],
        "mysql": ["systemctl", "is-active", "mysql"],
        "libvirt": ["systemctl", "is-active", "libvirtd"],
        "ovs": ["systemctl", "is-active", "openvswitch-switch"],
        "kvm": ["sh", "-c", "lsmod | grep -E '^kvm' || true"],
    }
    return commands.get(name)


def _windows_probe_command(name: str) -> Optional[list]:
    commands = {
        "os_release": ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber"],
        "kernel": ["powershell", "-NoProfile", "-Command", "[Environment]::OSVersion.VersionString"],
        "arch": ["powershell", "-NoProfile", "-Command", "[Runtime.InteropServices.RuntimeInformation]::OSArchitecture"],
        "cpu": ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors"],
        "memory": ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory"],
        "virtualization": ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Processor | Select-Object -ExpandProperty VirtualizationFirmwareEnabled"],
        "python": ["powershell", "-NoProfile", "-Command", "python --version; py -3.8 --version 2>$null"],
        "system_python": ["powershell", "-NoProfile", "-Command", "py -0p 2>$null; where.exe python 2>$null; python --version 2>$null"],
        "rust": ["powershell", "-NoProfile", "-Command", "rustc --version 2>$null; cargo --version 2>$null"],
        "docker_version": ["powershell", "-NoProfile", "-Command", "docker version --format '{{.Server.Version}}' 2>$null"],
        "compose_version": ["powershell", "-NoProfile", "-Command", "docker compose version 2>$null; docker-compose --version 2>$null"],
        "ports": ["powershell", "-NoProfile", "-Command", "Get-NetTCPConnection -State Listen | Select-Object -First 80 LocalAddress,LocalPort,OwningProcess"],
        "services": ["powershell", "-NoProfile", "-Command", "Get-Service | Where-Object {$_.Status -eq 'Running'} | Select-Object -First 80 Name,Status"],
        "screen": ["powershell", "-NoProfile", "-Command", "'screen is not a Windows service'"],
        "processes": ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'vemu|klonet|gunicorn|celery|screen|master_main|worker_main|web_terminal' } | Select-Object -First 80 ProcessId,CommandLine"],
        "nginx": ["powershell", "-NoProfile", "-Command", "Get-Service nginx -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "docker": ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        "docker_containers": ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        "docker_images": ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}"],
        "docker_networks": ["docker", "network", "ls", "--format", "{{.Name}}\t{{.Driver}}\t{{.Scope}}"],
        "redis": ["powershell", "-NoProfile", "-Command", "Get-Service redis* -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "rabbitmq": ["powershell", "-NoProfile", "-Command", "Get-Service rabbit* -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "mysql": ["powershell", "-NoProfile", "-Command", "Get-Service mysql* -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "libvirt": ["powershell", "-NoProfile", "-Command", "'libvirt is not a Windows service'"],
        "ovs": ["powershell", "-NoProfile", "-Command", "Get-Service *openvswitch* -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "kvm": ["powershell", "-NoProfile", "-Command", "'kvm is not available on Windows'"],
    }
    return commands.get(name)


def _disk_usage_probe() -> ProbeResult:
    try:
        usage = shutil.disk_usage(Path.cwd())
    except OSError as exc:
        return ProbeResult("disk", STATUS_UNCHECKED, str(exc))
    total_gb = usage.total / 1024 / 1024 / 1024
    free_gb = usage.free / 1024 / 1024 / 1024
    return ProbeResult("disk", STATUS_DETECTED, f"total={total_gb:.1f}GB free={free_gb:.1f}GB")


def _is_sensitive_path(path: Path) -> bool:
    lower = path.name.lower()
    return any(part in lower for part in _SENSITIVE_NAME_PARTS)


def _is_safe_ops_file_path(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name in DEPLOYMENT_ASSET_NAMES or lower_name.startswith("dockerfile"):
        return True
    if path.suffix.lower() in _SAFE_OPS_FILE_SUFFIXES:
        return True
    normalized_parts = {part.lower() for part in path.parts}
    return "nginx" in normalized_parts and {"sites-enabled", "sites-available"} & normalized_parts


def _safe_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, MAX_LOG_CHARS))


def _format_mtime(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _render_tool_result(name: str, results: Iterable[ProbeResult]) -> str:
    rendered = [name]
    rendered.extend(result.render() for result in results)
    return "\n".join(rendered)


def _single_line(text: str, max_chars: int = 600) -> str:
    compact = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."
