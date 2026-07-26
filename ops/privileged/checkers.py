"""执行后证据检查器注册表。"""

from __future__ import annotations

import importlib.util
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from klonet_agent.ops.privileged.contracts import CheckResult, ExecutionEvidence
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy


Checker = Callable[[Dict[str, Any], Optional[ExecutionEvidence]], CheckResult]


class DefaultCheckerRegistry:
    """集中管理可审计的确定性检查，未知检查永远不是成功。"""

    def __init__(self) -> None:
        self._checkers: dict[str, Checker] = {
            "exit_code_zero": self._exit_code_zero,
            "file_exists": self._file_exists,
            "file_contains": self._file_contains,
            "service_active": self._service_active,
            "process_running": self._process_running,
            "process_cwd_matches": self._process_cwd_matches,
            "port_listening": self._port_listening,
            "screen_session_exists": self._screen_session_exists,
            "container_running": self._container_running,
            "command_available": self._command_available,
            "python_import_succeeds": self._python_import_succeeds,
            "package_version": self._package_version,
            "nginx_config_valid": self._nginx_config_valid,
            "log_has_no_fatal_error": self._log_has_no_fatal_error,
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._checkers))

    def run(
        self,
        specification: dict[str, Any],
        *,
        evidence: ExecutionEvidence | None = None,
    ) -> CheckResult:
        name = str(specification.get("checker") or "")
        checker = self._checkers.get(name)
        if checker is None:
            return CheckResult(
                checker=name or "unknown",
                status="unavailable",
                observed="checker is not registered",
            )
        args = specification.get("args")
        if not isinstance(args, dict):
            args = {}
        try:
            return checker(args, evidence)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return CheckResult(
                checker=name,
                status="unavailable",
                observed=str(exc),
            )

    def _exit_code_zero(self, args, evidence):
        del args
        if evidence is None or evidence.return_code is None:
            return CheckResult(
                "exit_code_zero", "unavailable", expected="return_code=0",
                observed="no conclusive execution return code",
            )
        passed = evidence.return_code == 0 and not evidence.timed_out
        return CheckResult(
            "exit_code_zero",
            "passed" if passed else "failed",
            expected="return_code=0",
            observed="return_code=%s" % evidence.return_code,
            evidence=evidence.stderr[-500:],
        )

    def _file_exists(self, args, evidence):
        del evidence
        path = Path(str(args["path"])).expanduser()
        exists = path.exists()
        return CheckResult(
            "file_exists", "passed" if exists else "failed",
            expected="exists", observed=str(path),
        )

    def _file_contains(self, args, evidence):
        del evidence
        path = Path(str(args["path"])).expanduser()
        if not path.is_file():
            return CheckResult(
                "file_contains", "failed", expected=str(args.get("text", "")),
                observed="file missing: %s" % path,
            )
        needle = str(args.get("text", ""))
        found = needle in path.read_text(encoding="utf-8", errors="replace")
        return CheckResult(
            "file_contains", "passed" if found else "failed",
            expected=needle, observed="content matched" if found else "content missing",
        )

    def _service_active(self, args, evidence):
        del evidence
        return self._command_check(
            "service_active",
            ["systemctl", "is-active", str(args["service"])],
            expected="active",
            success=lambda item: item.returncode == 0 and item.stdout.strip() == "active",
        )

    def _process_running(self, args, evidence):
        del evidence
        return self._command_check(
            "process_running",
            ["pgrep", "-f", str(args["pattern"])],
            expected="matching PID",
        )

    def _process_cwd_matches(self, args, evidence):
        del evidence
        pid = int(args["pid"])
        expected = str(Path(str(args["cwd"])).resolve())
        actual = str(Path("/proc/%s/cwd" % pid).resolve())
        return CheckResult(
            "process_cwd_matches",
            "passed" if actual == expected else "failed",
            expected=expected,
            observed=actual,
        )

    def _port_listening(self, args, evidence):
        del evidence
        host = str(args.get("host") or "127.0.0.1")
        port = int(args["port"])
        with socket.socket() as client:
            client.settimeout(float(args.get("timeout", 1)))
            listening = client.connect_ex((host, port)) == 0
        return CheckResult(
            "port_listening", "passed" if listening else "failed",
            expected="%s:%s listening" % (host, port),
            observed="reachable" if listening else "not reachable",
        )

    def _screen_session_exists(self, args, evidence):
        del evidence
        session = str(args["session"])
        return self._command_check(
            "screen_session_exists",
            ["screen", "-S", session, "-Q", "select", "."],
            expected=session,
        )

    def _container_running(self, args, evidence):
        del evidence
        name = str(args["container"])
        return self._command_check(
            "container_running",
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            expected="true",
            success=lambda item: item.returncode == 0 and item.stdout.strip() == "true",
        )

    def _command_available(self, args, evidence):
        del evidence
        command = str(args["command"])
        found = shutil.which(command)
        return CheckResult(
            "command_available", "passed" if found else "failed",
            expected="command on PATH", observed=found or "not found",
        )

    def _python_import_succeeds(self, args, evidence):
        del evidence
        module = str(args["module"])
        found = importlib.util.find_spec(module) is not None
        return CheckResult(
            "python_import_succeeds", "passed" if found else "failed",
            expected="import %s" % module,
            observed="module found" if found else "module not found",
        )

    def _package_version(self, args, evidence):
        del evidence
        package = str(args["package"])
        result = self._command_check(
            "package_version",
            ["python", "-m", "pip", "show", package],
            expected=str(args.get("version") or "installed"),
        )
        if result.status == "passed" and args.get("version"):
            expected_line = "Version: %s" % args["version"]
            result.status = "passed" if expected_line in result.evidence else "failed"
            result.observed = expected_line if result.status == "passed" else result.evidence
        return result

    def _nginx_config_valid(self, args, evidence):
        del evidence
        command = [str(args.get("binary") or "nginx"), "-t"]
        return self._command_check("nginx_config_valid", command, expected="syntax is ok")

    def _log_has_no_fatal_error(self, args, evidence):
        del evidence
        path = Path(str(args["path"])).expanduser()
        if not path.is_file():
            return CheckResult(
                "log_has_no_fatal_error", "unavailable", observed="log missing: %s" % path
            )
        tail_chars = max(1, min(int(args.get("tail_chars", 12000)), 100000))
        content = path.read_text(encoding="utf-8", errors="replace")[-tail_chars:]
        pattern = str(args.get("pattern") or r"\b(?:fatal|panic|traceback)\b")
        matched = re.search(pattern, content, re.I)
        return CheckResult(
            "log_has_no_fatal_error", "failed" if matched else "passed",
            expected="no fatal pattern", observed=matched.group(0) if matched else "clean",
        )

    @staticmethod
    def _command_check(name, command, *, expected, success=None):
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=False,
        )
        predicate = success or (lambda item: item.returncode == 0)
        passed = bool(predicate(result))
        combined = (result.stdout or "") + (result.stderr or "")
        return CheckResult(
            name,
            "passed" if passed else "failed",
            expected=expected,
            observed=combined.strip()[:500],
            evidence=combined.strip()[:2000],
        )


def infer_postconditions(command: str) -> list[dict[str, Any]]:
    """为已知命令族补充结果检查，不信任单独的退出码。"""

    normalized = " ".join((command or "").split())
    checks: list[dict[str, Any]] = []
    service = re.search(
        r"\b(?:systemctl|service)\s+(?:restart|start|reload)\s+([\w@.-]+)",
        normalized,
    )
    if service:
        checks.append(
            {"checker": "service_active", "args": {"service": service.group(1)}}
        )
    if re.search(r"\bnginx\b", normalized) and (
        re.search(r"\bnginx\s+-t\b", normalized)
        or re.search(r"\b(?:systemctl|service)\s+reload\s+nginx\b", normalized)
    ):
        checks.append({"checker": "nginx_config_valid", "args": {}})
    screen = re.search(r"\bscreen\s+(?:-[A-Za-z]+\s+)*-S\s+([\w.-]+)", normalized)
    if screen:
        checks.append(
            {"checker": "screen_session_exists", "args": {"session": screen.group(1)}}
        )
    container = re.search(
        r"\bdocker\s+(?:run|restart|start)\b(?:[^\n]*?--name\s+([\w.-]+)|\s+([\w.-]+))",
        normalized,
    )
    if container:
        checks.append(
            {
                "checker": "container_running",
                "args": {"container": container.group(1) or container.group(2)},
            }
        )
    package_match = re.search(
        r"\b(?:pip|pip3|python\d*\s+-m\s+pip)\s+install\s+([A-Za-z0-9_.-]+)"
        r"(?:==([A-Za-z0-9_.+-]+))?",
        normalized,
    )
    if package_match:
        package = package_match.group(1)
        version = package_match.group(2)
        args = {"package": package}
        if version:
            args["version"] = version
        checks.append({"checker": "package_version", "args": args})
        checks.append(
            {
                "checker": "python_import_succeeds",
                "args": {"module": package.replace("-", "_")},
            }
        )
    copy_match = re.search(r"\b(?:cp|install)\b[^\n]*\s+(\S+)\s*$", normalized)
    if copy_match:
        checks.append(
            {"checker": "file_exists", "args": {"path": copy_match.group(1)}}
        )
    return _deduplicate(checks)


def ensure_postconditions(
    command: str,
    declared: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    checks = _deduplicate(list(declared) + infer_postconditions(command))
    if checks:
        return checks, "full"
    risk, _ = PrivilegedRiskPolicy().classify_command(command)
    if risk == "readonly":
        return [{"checker": "exit_code_zero", "args": {}}], "full"
    return [{"checker": "exit_code_zero", "args": {}}], "partial"


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = repr(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output
