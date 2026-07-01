"""Read-only environment inspection tools for Klonet operations diagnosis."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
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
_SECRET_PATTERNS = (
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
                    f"hardcopy snapshot; showing last {len(tail)} chars",
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
        return str(Path(path).resolve())
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
