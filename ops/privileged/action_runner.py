"""Direct registered-action backend for Ops-Privilege.

This backend deliberately does not import or invoke the ordinary Ops recipe
runner, ``klonet-agent-op``, or its sudoers contract.  The planner still emits
registered actions; this module compiles confirmed actions into bounded argv
execution and local filesystem operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from klonet_agent.ops.actions import (
    OpsActionRegistry,
    configured_ops_action_registry,
)
from klonet_agent.ops.command_policy import command_exists, decide_ops_command
from klonet_agent.ops.privileged.contracts import PrivilegedStep
from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES


DIRECT_PRIVILEGED_ACTIONS = frozenset(
    {
        "manual_checkpoint",
        "validate_project_files",
        "prepare_project_files",
        "extract_archive",
        "run_install_script",
        "ensure_shared_services",
        "start_screen_component",
        "start_platform_screens",
        "restart_screen_component",
        "stop_screen_component",
        "stop_platform_screens",
        "write_ops_file",
        "replace_text_in_file",
        "install_nginx_config",
        "reload_nginx",
        "start_docker_container",
        "run_ops_command",
        "copy_files",
        "move_path",
        "create_directory",
        "remove_path",
        "manage_service",
        "manage_process",
        "manage_container",
        "install_system_packages",
        "install_python_packages",
        "manage_file_permissions",
        "git_operation",
        "ensure_user_group",
        "remove_python_package_entries",
        "sync_directory",
        "merge_json_file",
        "start_redis_instance",
        "run_reviewed_script",
        "manage_libvirt_domain",
        "manage_docker_network",
        "manage_docker_image",
        "manage_network_link",
        "manage_ovs_resource",
    }
)

_COMPONENTS = ("master", "celery", "web_terminal", "worker")
_COMPONENT_SUFFIX = {
    "master": "m",
    "celery": "c",
    "web_terminal": "web",
    "worker": "w",
}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:^|[._-])(?:\.env|id_rsa|id_ed25519|private[_-]?key|"
    r"secret|token|credential|password)(?:$|[._-])"
)
_SENSITIVE_CONTENT = re.compile(
    r"(?i)\b[A-Za-z0-9_-]*(?:password|passwd|pwd|api[_-]?key|"
    r"secret|token)[A-Za-z0-9_-]*\s*[:=]"
)
_SAFE_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:!<>=~,-]{0,120}$")
_SAFE_SCRIPT_ARG = re.compile(r"^[A-Za-z0-9_./:@+=,-]{1,240}$")
_INSTALL_SCRIPT_ARGS = {
    "base_requ_setup.sh": ("NORMAL",),
    "docker_service.sh": (),
    "docker_master.sh": (),
    "docker_worker.sh": (),
}
_SAFE_TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".service",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_PROTECTED_REMOVE_PATHS = {
    Path("/"),
    Path("/bin"),
    Path("/boot"),
    Path("/dev"),
    Path("/etc"),
    Path("/home"),
    Path("/lib"),
    Path("/lib64"),
    Path("/opt"),
    Path("/proc"),
    Path("/root"),
    Path("/run"),
    Path("/sbin"),
    Path("/srv"),
    Path("/sys"),
    Path("/tmp"),
    Path("/usr"),
    Path("/var"),
}


@dataclass(frozen=True)
class DirectActionResult:
    status: str
    output: str
    next_required_action: str = ""


class DirectPrivilegedActionRunner:
    """Execute one confirmed action without the ordinary Ops helper."""

    def __init__(
        self,
        *,
        action_registry: OpsActionRegistry | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.action_registry = (
            action_registry or configured_ops_action_registry()
        )
        self.command_runner = command_runner or self._run_command

    def __call__(self, step: PrivilegedStep) -> DirectActionResult:
        spec = self.action_registry.get(step.action)
        if spec is None:
            return self._blocked("action_not_registered=%s" % step.action)
        if spec.name not in DIRECT_PRIVILEGED_ACTIONS:
            return self._blocked(
                "direct_privileged_capability_not_implemented=%s" % spec.name,
                next_action="replan_with_supported_action",
            )
        problem = self.action_registry.validate_args(spec, step.args)
        if problem:
            return self._blocked(problem)
        handler = getattr(self, "_action_" + spec.name, None)
        if handler is None:
            return self._blocked(
                "direct_privileged_handler_missing=%s" % spec.name
            )
        try:
            return handler(step)
        except subprocess.TimeoutExpired as exc:
            return DirectActionResult(
                "failed",
                "direct_action_timeout seconds=%s environment_changed=unknown"
                % int(exc.timeout or step.timeout),
                "inspect_runtime",
            )
        except subprocess.CalledProcessError as exc:
            return DirectActionResult(
                "failed",
                (
                    "direct_action_failed returncode=%s stdout=%s stderr=%s "
                    "environment_changed=unknown"
                )
                % (
                    exc.returncode,
                    _one_line(exc.stdout),
                    _one_line(exc.stderr),
                ),
                "inspect_runtime",
            )
        except OSError as exc:
            return DirectActionResult(
                "failed",
                "direct_action_os_error=%s environment_changed=unknown"
                % _one_line(exc),
                "inspect_runtime",
            )

    def _action_manual_checkpoint(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        reason = _one_line(step.args.get("reason") or "已确认检查点")
        return DirectActionResult(
            "completed",
            "action=manual_checkpoint reason=%s environment unchanged" % reason,
        )

    def _action_validate_project_files(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        root, problem = _project_root(step)
        if problem:
            return self._blocked(problem)
        found = _entry_sources(root)
        missing = [name for name in REQUIRED_ENTRY_FILES if name not in found]
        package = root / "vemu_uestc" / "__init__.py"
        config = root / "vemu_uestc" / "vemu_config" / "config.py"
        if missing or not package.is_file() or not config.is_file():
            details = []
            if missing:
                details.append("missing_entries=" + ",".join(missing))
            if not package.is_file():
                details.append("backend_package_missing")
            if not config.is_file():
                details.append("backend_config_missing")
            return self._blocked(
                "invalid_project_layout " + " ".join(details)
            )
        return DirectActionResult(
            "completed",
            (
                "action=validate_project_files project_root=%s "
                "backend_package=%s config=%s entry_sources=%s "
                "environment unchanged"
            )
            % (
                root,
                root / "vemu_uestc",
                config,
                ",".join(found[name] for name in REQUIRED_ENTRY_FILES),
            ),
        )

    def _action_prepare_project_files(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        root, problem = _project_root(step)
        if problem:
            return self._blocked(problem)
        raw_source = str(step.args.get("source_root") or "").strip()
        source = (
            Path(raw_source).expanduser()
            if raw_source
            else _default_entry_source(root)
        )
        try:
            source = source.resolve()
        except OSError:
            return self._blocked("invalid_source_root")
        if not source.is_dir():
            return self._blocked("source_root_not_found=%s" % source)
        missing = [
            name for name in REQUIRED_ENTRY_FILES
            if not (source / name).is_file()
        ]
        if missing:
            return self._blocked(
                "entry_sources_missing=%s" % ",".join(missing)
            )
        for name in REQUIRED_ENTRY_FILES:
            shutil.copy2(source / name, root / name)
        return DirectActionResult(
            "completed",
            (
                "action=prepare_project_files source_root=%s "
                "project_root=%s copied=%s environment_changed=true"
            )
            % (source, root, ",".join(REQUIRED_ENTRY_FILES)),
        )

    def _action_extract_archive(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        archive = _absolute_path(step.args.get("archive_path"))
        destination = _absolute_path(step.args.get("destination_dir"))
        if (
            archive is None
            or destination is None
            or not archive.is_file()
            or _protected_target(destination)
        ):
            return self._blocked("invalid_archive_or_destination")
        try:
            members = _safe_archive_members(archive, destination)
        except (tarfile.TarError, zipfile.BadZipFile, OSError, ValueError) as exc:
            return self._blocked(
                "archive_preflight_failed=%s" % _one_line(exc)
            )
        if len(members) > 20_000:
            return self._blocked("archive_member_limit_exceeded")
        try:
            destination.mkdir(parents=True, exist_ok=True)
            if tarfile.is_tarfile(str(archive)):
                with tarfile.open(str(archive), "r:*") as opened:
                    opened.extractall(str(destination))
            else:
                with zipfile.ZipFile(str(archive)) as opened:
                    opened.extractall(str(destination))
        except PermissionError:
            program = "tar" if tarfile.is_tarfile(str(archive)) else "unzip"
            argv = (
                [program, "-xf", str(archive), "-C", str(destination)]
                if program == "tar"
                else [program, str(archive), "-d", str(destination)]
            )
            result = self._command(_sudo_if_needed(argv), timeout=step.timeout)
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "archive_extract_failed stderr=%s environment_changed=unknown"
                    % _one_line(result.stderr),
                    "inspect_path_permissions",
                )
        return DirectActionResult(
            "completed",
            (
                "action=extract_archive archive=%s destination=%s "
                "members=%s environment_changed=true"
            )
            % (archive, destination, len(members)),
        )

    def _action_run_install_script(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        script_dir = _absolute_path(step.args.get("script_dir"))
        script_name = str(step.args.get("script_name") or "").strip()
        script_args = _string_list(step.args.get("script_args"))
        if (
            script_dir is None
            or not script_dir.is_dir()
            or script_name not in _INSTALL_SCRIPT_ARGS
            or tuple(script_args) != _INSTALL_SCRIPT_ARGS[script_name]
        ):
            return self._blocked("unsupported_install_script_or_args")
        script = script_dir / script_name
        if not script.is_file() or script.is_symlink():
            return self._blocked("install_script_not_regular_file")
        result = self._command(
            _sudo_if_needed(["bash", str(script), *script_args]),
            cwd=script_dir,
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "install_script_failed name=%s returncode=%s stderr=%s environment_changed=unknown"
                % (script_name, result.returncode, _one_line(result.stderr, 4000)),
                "inspect_install_scripts",
            )
        return DirectActionResult(
            "completed",
            "action=run_install_script name=%s args=%s environment_changed=true"
            % (script_name, ",".join(script_args) or "none"),
        )

    def _action_ensure_shared_services(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        forwarded = PrivilegedStep.from_dict(step.to_dict())
        forwarded.action = "run_install_script"
        forwarded.args = {
            "script_dir": step.args.get("script_dir"),
            "script_name": "docker_service.sh",
            "script_args": [],
        }
        result = self._action_run_install_script(forwarded)
        if result.status == "completed":
            return DirectActionResult(
                "completed",
                "action=ensure_shared_services script=docker_service.sh environment_changed=true",
            )
        return result

    def _action_start_screen_component(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        platform, root, problem = _platform_root(step)
        component = str(step.args.get("component") or "").strip()
        session = str(step.args.get("screen_session") or "").strip()
        if problem:
            return self._blocked(problem)
        if component not in _COMPONENTS:
            return self._blocked("invalid_component")
        expected = "%s_%s" % (platform, _COMPONENT_SUFFIX[component])
        if session != expected:
            return self._blocked("screen_session_mismatch expected=%s" % expected)
        if session in self._existing_screen_sessions():
            return self._blocked("screen_session_already_exists=%s" % session)
        return self._start_one_component(component, session, root, step)

    def _action_start_platform_screens(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        platform, root, problem = _platform_root(step)
        if problem:
            return self._blocked(problem)
        missing = [
            name for name in REQUIRED_ENTRY_FILES
            if not (root / name).is_file()
        ]
        if missing:
            return self._blocked(
                "runtime_entries_missing=%s" % ",".join(missing)
            )
        python = _python_executable(step.args)
        if not python:
            return self._blocked("python3.8_not_found")
        existing = self._existing_screen_sessions()
        sessions = {
            component: "%s_%s" % (platform, _COMPONENT_SUFFIX[component])
            for component in _COMPONENTS
        }
        conflicts = [
            session for session in sessions.values() if session in existing
        ]
        if conflicts:
            return self._blocked(
                "screen_session_already_exists=%s" % ",".join(conflicts)
            )
        configured_ports = _configured_runtime_ports(root)
        if not configured_ports:
            return self._blocked(
                "runtime_ports_not_found_in_active_config"
            )
        ss = shutil.which("ss")
        if ss:
            listeners = self._command(
                [ss, "-ltn"],
                timeout=min(step.timeout, 20),
            )
            occupied = _listening_ports(
                "%s\n%s" % (listeners.stdout or "", listeners.stderr or ""),
                configured_ports,
            )
            if occupied:
                return self._blocked(
                    "runtime_port_already_listening=%s"
                    % ",".join(str(port) for port in occupied)
                )
        preflight = {
            "master": [
                python, "-m", "gunicorn", "--check-config",
                "-c", "gun.py", "master_main:flask_app",
            ],
            "worker": [
                python, "-m", "gunicorn", "--check-config",
                "-c", "worker_gun.py", "worker_main:flask_app",
            ],
            "celery": [python, "-c", "from celery_worker import celery"],
            "web_terminal": [python, "-c", "import web_terminal_main"],
        }
        for component in _COMPONENTS:
            result = self._command(
                preflight[component],
                cwd=root,
                timeout=min(step.timeout, 30),
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    (
                        "startup_preflight_failed component=%s returncode=%s "
                        "stderr=%s environment_changed=false"
                    )
                    % (
                        component,
                        result.returncode,
                        _one_line(result.stderr or result.stdout, 10000),
                    ),
                    "inspect_project_layout",
                )
        commands = _component_commands(python)
        started = []
        for component in _COMPONENTS:
            shell_command = "cd %s && exec %s" % (
                shlex.quote(str(root)),
                shlex.join(commands[component]),
            )
            result = self._command(
                [
                    "screen",
                    "-dmS",
                    sessions[component],
                    "bash",
                    "-lc",
                    shell_command,
                ],
                cwd=root,
                timeout=min(step.timeout, 30),
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    (
                        "screen_start_failed component=%s returncode=%s "
                        "stderr=%s started=%s environment_changed=unknown"
                    )
                    % (
                        component,
                        result.returncode,
                        _one_line(result.stderr),
                        ",".join(started) or "none",
                    ),
                    "inspect_runtime",
                )
            started.append(sessions[component])
        return DirectActionResult(
            "completed",
            (
                "action=start_platform_screens platform=%s project_root=%s "
                "python=%s screen_sessions=%s environment_changed=true"
            )
            % (platform, root, python, ",".join(started)),
        )

    def _action_restart_screen_component(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        platform, root, problem = _platform_root(step)
        component = str(step.args.get("component") or "").strip()
        session = str(step.args.get("screen_session") or "").strip()
        if problem:
            return self._blocked(problem)
        if component not in _COMPONENTS or not _safe_token(session):
            return self._blocked("invalid_component_or_screen_session")
        expected = "%s_%s" % (platform, _COMPONENT_SUFFIX[component])
        if session != expected:
            return self._blocked(
                "screen_session_mismatch expected=%s" % expected
            )
        self._command(["screen", "-S", session, "-X", "quit"], timeout=20)
        return self._start_one_component(
            component, session, root, step
        )

    def _action_stop_screen_component(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        session = str(step.args.get("screen_session") or "").strip()
        if not _safe_token(session):
            return self._blocked("invalid_screen_session")
        result = self._command(
            ["screen", "-S", session, "-X", "quit"],
            timeout=min(step.timeout, 30),
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "screen_stop_failed session=%s stderr=%s environment_changed=unknown"
                % (session, _one_line(result.stderr)),
                "inspect_runtime",
            )
        return DirectActionResult(
            "completed",
            "action=stop_screen_component session=%s environment_changed=true"
            % session,
        )

    def _action_stop_platform_screens(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        platform = str(step.args.get("platform") or "").strip()
        if not _safe_token(platform):
            return self._blocked("invalid_platform")
        stopped = []
        for component in _COMPONENTS:
            session = "%s_%s" % (platform, _COMPONENT_SUFFIX[component])
            result = self._command(
                ["screen", "-S", session, "-X", "quit"],
                timeout=min(step.timeout, 30),
            )
            if result.returncode == 0:
                stopped.append(session)
        if not stopped:
            return self._blocked("platform_screen_sessions_not_found")
        return DirectActionResult(
            "completed",
            "action=stop_platform_screens stopped=%s environment_changed=true"
            % ",".join(stopped),
        )

    def _action_write_ops_file(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        path = _absolute_path(step.args.get("path"))
        content = str(step.args.get("content") or "")
        if path is None:
            return self._blocked("invalid_write_path")
        if _SENSITIVE_NAME.search(path.name) or _SENSITIVE_CONTENT.search(content):
            return self._blocked("sensitive_file_or_content_not_allowed")
        mode = str(step.args.get("mode") or "replace").strip()
        if mode != "replace":
            return self._blocked(
                "direct_incremental_write_not_implemented",
                next_action="replan_with_replace_or_run_ops_command",
            )
        result = self._write_file(path, content, step.timeout)
        if result:
            return result
        return DirectActionResult(
            "completed",
            "action=write_ops_file path=%s bytes=%s environment_changed=true"
            % (path, len(content.encode("utf-8"))),
        )

    def _action_replace_text_in_file(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        path = _absolute_path(step.args.get("path"))
        old_text = str(step.args.get("old_text") or "")
        new_text = str(step.args.get("new_text") or "")
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() not in _SAFE_TEXT_SUFFIXES
            or _SENSITIVE_NAME.search(path.name)
        ):
            return self._blocked("invalid_text_replacement_target")
        if (
            not old_text
            or old_text == new_text
            or len(old_text) > 4000
            or len(new_text) > 4000
            or _SENSITIVE_CONTENT.search(old_text)
            or _SENSITIVE_CONTENT.search(new_text)
        ):
            return self._blocked("invalid_or_sensitive_replacement")
        try:
            if path.stat().st_size > 2_000_000:
                return self._blocked("replacement_target_too_large")
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._blocked("replacement_target_not_readable_text")
        matches = content.count(old_text)
        if matches != 1:
            return self._blocked(
                "replacement_match_count=%s expected=1" % matches
            )
        updated = content.replace(old_text, new_text, 1)
        backup = path.with_name(
            "%s.klonet-agent.bak.%s" % (path.name, time.time_ns())
        )
        try:
            if os.access(path.parent, os.W_OK):
                shutil.copy2(path, backup)
            else:
                copied = self._command(
                    _sudo_if_needed(["cp", "-p", str(path), str(backup)]),
                    timeout=step.timeout,
                )
                if copied.returncode != 0:
                    return DirectActionResult(
                        "failed",
                        "replacement_backup_failed path=%s stderr=%s environment_changed=false"
                        % (path, _one_line(copied.stderr)),
                        "inspect_path_permissions",
                    )
            result = self._write_file(path, updated, step.timeout)
        except OSError as exc:
            return DirectActionResult(
                "failed",
                "replacement_failed path=%s error=%s environment_changed=unknown"
                % (path, exc.__class__.__name__),
                "inspect_path_permissions",
            )
        if result:
            return result
        return DirectActionResult(
            "completed",
            (
                "action=replace_text_in_file path=%s backup=%s "
                "matches=1 environment_changed=true"
            )
            % (path, backup),
        )

    def _action_install_nginx_config(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        source = _absolute_path(step.args.get("source_path"))
        name = str(step.args.get("config_name") or "").strip()
        if source is None or not source.is_file():
            return self._blocked("nginx_source_not_found")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}\.conf", name):
            return self._blocked("invalid_nginx_config_name")
        destination = Path("/etc/nginx/sites-available") / name
        command = _sudo_if_needed(
            ["install", "-o", "root", "-g", "root", "-m", "0644",
             str(source), str(destination)]
        )
        result = self._command(command, timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "nginx_config_install_failed stderr=%s environment_changed=unknown"
                % _one_line(result.stderr),
                "inspect_nginx_routes",
            )
        return DirectActionResult(
            "completed",
            "action=install_nginx_config destination=%s environment_changed=true"
            % destination,
        )

    def _action_reload_nginx(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        nginx = shutil.which("nginx")
        if not nginx:
            return self._blocked("nginx_not_found")
        check = self._command(
            _sudo_if_needed([nginx, "-t"]),
            timeout=min(step.timeout, 30),
        )
        if check.returncode != 0:
            return DirectActionResult(
                "failed",
                "nginx_config_test_failed stderr=%s environment_changed=false"
                % _one_line(check.stderr),
                "inspect_nginx_routes",
            )
        reload_result = self._command(
            _sudo_if_needed([nginx, "-s", "reload"]),
            timeout=min(step.timeout, 30),
        )
        if reload_result.returncode != 0:
            return DirectActionResult(
                "failed",
                "nginx_reload_failed stderr=%s environment_changed=unknown"
                % _one_line(reload_result.stderr),
                "inspect_nginx_routes",
            )
        return DirectActionResult(
            "completed",
            "action=reload_nginx config_test=passed environment_changed=true",
        )

    def _action_start_docker_container(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        name = str(step.args.get("name") or "").strip()
        if not _safe_token(name):
            return self._blocked("invalid_container_name")
        docker = shutil.which("docker")
        if not docker:
            return self._blocked("docker_not_found")
        result = self._command(
            _sudo_if_needed([docker, "start", name]),
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "docker_start_failed name=%s stderr=%s environment_changed=unknown"
                % (name, _one_line(result.stderr)),
                "inspect_service_health",
            )
        return DirectActionResult(
            "completed",
            "action=start_docker_container name=%s environment_changed=true"
            % name,
        )

    def _action_run_ops_command(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        decision = decide_ops_command(step.args)
        if not decision.allowed:
            return self._blocked(
                "controlled_argv_not_allowed=%s" % decision.reason
            )
        if not command_exists(decision.program):
            return self._blocked(
                "program_not_found=%s" % decision.program
            )
        command = [decision.program, *decision.argv]
        if decision.requires_sudo:
            command = _sudo_if_needed(command)
        env = os.environ.copy()
        env.update(dict(decision.env))
        result = self._command(
            command,
            cwd=Path(decision.cwd) if decision.cwd else None,
            env=env,
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                (
                    "controlled_argv_failed category=%s returncode=%s "
                    "stdout=%s stderr=%s environment_changed=unknown"
                )
                % (
                    decision.category,
                    result.returncode,
                    _one_line(result.stdout),
                    _one_line(result.stderr),
                ),
                "inspect_runtime",
            )
        return DirectActionResult(
            "completed",
            (
                "action=run_ops_command category=%s program=%s cwd=%s "
                "output=%s environment_changed=%s"
            )
            % (
                decision.category,
                decision.program,
                decision.cwd or ".",
                _one_line(result.stdout),
                "false" if decision.risk == "normal" else "unknown",
            ),
        )

    def _action_copy_files(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        sources = _path_list(step.args.get("sources"))
        destination = _absolute_path(step.args.get("destination"))
        if not sources or destination is None:
            return self._blocked("copy_sources_and_destination_required")
        if not destination.is_dir():
            return self._blocked("copy_destination_must_be_directory")
        missing = [str(path) for path in sources if not path.is_file()]
        if missing:
            return self._blocked("copy_sources_missing=%s" % ",".join(missing))
        copied = []
        try:
            for source in sources:
                target = destination / source.name
                shutil.copy2(source, target)
                copied.append(str(target))
        except PermissionError:
            for source in sources:
                target = destination / source.name
                result = self._command(
                    _sudo_if_needed(
                        ["install", "-m", "0644", str(source), str(target)]
                    ),
                    timeout=step.timeout,
                )
                if result.returncode != 0:
                    return DirectActionResult(
                        "failed",
                        "copy_files_failed target=%s stderr=%s environment_changed=unknown"
                        % (target, _one_line(result.stderr)),
                        "inspect_path_permissions",
                    )
                copied.append(str(target))
        return DirectActionResult(
            "completed",
            "action=copy_files copied=%s environment_changed=true"
            % ",".join(copied),
        )

    def _action_move_path(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        source = _absolute_path(step.args.get("source"))
        destination = _absolute_path(step.args.get("destination"))
        if source is None or destination is None or not source.exists():
            return self._blocked("move_source_or_destination_invalid")
        if _protected_target(source) or _protected_target(destination):
            return self._blocked("move_protected_path_refused")
        try:
            result_path = shutil.move(str(source), str(destination))
        except PermissionError:
            result = self._command(
                _sudo_if_needed(["mv", str(source), str(destination)]),
                timeout=step.timeout,
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "move_path_failed stderr=%s environment_changed=unknown"
                    % _one_line(result.stderr),
                    "inspect_path_permissions",
                )
            result_path = str(destination)
        return DirectActionResult(
            "completed",
            "action=move_path destination=%s environment_changed=true"
            % result_path,
        )

    def _action_create_directory(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        path = _absolute_path(step.args.get("path"))
        if path is None or _protected_target(path):
            return self._blocked("invalid_or_protected_directory")
        parents = _truthy(step.args.get("parents"), default=True)
        mode_text = str(step.args.get("mode") or "0755")
        if not re.fullmatch(r"0?[0-7]{3,4}", mode_text):
            return self._blocked("invalid_directory_mode")
        mode = int(mode_text, 8)
        try:
            path.mkdir(parents=parents, exist_ok=True, mode=mode)
        except PermissionError:
            argv = ["mkdir"]
            if parents:
                argv.append("-p")
            argv.append(str(path))
            result = self._command(
                _sudo_if_needed(argv),
                timeout=step.timeout,
            )
            if result.returncode == 0:
                result = self._command(
                    _sudo_if_needed(["chmod", mode_text, str(path)]),
                    timeout=step.timeout,
                )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "create_directory_failed stderr=%s environment_changed=unknown"
                    % _one_line(result.stderr),
                    "inspect_path_permissions",
                )
        return DirectActionResult(
            "completed",
            "action=create_directory path=%s mode=%s environment_changed=true"
            % (path, oct(mode)),
        )

    def _action_remove_path(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        path = _absolute_path(step.args.get("path"))
        if path is None or _protected_target(path):
            return self._blocked("remove_protected_or_invalid_path")
        if not path.exists() and not path.is_symlink():
            return DirectActionResult(
                "completed",
                "action=remove_path path=%s already_absent=true environment_changed=false"
                % path,
            )
        recursive = _truthy(step.args.get("recursive"))
        if path.is_dir() and any(path.iterdir()) and not recursive:
            return self._blocked("nonempty_directory_requires_recursive=true")
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path) if recursive else path.rmdir()
            else:
                path.unlink()
        except PermissionError:
            argv = ["rm", "-rf" if recursive else "-f", "--", str(path)]
            result = self._command(
                _sudo_if_needed(argv),
                timeout=step.timeout,
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "remove_path_failed stderr=%s environment_changed=unknown"
                    % _one_line(result.stderr),
                    "inspect_path_permissions",
                )
        return DirectActionResult(
            "completed",
            "action=remove_path path=%s environment_changed=true" % path,
        )

    def _action_manage_service(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        service = str(step.args.get("service") or "").strip()
        operation = str(step.args.get("operation") or "").strip()
        if not _safe_token(service):
            return self._blocked("invalid_service_name")
        allowed = {"start", "stop", "restart", "reload", "enable", "disable"}
        if operation not in allowed:
            return self._blocked("unsupported_service_operation")
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return self._blocked("systemctl_not_found")
        result = self._command(
            _sudo_if_needed([systemctl, operation, service]),
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "manage_service_failed service=%s operation=%s stderr=%s environment_changed=unknown"
                % (service, operation, _one_line(result.stderr)),
                "inspect_service",
            )
        verify_operation = (
            "is-active" if operation in {"start", "restart", "reload"}
            else "is-enabled" if operation == "enable"
            else ""
        )
        if verify_operation:
            verify = self._command(
                [systemctl, verify_operation, service],
                timeout=min(step.timeout, 30),
            )
            if verify.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "service_postcondition_failed service=%s state=%s environment_changed=unknown"
                    % (service, _one_line(verify.stdout or verify.stderr)),
                    "inspect_service",
                )
        return DirectActionResult(
            "completed",
            "action=manage_service service=%s operation=%s environment_changed=true"
            % (service, operation),
        )

    def _action_manage_process(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        try:
            pid = int(step.args.get("pid"))
        except (TypeError, ValueError):
            return self._blocked("invalid_pid")
        signal_name = str(step.args.get("signal") or "").upper()
        if pid <= 1 or signal_name not in {"TERM", "KILL", "HUP", "INT"}:
            return self._blocked("pid_or_signal_not_allowed")
        expected = str(step.args.get("expected_command") or "").strip()
        cmdline = _proc_cmdline(pid)
        if not cmdline:
            return self._blocked("pid_not_found")
        if expected and expected not in cmdline:
            return self._blocked("pid_identity_mismatch")
        result = self._command(
            _sudo_if_needed(["kill", "-%s" % signal_name, str(pid)]),
            timeout=min(step.timeout, 30),
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "manage_process_failed pid=%s stderr=%s environment_changed=unknown"
                % (pid, _one_line(result.stderr)),
                "inspect_process",
            )
        return DirectActionResult(
            "completed",
            "action=manage_process pid=%s signal=%s observed_command=%s environment_changed=true"
            % (pid, signal_name, _one_line(cmdline, 300)),
        )

    def _action_manage_container(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        engine = str(step.args.get("engine") or "docker").strip()
        name = str(step.args.get("name") or "").strip()
        operation = str(step.args.get("operation") or "").strip()
        if engine != "docker" or not _safe_token(name):
            return self._blocked("invalid_container_engine_or_name")
        if operation not in {
            "start", "stop", "restart", "remove", "set_restart_policy",
        }:
            return self._blocked("unsupported_container_operation")
        docker = shutil.which("docker")
        if not docker:
            return self._blocked("docker_not_found")
        if operation == "set_restart_policy":
            policy = str(step.args.get("restart_policy") or "").strip()
            if policy not in {"no", "always", "unless-stopped", "on-failure"}:
                return self._blocked("invalid_container_restart_policy")
            argv = [docker, "update", "--restart", policy, name]
        else:
            argv = [docker, "rm" if operation == "remove" else operation]
            if operation == "remove" and _truthy(step.args.get("force")):
                argv.append("-f")
            argv.append(name)
        result = self._command(
            _sudo_if_needed(argv),
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "manage_container_failed name=%s operation=%s stderr=%s environment_changed=unknown"
                % (name, operation, _one_line(result.stderr)),
                "inspect_docker",
            )
        return DirectActionResult(
            "completed",
            "action=manage_container name=%s operation=%s environment_changed=true"
            % (name, operation),
        )

    def _action_install_system_packages(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        packages = _string_list(step.args.get("packages"))
        if not packages or any(not _SAFE_PACKAGE.fullmatch(item) for item in packages):
            return self._blocked("invalid_system_package_names")
        apt = shutil.which("apt-get") or shutil.which("apt")
        if not apt:
            return self._blocked("apt_not_found")
        if _truthy(step.args.get("update")):
            update = self._command(
                _sudo_if_needed([apt, "update"]),
                timeout=step.timeout,
            )
            if update.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "apt_update_failed stderr=%s environment_changed=unknown"
                    % _one_line(update.stderr),
                    "inspect_system",
                )
        result = self._command(
            _sudo_if_needed(
                [apt, "install", "-y", "--no-install-recommends", *packages]
            ),
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "system_package_install_failed packages=%s stderr=%s environment_changed=unknown"
                % (",".join(packages), _one_line(result.stderr)),
                "inspect_system",
            )
        return DirectActionResult(
            "completed",
            "action=install_system_packages packages=%s environment_changed=true"
            % ",".join(packages),
        )

    def _action_install_python_packages(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        python = _absolute_path(step.args.get("python_executable"))
        operation = str(step.args.get("operation") or "").strip()
        packages = _string_list(step.args.get("packages"))
        if (
            python is None
            or not python.is_file()
            or operation not in {"install", "uninstall"}
            or not packages
            or any(not _SAFE_PACKAGE.fullmatch(item) for item in packages)
        ):
            return self._blocked("invalid_python_package_operation")
        argv = [str(python), "-m", "pip", operation]
        if operation == "uninstall":
            argv.append("-y")
        elif _truthy(step.args.get("upgrade")):
            argv.append("--upgrade")
        argv.extend(packages)
        if _truthy(step.args.get("system")):
            argv = _sudo_if_needed(argv)
        result = self._command(argv, timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "python_package_change_failed operation=%s packages=%s stderr=%s environment_changed=unknown"
                % (operation, ",".join(packages), _one_line(result.stderr)),
                "inspect_python_runtime",
            )
        return DirectActionResult(
            "completed",
            "action=install_python_packages operation=%s packages=%s python=%s environment_changed=true"
            % (operation, ",".join(packages), python),
        )

    def _action_manage_file_permissions(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        path = _absolute_path(step.args.get("path"))
        if path is None or not path.exists() or _protected_target(path):
            return self._blocked("invalid_or_protected_permission_target")
        mode = str(step.args.get("mode") or "").strip()
        owner = str(step.args.get("owner") or "").strip()
        group = str(step.args.get("group") or "").strip()
        recursive = _truthy(step.args.get("recursive"))
        if not mode and not owner and not group:
            return self._blocked("mode_or_owner_required")
        if mode and not re.fullmatch(r"0?[0-7]{3,4}", mode):
            return self._blocked("invalid_permission_mode")
        if any(value and not _safe_token(value) for value in (owner, group)):
            return self._blocked("invalid_owner_or_group")
        commands = []
        if mode:
            commands.append(["chmod", *(["-R"] if recursive else []), mode, str(path)])
        if owner or group:
            identity = "%s:%s" % (owner, group) if group else owner
            commands.append(["chown", *(["-R"] if recursive else []), identity, str(path)])
        for argv in commands:
            result = self._command(
                _sudo_if_needed(argv),
                timeout=step.timeout,
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "manage_file_permissions_failed stderr=%s environment_changed=unknown"
                    % _one_line(result.stderr),
                    "inspect_path_permissions",
                )
        return DirectActionResult(
            "completed",
            "action=manage_file_permissions path=%s mode=%s owner=%s group=%s environment_changed=true"
            % (path, mode or "unchanged", owner or "unchanged", group or "unchanged"),
        )

    def _action_git_operation(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        repository = _absolute_path(step.args.get("repository"))
        operation = str(step.args.get("operation") or "").strip()
        if repository is None:
            return self._blocked("invalid_git_repository_path")
        argv = _git_operation_argv(operation, step.args)
        if not argv:
            return self._blocked("unsupported_or_invalid_git_operation")
        cwd = repository
        if operation == "clone":
            cwd = repository.parent
        decision = decide_ops_command(
            {"program": "git", "argv": argv, "cwd": str(cwd)}
        )
        if not decision.allowed:
            return self._blocked("git_operation_not_allowed=%s" % decision.reason)
        result = self._command(
            ["git", *decision.argv],
            cwd=cwd,
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "git_operation_failed operation=%s stderr=%s environment_changed=unknown"
                % (operation, _one_line(result.stderr)),
                "inspect_git_repository",
            )
        return DirectActionResult(
            "completed",
            "action=git_operation operation=%s repository=%s output=%s environment_changed=%s"
            % (
                operation,
                repository,
                _one_line(result.stdout),
                "false" if operation in {"status", "rev_parse"} else "true",
            ),
        )

    def _action_ensure_user_group(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        user = str(step.args.get("user") or "").strip()
        group = str(step.args.get("group") or "").strip()
        if not _safe_token(user) or not _safe_token(group):
            return self._blocked("invalid_user_or_group")
        identity = self._command(["id", "-nG", user], timeout=20)
        if identity.returncode != 0:
            return self._blocked("user_not_found=%s" % user)
        if group in identity.stdout.split():
            return DirectActionResult(
                "completed",
                "action=ensure_user_group user=%s group=%s already_member=true environment_changed=false"
                % (user, group),
            )
        result = self._command(
            _sudo_if_needed(["usermod", "-aG", group, user]),
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "ensure_user_group_failed stderr=%s environment_changed=unknown"
                % _one_line(result.stderr),
                "inspect_system",
            )
        return DirectActionResult(
            "completed",
            "action=ensure_user_group user=%s group=%s environment_changed=true"
            % (user, group),
        )

    def _action_remove_python_package_entries(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        root = _absolute_path(step.args.get("site_packages_dir"))
        package = str(step.args.get("package") or "").strip()
        entries = _string_list(step.args.get("entries"))
        if (
            root is None
            or not root.is_dir()
            or not _SAFE_PACKAGE.fullmatch(package)
            or not entries
        ):
            return self._blocked("invalid_python_cleanup_target")
        normalized = package.lower().replace("-", "_")
        targets = []
        for name in entries:
            if (
                "/" in name
                or "\\" in name
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", name)
                or not name.lower().replace("-", "_").startswith(normalized)
            ):
                return self._blocked("python_cleanup_entry_not_owned=%s" % name)
            target = (root / name).resolve()
            if target.parent != root.resolve() or not target.exists():
                return self._blocked("python_cleanup_entry_missing=%s" % name)
            targets.append(target)
        for target in targets:
            try:
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            except PermissionError:
                result = self._command(
                    _sudo_if_needed(["rm", "-rf", "--", str(target)]),
                    timeout=step.timeout,
                )
                if result.returncode != 0:
                    return DirectActionResult(
                        "failed",
                        "python_cleanup_failed entry=%s stderr=%s environment_changed=unknown"
                        % (target.name, _one_line(result.stderr)),
                        "inspect_python_runtime",
                    )
        return DirectActionResult(
            "completed",
            "action=remove_python_package_entries package=%s removed=%s environment_changed=true"
            % (package, ",".join(item.name for item in targets)),
        )

    def _action_sync_directory(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        source = _absolute_path(step.args.get("source"))
        destination = _absolute_path(step.args.get("destination"))
        if (
            source is None
            or destination is None
            or not source.is_dir()
            or source.is_symlink()
            or _protected_target(source)
            or _protected_target(destination)
            or destination == source
            or source in destination.parents
        ):
            return self._blocked("invalid_directory_sync_scope")
        symlink = next(
            (item for item in source.rglob("*") if item.is_symlink()),
            None,
        )
        if symlink is not None:
            return self._blocked("directory_sync_symlink_refused=%s" % symlink)
        try:
            shutil.copytree(source, destination, dirs_exist_ok=True)
        except PermissionError:
            return self._blocked(
                "directory_sync_requires_unimplemented_privileged_copy",
                next_action="choose_user_writable_destination",
            )
        return DirectActionResult(
            "completed",
            "action=sync_directory source=%s destination=%s environment_changed=true"
            % (source, destination),
        )

    def _action_merge_json_file(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        path = _absolute_path(step.args.get("path"))
        patch = step.args.get("patch")
        if isinstance(patch, str):
            try:
                patch = json.loads(patch)
            except json.JSONDecodeError:
                return self._blocked("json_patch_invalid")
        if (
            path is None
            or path.suffix.lower() != ".json"
            or not path.is_file()
            or not isinstance(patch, dict)
            or _SENSITIVE_NAME.search(path.name)
            or _contains_sensitive_mapping(patch)
        ):
            return self._blocked("invalid_or_sensitive_json_merge")
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return self._blocked("target_json_invalid")
        if not isinstance(current, dict):
            return self._blocked("target_json_must_be_object")
        merged = _deep_merge_json(current, patch)
        content = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
        backup = path.with_name(
            "%s.klonet-agent.bak.%s" % (path.name, time.time_ns())
        )
        try:
            shutil.copy2(path, backup)
        except PermissionError:
            copied = self._command(
                _sudo_if_needed(["cp", "-p", str(path), str(backup)]),
                timeout=step.timeout,
            )
            if copied.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "json_backup_failed stderr=%s environment_changed=false"
                    % _one_line(copied.stderr),
                    "inspect_path_permissions",
                )
        result = self._write_file(path, content, step.timeout)
        if result:
            return result
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return DirectActionResult(
                "failed",
                "json_postcondition_failed backup=%s environment_changed=true"
                % backup,
                "restore_backup_then_replan",
            )
        return DirectActionResult(
            "completed",
            "action=merge_json_file path=%s backup=%s keys=%s environment_changed=true"
            % (path, backup, ",".join(sorted(patch)[:30])),
        )

    def _action_start_redis_instance(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        binary = _absolute_path(step.args.get("binary"))
        config = _absolute_path(step.args.get("config_path"))
        try:
            expected_port = int(step.args.get("expected_port"))
        except (TypeError, ValueError):
            return self._blocked("invalid_redis_expected_port")
        if (
            binary is None
            or config is None
            or not binary.is_file()
            or not os.access(binary, os.X_OK)
            or not config.is_file()
            or not 1 <= expected_port <= 65535
        ):
            return self._blocked("invalid_redis_binary_or_config")
        configured_port = _redis_config_port(config)
        if configured_port != expected_port:
            return self._blocked(
                "redis_port_mismatch configured=%s expected=%s"
                % (configured_port or "unknown", expected_port)
            )
        if _tcp_listening("127.0.0.1", expected_port):
            return DirectActionResult(
                "completed",
                "action=start_redis_instance port=%s already_listening=true environment_changed=false"
                % expected_port,
            )
        result = self._command(
            _sudo_if_needed(
                [str(binary), str(config), "--daemonize", "yes"]
            ),
            cwd=config.parent,
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "redis_start_failed port=%s stderr=%s environment_changed=unknown"
                % (expected_port, _one_line(result.stderr, 3000)),
                "inspect_redis",
            )
        if not _tcp_listening("127.0.0.1", expected_port, timeout=2.0):
            return DirectActionResult(
                "failed",
                "redis_postcondition_failed port=%s environment_changed=unknown"
                % expected_port,
                "inspect_redis",
            )
        return DirectActionResult(
            "completed",
            "action=start_redis_instance port=%s config=%s environment_changed=true"
            % (expected_port, config),
        )

    def _action_run_reviewed_script(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        script = _absolute_path(step.args.get("script_path"))
        cwd = _absolute_path(step.args.get("cwd"))
        expected_hash = str(step.args.get("sha256") or "").lower().strip()
        args = _string_list(step.args.get("argv"))
        if (
            script is None
            or cwd is None
            or not script.is_file()
            or script.is_symlink()
            or not cwd.is_dir()
            or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
            or any(not _SAFE_SCRIPT_ARG.fullmatch(item) for item in args)
        ):
            return self._blocked("invalid_reviewed_script_contract")
        actual_hash = _file_sha256(script)
        if actual_hash != expected_hash:
            return self._blocked(
                "reviewed_script_hash_mismatch actual=%s" % actual_hash
            )
        interpreter = (
            "python3" if script.suffix == ".py"
            else "bash" if script.suffix in {".sh", ""}
            else ""
        )
        if not interpreter:
            return self._blocked("reviewed_script_type_not_allowed")
        result = self._command(
            _sudo_if_needed([interpreter, str(script), *args]),
            cwd=cwd,
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "reviewed_script_failed sha256=%s returncode=%s stderr=%s environment_changed=unknown"
                % (expected_hash, result.returncode, _one_line(result.stderr, 4000)),
                "inspect_runtime",
            )
        return DirectActionResult(
            "completed",
            "action=run_reviewed_script script=%s sha256=%s environment_changed=true"
            % (script, expected_hash),
        )

    def _action_manage_libvirt_domain(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        domain = str(step.args.get("domain") or "").strip()
        operation = str(step.args.get("operation") or "").strip()
        if not _safe_token(domain) or operation not in {
            "start", "shutdown", "reboot", "destroy", "undefine",
        }:
            return self._blocked("invalid_libvirt_domain_operation")
        if operation in {"destroy", "undefine"} and not _truthy(
            step.args.get("ownership_confirmed")
        ):
            return self._blocked("destructive_domain_operation_requires_ownership_confirmed")
        virsh = shutil.which("virsh")
        if not virsh:
            return self._blocked("virsh_not_found")
        exists = self._command([virsh, "dominfo", domain], timeout=20)
        if exists.returncode != 0:
            return self._blocked("libvirt_domain_not_found=%s" % domain)
        result = self._command(
            _sudo_if_needed([virsh, operation, domain]),
            timeout=step.timeout,
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "libvirt_domain_change_failed domain=%s operation=%s stderr=%s environment_changed=unknown"
                % (domain, operation, _one_line(result.stderr)),
                "inspect_libvirt",
            )
        return DirectActionResult(
            "completed",
            "action=manage_libvirt_domain domain=%s operation=%s environment_changed=true"
            % (domain, operation),
        )

    def _action_manage_docker_network(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        network = str(step.args.get("network") or "").strip()
        operation = str(step.args.get("operation") or "").strip()
        container = str(step.args.get("container") or "").strip()
        if not _safe_token(network) or operation not in {
            "create", "remove", "connect", "disconnect",
        }:
            return self._blocked("invalid_docker_network_operation")
        if operation in {"connect", "disconnect"} and not _safe_token(container):
            return self._blocked("docker_network_container_required")
        if operation in {"remove", "disconnect"} and not _truthy(
            step.args.get("ownership_confirmed")
        ):
            return self._blocked("docker_network_removal_requires_ownership_confirmed")
        docker = shutil.which("docker")
        if not docker:
            return self._blocked("docker_not_found")
        if operation == "create":
            driver = str(step.args.get("driver") or "bridge").strip()
            if driver not in {"bridge", "overlay"}:
                return self._blocked("docker_network_driver_not_allowed")
            argv = [docker, "network", "create", "--driver", driver]
            if driver == "overlay" and _truthy(
                step.args.get("attachable"),
                default=True,
            ):
                argv.append("--attachable")
            argv.append(network)
        elif operation == "remove":
            argv = [docker, "network", "rm", network]
        else:
            argv = [docker, "network", operation, network, container]
        result = self._command(_sudo_if_needed(argv), timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "docker_network_change_failed network=%s operation=%s stderr=%s environment_changed=unknown"
                % (network, operation, _one_line(result.stderr)),
                "inspect_docker_networks",
            )
        return DirectActionResult(
            "completed",
            "action=manage_docker_network network=%s operation=%s environment_changed=true"
            % (network, operation),
        )

    def _action_manage_docker_image(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        operation = str(step.args.get("operation") or "").strip()
        image = str(step.args.get("image") or "").strip()
        source_image = str(step.args.get("source_image") or "").strip()
        archive = _absolute_path(step.args.get("archive_path"))
        docker = shutil.which("docker")
        if not docker:
            return self._blocked("docker_not_found")
        if operation == "load":
            expected_image = str(step.args.get("expected_image") or "").strip()
            if (
                archive is None
                or not archive.is_file()
                or archive.is_symlink()
                or not _safe_image_reference(expected_image)
            ):
                return self._blocked("invalid_docker_image_archive")
            argv = [docker, "load", "--input", str(archive)]
        elif operation == "tag":
            if not _safe_image_reference(source_image) or not _safe_image_reference(image):
                return self._blocked("invalid_docker_image_reference")
            argv = [docker, "tag", source_image, image]
        elif operation == "remove":
            if (
                not _safe_image_reference(image)
                or not _truthy(step.args.get("ownership_confirmed"))
            ):
                return self._blocked(
                    "docker_image_removal_requires_owned_image"
                )
            argv = [docker, "image", "rm"]
            if _truthy(step.args.get("force")):
                argv.append("--force")
            argv.append(image)
        else:
            return self._blocked("unsupported_docker_image_operation")
        result = self._command(_sudo_if_needed(argv), timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "docker_image_change_failed operation=%s image=%s stderr=%s environment_changed=unknown"
                % (operation, image or archive, _one_line(result.stderr)),
                "inspect_docker_images",
            )
        if operation == "load":
            verify = self._command(
                _sudo_if_needed(
                    [docker, "image", "inspect", expected_image]
                ),
                timeout=min(step.timeout, 30),
            )
            if verify.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "docker_image_postcondition_failed expected_image=%s environment_changed=true"
                    % expected_image,
                    "inspect_docker_images",
                )
            image = expected_image
        return DirectActionResult(
            "completed",
            "action=manage_docker_image operation=%s image=%s environment_changed=true"
            % (operation, image or archive),
        )

    def _action_manage_network_link(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        name = str(step.args.get("name") or "").strip()
        operation = str(step.args.get("operation") or "").strip()
        if not _safe_token(name) or operation not in {"up", "down", "delete"}:
            return self._blocked("invalid_network_link_operation")
        if operation == "delete" and not _truthy(
            step.args.get("ownership_confirmed")
        ):
            return self._blocked(
                "network_link_delete_requires_ownership_confirmed"
            )
        ip = shutil.which("ip")
        if not ip:
            return self._blocked("ip_command_not_found")
        exists = self._command([ip, "-o", "link", "show", "dev", name], timeout=20)
        if exists.returncode != 0:
            return self._blocked("network_link_not_found=%s" % name)
        argv = (
            [ip, "link", "delete", "dev", name]
            if operation == "delete"
            else [ip, "link", "set", "dev", name, operation]
        )
        result = self._command(_sudo_if_needed(argv), timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "network_link_change_failed name=%s operation=%s stderr=%s environment_changed=unknown"
                % (name, operation, _one_line(result.stderr)),
                "inspect_network_links",
            )
        return DirectActionResult(
            "completed",
            "action=manage_network_link name=%s operation=%s environment_changed=true"
            % (name, operation),
        )

    def _action_manage_ovs_resource(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        resource_type = str(step.args.get("resource_type") or "").strip()
        name = str(step.args.get("name") or "").strip()
        operation = str(step.args.get("operation") or "").strip()
        bridge = str(step.args.get("bridge") or "").strip()
        if (
            resource_type not in {"bridge", "port"}
            or not _safe_token(name)
            or operation not in {"add", "remove"}
            or (resource_type == "port" and not _safe_token(bridge))
        ):
            return self._blocked("invalid_ovs_resource_operation")
        if operation == "remove" and not _truthy(
            step.args.get("ownership_confirmed")
        ):
            return self._blocked("ovs_removal_requires_ownership_confirmed")
        ovs = shutil.which("ovs-vsctl")
        if not ovs:
            return self._blocked("ovs_vsctl_not_found")
        if resource_type == "bridge":
            argv = [ovs, "--may-exist" if operation == "add" else "--if-exists",
                    "add-br" if operation == "add" else "del-br", name]
        else:
            argv = [
                ovs,
                "--may-exist" if operation == "add" else "--if-exists",
                "add-port" if operation == "add" else "del-port",
                bridge,
                name,
            ]
        result = self._command(_sudo_if_needed(argv), timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "ovs_change_failed type=%s name=%s operation=%s stderr=%s environment_changed=unknown"
                % (resource_type, name, operation, _one_line(result.stderr)),
                "inspect_ovs",
            )
        return DirectActionResult(
            "completed",
            "action=manage_ovs_resource type=%s name=%s operation=%s environment_changed=true"
            % (resource_type, name, operation),
        )

    def _start_one_component(
        self,
        component: str,
        session: str,
        root: Path,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        python = _python_executable(step.args)
        if not python:
            return self._blocked("python3.8_not_found")
        command = _component_commands(python)[component]
        shell_command = "cd %s && exec %s" % (
            shlex.quote(str(root)),
            shlex.join(command),
        )
        result = self._command(
            ["screen", "-dmS", session, "bash", "-lc", shell_command],
            cwd=root,
            timeout=min(step.timeout, 30),
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "screen_start_failed component=%s stderr=%s environment_changed=unknown"
                % (component, _one_line(result.stderr)),
                "inspect_runtime",
            )
        return DirectActionResult(
            "completed",
            "action=restart_screen_component component=%s session=%s environment_changed=true"
            % (component, session),
        )

    def _existing_screen_sessions(self) -> set[str]:
        result = self._command(["screen", "-ls"], timeout=15)
        text = "%s\n%s" % (result.stdout or "", result.stderr or "")
        return set(
            match.group(1)
            for match in re.finditer(
                r"(?:^|\s|\.)([A-Za-z0-9_.:-]+)(?:\s|$)",
                text,
            )
        )

    def _write_file(
        self,
        path: Path,
        content: str,
        timeout: int,
    ) -> DirectActionResult | None:
        parent = path.parent
        if parent.is_dir() and os.access(parent, os.W_OK):
            temp_path = parent / (".%s.klonet-agent.tmp" % path.name)
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, path)
            return None
        temp_dir = Path(tempfile.mkdtemp(prefix="klonet-priv-write-"))
        try:
            staged = temp_dir / path.name
            staged.write_text(content, encoding="utf-8")
            command = _sudo_if_needed(
                ["install", "-m", "0644", str(staged), str(path)]
            )
            result = self._command(command, timeout=timeout)
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "privileged_write_failed path=%s stderr=%s environment_changed=unknown"
                    % (path, _one_line(result.stderr)),
                    "inspect_path_permissions",
                )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return None

    def _command(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess:
        return self.command_runner(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=max(1, min(int(timeout), 3600)),
        )

    @staticmethod
    def _run_command(
        argv: list[str],
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        options = {
            "cwd": cwd,
            "env": env,
            "timeout": timeout,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        }
        if argv[:1] == ["sudo"] and sys.stdin.isatty():
            # Keep stdin and stderr attached to the user's terminal so sudo can
            # request a password without the password entering prompts, args,
            # logs, or captured evidence. Stdout remains bounded evidence.
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=None,
                **options,
            )
        return subprocess.run(
            argv,
            capture_output=True,
            **options,
        )

    @staticmethod
    def _blocked(
        reason: str,
        *,
        next_action: str = "",
    ) -> DirectActionResult:
        return DirectActionResult(
            "blocked",
            "%s environment_changed=false" % _one_line(reason),
            next_action,
        )


def _project_root(step: PrivilegedStep) -> tuple[Path, str]:
    raw = str(step.args.get("project_root") or "").strip()
    if not raw:
        return Path("."), "project_root_required"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return path, "project_root_must_be_absolute"
    try:
        path = path.resolve()
    except OSError:
        return path, "invalid_project_root"
    if not path.is_dir():
        return path, "project_root_not_found=%s" % path
    return path, ""


def _platform_root(
    step: PrivilegedStep,
) -> tuple[str, Path, str]:
    platform = str(step.args.get("platform") or "").strip()
    root, problem = _project_root(step)
    if problem:
        return platform, root, problem
    if not _safe_token(platform):
        return platform, root, "invalid_platform"
    return platform, root, ""


def _entry_sources(root: Path) -> dict[str, str]:
    found = {}
    candidates = (root, root / "mains", root / "vemu_uestc" / "mains")
    for name in REQUIRED_ENTRY_FILES:
        for candidate in candidates:
            path = candidate / name
            if path.is_file():
                found[name] = str(path.relative_to(root))
                break
    return found


def _default_entry_source(root: Path) -> Path:
    candidates = (root / "mains", root / "vemu_uestc" / "mains")
    return next(
        (
            path for path in candidates
            if all((path / name).is_file() for name in REQUIRED_ENTRY_FILES)
        ),
        candidates[0],
    )


def _python_executable(args: dict) -> str:
    requested = str(args.get("python_executable") or "").strip()
    candidates = (
        requested,
        "/usr/bin/python3.8",
        "/usr/local/python3/bin/python3.8",
        "/usr/local/bin/python3.8",
    )
    for raw in candidates:
        if raw and Path(raw).is_file() and os.access(raw, os.X_OK):
            return str(Path(raw).resolve())
    return ""


def _component_commands(python: str) -> dict[str, list[str]]:
    return {
        "master": [
            python, "-m", "gunicorn", "-c", "gun.py",
            "master_main:flask_app",
        ],
        "celery": [
            python, "-m", "celery", "-A", "celery_worker.celery",
            "worker", "--loglevel=info",
        ],
        "web_terminal": [python, "web_terminal_main.py"],
        "worker": [
            python, "-m", "gunicorn", "-c", "worker_gun.py",
            "worker_main:flask_app",
        ],
    }


def _configured_runtime_ports(root: Path) -> list[int]:
    config = root / "vemu_uestc" / "vemu_config" / "config.py"
    if not config.is_file():
        return []
    try:
        content = config.read_text(
            encoding="utf-8-sig",
            errors="replace",
        )
    except OSError:
        return []
    active = re.search(
        r"\bPROJ_CONFIG\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        content,
    )
    if active:
        class_name = active.group(1)
        class_match = re.search(
            rf"^class\s+{re.escape(class_name)}\b.*?:\s*$",
            content,
            flags=re.MULTILINE,
        )
        if class_match:
            next_class = re.search(
                r"^class\s+[A-Za-z_][A-Za-z0-9_]*\b.*?:\s*$",
                content[class_match.end():],
                flags=re.MULTILINE,
            )
            end = (
                class_match.end() + next_class.start()
                if next_class else len(content)
            )
            content = content[class_match.start():end]
    names = (
        "master_port",
        "worker_port",
        "web_terminal_port",
        "terminal_port",
    )
    ports = []
    for match in re.finditer(
        rf"\b({'|'.join(names)})\s*=\s*['\"]?(\d{{1,5}})",
        content,
    ):
        port = int(match.group(2))
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _listening_ports(text: str, configured: list[int]) -> list[int]:
    return [
        port for port in configured
        if re.search(rf":{port}\b", text)
    ]


def _absolute_path(value) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    try:
        return path.resolve()
    except OSError:
        return None


def _path_list(value) -> list[Path]:
    items = value if isinstance(value, list) else []
    result = []
    for item in items[:100]:
        path = _absolute_path(item)
        if path is None:
            return []
        result.append(path)
    return result


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()
        for item in value[:100]
        if str(item).strip()
    ]


def _truthy(value, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _protected_target(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return True
    return resolved in _PROTECTED_REMOVE_PATHS


def _proc_cmdline(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\x00")
        if part
    )


def _git_operation_argv(operation: str, args: dict) -> list[str]:
    remote = str(args.get("remote") or "").strip()
    ref = str(args.get("ref") or "").strip()
    if operation == "status":
        return ["status", "--short", "--branch"]
    if operation == "rev_parse":
        return ["rev-parse", ref or "HEAD"]
    if operation == "pull":
        return (
            ["pull", "--ff-only", remote, ref]
            if remote and ref
            else ["pull", "--ff-only"]
        )
    if operation == "fetch":
        return ["fetch", remote] if remote else ["fetch", "--all", "--prune"]
    if operation in {"checkout", "switch"} and ref:
        create = _truthy(args.get("create"))
        return [operation, "-b" if operation == "checkout" else "-c", ref] if create else [operation, ref]
    if operation == "clone":
        url = str(args.get("url") or "").strip()
        repository = _absolute_path(args.get("repository"))
        if not url or repository is None:
            return []
        return ["clone", url, repository.name]
    if operation == "submodule_update":
        return ["submodule", "update", "--init", "--recursive"]
    if operation == "reset" and ref:
        return ["reset", "--hard", ref]
    if operation == "revert" and ref:
        return ["revert", "--no-edit", ref]
    if operation == "restore":
        path = str(args.get("path") or "").strip()
        return ["restore", "--", path] if path else []
    if operation == "tag":
        tag = str(args.get("tag") or "").strip()
        return ["tag", tag, ref] if tag and ref else ["tag", tag] if tag else []
    if operation == "push":
        if _truthy(args.get("force_with_lease")):
            return (
                ["push", "--force-with-lease", remote, ref]
                if remote and ref
                else []
            )
        if remote and ref:
            return ["push", remote, ref]
        return ["push"]
    return []


def _safe_archive_members(archive: Path, destination: Path) -> list[str]:
    destination = destination.resolve()
    if tarfile.is_tarfile(str(archive)):
        with tarfile.open(str(archive), "r:*") as opened:
            raw_members = opened.getmembers()
            names = [item.name for item in raw_members]
            if any(item.isdev() or item.issym() or item.islnk() for item in raw_members):
                raise ValueError("archive_links_or_devices_not_allowed")
    elif zipfile.is_zipfile(str(archive)):
        with zipfile.ZipFile(str(archive)) as opened:
            names = opened.namelist()
    else:
        raise ValueError("unsupported_archive_type")
    for name in names:
        if not name or "\x00" in name:
            raise ValueError("invalid_archive_member")
        target = (destination / name).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise ValueError("archive_path_traversal=%s" % name) from exc
    return names


def _contains_sensitive_mapping(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(
                r"(?i)(?:password|passwd|pwd|api[_-]?key|secret|token|credential)",
                str(key),
            ):
                return True
            if _contains_sensitive_mapping(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_mapping(item) for item in value)
    return False


def _deep_merge_json(current: dict, patch: dict) -> dict:
    result = dict(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_json(result[key], value)
        else:
            result[key] = value
    return result


def _redis_config_port(path: Path) -> int | None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*port\s+(\d{1,5})\s*(?:#.*)?$", content)
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def _tcp_listening(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sudo_if_needed(argv: list[str]) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return argv
    return ["sudo", *argv]


def _safe_token(value: str) -> bool:
    return bool(_SAFE_TOKEN.fullmatch(value or ""))


def _safe_image_reference(value: str) -> bool:
    return bool(
        value
        and len(value) <= 255
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", value)
        and ".." not in value
    )


def _one_line(value, limit: int = 1600) -> str:
    return " ".join(str(value or "").split())[:limit]
