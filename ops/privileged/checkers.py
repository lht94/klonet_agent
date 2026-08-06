"""执行后证据检查器注册表。"""

from __future__ import annotations

import ast
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from klonet_agent.ops.privileged.contracts import CheckResult, ExecutionEvidence
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy
from klonet_agent.tools.environment import redact_sensitive_text


Checker = Callable[[Dict[str, Any], Optional[ExecutionEvidence]], CheckResult]
_STATIC_ATTRIBUTE_UNAVAILABLE = object()
_NGINX_PRIVILEGE_FAILURE = re.compile(
    r"(?i)(?:permission denied|operation not permitted|requires? root|"
    r"interactive authentication required|/run/nginx\.pid|/var/log/nginx/)"
)

CHECKER_ARGUMENT_HINTS = {
    "exit_code_zero": "none",
    "file_exists": "path",
    "file_absent": "path",
    "file_contains": "path,text",
    "json_file_valid": "path",
    "service_active": "service",
    "service_inactive": "service",
    "process_running": "pattern",
    "process_not_running": "pattern",
    "process_pid_absent": "pid",
    "process_cwd_matches": "pid,cwd",
    "port_listening": "port; optional host,timeout",
    "port_not_listening": "port; optional host,timeout",
    "screen_session_exists": "session",
    "screen_session_absent": "session",
    "container_running": "container",
    "container_absent": "container",
    "container_restart_policy": "container,policy",
    "docker_image_state": "image; optional present",
    "docker_network_state": "network; optional present",
    "docker_network_attachment": "network,container; optional attached",
    "network_link_state": "name,state",
    "libvirt_domain_state": "domain,state",
    "ovs_resource_state": "resource_type,name; optional present",
    "http_status": "url; optional status or statuses",
    "git_revision": "repository,revision",
    "user_in_group": "user,group",
    "file_mode": "path,mode",
    "system_package_installed": "package",
    "python_package_state": "python_executable,package; optional present",
    "command_available": "command",
    "python_import_succeeds": "module; optional python_executable,cwd",
    "python_attribute_equals": (
        "module,attribute,expected; optional python_executable,cwd"
    ),
    "package_version": "package; optional version",
    "nginx_config_valid": "optional binary",
    "log_has_no_fatal_error": "path; optional tail_chars,pattern",
}

# Machine-readable counterpart of ``CHECKER_ARGUMENT_HINTS``.  The catalog is
# shown to the model, but model output must still be validated before a checker
# contract is accepted; otherwise a known checker with missing arguments only
# fails after the privileged command has already run.
CHECKER_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "exit_code_zero": (),
    "file_exists": ("path",),
    "file_absent": ("path",),
    "file_contains": ("path", "text"),
    "json_file_valid": ("path",),
    "service_active": ("service",),
    "service_inactive": ("service",),
    "process_running": ("pattern",),
    "process_not_running": ("pattern",),
    "process_pid_absent": ("pid",),
    "process_cwd_matches": ("pid", "cwd"),
    "port_listening": ("port",),
    "port_not_listening": ("port",),
    "screen_session_exists": ("session",),
    "screen_session_absent": ("session",),
    "container_running": ("container",),
    "container_absent": ("container",),
    "container_restart_policy": ("container", "policy"),
    "docker_image_state": ("image",),
    "docker_network_state": ("network",),
    "docker_network_attachment": ("network", "container"),
    "network_link_state": ("name", "state"),
    "libvirt_domain_state": ("domain", "state"),
    "ovs_resource_state": ("resource_type", "name"),
    "http_status": ("url",),
    "git_revision": ("repository", "revision"),
    "user_in_group": ("user", "group"),
    "file_mode": ("path", "mode"),
    "system_package_installed": ("package",),
    "python_package_state": ("python_executable", "package"),
    "command_available": ("command",),
    "python_import_succeeds": ("module",),
    "python_attribute_equals": ("module", "attribute", "expected"),
    "package_version": ("package",),
    "nginx_config_valid": (),
    "log_has_no_fatal_error": ("path",),
}


class DefaultCheckerRegistry:
    """集中管理可审计的确定性检查，未知检查永远不是成功。"""

    def __init__(self) -> None:
        self._checkers: dict[str, Checker] = {
            "exit_code_zero": self._exit_code_zero,
            "file_exists": self._file_exists,
            "file_absent": self._file_absent,
            "file_contains": self._file_contains,
            "json_file_valid": self._json_file_valid,
            "service_active": self._service_active,
            "service_inactive": self._service_inactive,
            "process_running": self._process_running,
            "process_not_running": self._process_not_running,
            "process_pid_absent": self._process_pid_absent,
            "process_cwd_matches": self._process_cwd_matches,
            "port_listening": self._port_listening,
            "port_not_listening": self._port_not_listening,
            "screen_session_exists": self._screen_session_exists,
            "screen_session_absent": self._screen_session_absent,
            "container_running": self._container_running,
            "container_absent": self._container_absent,
            "container_restart_policy": self._container_restart_policy,
            "docker_image_state": self._docker_image_state,
            "docker_network_state": self._docker_network_state,
            "docker_network_attachment": self._docker_network_attachment,
            "network_link_state": self._network_link_state,
            "libvirt_domain_state": self._libvirt_domain_state,
            "ovs_resource_state": self._ovs_resource_state,
            "http_status": self._http_status,
            "git_revision": self._git_revision,
            "user_in_group": self._user_in_group,
            "file_mode": self._file_mode,
            "system_package_installed": self._system_package_installed,
            "python_package_state": self._python_package_state,
            "command_available": self._command_available,
            "python_import_succeeds": self._python_import_succeeds,
            "python_attribute_equals": self._python_attribute_equals,
            "package_version": self._package_version,
            "nginx_config_valid": self._nginx_config_valid,
            "log_has_no_fatal_error": self._log_has_no_fatal_error,
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._checkers))

    def render_catalog(self) -> str:
        return "\n".join(
            "- checker=%s args=%s"
            % (name, CHECKER_ARGUMENT_HINTS.get(name, "see checker contract"))
            for name in self.names
        )

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
        except Exception as exc:
            return CheckResult(
                checker=name,
                status="unavailable",
                observed=(
                    "%s: %s"
                    % (type(exc).__name__, redact_sensitive_text(str(exc)))
                )[:500],
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

    def _file_absent(self, args, evidence):
        del evidence
        path = Path(str(args["path"])).expanduser()
        absent = not path.exists()
        return CheckResult(
            "file_absent", "passed" if absent else "failed",
            expected="absent", observed=str(path),
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

    def _json_file_valid(self, args, evidence):
        del evidence
        path = Path(str(args["path"])).expanduser()
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return CheckResult(
                "json_file_valid",
                "failed",
                expected="valid JSON",
                observed=exc.__class__.__name__,
            )
        return CheckResult(
            "json_file_valid", "passed",
            expected="valid JSON", observed=str(path),
        )

    def _service_active(self, args, evidence):
        del evidence
        return self._command_check(
            "service_active",
            ["systemctl", "is-active", str(args["service"])],
            expected="active",
            success=lambda item: item.returncode == 0 and item.stdout.strip() == "active",
        )

    def _service_inactive(self, args, evidence):
        del evidence
        return self._command_check(
            "service_inactive",
            ["systemctl", "is-active", str(args["service"])],
            expected="inactive",
            success=lambda item: item.stdout.strip() in {
                "inactive", "failed", "unknown",
            },
        )

    def _process_running(self, args, evidence):
        del evidence
        return self._command_check(
            "process_running",
            ["pgrep", "-f", str(args["pattern"])],
            expected="matching PID",
        )

    def _process_not_running(self, args, evidence):
        del evidence
        return self._command_check(
            "process_not_running",
            ["pgrep", "-f", str(args["pattern"])],
            expected="no matching PID",
            success=lambda item: item.returncode == 1,
        )

    def _process_pid_absent(self, args, evidence):
        del evidence
        pid = int(args["pid"])
        proc_path = Path("/proc/%s" % pid)
        absent = not proc_path.exists()
        zombie = False
        if not absent:
            try:
                stat = (proc_path / "stat").read_text(encoding="utf-8", errors="replace")
                after_comm = stat.rsplit(")", 1)[1].split()
                zombie = bool(after_comm and after_comm[0] == "Z")
            except (OSError, IndexError):
                zombie = False
        passed = absent or zombie
        return CheckResult(
            "process_pid_absent",
            "passed" if passed else "failed",
            expected="PID %s absent or zombie after signal" % pid,
            observed="absent" if absent else "zombie" if zombie else "still present",
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

    def _port_not_listening(self, args, evidence):
        result = self._port_listening(args, evidence)
        return CheckResult(
            "port_not_listening",
            "passed" if result.status == "failed" else "failed",
            expected="%s:%s not listening" % (
                args.get("host") or "127.0.0.1",
                args["port"],
            ),
            observed=result.observed,
        )

    def _screen_session_exists(self, args, evidence):
        del evidence
        session = str(args["session"])
        return self._command_check(
            "screen_session_exists",
            ["screen", "-S", session, "-Q", "select", "."],
            expected=session,
        )

    def _screen_session_absent(self, args, evidence):
        del evidence
        session = str(args["session"])
        return self._command_check(
            "screen_session_absent",
            ["screen", "-S", session, "-Q", "select", "."],
            expected="session absent",
            success=lambda item: item.returncode != 0,
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

    def _container_absent(self, args, evidence):
        del evidence
        name = str(args["container"])
        return self._command_check(
            "container_absent",
            ["docker", "inspect", name],
            expected="container absent",
            success=lambda item: item.returncode != 0,
        )

    def _container_restart_policy(self, args, evidence):
        del evidence
        name = str(args["container"])
        expected = str(args["policy"])
        return self._command_check(
            "container_restart_policy",
            ["docker", "inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", name],
            expected=expected,
            success=lambda item: (
                item.returncode == 0 and item.stdout.strip() == expected
            ),
        )

    def _docker_image_state(self, args, evidence):
        del evidence
        image = str(args["image"])
        expected_present = _bool_arg(args.get("present"), default=True)
        return self._command_check(
            "docker_image_state",
            ["docker", "image", "inspect", image],
            expected="present" if expected_present else "absent",
            success=(
                (lambda item: item.returncode == 0)
                if expected_present
                else (lambda item: item.returncode != 0)
            ),
        )

    def _docker_network_state(self, args, evidence):
        del evidence
        network = str(args["network"])
        expected_present = _bool_arg(args.get("present"), default=True)
        return self._command_check(
            "docker_network_state",
            ["docker", "network", "inspect", network],
            expected="present" if expected_present else "absent",
            success=(
                (lambda item: item.returncode == 0)
                if expected_present
                else (lambda item: item.returncode != 0)
            ),
        )

    def _docker_network_attachment(self, args, evidence):
        del evidence
        network = str(args["network"])
        container = str(args["container"])
        expected_attached = _bool_arg(args.get("attached"), default=True)
        return self._command_check(
            "docker_network_attachment",
            [
                "docker",
                "network",
                "inspect",
                "-f",
                "{{json .Containers}}",
                network,
            ],
            expected=(
                "%s attached" % container
                if expected_attached
                else "%s detached" % container
            ),
            success=lambda item: (
                item.returncode == 0
                and (
                    (
                        expected_attached
                        and re.search(
                            r'"Name"\s*:\s*"%s"' % re.escape(container),
                            item.stdout,
                        )
                    )
                    or (
                        not expected_attached
                        and not re.search(
                            r'"Name"\s*:\s*"%s"' % re.escape(container),
                            item.stdout,
                        )
                    )
                )
            ),
        )

    def _network_link_state(self, args, evidence):
        del evidence
        name = str(args["name"])
        expected = str(args.get("state") or "present").lower()
        if expected not in {"present", "absent", "up", "down"}:
            raise ValueError("invalid expected network link state")
        command = ["ip", "-o", "link", "show", "dev", name]
        if expected == "absent":
            return self._command_check(
                "network_link_state",
                command,
                expected="absent",
                success=lambda item: item.returncode != 0,
            )
        return self._command_check(
            "network_link_state",
            command,
            expected=expected,
            success=lambda item: (
                item.returncode == 0
                and (
                    expected == "present"
                    or (
                        expected == "up"
                        and re.search(r"<[^>]*\bUP\b", item.stdout)
                    )
                    or (
                        expected == "down"
                        and not re.search(r"<[^>]*\bUP\b", item.stdout)
                    )
                )
            ),
        )

    def _libvirt_domain_state(self, args, evidence):
        del evidence
        domain = str(args["domain"])
        expected = str(args["state"])
        if expected == "absent":
            return self._command_check(
                "libvirt_domain_state",
                ["virsh", "dominfo", domain],
                expected="absent",
                success=lambda item: item.returncode != 0,
            )
        return self._command_check(
            "libvirt_domain_state",
            ["virsh", "domstate", domain],
            expected=expected,
            success=lambda item: (
                item.returncode == 0
                and expected.lower() in item.stdout.strip().lower()
            ),
        )

    def _ovs_resource_state(self, args, evidence):
        del evidence
        resource_type = str(args["resource_type"])
        name = str(args["name"])
        expected_present = _bool_arg(args.get("present"), default=True)
        command = (
            ["ovs-vsctl", "br-exists", name]
            if resource_type == "bridge"
            else ["ovs-vsctl", "port-to-br", name]
        )
        return self._command_check(
            "ovs_resource_state",
            command,
            expected="present" if expected_present else "absent",
            success=(
                (lambda item: item.returncode == 0)
                if expected_present
                else (lambda item: item.returncode != 0)
            ),
        )

    def _http_status(self, args, evidence):
        del evidence
        url = str(args["url"])
        raw_statuses = args.get("statuses")
        if isinstance(raw_statuses, list) and raw_statuses:
            expected = sorted({int(item) for item in raw_statuses})
        else:
            expected = [int(args.get("status", 200))]
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                observed = int(response.status)
        except urllib.error.HTTPError as exc:
            observed = int(exc.code)
        except (urllib.error.URLError, OSError) as exc:
            return CheckResult(
                "http_status", "failed",
                expected=str(expected), observed=exc.__class__.__name__,
            )
        return CheckResult(
            "http_status",
            "passed" if observed in expected else "failed",
            expected=str(expected),
            observed=str(observed),
        )

    def _git_revision(self, args, evidence):
        del evidence
        repository = str(args["repository"])
        expected = str(args["revision"])
        return self._command_check(
            "git_revision",
            ["git", "-C", repository, "rev-parse", "HEAD"],
            expected=expected,
            success=lambda item: (
                item.returncode == 0
                and item.stdout.strip().startswith(expected)
            ),
        )

    def _user_in_group(self, args, evidence):
        del evidence
        user = str(args["user"])
        group = str(args["group"])
        return self._command_check(
            "user_in_group",
            ["id", "-nG", user],
            expected=group,
            success=lambda item: (
                item.returncode == 0 and group in item.stdout.split()
            ),
        )

    def _file_mode(self, args, evidence):
        del evidence
        path = Path(str(args["path"])).expanduser()
        expected = str(args["mode"]).lstrip("0")
        if not path.exists():
            return CheckResult(
                "file_mode", "failed", expected=expected, observed="missing"
            )
        observed = oct(path.stat().st_mode & 0o7777)[2:]
        return CheckResult(
            "file_mode",
            "passed" if observed == expected else "failed",
            expected=expected,
            observed=observed,
        )

    def _system_package_installed(self, args, evidence):
        del evidence
        package = str(args["package"])
        return self._command_check(
            "system_package_installed",
            ["dpkg-query", "-W", "-f=${Status}", package],
            expected="install ok installed",
            success=lambda item: (
                item.returncode == 0
                and item.stdout.strip() == "install ok installed"
            ),
        )

    def _python_package_state(self, args, evidence):
        del evidence
        python = str(args["python_executable"])
        package = str(args["package"])
        expected_present = _bool_arg(args.get("present"), default=True)
        return self._command_check(
            "python_package_state",
            [python, "-m", "pip", "show", package],
            expected="installed" if expected_present else "absent",
            success=(
                (lambda item: item.returncode == 0)
                if expected_present
                else (lambda item: item.returncode != 0)
            ),
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
        target = str(args["module"]).strip()
        if not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*",
            target,
        ):
            raise ValueError("invalid Python import target")
        python = str(args.get("python_executable") or sys.executable).strip()
        cwd_value = str(args.get("cwd") or "").strip()
        cwd = None
        if cwd_value:
            cwd_path = Path(cwd_value).expanduser()
            if not cwd_path.is_dir():
                raise ValueError("python import cwd is not a directory: %s" % cwd_path)
            cwd = str(cwd_path)
        script = (
            "import importlib,sys\n"
            "p=sys.argv[1].split('.')\n"
            "last=None\n"
            "for i in range(len(p),0,-1):\n"
            " candidate='.'.join(p[:i])\n"
            " try:\n"
            "  obj=importlib.import_module(candidate)\n"
            " except ModuleNotFoundError as exc:\n"
            "  if exc.name and not (candidate==exc.name or candidate.startswith(exc.name+'.')): raise\n"
            "  last=exc; continue\n"
            " for name in p[i:]: obj=getattr(obj,name)\n"
            " raise SystemExit(0)\n"
            "raise last\n"
        )
        return self._command_check(
            "python_import_succeeds",
            [python, "-c", script, target],
            cwd=cwd,
            expected="import %s" % target,
        )

    def _python_attribute_equals(self, args, evidence):
        """Read one public Python attribute and compare its JSON value."""

        del evidence
        module = str(args["module"]).strip()
        attribute = str(args["attribute"]).strip()
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
            raise ValueError("invalid Python module target")
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", attribute):
            raise ValueError("invalid Python attribute target")
        python = str(args.get("python_executable") or sys.executable).strip()
        cwd_value = str(args.get("cwd") or "").strip()
        cwd = None
        if cwd_value:
            cwd_path = Path(cwd_value).expanduser()
            if not cwd_path.is_dir():
                raise ValueError(
                    "python attribute cwd is not a directory: %s" % cwd_path
                )
            cwd = str(cwd_path)
        expected_json = json.dumps(
            args["expected"], ensure_ascii=False, sort_keys=True
        )
        script = (
            "import importlib,json,sys\n"
            "obj=importlib.import_module(sys.argv[1])\n"
            "for name in sys.argv[2].split('.'):\n"
            " obj=getattr(obj,name)\n"
            "expected=json.loads(sys.argv[3])\n"
            "print(json.dumps(obj,ensure_ascii=False,sort_keys=True))\n"
            "raise SystemExit(0 if obj==expected else 1)\n"
        )
        result = self._command_check(
            "python_attribute_equals",
            [python, "-c", script, module, attribute, expected_json],
            cwd=cwd,
            expected="%s.%s == %s" % (module, attribute, expected_json),
        )
        if result.status == "passed" or cwd is None:
            return result
        static = self._static_python_attribute(cwd, module, attribute)
        if static is _STATIC_ATTRIBUTE_UNAVAILABLE:
            return result
        matched = static == args["expected"]
        return CheckResult(
            "python_attribute_equals",
            "passed" if matched else "failed",
            expected="%s.%s == %s" % (module, attribute, expected_json),
            observed=(
                "%s (static literal)"
                % json.dumps(static, ensure_ascii=False, sort_keys=True)
            ),
            evidence="module import failed; compared literal assignment without executing imports",
        )

    @staticmethod
    def _static_python_attribute(cwd, module, attribute):
        """Resolve a literal module/class attribute without importing the module."""

        module_path = Path(cwd).joinpath(*module.split(".")).with_suffix(".py")
        try:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            return _STATIC_ATTRIBUTE_UNAVAILABLE
        body = tree.body
        parts = attribute.split(".")
        for name in parts[:-1]:
            classes = [
                node for node in body
                if isinstance(node, ast.ClassDef) and node.name == name
            ]
            if not classes:
                return _STATIC_ATTRIBUTE_UNAVAILABLE
            body = classes[-1].body
        target_name = parts[-1]
        for node in reversed(body):
            value = None
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == target_name
                for target in node.targets
            ):
                value = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == target_name
            ):
                value = node.value
            if value is None:
                continue
            try:
                return ast.literal_eval(value)
            except (ValueError, TypeError):
                return _STATIC_ATTRIBUTE_UNAVAILABLE
        return _STATIC_ATTRIBUTE_UNAVAILABLE

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
        binary = str(args.get("binary") or "nginx")
        direct = self._command_check(
            "nginx_config_valid",
            [binary, "-t"],
            expected="syntax is ok",
        )
        if direct.status == "passed":
            return direct
        if self._nginx_parse_completed_with_privilege_only_failure(direct):
            direct.status = "passed"
            direct.observed = (
                "syntax parse passed; privileged runtime access deferred"
            )
            return direct
        if not _NGINX_PRIVILEGE_FAILURE.search(
            str(direct.observed or "")
        ):
            return direct

        # ``nginx -t`` parses the full configuration but also attempts to
        # open the configured root-owned PID and log paths.  A non-root
        # verifier must not turn that incidental access failure into a syntax
        # failure, nor should it prompt for credentials.  Retry against an
        # ephemeral copy whose only changes are process-runtime paths; all
        # includes (notably sites-enabled) remain the real files.
        config = Path(
            str(args.get("config") or "/etc/nginx/nginx.conf")
        ).expanduser()
        try:
            original = config.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return direct
        with tempfile.TemporaryDirectory(prefix="klonet-nginx-check-") as raw_dir:
            directory = Path(raw_dir)
            candidate = directory / "nginx.conf"
            # Preserve the original prefix-relative include semantics without
            # copying configuration or secrets.  Debian-style module snippets
            # also load ``modules/<name>.so`` relative to the prefix, while
            # the actual modules live under /usr/lib/nginx/modules.
            try:
                siblings = tuple(config.parent.iterdir())
            except OSError:
                siblings = ()
            for sibling in siblings:
                if sibling == config:
                    continue
                link = directory / sibling.name
                try:
                    link.symlink_to(
                        sibling,
                        target_is_directory=sibling.is_dir(),
                    )
                except OSError:
                    pass
            modules = directory / "modules"
            system_modules = Path("/usr/lib/nginx/modules")
            if not modules.exists() and system_modules.is_dir():
                try:
                    modules.symlink_to(system_modules, target_is_directory=True)
                except OSError:
                    pass
            isolated = re.sub(
                r"(?im)(^\s*pid\s+)[^;]+;",
                r"\g<1>%s;" % (directory / "nginx.pid"),
                original,
            )
            isolated = re.sub(
                r"(?i)\berror_log\s+[^;]+;",
                "error_log stderr;",
                isolated,
            )
            isolated = re.sub(
                r"(?i)\baccess_log\s+[^;]+;",
                "access_log off;",
                isolated,
            )
            try:
                candidate.write_text(isolated, encoding="utf-8")
            except (OSError, UnicodeError):
                return direct
            retried = self._command_check(
                "nginx_config_valid",
                [
                    binary,
                    "-t",
                    "-p",
                    str(directory) + "/",
                    "-c",
                    str(candidate),
                ],
                expected="syntax is ok",
            )
        if retried.status == "passed":
            retried.observed = "non-privileged config parse passed: %s" % (
                retried.observed
            )
        return retried

    @staticmethod
    def _nginx_parse_completed_with_privilege_only_failure(result):
        detail = str(result.evidence or result.observed or "")
        if "syntax is ok" not in detail.lower():
            return False
        fatal_lines = [
            line
            for line in detail.splitlines()
            if "[emerg]" in line.lower() or "[alert]" in line.lower()
        ]
        return bool(fatal_lines) and all(
            _NGINX_PRIVILEGE_FAILURE.search(line) for line in fatal_lines
        )

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
    def _command_check(
        name,
        command,
        *,
        expected,
        success=None,
        cwd=None,
    ):
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=False,
            cwd=cwd,
        )
        predicate = success or (lambda item: item.returncode == 0)
        passed = bool(predicate(result))
        combined = (result.stdout or "") + (result.stderr or "")
        detail = combined.strip()
        return CheckResult(
            name,
            "passed" if passed else "failed",
            expected=expected,
            observed=(detail[:500] if passed else detail[-500:]),
            evidence=(detail[:2000] if passed else detail[-2000:]),
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


def _bool_arg(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid boolean value")
