"""Read-only environment inspection tools for Klonet operations diagnosis."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MAX_LOG_CHARS = 8000
PROBE_TIMEOUT_SECONDS = 5
STATUS_DETECTED = "detected"
STATUS_MISSING = "missing"
STATUS_UNCHECKED = "unchecked"

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
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token)\s*[:=]\s*([^\s]+)"
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


def inspect_system_environment(args: dict | None = None) -> str:
    """Inspect basic local system facts without modifying the host."""

    requested = _requested_checks(args, default=("os", "python", "disk", "virtualization"))
    results: list[ProbeResult] = []
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


def inspect_klonet_runtime(args: dict | None = None) -> str:
    """Inspect local runtime hints relevant to Klonet troubleshooting."""

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
    results = [run_read_only_probe(check) for check in requested]
    return _render_tool_result("inspect_klonet_runtime", results)


def read_klonet_logs(args: dict | None = None) -> str:
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
            ProbeResult(str(path), STATUS_DETECTED, f"showing last {len(tail)} chars").render(),
            redact_sensitive_text(tail),
        ]
    )


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


def _requested_checks(args: dict | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    checks = (args or {}).get("checks")
    if not isinstance(checks, list) or not checks:
        return default
    result = []
    for check in checks:
        normalized = str(check or "").strip().lower()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result) or default


def _probe_command(name: str) -> list[str] | None:
    if os.name == "nt":
        return _windows_probe_command(name)
    return _posix_probe_command(name)


def _posix_probe_command(name: str) -> list[str] | None:
    commands = {
        "virtualization": ["sh", "-c", "egrep -c '(vmx|svm)' /proc/cpuinfo 2>/dev/null || true"],
        "ports": ["ss", "-ltnp"],
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
        "redis": ["systemctl", "is-active", "redis"],
        "rabbitmq": ["systemctl", "is-active", "rabbitmq-server"],
        "mysql": ["systemctl", "is-active", "mysql"],
        "libvirt": ["systemctl", "is-active", "libvirtd"],
        "ovs": ["systemctl", "is-active", "openvswitch-switch"],
        "kvm": ["sh", "-c", "lsmod | grep -E '^kvm' || true"],
    }
    return commands.get(name)


def _windows_probe_command(name: str) -> list[str] | None:
    commands = {
        "virtualization": ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Processor | Select-Object -ExpandProperty VirtualizationFirmwareEnabled"],
        "ports": ["powershell", "-NoProfile", "-Command", "Get-NetTCPConnection -State Listen | Select-Object -First 80 LocalAddress,LocalPort,OwningProcess"],
        "screen": ["powershell", "-NoProfile", "-Command", "'screen is not a Windows service'"],
        "processes": ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'vemu|klonet|gunicorn|celery|screen|master_main|worker_main|web_terminal' } | Select-Object -First 80 ProcessId,CommandLine"],
        "nginx": ["powershell", "-NoProfile", "-Command", "Get-Service nginx -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "docker": ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        "redis": ["powershell", "-NoProfile", "-Command", "Get-Service redis* -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "rabbitmq": ["powershell", "-NoProfile", "-Command", "Get-Service rabbit* -ErrorAction SilentlyContinue | Select-Object Name,Status"],
        "mysql": ["powershell", "-NoProfile", "-Command", "Get-Service mysql* -ErrorAction SilentlyContinue | Select-Object Name,Status"],
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


def _safe_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, MAX_LOG_CHARS))


def _render_tool_result(name: str, results: Iterable[ProbeResult]) -> str:
    rendered = [name]
    rendered.extend(result.render() for result in results)
    return "\n".join(rendered)


def _single_line(text: str, max_chars: int = 600) -> str:
    compact = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."
