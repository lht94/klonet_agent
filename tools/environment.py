"""Read-only environment inspection tools for Klonet operations diagnosis."""

from __future__ import annotations

import ast
import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence


MAX_LOG_CHARS = 8000
MAX_ROOT_READ_CHARS = 20000
MAX_SCREEN_CHARS = 8000
PROBE_TIMEOUT_SECONDS = 5
OPS_HELPER_PATH = "/usr/local/bin/klonet-agent-op"
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


def http_transport_for_url(url: str) -> str:
    """Return the authoritative transport policy for one HTTP target.

    Runtime checks against the local host must describe the local service, not
    an ambient HTTP proxy.  Non-loopback traffic deliberately keeps urllib's
    normal environment-proxy behavior.
    """

    hostname = (urllib.parse.urlparse(str(url)).hostname or "").strip().lower()
    if hostname == "localhost":
        return "direct_loopback"
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return "direct_loopback"
    except ValueError:
        pass
    return "default"


def open_http_request(request_or_url, *, timeout: float):
    """Open HTTP using the single runtime-inspection transport policy."""

    url = (
        request_or_url.full_url
        if isinstance(request_or_url, urllib.request.Request)
        else str(request_or_url)
    )
    if http_transport_for_url(url) == "direct_loopback":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request_or_url, timeout=timeout)
    return urllib.request.urlopen(request_or_url, timeout=timeout)


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
        r"(?i)(--(?:password|passwd|pwd|api-key|api_key|secret|token|requirepass)(?:=|\s+))([^\s]+)"
    ),
    re.compile(
        r"(?i)((?:redis|rediss|mysql|amqp)://[^:\s/@]+:)([^@\s/]+)(@)"
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
    try:
        script_dir_available = script_dir.exists() and script_dir.is_dir()
    except OSError:
        script_dir_available = False
    if not script_dir_available:
        helper_result = _root_inspect_install_scripts(raw_dir, args.get("scripts"))
        if helper_result:
            return helper_result
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
            helper_result = _root_inspect_install_scripts(raw_dir, scripts)
            if helper_result:
                return helper_result
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


def read_root_file(args: Optional[dict] = None) -> str:
    """Read any root-owned regular file through the root helper without writing."""

    args = args or {}
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "Error: path is required"
    try:
        max_chars = max(1, min(int(args.get("max_chars", MAX_LOG_CHARS)), MAX_ROOT_READ_CHARS))
    except (TypeError, ValueError):
        return "Error: invalid max_chars"
    return _root_read_file(raw_path, max_chars=max_chars) or (
        f"Error: root read helper unavailable for path={raw_path}"
    )


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
    screen_rows = _screen_instance_rows()
    process_rows = [
        row
        for row in _process_instance_rows()
        if row.get("role") and row.get("role") != "unknown"
    ]
    unresolved_process_rows = []
    root_aliases = {
        str(_canonical_runtime_root(Path(row["project_root"])).resolve()): row["platform"]
        for row in screen_rows
        if str(row.get("project_root") or "").startswith("/")
    }
    screen_roots = {
        row["platform"]: str(
            _canonical_runtime_root(Path(row["project_root"])).resolve()
        )
        for row in screen_rows
        if str(row.get("project_root") or "").startswith("/")
    }
    qualified_screen_roots = set(root_aliases)
    for row in process_rows:
        root = _runtime_root_from_process_row(row)
        screen_platform = _screen_platform_from_command(str(row.get("cmd") or ""))
        if root and screen_platform:
            qualified_screen_roots.add(str(
                _canonical_runtime_root(Path(root)).resolve()
            ))
            current = root_aliases.get(root, "")
            if not current or len(screen_platform) < len(current):
                root_aliases[root] = screen_platform
            screen_roots[screen_platform] = root
    for row in screen_rows:
        root = screen_roots.get(row["platform"], "")
        identity = f"root:{root}" if root else f"screen:{row['platform']}"
        entry = _platform_entry(instances, row["platform"], identity=identity)
        entry["roles"].add(row["role"])
        entry["screen_sessions"].append(row["session"])
        entry["sources"].add("screen")
    for row in process_rows:
        root = _runtime_root_from_process_row(row)
        platform = root_aliases.get(root) or row["platform"]
        if root and not _is_klonet_platform_runtime_root(
            Path(root), screen_roots=qualified_screen_roots,
        ):
            # Keep the process visible as unresolved/external evidence, but do
            # not promote an arbitrary cwd into a manageable platform.
            unresolved_process_rows.append(row)
            continue
        if not root and platform == "unknown":
            unresolved_process_rows.append(row)
            continue
        identity = f"root:{root}" if root else f"process:{platform}"
        entry = _platform_entry(instances, platform, identity=identity)
        entry["roles"].add(row["role"])
        entry["pids"].append(row["pid"])
        if root:
            entry["project_roots"].add(root)
        entry["sources"].add("process")
    for root in _requested_project_roots(args):
        platform_name = _platform_from_project_root(root)
        canonical_root = str(_canonical_runtime_root(root))
        platform_name = root_aliases.get(canonical_root) or platform_name
        entry = _platform_entry(
            instances,
            platform_name,
            identity=f"root:{canonical_root}",
        )
        entry["project_roots"].add(canonical_root)
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
    lines.append(f"instance_count={len(instances)}")
    if unresolved_process_rows:
        unresolved_roles = sorted(
            {
                str(row.get("role") or "unknown")
                for row in unresolved_process_rows
            }
        )
        unresolved_pids = sorted(
            {
                int(row["pid"])
                for row in unresolved_process_rows
                if str(row.get("pid", "")).isdigit()
            }
        )
        lines.append(
            "unresolved_process_evidence="
            + "roles:"
            + ",".join(unresolved_roles)
            + " pids:"
            + ",".join(str(pid) for pid in unresolved_pids)
        )
    for identity in sorted(instances)[:max_instances]:
        entry = instances[identity]
        detail_parts = [
            f"platform={entry['platform']}",
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


def inspect_running_platforms(args: Optional[dict] = None) -> str:
    """Classify runtime roots by master/worker API health, never by code presence."""

    args = args or {}
    instances: dict[str, dict] = {}
    process_rows = (
        _process_instance_rows(allow_interactive_sudo=True)
        if args.get("allow_interactive_sudo") is True
        else _process_instance_rows()
    )
    screen_rows = _screen_instance_rows()
    root_aliases = {
        str(_canonical_runtime_root(Path(row["project_root"])).resolve()): row["platform"]
        for row in screen_rows
        if str(row.get("project_root") or "").startswith("/")
    }
    screen_sessions_by_root: dict[str, dict[str, list[str]]] = {}
    for row in screen_rows:
        root_text = str(row.get("project_root") or "").strip()
        role = str(row.get("role") or "").strip()
        session_token = str(row.get("session") or "").strip()
        session = session_token.split(".", 1)[1] if "." in session_token else session_token
        if (
            not root_text.startswith("/")
            or not role
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", session)
        ):
            continue
        root = str(_canonical_runtime_root(Path(root_text)).resolve())
        screen_sessions_by_root.setdefault(root, {}).setdefault(role, []).append(
            session
        )
    # ``screen -ls`` is intentionally scoped to the caller's account.  When
    # the runtime belongs to another user, recover the same ownership fact
    # from the already-filtered Screen process and its verified cwd instead
    # of treating the session as absent or scanning names independently.
    for row in process_rows:
        command = str(row.get("cmd") or "")
        root_text = _runtime_root_from_process_row(row)
        match = re.search(r"(?:^|\s)-dmS\s+(\S+)", command)
        parsed = _platform_role_from_name(match.group(1)) if match else None
        if not root_text or parsed is None:
            continue
        _platform, role = parsed
        session = match.group(1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", session):
            continue
        root = str(_canonical_runtime_root(Path(root_text)).resolve())
        screen_sessions_by_root.setdefault(root, {}).setdefault(role, []).append(
            session
        )
    screen_roots = set(root_aliases)
    external_runtime_rows: list[dict] = []
    for row in process_rows:
        root_text = _runtime_root_from_process_row(row)
        screen_platform = _screen_platform_from_command(str(row.get("cmd") or ""))
        if root_text and screen_platform:
            canonical = str(_canonical_runtime_root(Path(root_text)).resolve())
            screen_roots.add(canonical)
            current = root_aliases.get(canonical, "")
            if not current or len(screen_platform) < len(current):
                root_aliases[canonical] = screen_platform
    for row in process_rows:
        root_text = _runtime_root_from_process_row(row)
        role = str(row.get("role") or "")
        if str(row.get("executable") or "").upper() == "SCREEN":
            # Screen is startup/session evidence, not a backend role process.
            # The actual interpreter descendants carry the runtime identity.
            continue
        if not root_text or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", role):
            continue
        root = str(_canonical_runtime_root(Path(root_text)).resolve())
        if not _is_klonet_platform_runtime_root(
            Path(root), screen_roots=screen_roots,
        ):
            external_runtime_rows.append(row)
            continue
        entry = instances.setdefault(
            root,
            {
                "platform": root_aliases.get(root) or _platform_from_project_root(Path(root)),
                "roles": set(),
                "pids": [],
            },
        )
        entry["roles"].add(role)
        if str(row.get("pid", "")).isdigit():
            entry["pids"].append(int(row["pid"]))
        entry.setdefault("role_pids", {}).setdefault(role, []).append(int(row["pid"]))
        entry.setdefault("role_rows", {}).setdefault(role, []).append(row)
        if (
            str(row.get("pid", "")).isdigit()
            and str(row.get("uid", "")).isdigit()
            and str(row.get("executable") or "").startswith("/")
        ):
            entry.setdefault("role_identities", {}).setdefault(role, []).append(
                "%s:%s:%s" % (
                    int(row["pid"]), int(row["uid"]), row["executable"],
                )
            )
        if str(row.get("pgid", "")).isdigit():
            entry.setdefault("role_pgids", {}).setdefault(role, []).append(int(row["pgid"]))

    _qualify_colliding_platform_names(instances, root_aliases)

    rows = []
    healthy_count = 0
    for root_text in sorted(instances):
        root = Path(root_text)
        entry = instances[root_text]
        roles = set(entry["roles"])
        ports = _read_config_ports_from_root(root)
        role_bindings = {
            role: _runtime_role_listener_binding(
                root,
                role,
                _safe_port(ports.get(port_key)),
            )
            for role, port_key in (
                ("master", "master_port"),
                ("worker", "worker_port"),
            )
        }
        missing_roles = sorted({"master", "worker"} - roles)
        endpoint_fields = []
        endpoints_healthy = True
        for role, port_key in (("master", "master_port"), ("worker", "worker_port")):
            port = _safe_port(ports.get(port_key))
            if role not in roles:
                port_field = f"{role}_port={port} " if port is not None else ""
                endpoint_fields.append(
                    f"{port_field}{role}_endpoint=not_checked reason=role_not_running"
                )
                endpoints_healthy = False
                continue
            if port is None:
                endpoint_fields.append(f"{role}_endpoint=not_checked reason=config_port_missing")
                endpoints_healthy = False
                continue
            endpoint_status, http_status, detail = _probe_backend_endpoint(port)
            endpoint_fields.append(
                f"{role}_port={port} {role}_endpoint={endpoint_status} "
                f"http_status={http_status} detail={_single_line(detail, max_chars=160)}"
            )
            if endpoint_status != "healthy":
                endpoints_healthy = False
        backend_status = "healthy" if not missing_roles and endpoints_healthy else "abnormal"
        if backend_status == "healthy":
            healthy_count += 1
        listener_fields = []
        for role in ("master", "worker"):
            binding = role_bindings[role]
            listener_fields.extend([
                f"{role}_listener_status={binding.get('status', 'unknown')}",
                f"{role}_listener_pid={binding.get('listener_pid', 'none')}",
                f"{role}_listener_pgid={binding.get('listener_pgid', 'none')}",
            ])
        component_specs = _runtime_component_specs(
            root, roles, entry.get("role_rows", {}),
        )
        component_ownership = _runtime_component_ownership(
            root,
            entry.get("role_rows", {}),
            screen_rows,
        )
        custom_identity_fields = " ".join(
            "%s_identities=%s" % (
                role,
                ",".join(sorted(set(identities))) or "none",
            )
            for role, identities in sorted(
                entry.get("role_identities", {}).items()
            )
            if role not in {"master", "worker", "celery", "web_terminal"}
        )
        rows.append(
            "platform=%s project_root=%s runtime_source=process roles=%s pids=%s "
            "master_pids=%s worker_pids=%s master_pgids=%s worker_pgids=%s "
            "master_identities=%s worker_identities=%s "
            "celery_identities=%s web_terminal_identities=%s "
            "runtime_identities=%s %s "
                "role_bindings_b64=%s "
                "screen_sessions_b64=%s "
                "component_ownership_b64=%s "
                "backend_status=%s missing_roles=%s configured_ports=%s "
            "managed_components=%s component_specs_b64=%s %s %s"
            % (
                entry["platform"],
                root_text,
                ",".join(sorted(roles)) or "none",
                ",".join(str(pid) for pid in sorted(set(entry["pids"]))) or "none",
                ",".join(str(pid) for pid in sorted(set(entry.get("role_pids", {}).get("master", [])))) or "none",
                ",".join(str(pid) for pid in sorted(set(entry.get("role_pids", {}).get("worker", [])))) or "none",
                ",".join(str(pid) for pid in sorted(set(entry.get("role_pgids", {}).get("master", [])))) or "none",
                ",".join(str(pid) for pid in sorted(set(entry.get("role_pgids", {}).get("worker", [])))) or "none",
                ",".join(sorted(set(entry.get("role_identities", {}).get("master", [])))) or "none",
                ",".join(sorted(set(entry.get("role_identities", {}).get("worker", [])))) or "none",
                ",".join(sorted(set(entry.get("role_identities", {}).get("celery", [])))) or "none",
                ",".join(sorted(set(entry.get("role_identities", {}).get("web_terminal", [])))) or "none",
                ",".join(sorted({
                    identity
                    for identities in entry.get("role_identities", {}).values()
                    for identity in identities
                })) or "none",
                custom_identity_fields,
                base64.urlsafe_b64encode(json.dumps(
                    role_bindings,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).decode("ascii"),
                base64.urlsafe_b64encode(json.dumps(
                    {
                        role: sorted(set(sessions))
                        for role, sessions in screen_sessions_by_root.get(
                            root_text, {}
                        ).items()
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).decode("ascii"),
                base64.urlsafe_b64encode(json.dumps(
                    component_ownership,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).decode("ascii"),
                backend_status,
                ",".join(missing_roles) or "none",
                ",".join(
                    "%s:%s" % (key, ports[key])
                    for key in _ordered_ports(ports)
                ) or "none",
                ",".join(
                    spec["name"] for spec in component_specs
                    if spec.get("category") == "application"
                    and spec.get("managed")
                ) or "none",
                base64.urlsafe_b64encode(json.dumps(
                    component_specs,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")).decode("ascii"),
                " ".join(listener_fields),
                " ".join(endpoint_fields),
            )
        )

    runtime_roots = set(instances)
    requested_roots = _requested_project_roots(args)
    candidate_code_roots = (
        requested_roots
        if isinstance(args.get("project_roots"), list)
        else _discover_klonet_code_roots(_default_code_search_roots())
    )
    code_only_roots = []
    for root in candidate_code_roots:
        canonical = str(_canonical_runtime_root(root).resolve())
        covered_by_runtime = canonical in runtime_roots or any(
            root.name == "vemu_uestc"
            and str(root.parent.resolve()) == runtime_root
            for runtime_root in runtime_roots
        )
        if not covered_by_runtime and canonical not in code_only_roots:
            code_only_roots.append(canonical)

    lines = [
        "inspect_running_platforms",
        f"runtime_candidate_count={len(rows)}",
        f"healthy_count={healthy_count}",
        f"abnormal_count={len(rows) - healthy_count}",
    ]
    lines.append(f"code_only_count={len(code_only_roots)}")
    external_roots = sorted({
        str(_canonical_runtime_root(Path(_runtime_root_from_process_row(row))).resolve())
        for row in external_runtime_rows
        if _runtime_root_from_process_row(row)
    })
    lines.append(f"external_runtime_count={len(external_roots)}")
    lines.extend(rows)
    lines.extend(f"code_only_root={root}" for root in sorted(code_only_roots))
    lines.extend(
        "external_runtime_root=%s classification=conflict_evidence_only" % root
        for root in external_roots
    )
    lines.append("environment unchanged")
    return "\n".join(lines)


def _probe_backend_endpoint(port: int) -> tuple[str, int, str]:
    url = "http://127.0.0.1:%s/server_health/" % port
    request = urllib.request.Request(url, method="GET")
    transport = http_transport_for_url(url)
    try:
        with open_http_request(request, timeout=3) as response:
            body = response.read(1000).decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                return (
                    "invalid_response",
                    int(response.status),
                    "response_not_json transport=%s" % transport,
                )
            if int(response.status) == 200 and payload.get("code") == 1:
                return (
                    "healthy",
                    int(response.status),
                    "code=1 transport=%s" % transport,
                )
            return (
                "unhealthy",
                int(response.status),
                "code=%s transport=%s" % (payload.get("code"), transport),
            )
    except urllib.error.HTTPError as exc:
        return (
            "http_error",
            int(exc.code),
            "%s transport=%s" % (exc.__class__.__name__, transport),
        )
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return (
            "unreachable",
            0,
            "%s transport=%s" % (reason.__class__.__name__, transport),
        )


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
    max_chars = _safe_int(
        args.get("max_chars"),
        MAX_LOG_CHARS,
        maximum=MAX_ROOT_READ_CHARS,
    )
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
    if not _is_safe_ops_file_path(path):
        return f"Error: refused to read unsupported ops file: {path.name}"
    try:
        path_available = path.exists() and path.is_file()
    except OSError:
        path_available = False
    if not path_available:
        helper_result = _root_read_file(
            raw_path,
            max_chars=_safe_int(
                args.get("max_chars"),
                MAX_LOG_CHARS,
                maximum=MAX_ROOT_READ_CHARS,
            ),
        )
        if helper_result:
            return helper_result.replace("read_root_file", "read_ops_file", 1)
        return _render_tool_result(
            "read_ops_file",
            [ProbeResult(str(path), STATUS_UNCHECKED, "file does not exist or is not a file")],
        )
    resolved_path = path.resolve()
    stat = path.stat()
    max_chars = _safe_int(
        args.get("max_chars"),
        MAX_LOG_CHARS,
        maximum=MAX_ROOT_READ_CHARS,
    )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        helper_result = _root_read_file(str(resolved_path), max_chars=max_chars)
        if helper_result:
            return helper_result.replace("read_root_file", "read_ops_file", 1)
        return _render_tool_result(
            "read_ops_file",
            [ProbeResult(str(path), STATUS_UNCHECKED, str(exc))],
        )
    view = str(args.get("view") or "tail").strip().lower()
    if view not in {"head", "tail"}:
        return "Error: view must be head or tail"
    snippet = text[:max_chars] if view == "head" else text[-max_chars:]
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
                    f"showing {view} {len(snippet)} chars"
                ),
            ).render(),
            redact_sensitive_text(snippet),
        ]
    )


def inspect_privilege_capabilities(args: Optional[dict] = None) -> str:
    """Report verified read-only privilege channels without exposing secrets."""

    uid = os.geteuid() if hasattr(os, "geteuid") else -1
    gid = os.getegid() if hasattr(os, "getegid") else -1
    sudo_path = shutil.which("sudo")
    sudo_noninteractive = False
    if sudo_path:
        try:
            checked = subprocess.run(
                [sudo_path, "-n", "true"],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            sudo_noninteractive = checked.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            sudo_noninteractive = False
    helper = Path(OPS_HELPER_PATH)
    helper_present = helper.is_file() and os.access(str(helper), os.X_OK)
    cross_user_proc = False
    proc_sample = "none"
    if os.name == "posix":
        for candidate in sorted(Path("/proc").glob("[0-9]*"))[:512]:
            try:
                owner = candidate.stat().st_uid
            except OSError:
                continue
            if owner == uid:
                continue
            proc_sample = candidate.name
            try:
                (candidate / "cwd").resolve(strict=True)
                cross_user_proc = True
            except OSError:
                cross_user_proc = False
            break
    return "\n".join([
        "inspect_privilege_capabilities",
        "current_uid=%s current_gid=%s" % (uid, gid),
        "sudo_binary=%s sudo_noninteractive=%s" % (
            "present" if sudo_path else "missing",
            str(sudo_noninteractive).lower(),
        ),
        "ops_helper=%s helper_executable=%s" % (
            "present" if helper_present else "missing",
            str(helper_present).lower(),
        ),
        "cross_user_proc_direct=%s sample_pid=%s" % (
            str(cross_user_proc).lower(), proc_sample,
        ),
        "capability_policy=direct_then_controlled_privilege",
        "environment unchanged",
    ])


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
        snapshot_path.chmod(0o666)

        owner_uid = _screen_session_owner_uid(session)
        command = [
            "screen", "-S", session, "-X", "hardcopy", "-h", str(snapshot_path),
        ]
        if owner_uid is not None and owner_uid != os.geteuid():
            command = ["sudo", "-n", "-u", "#%s" % owner_uid, *command]
        completed = subprocess.run(
            command,
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


def _screen_session_owner_uid(session: str) -> int | None:
    """Resolve the owner of one exact detached Screen process."""

    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv = [
                item.decode("utf-8", errors="replace")
                for item in (entry / "cmdline").read_bytes().split(b"\x00")
                if item
            ]
            uid = entry.stat().st_uid
        except OSError:
            continue
        if not argv or Path(argv[0]).name not in {"SCREEN", "screen"}:
            continue
        if any(
            argv[index] == "-dmS" and argv[index + 1] == session
            for index in range(len(argv) - 1)
        ):
            return uid
    return None


def _platform_entry(instances: dict, platform: str, *, identity: str = "") -> dict:
    normalized = platform or "unknown"
    key = identity or f"platform:{normalized}"
    if key not in instances:
        instances[key] = {
            "platform": normalized,
            "roles": set(),
            "screen_sessions": [],
            "pids": [],
            "project_roots": set(),
            "ports": {},
            "sources": set(),
        }
    return instances[key]


def _canonical_runtime_root(root: Path) -> Path:
    if root.name.lower() == "mains" and root.parent != root:
        return root.parent
    return root


def _is_klonet_platform_runtime_root(
    root: Path,
    *,
    screen_roots: set[str] | None = None,
) -> bool:
    """Return whether a process cwd is a manageable Klonet instance root.

    A runtime may import Klonet code while running in a container, simulation,
    or unrelated working directory.  Such a process is useful ownership and
    conflict evidence, but its cwd is not automatically a platform identity.
    A root qualifies only through an existing platform contract: exact Screen
    ownership, readable Klonet configuration, a runtime manifest, or the
    standard master/worker entrypoint pair.
    """

    try:
        canonical = _canonical_runtime_root(Path(root)).resolve()
    except (OSError, RuntimeError):
        return False
    if str(canonical) in set(screen_roots or set()):
        return True
    if not canonical.is_dir() or _is_sensitive_path(canonical):
        return False
    if _read_config_ports_from_root(canonical):
        return True
    if (canonical / ".klonet" / "runtime_components.json").is_file():
        return True
    for entry_root in (canonical, canonical / "mains"):
        if (
            (entry_root / "master_main.py").is_file()
            and (entry_root / "worker_main.py").is_file()
        ):
            return True
    return False


def _runtime_root_from_process_row(row: dict) -> str:
    """Resolve runtime ownership, never merely imported code ownership."""

    cwd = str(row.get("cwd") or "").strip()
    if cwd and cwd != "?":
        return str(_canonical_runtime_root(Path(cwd)))
    command = str(row.get("cmd") or "")
    cd_match = re.search(r"(?:^|\s)cd\s+([^\s;&]+)", command)
    if cd_match:
        return str(_canonical_runtime_root(Path(cd_match.group(1).strip("'\""))))
    # A gunicorn config, module, or entrypoint path identifies imported code,
    # not the process runtime root.  Treating it as cwd collapses container or
    # simulation workers into the source platform.  Cross-user runtime roots
    # are recovered separately through the observed Screen/PPID chain; port
    # ownership records retain command-derived code_root for conflict reports.
    return ""


def _screen_platform_from_command(command: str) -> str:
    match = re.search(r"(?:^|\s)-dmS\s+(\S+)", command or "")
    if not match:
        return ""
    parsed = _platform_role_from_name(match.group(1))
    return parsed[0] if parsed else ""


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
        pid_text, logical_name = token.split(".", 1)
        parsed = _platform_role_from_name(logical_name)
        if parsed:
            platform, role = parsed
            project_root = ""
            if pid_text.isdigit():
                project_root = _read_proc_link("/proc/%s/cwd" % pid_text)
            rows.append({
                "session": token,
                "platform": platform,
                "role": role,
                "pid": int(pid_text) if pid_text.isdigit() else None,
                "project_root": (
                    str(_canonical_runtime_root(Path(project_root)).resolve())
                    if project_root.startswith("/") else ""
                ),
            })
    return rows


def _proc_parent_pid(pid: int) -> int:
    try:
        text = Path("/proc/%s/status" % int(pid)).read_text(
            encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError):
        return 0
    match = re.search(r"(?m)^PPid:\s*(\d+)\s*$", text)
    return int(match.group(1)) if match else 0


def _screen_ancestor_session(pid: int, screen_rows: list[dict]) -> str:
    """Return the exact observed Screen ancestor for one runtime PID."""

    sessions = {
        int(row["pid"]): str(row.get("session") or "").split(".", 1)[-1]
        for row in screen_rows
        if str(row.get("pid") or "").isdigit()
        and str(row.get("session") or "")
    }
    current = int(pid)
    visited: set[int] = set()
    for _depth in range(32):
        if current in sessions:
            return sessions[current]
        if current <= 1 or current in visited:
            break
        visited.add(current)
        current = _proc_parent_pid(current)
    return ""


def _runtime_component_ownership(
    root: Path,
    role_rows: dict[str, list[dict]],
    screen_rows: list[dict],
) -> dict[str, dict[str, list[dict]]]:
    """Freeze process-group to Screen ownership for one runtime instance."""

    canonical_root = str(_canonical_runtime_root(root).resolve())
    root_screens = [
        row for row in screen_rows
        if str(row.get("project_root") or "").rstrip("/") == canonical_root
    ]
    result: dict[str, dict[str, list[dict]]] = {}
    for role, rows in sorted(role_rows.items()):
        groups: dict[int, list[dict]] = {}
        for row in rows:
            try:
                pid = int(row.get("pid") or 0)
                pgid = int(row.get("pgid") or pid)
            except (TypeError, ValueError):
                continue
            if pid > 1 and pgid > 1:
                groups.setdefault(pgid, []).append(row)
        managed_groups = []
        orphan_groups = []
        for pgid, members in sorted(groups.items()):
            pids = sorted({int(item.get("pid") or 0) for item in members})
            sessions = sorted({
                session
                for item in members
                if (session := _screen_ancestor_session(
                    int(item.get("pid") or 0), root_screens,
                ))
            })
            frozen = {
                "pgid": pgid,
                "pids": pids,
                "screen_session": sessions[0] if len(sessions) == 1 else "",
            }
            (managed_groups if len(sessions) == 1 else orphan_groups).append(
                frozen,
            )
        result[str(role)] = {
            "managed_groups": managed_groups,
            "orphan_groups": orphan_groups,
        }
    return result


def _qualify_colliding_platform_names(
    instances: dict[str, dict],
    screen_aliases: dict[str, str],
) -> None:
    """Make runtime prefixes unique without inventing random identities.

    ``instances`` is already keyed by canonical project root, which remains
    the authoritative identity.  Existing Screen prefixes are preferred.  If
    roots without Screen evidence share a basename, preserve the shortest
    root's familiar name and qualify the others with as much parent path as is
    required for uniqueness.
    """

    groups: dict[str, list[str]] = {}
    for root, entry in instances.items():
        groups.setdefault(str(entry.get("platform") or "unknown"), []).append(root)
    used = {
        str(entry.get("platform") or "")
        for entry in instances.values()
        if str(entry.get("platform") or "")
    }
    for alias, roots in groups.items():
        if len(roots) < 2:
            continue
        ordered = sorted(roots, key=lambda item: (len(Path(item).parts), item))
        preserved = ordered[0]
        for root in ordered:
            explicit = str(screen_aliases.get(root) or "").strip()
            if explicit and sum(
                1 for value in screen_aliases.values() if value == explicit
            ) == 1:
                instances[root]["platform"] = explicit
                used.add(explicit)
                if explicit == alias:
                    preserved = root
        for root in ordered:
            if root == preserved or str(screen_aliases.get(root) or "").strip():
                continue
            parts = [
                re.sub(r"[^A-Za-z0-9_-]+", "_", part).strip("_")
                for part in Path(root).parts
                if part and part != "/"
            ]
            candidate = ""
            for width in range(2, len(parts) + 1):
                candidate = "_".join(parts[-width:])
                if candidate and candidate not in used:
                    break
            if not candidate or candidate in used:
                candidate = "instance_%s" % hashlib.sha256(
                    root.encode("utf-8")
                ).hexdigest()[:8]
            instances[root]["platform"] = candidate
            used.add(candidate)


def _process_instance_rows(*, allow_interactive_sudo: bool = False) -> list:
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
            # This is the only inventory probe which boundedly walks all
            # matching /proc entries.  Treating its normal 5-8 second runtime
            # as "no processes" turns a transient timeout into a false empty
            # platform inventory, so give the same bounded operation enough
            # time to finish instead of changing its semantic result.
            timeout=PROBE_TIMEOUT_SECONDS * 4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    rows = []
    privileged_cwds: dict[int, str] = {}
    sudo_ready: bool | None = None
    for raw_line in (completed.stdout or "").splitlines():
        match = re.search(
            r"\bpid=(\d+)(?:\s+pgid=(\d+))?(?:\s+ppid=(\d+))?"
            r"(?:\s+uid=(\d+)\s+exe=(\S+))?\s+cwd=(\S+)\s+cmd=(.*)$",
            raw_line,
        )
        if not match:
            continue
        pid = int(match.group(1))
        pgid = match.group(2)
        ppid = match.group(3)
        uid = match.group(4)
        executable = match.group(5)
        cwd = match.group(6)
        cmd = match.group(7)
        if not _is_runtime_process_executable(executable or "", cmd):
            continue
        role = _role_from_command(cmd)
        if not role:
            continue
        if cwd == "?":
            process_group = int(pgid) if pgid and pgid.isdigit() else pid
            if process_group not in privileged_cwds:
                if allow_interactive_sudo and sudo_ready is None:
                    sudo_ready = _authenticate_readonly_sudo_once()
                privileged_cwds[process_group] = _privileged_process_cwd(pid)
            cwd = privileged_cwds[process_group]
        row = {
            "pid": pid,
            "ppid": int(ppid) if ppid and ppid.isdigit() else 0,
            "pgid": int(pgid) if pgid and pgid.isdigit() else pid,
            "uid": int(uid) if uid and uid.isdigit() else None,
            "executable": executable or "",
            "cwd": cwd,
            "cmd": redact_sensitive_text(_single_line(cmd, max_chars=300)),
            "platform": "unknown",
            "role": role,
        }
        runtime_root = _runtime_root_from_process_row(row)
        if runtime_root:
            row["platform"] = _platform_from_project_root(Path(runtime_root))
        rows.append(row)
    # Cross-user ptrace restrictions can hide a Gunicorn child's cwd even
    # though its Screen ancestor has an exact, visible ``cd <root>/mains`` in
    # argv. Propagate only through the observed PPID chain; never associate
    # processes by name alone.
    by_pid = {int(row["pid"]): row for row in rows}
    for _attempt in range(4):
        changed = False
        for row in rows:
            if _runtime_root_from_process_row(row):
                continue
            parent = by_pid.get(int(row.get("ppid") or 0))
            parent_root = _runtime_root_from_process_row(parent or {})
            if not parent_root:
                continue
            row["cwd"] = str(Path(parent_root) / "mains")
            row["platform"] = _platform_from_project_root(Path(parent_root))
            changed = True
        if not changed:
            break
    return rows


def _authenticate_readonly_sudo_once() -> bool:
    """Authenticate one explicitly privileged read-only inventory probe.

    The password remains attached to the terminal and is never captured.  A
    successful validation only establishes sudo's credential cache; every
    subsequent metadata read still uses ``sudo -n`` and therefore cannot
    create a prompt loop.
    """

    if os.name == "nt" or shutil.which("sudo") is None:
        return False
    options = {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
        "timeout": PROBE_TIMEOUT_SECONDS,
    }
    try:
        cached = subprocess.run(
            ["sudo", "-n", "-v"], capture_output=True, **options,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if cached.returncode == 0:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        authenticated = subprocess.run(
            ["sudo", "-v"],
            stdout=subprocess.DEVNULL,
            stderr=None,
            **{**options, "timeout": 120},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return authenticated.returncode == 0


def _is_runtime_process_executable(executable: str, command: str) -> bool:
    """Reject observer shells whose argv merely mentions runtime keywords."""

    name = Path(str(executable or "")).name.lower()
    if name == "screen" or str(command or "").lstrip().startswith("SCREEN "):
        return True
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name):
        return True
    if name in {"gunicorn", "celery"}:
        return True
    # Cross-user probes can hide exe.  Accept only an argv whose executable
    # token itself is a recognized runtime, never a shell containing keywords.
    first = str(command or "").strip().split(None, 1)[0] if str(command or "").strip() else ""
    first_name = Path(first).name.lower()
    return bool(
        re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", first_name)
        or first_name in {"gunicorn", "celery", "screen"}
    )


def _privileged_process_cwd(pid: int) -> str:
    """Read one already-filtered runtime cwd without requiring a helper."""

    if pid <= 1 or os.name == "nt":
        return "?"
    try:
        completed = subprocess.run(
            ["sudo", "-n", "readlink", "/proc/%s/cwd" % pid],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and value.startswith("/") else "?"


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


def _default_code_search_roots() -> list[Path]:
    home_root = Path("/home")
    return [home_root] if home_root.is_dir() else []


def _discover_klonet_code_roots(search_roots: Iterable[Path]) -> list[Path]:
    """Boundedly enumerate code roots without treating them as running."""

    ignored_names = {
        ".cache", ".config", ".git", ".local", ".tox", ".venv",
        "__pycache__", "anaconda3", "miniconda3", "node_modules",
        "site-packages", "venv",
    }
    found: list[Path] = []
    visited = 0
    for raw_base in search_roots:
        base = Path(raw_base).expanduser()
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for current, directory_names, file_names in os.walk(
            str(base), followlinks=False
        ):
            visited += 1
            if visited > 10000:
                return sorted(found, key=str)
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            directory_names[:] = [
                name
                for name in directory_names
                if name not in ignored_names
                and not name.startswith(".")
                and not (name == "klonet_source" and current_path.name == "knowledge")
                and not (current_path / name).is_symlink()
            ]
            if depth >= 7:
                directory_names[:] = []
            if (
                current_path.name == "mains"
                and "master_main.py" in file_names
                and "worker_main.py" in file_names
            ):
                root = current_path.parent.resolve()
                if root not in found and not _is_sensitive_path(root):
                    found.append(root)
                directory_names[:] = []
    return sorted(found, key=str)


def _platform_role_from_name(name: str) -> Optional[tuple]:
    suffix_roles = (
        ("_data_server", "data_server"),
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
    if (
        "web_terminal_main.py" in lowered
        or "web_terminal_main" in lowered
        or "create_web_terminal_app" in lowered
    ):
        return "web_terminal"
    if "worker_main" in lowered or "worker_gun.py" in lowered:
        return "worker"
    if "celery" in lowered:
        return "celery"
    # Resolve the application entrypoint before considering a generic Gunicorn
    # config name. ``data_server_gun.py`` contains the substring ``gun.py``;
    # substring-first classification used to turn data_server into a second
    # master role and polluted RuntimeInventory identities.
    generic = re.search(
        r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_-]{1,48})_main"
        r"(?:\.py|:|\b)",
        command or "",
        re.I,
    )
    if generic is not None:
        return generic.group(1).lower().replace("-", "_")
    if re.search(
        r"(?:^|\s)-c\s+(?:[^\s/]+/)*gun\.py(?:\s|$)",
        command or "",
        re.I,
    ):
        return "master"
    return ""


def _runtime_component_specs(
    root: Path,
    running_roles: set[str],
    role_rows: Optional[dict[str, list[dict]]] = None,
) -> list[dict]:
    builtins = [
        {"name": "master", "category": "application", "managed": True,
         "default_restart": True, "screen_suffix": "m", "start_after": []},
        {"name": "celery", "category": "application", "managed": True,
         "default_restart": True, "screen_suffix": "c", "start_after": ["master"]},
        {"name": "web_terminal", "category": "application", "managed": True,
         "default_restart": True, "screen_suffix": "web", "start_after": ["celery"]},
        {"name": "worker", "category": "application", "managed": True,
         "default_restart": True, "screen_suffix": "w", "start_after": ["web_terminal"]},
    ]
    by_name = {item["name"]: item for item in builtins}
    manifest = root / ".klonet" / "runtime_components.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raw = {}
    items = raw.get("components") if isinstance(raw, dict) else []
    for item in items if isinstance(items, list) else []:
        normalized = _safe_runtime_component_manifest_item(item)
        if normalized and normalized["name"] not in by_name:
            by_name[normalized["name"]] = normalized
    for role in sorted(running_roles):
        if role not in by_name:
            observed_argv = _observed_runtime_component_argv(
                root, list((role_rows or {}).get(role, [])),
            )
            discovered = {
                "name": role,
                "category": "application",
                "managed": True,
                "default_restart": False,
                "screen_suffix": role,
                "start_after": [],
                "discovery_status": (
                    "observed_runtime_contract"
                    if observed_argv else "command_contract_missing"
                ),
            }
            if observed_argv:
                discovered["command_argv"] = observed_argv
                observed_preflight = _observed_runtime_component_preflight(
                    observed_argv,
                )
                if observed_preflight:
                    discovered["preflight_argv"] = observed_preflight
            by_name[role] = discovered
    return list(by_name.values())


def _observed_runtime_component_argv(
    root: Path,
    rows: list[dict],
) -> list[str]:
    """Freeze a safe current argv for an inventory-discovered component.

    Only a process already attributed to the exact runtime root may define the
    contract.  Prefer the process-group leader so Gunicorn children cannot
    create multiple competing identities.  Complex shell syntax and redacted
    argv are deliberately rejected; Discovery/Binding can then use the normal
    read-only/Shell fallback instead of guessing.
    """

    root_text = str(_canonical_runtime_root(Path(root)).resolve())
    candidates = sorted(
        rows,
        key=lambda item: (
            0 if int(item.get("pid") or 0) == int(item.get("pgid") or -1) else 1,
            0 if int(item.get("ppid") or 0) == 1 else 1,
            int(item.get("pid") or 0),
        ),
    )
    for row in candidates:
        cwd = str(row.get("cwd") or "")
        if not cwd.startswith("/"):
            continue
        try:
            if str(_canonical_runtime_root(Path(cwd)).resolve()) != root_text:
                continue
        except OSError:
            continue
        command = str(row.get("cmd") or "").strip()
        if not command or "<redacted>" in command.lower():
            continue
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if (
            not _safe_runtime_component_argv(argv)
            or any(re.search(r"[;&|<>`\n\x00]", item) for item in argv)
            or redact_sensitive_text(" ".join(argv)) != " ".join(argv)
        ):
            continue
        executable = str(row.get("executable") or "")
        if executable.startswith("/") and Path(argv[0]).name != Path(executable).name:
            continue
        return argv
    return []


def _observed_runtime_component_preflight(argv: list[str]) -> list[str]:
    """Derive a non-mutating startup check from a frozen runtime command.

    Gunicorn provides a deterministic configuration check that loads the same
    config and application target without starting a listener.  Discovery may
    freeze this contract because it is derived from the exact observed argv;
    unsupported launchers remain without a preflight and must be resolved by
    the normal Binding/Discovery path rather than guessed here.
    """

    values = [str(item) for item in argv]
    if "--check-config" in values:
        return values
    try:
        module_index = values.index("-m")
    except ValueError:
        module_index = -1
    if (
        module_index >= 0
        and module_index + 1 < len(values)
        and values[module_index + 1] == "gunicorn"
    ):
        return [
            *values[: module_index + 2],
            "--check-config",
            *values[module_index + 2 :],
        ]
    if values and Path(values[0]).name.startswith("gunicorn"):
        return [values[0], "--check-config", *values[1:]]
    return []


def _safe_runtime_component_manifest_item(value) -> dict:
    if not isinstance(value, dict):
        return {}
    name = str(value.get("name") or "").strip().lower().replace("-", "_")
    suffix = str(value.get("screen_suffix") or name).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        return {}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", suffix):
        return {}
    command = value.get("command_argv")
    preflight = value.get("preflight_argv")
    if not _safe_runtime_component_argv(command) or not _safe_runtime_component_argv(preflight):
        return {}
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if redact_sensitive_text(serialized) != serialized:
        return {}
    ports = []
    for item in value.get("ports") or []:
        try:
            port = int(item)
        except (TypeError, ValueError):
            return {}
        if not 1 <= port <= 65535:
            return {}
        ports.append(port)
    checks = [
        dict(item) for item in value.get("health_checks") or []
        if isinstance(item, dict)
    ]
    return {
        "name": name,
        "category": (
            "shared_dependency"
            if value.get("category") == "shared_dependency"
            else "application"
        ),
        "managed": bool(value.get("managed", True)),
        "default_restart": bool(value.get("default_restart", True)),
        "screen_suffix": suffix,
        "command_argv": [str(item) for item in command],
        "preflight_argv": [str(item) for item in preflight],
        "ports": ports,
        "health_checks": checks,
        "start_after": [
            str(item) for item in value.get("start_after") or []
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", str(item))
        ],
    }


def _safe_runtime_component_argv(value) -> bool:
    return bool(
        isinstance(value, list)
        and 1 <= len(value) <= 64
        and all(
            str(item) and len(str(item)) <= 2000
            and "\x00" not in str(item) and "\n" not in str(item)
            for item in value
        )
    )


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
    active_ports = _read_active_config_class_ports(text)
    if active_ports:
        return active_ports
    ports = {}
    for key in KLONET_PORT_KEYS:
        match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*['\"]?(\d+)['\"]?", text)
        if match:
            ports[key] = match.group(1)
    return ports


def _read_config_ports_from_root(root: Path) -> dict:
    candidates = (
        root / "config.py",
        root / "vemu_config" / "config.py",
        root / "vemu_uestc" / "config.py",
        root / "vemu_uestc" / "vemu_config" / "config.py",
        root / "mains" / "config.py",
    )
    for config_path in candidates:
        ports = _read_config_ports(config_path)
        if ports:
            return ports
    return {}


def _read_active_config_class_ports(text: str) -> dict:
    try:
        tree = ast.parse(text or "")
    except SyntaxError:
        return {}
    classes: dict[str, ast.ClassDef] = {}
    active_class = ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes[node.name] = node
        elif isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "PROJ_CONFIG" in names and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Name):
                    active_class = func.id
    if not active_class or active_class not in classes:
        return {}
    return _class_ports(active_class, classes, set())


def _class_ports(class_name: str, classes: dict[str, ast.ClassDef], seen: set[str]) -> dict:
    if class_name in seen:
        return {}
    seen.add(class_name)
    node = classes.get(class_name)
    if node is None:
        return {}
    ports = {}
    for base in node.bases:
        if isinstance(base, ast.Name):
            ports.update(_class_ports(base.id, classes, seen))
    for item in node.body:
        if not isinstance(item, ast.Assign):
            continue
        for target in item.targets:
            if isinstance(target, ast.Name) and target.id in KLONET_PORT_KEYS:
                value = _literal_port_value(item.value)
                if value is not None:
                    ports[target.id] = str(value)
    return ports


def _literal_port_value(node: ast.AST) -> Optional[int]:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


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

    allow_interactive_sudo = args.get("allow_interactive_sudo") is True
    return [
        _port_owner_result(port, allow_interactive_sudo=allow_interactive_sudo)
        for port in ports
    ]


def _port_owner_result(
    port: int,
    *,
    allow_interactive_sudo: bool = False,
) -> ProbeResult:
    """Return one authoritative listener record, using cached sudo if present.

    The ordinary query remains first and sufficient for same-user listeners.
    ``sudo -n`` is only a read-only enrichment path; it never prompts here.
    Interactive authentication remains owned by the privileged workflow.
    """

    ss_path = shutil.which("ss")
    if not ss_path:
        return ProbeResult("port_owner", STATUS_UNCHECKED, f"port={port} ss not found")
    attempts = (["ss", "-ltnp", f"sport = :{port}"],)
    if shutil.which("sudo"):
        attempts += (["sudo", "-n", ss_path, "-ltnp", f"sport = :{port}"],)
        if allow_interactive_sudo:
            attempts += (["sudo", ss_path, "-ltnp", f"sport = :{port}"],)
    saw_listener = False
    last_problem = ""
    for argv in attempts:
        try:
            interactive_attempt = argv[:1] == ["sudo"] and "-n" not in argv
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120 if interactive_attempt else PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            last_problem = "probe timed out"
            continue
        except OSError as exc:
            last_problem = str(exc)
            continue
        output = redact_sensitive_text((completed.stdout or "").strip())
        if completed.returncode != 0:
            last_problem = redact_sensitive_text(
                (completed.stderr or completed.stdout or "ss failed").strip()
            )
            continue
        pids = _pids_from_ss_output(output)
        if pids:
            return _process_owner_result(port, pids)
        listening = _port_is_listening(output, port)
        if not listening:
            # ``ss`` exposes listener addresses without elevated privileges;
            # root is needed only for process metadata.  A successful query
            # with no matching socket is therefore already authoritative and
            # must not be downgraded by a later optional sudo enrichment.
            return ProbeResult(
                "port_owner", STATUS_MISSING, f"port={port} not listening"
            )
        saw_listener = True
    if saw_listener:
        return ProbeResult(
            "port_owner",
            STATUS_DETECTED,
            f"port={port} pid=unchecked reason=ss did not expose pid",
        )
    if last_problem:
        return ProbeResult("port_owner", STATUS_UNCHECKED, f"port={port} {last_problem}")
    return ProbeResult("port_owner", STATUS_MISSING, f"port={port} not listening")


def _runtime_role_listener_binding(
    project_root: Path,
    role: str,
    port: int | None,
) -> dict:
    """Bind a configured role port to its actual process group.

    A command path proves only which code was imported.  Runtime ownership is
    confirmed independently from cwd.  This prevents two launchers which reuse
    the same ``worker_gun.py`` from being collapsed into one runtime identity.
    """

    binding = {
        "role": role,
        "configured_port": port,
        "status": "config_port_missing" if port is None else "owner_unavailable",
    }
    if port is None:
        return binding
    owner = _port_owner_result(port)
    binding["owner_probe_status"] = owner.status
    detail = owner.detail
    if owner.status == STATUS_MISSING:
        binding["status"] = "not_listening"
        return binding
    pid_match = re.search(r"\bpid=(\d+)\b", detail)
    if pid_match is None:
        binding["status"] = "owner_unavailable"
        return binding
    pid = int(pid_match.group(1))
    process_detail = _process_detail(pid)
    observed_role = _role_from_command(str(process_detail.get("cmd") or ""))
    pgid = _safe_int(process_detail.get("pgid"), pid)
    cwd = str(process_detail.get("cwd") or "").strip()
    if cwd in {"", "?", "unchecked"}:
        cwd = _privileged_process_cwd(pid)
    command = str(process_detail.get("cmd") or "")
    command_root = _command_code_root(command)
    target = project_root.resolve()
    runtime_matches = bool(
        cwd.startswith("/")
        and (
            Path(cwd).resolve() == target
            or _canonical_runtime_root(Path(cwd).resolve()) == target
        )
    )
    binding.update({
        "listener_pid": pid,
        "listener_pgid": pgid,
        "listener_pids": _pids_from_port_owner_detail(detail),
        "observed_role": observed_role or "unknown",
        "runtime_root": cwd if cwd.startswith("/") else "unknown",
        "code_root": command_root or "unknown",
    })
    if observed_role and observed_role != role:
        binding["status"] = "role_conflict"
    elif runtime_matches:
        binding["status"] = "confirmed"
    elif cwd.startswith("/"):
        binding["status"] = "runtime_conflict"
    else:
        binding["status"] = "owner_ambiguous"
    return binding


def _pids_from_port_owner_detail(detail: str) -> list[int]:
    match = re.search(r"\blistener_pids=([0-9,]+)", detail or "")
    if match:
        return _dedupe_ints(match.group(1).split(","))
    match = re.search(r"\bpid=(\d+)\b", detail or "")
    return [int(match.group(1))] if match else []


def _command_code_root(command: str) -> str:
    match = re.search(
        r"((?:/[A-Za-z0-9._-]+)+)/mains(?:/|\b)", command or "",
    )
    if not match:
        return ""
    return str(Path(match.group(1).strip()).resolve())


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


def _process_owner_result(port: int, pids: list[int]) -> ProbeResult:
    details = {pid: _process_detail(pid) for pid in pids}
    root_pid = _process_tree_root_pid(pids, details)
    detail = details.get(root_pid, {})
    fields = [f"port={port}", f"pid={root_pid}"]
    if len(pids) > 1:
        fields.append("listener_pids=" + ",".join(str(pid) for pid in pids))
        fields.append(f"tree_root_pid={root_pid}")
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
    if not cwd:
        privileged_cwd = _privileged_process_cwd(pid)
        cwd = privileged_cwd if privileged_cwd != "?" else ""
    if not cmd:
        cmd = ps.get("cmd", "")
    return {
        "ppid": ps.get("ppid", ""),
        "pgid": ps.get("pgid", ""),
        "sid": ps.get("sid", ""),
        "user": ps.get("user", ""),
        "cmd": redact_sensitive_text(_single_line(cmd, max_chars=300)) if cmd else "unchecked",
        "cwd": cwd or "unchecked",
    }


def _detail_fields(detail: dict) -> list:
    fields = []
    for key in ("ppid", "pgid", "sid", "user", "cmd", "cwd"):
        value = str(detail.get(key) or "unchecked")
        fields.append(f"{key}={value}")
    return fields


def _ps_detail(pid: int) -> dict:
    if shutil.which("ps") is None:
        return {}
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,ppid=,pgid=,sid=,user=,args="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    line = (completed.stdout or "").strip()
    match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
    if not match:
        return {}
    return {
        "pid": match.group(1),
        "ppid": match.group(2),
        "pgid": match.group(3),
        "sid": match.group(4),
        "user": match.group(5),
        "cmd": match.group(6),
    }


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


def _pids_from_ss_output(output: str) -> list[int]:
    return _dedupe_ints(re.findall(r"\bpid=(\d+)\b", output or ""))


def _process_tree_root_pid(pids: list[int], details: dict[int, dict]) -> int:
    pid_set = set(pids)
    for pid in pids:
        try:
            ppid = int(details.get(pid, {}).get("ppid") or 0)
        except (TypeError, ValueError):
            ppid = 0
        if ppid not in pid_set:
            return pid
    return pids[0]


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
                "master_main|worker_main|web_terminal' 2>/dev/null); do "
                "[ \"$pid\" = \"$$\" ] && continue; "
                "meta=$(ps -o uid=,pgid=,ppid= -p $pid 2>/dev/null); "
                "set -- $meta; uid=$1; pgid=$2; ppid=$3; "
                "cwd=$(readlink /proc/$pid/cwd 2>/dev/null || echo '?'); "
                "cmd=$(tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null | "
                "sed 's/[[:space:]]\\+/ /g'); "
                "[ -n \"$cmd\" ] || cmd=$(ps -p \"$pid\" -o args= 2>/dev/null); "
                "exe=${cmd%% *}; "
                "printf 'pid=%s pgid=%s ppid=%s uid=%s exe=%s cwd=%s cmd=%s\\n' \"$pid\" \"${pgid:-$pid}\" \"${ppid:-0}\" \"$uid\" \"$exe\" \"$cwd\" \"$cmd\"; "
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


def _root_read_file(path: str, *, max_chars: int) -> str:
    command = [
        "sudo",
        "-n",
        OPS_HELPER_PATH,
        "read-file",
        "--execute",
        "--path",
        path,
        "--max-chars",
        str(max(1, min(max_chars, MAX_ROOT_READ_CHARS))),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if completed.returncode != 0 or not completed.stdout:
        return ""
    return "read_root_file\n" + _strip_helper_header(completed.stdout)


def _root_inspect_install_scripts(script_dir: str, scripts) -> str:
    script_names = []
    if isinstance(scripts, list):
        script_names = [str(item).strip() for item in scripts if str(item).strip()]
    command = [
        "sudo",
        "-n",
        OPS_HELPER_PATH,
        "inspect-install-scripts",
        "--execute",
        "--script-dir",
        script_dir,
    ]
    if script_names:
        command.extend(["--scripts", ",".join(script_names)])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if completed.returncode != 0 or not completed.stdout:
        return ""
    body = _strip_helper_header(completed.stdout)
    return "inspect_install_scripts\nroot_helper=true\n" + body


def _strip_helper_header(text: str) -> str:
    lines = (text or "").splitlines()
    if lines and lines[0] == "klonet_agent_op":
        lines = lines[1:]
    converted = []
    for line in lines:
        if line == "action=read-file":
            continue
        if line == "action=inspect-install-scripts":
            continue
        converted.append(line)
    return "\n".join(converted)


def _safe_int(value, default: int, *, maximum: int = MAX_LOG_CHARS) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


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
