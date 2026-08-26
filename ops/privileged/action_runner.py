"""Direct registered-action backend for Ops-Privilege.

This backend deliberately does not import or invoke the ordinary Ops recipe
runner, ``klonet-agent-op``, or its sudoers contract.  The planner still emits
registered actions; this module compiles confirmed actions into bounded argv
execution and local filesystem operations.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from klonet_agent.ops.actions import (
    OpsActionRegistry,
    configured_ops_action_registry,
)
from klonet_agent.ops.command_policy import command_exists, decide_ops_command
from klonet_agent.ops.privileged.contracts import PrivilegedStep, component_port_arg
from klonet_agent.ops.privileged.environment_facts import (
    REQUIRED_ENTRY_FILES,
    process_belongs_to_project_root,
)
from klonet_agent.ops.privileged.planner_schema import normalize_process_signal


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
        "insert_text_before_anchor",
        "edit_text_file",
        "upsert_python_class",
        "set_python_config_assignment",
        "set_python_class_attribute",
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
        "stop_klonet_component",
        "stop_klonet_runtime_instance",
        "create_docker_container",
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
        "ensure_klonet_redis_instance",
        "repair_klonet_active_master_ip",
        "run_reviewed_script",
        "manage_libvirt_domain",
        "manage_docker_network",
        "manage_docker_image",
        "manage_network_link",
        "manage_ovs_resource",
    }
)


def _ops_backup_path(path: Path) -> Path:
    """Return a recoverable, sibling backup path for a managed file write."""

    return path.with_name(
        "%s.klonet-agent.bak.%s" % (path.name, time.time_ns())
    )

_COMPONENTS = ("master", "celery", "web_terminal", "worker")
_COMPONENT_SUFFIX = {
    "master": "m",
    "celery": "c",
    "web_terminal": "web",
    "worker": "w",
}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SAFE_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_SAFE_CONTAINER_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,239}$")
_SAFE_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
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
    metadata: dict[str, str] = field(default_factory=dict)


class DirectPrivilegedActionRunner:
    """Execute one confirmed action without the ordinary Ops helper."""

    def __init__(
        self,
        *,
        action_registry: OpsActionRegistry | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        on_command: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.action_registry = (
            action_registry or configured_ops_action_registry()
        )
        self.command_runner = command_runner or self._run_command
        self.on_command = on_command
        self._active_action = ""
        # Authentication belongs to the Action execution boundary, not to
        # every low-level argv.  ``unknown`` is reset for each atomic Action;
        # after one interactive validation all actual commands use ``sudo
        # -n``.  A rejected validation is also remembered so one Action can
        # never produce an unbounded password-prompt loop.
        self._sudo_auth_state = "unknown"
        self._sudo_auth_error = ""

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
        self._active_action = spec.name
        self._sudo_auth_state = "unknown"
        self._sudo_auth_error = ""
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
        finally:
            self._active_action = ""

    def rollback(self, evidence) -> DirectActionResult:
        """Restore one exact text mutation recorded by this runner."""

        mutation = dict(getattr(evidence, "mutation", {}) or {})
        if mutation.get("kind") == "runtime_entry_files":
            try:
                backups = json.loads(str(mutation.get("backups") or "{}"))
                created = json.loads(str(mutation.get("created") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return self._blocked("rollback_metadata_invalid")
            if not isinstance(backups, dict) or not isinstance(created, list):
                return self._blocked("rollback_metadata_invalid")
            problem = self._restore_runtime_entry_files(
                backups, created, timeout=120,
            )
            if problem is not None:
                return problem
            return DirectActionResult(
                "completed",
                "runtime_entry_rollback_restored=%s removed_created=%s environment_changed=true"
                % (len(backups), len(created)),
            )
        path = _absolute_path(mutation.get("path"))
        backup = _absolute_path(mutation.get("backup"))
        created = str(mutation.get("created") or "").lower() == "true"
        if path is None or mutation.get("kind") != "text_file":
            return self._blocked("rollback_metadata_missing")
        if created:
            if not path.exists():
                return DirectActionResult(
                    "completed",
                    "rollback path=%s already_absent=true environment_changed=false"
                    % path,
                )
            try:
                path.unlink()
            except OSError as exc:
                return DirectActionResult(
                    "failed",
                    "rollback_remove_failed path=%s error=%s environment_changed=unknown"
                    % (path, exc.__class__.__name__),
                    "inspect_path_permissions",
                )
            return DirectActionResult(
                "completed",
                "rollback path=%s created_file_removed=true environment_changed=true"
                % path,
            )
        expected_prefix = "%s.klonet-agent.bak." % path.name
        if (
            backup is None
            or backup.parent != path.parent
            or not backup.name.startswith(expected_prefix)
            or not backup.is_file()
        ):
            return self._blocked("rollback_backup_missing_or_untrusted")
        try:
            original = backup.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._blocked("rollback_backup_not_readable")
        result = self._write_file(path, original, 120)
        if result:
            return result
        return DirectActionResult(
            "completed",
            "rollback path=%s backup=%s environment_changed=true" % (path, backup),
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
        expected_hashes = (
            step.args.get("entry_sha256s")
            if isinstance(step.args.get("entry_sha256s"), dict)
            else {}
        )
        if expected_hashes:
            changed = [
                name for name in REQUIRED_ENTRY_FILES
                if str(expected_hashes.get(name) or "").lower()
                != _file_sha256(source / name)
            ]
            if changed:
                return self._blocked(
                    "entry_sources_changed_after_approval=%s" % ",".join(changed)
                )
        expected_targets = (
            step.args.get("target_sha256s")
            if isinstance(step.args.get("target_sha256s"), dict)
            else {}
        )
        stale_targets = []
        for name, expected in expected_targets.items():
            target = root / name
            observed = _file_sha256(target) if target.is_file() else "missing"
            if observed != str(expected):
                stale_targets.append(name)
        if stale_targets:
            return self._blocked(
                "entry_targets_changed_after_approval=%s" % ",".join(stale_targets)
            )
        changed_names = [
            name for name in REQUIRED_ENTRY_FILES
            if not (root / name).is_file()
            or _file_sha256(source / name) != _file_sha256(root / name)
        ]
        candidates = {}
        for name in changed_names:
            try:
                content = (source / name).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                return self._blocked(
                    "entry_source_not_readable=%s:%s"
                    % (name, exc.__class__.__name__)
                )
            validation_problem = _candidate_validation_problem(root / name, content)
            if validation_problem:
                return self._blocked(
                    "entry_candidate_invalid=%s:%s"
                    % (name, validation_problem)
                )
            candidates[name] = content
        backups = {}
        created = []
        for name in changed_names:
            target = root / name
            result, backup, was_created = self._commit_text_candidate(
                target,
                None if not target.exists() else "existing",
                candidates[name],
                step.timeout,
            )
            if backup is not None:
                backups[str(target)] = str(backup)
            if was_created:
                created.append(str(target))
            if result is not None:
                rollback = self._restore_runtime_entry_files(
                    backups, created, timeout=step.timeout,
                )
                if rollback is None:
                    return DirectActionResult(
                        "failed",
                        "prepare_project_files_commit_failed name=%s reason=%s "
                        "partial_changes_rolled_back=true environment_changed=false"
                        % (name, _one_line(result.output, 2000)),
                        "inspect_path_permissions",
                    )
                return DirectActionResult(
                    "failed",
                    "prepare_project_files_commit_failed name=%s reason=%s "
                    "rollback_failed=%s environment_changed=unknown"
                    % (
                        name,
                        _one_line(result.output, 2000),
                        _one_line(rollback.output, 2000),
                    ),
                    "inspect_path_permissions",
                    metadata={
                        "kind": "runtime_entry_files",
                        "backups": json.dumps(backups, sort_keys=True),
                        "created": json.dumps(created),
                    },
                )
        mismatched = [
            name for name in REQUIRED_ENTRY_FILES
            if _file_sha256(source / name) != _file_sha256(root / name)
        ]
        if mismatched:
            rollback = self._restore_runtime_entry_files(
                backups, created, timeout=step.timeout,
            )
            if rollback is None:
                return DirectActionResult(
                    "failed",
                    "prepared_entry_hash_mismatch=%s "
                    "partial_changes_rolled_back=true environment_changed=false"
                    % ",".join(mismatched),
                    "inspect_project_layout",
                )
            return DirectActionResult(
                "failed",
                "prepared_entry_hash_mismatch=%s rollback_failed=%s "
                "environment_changed=unknown"
                % (",".join(mismatched), _one_line(rollback.output, 2000)),
                "inspect_project_layout",
                metadata={
                    "kind": "runtime_entry_files",
                    "backups": json.dumps(backups, sort_keys=True),
                    "created": json.dumps(created),
                },
            )
        return DirectActionResult(
            "completed",
            (
                "action=prepare_project_files source_root=%s "
                "project_root=%s copied=%s environment_changed=%s"
            )
                % (
                    source, root, ",".join(changed_names) or "none",
                    "true" if changed_names else "false",
                ),
            metadata={
                "kind": "runtime_entry_files",
                "backups": json.dumps(backups, sort_keys=True),
                "created": json.dumps(created),
            },
        )

    def _restore_runtime_entry_files(
        self,
        backups: dict[str, str],
        created: list[str],
        *,
        timeout: int,
    ) -> DirectActionResult | None:
        """Restore the exact runtime-entry transaction through one privilege path."""

        for target_text, backup_text in backups.items():
            target = _absolute_path(target_text)
            backup = _absolute_path(backup_text)
            expected_prefix = "%s.klonet-agent.bak." % (
                target.name if target is not None else ""
            )
            if (
                target is None
                or backup is None
                or target.name not in REQUIRED_ENTRY_FILES
                or backup.parent != target.parent
                or not backup.name.startswith(expected_prefix)
                or not backup.is_file()
            ):
                return self._blocked("rollback_backup_missing_or_untrusted")
            if os.access(target.parent, os.W_OK):
                try:
                    shutil.copy2(backup, target)
                except OSError as exc:
                    return DirectActionResult(
                        "failed",
                        "runtime_entry_restore_failed path=%s error=%s "
                        "environment_changed=unknown"
                        % (target, exc.__class__.__name__),
                        "inspect_path_permissions",
                    )
            else:
                copied = self._command(
                    _sudo_if_needed([
                        "cp", "-p", "--", str(backup), str(target),
                    ]),
                    timeout=timeout,
                )
                if copied.returncode != 0:
                    return DirectActionResult(
                        "failed",
                        "runtime_entry_restore_failed path=%s stderr=%s "
                        "environment_changed=unknown"
                        % (target, _one_line(copied.stderr)),
                        "inspect_path_permissions",
                    )
        for target_text in created:
            target = _absolute_path(target_text)
            if target is None or target.name not in REQUIRED_ENTRY_FILES:
                return self._blocked("rollback_created_path_invalid")
            if not target.exists():
                continue
            if os.access(target.parent, os.W_OK):
                try:
                    target.unlink()
                except OSError as exc:
                    return DirectActionResult(
                        "failed",
                        "runtime_entry_remove_failed path=%s error=%s "
                        "environment_changed=unknown"
                        % (target, exc.__class__.__name__),
                        "inspect_path_permissions",
                    )
            else:
                removed = self._command(
                    _sudo_if_needed(["rm", "-f", "--", str(target)]),
                    timeout=timeout,
                )
                if removed.returncode != 0:
                    return DirectActionResult(
                        "failed",
                        "runtime_entry_remove_failed path=%s stderr=%s "
                        "environment_changed=unknown"
                        % (target, _one_line(removed.stderr)),
                        "inspect_path_permissions",
                    )
        return None

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
        suffix = _component_suffix(component, step.args)
        if not suffix:
            return self._blocked("invalid_component")
        expected = "%s_%s" % (platform, suffix)
        if session != expected:
            return self._blocked("screen_session_mismatch expected=%s" % expected)
        run_as_uid = _runtime_run_as_uid(step.args)
        if run_as_uid is None:
            return self._blocked("invalid_run_as_uid")
        _dead_targets, cleanup_error = self._clean_dead_screen_session(
            session, run_as_uid,
        )
        if cleanup_error:
            return DirectActionResult(
                "failed", cleanup_error, "inspect_runtime",
            )
        targets = self._screen_session_targets(session, run_as_uid)
        if targets:
            port = component_port_arg(step.args, component)
            component_pids = (
                _listener_pids_for_port(port)
                if port else _component_pids(root, component)
            )
            if not component_pids:
                # A live Screen shell with no managed foreground component is
                # the missing-role state this Action was approved to repair.
                # Reuse the one authoritative replacement lifecycle instead
                # of returning a synthetic failure that requires LLM Replan.
                forwarded = PrivilegedStep.from_dict(step.to_dict())
                forwarded.action = "restart_screen_component"
                recovered = self._action_restart_screen_component(forwarded)
                if recovered.status == "completed":
                    return DirectActionResult(
                        "completed",
                        "action=start_screen_component component=%s session=%s "
                        "recovered_stale_screen=true %s"
                        % (component, session, recovered.output),
                        metadata={
                            **dict(recovered.metadata or {}),
                            "kind": "component_start",
                            "recovered_stale_screen": "true",
                        },
                    )
                return recovered
            owner_targets, ownership_observed = _screen_owner_targets_for_pids(
                component_pids,
            )
            if ownership_observed and set(targets).intersection(owner_targets):
                allowed = _allowed_runtime_cwds(root)
                if all(
                    _proc_cwd(pid)
                    and any(
                        _path_is_relative_to(Path(_proc_cwd(pid)), candidate)
                        for candidate in allowed
                    )
                    for pid in component_pids
                ):
                    return DirectActionResult(
                        "completed",
                        "action=start_screen_component component=%s session=%s "
                        "already_running=true pids=%s environment_changed=false"
                        % (
                            component, session,
                            ",".join(str(pid) for pid in component_pids),
                        ),
                        metadata={
                            "kind": "component_start",
                            "component": component,
                            "session": session,
                            "already_running": "true",
                        },
                    )
            return self._blocked(
                "screen_session_runtime_ownership_unresolved=%s" % session
            )
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
        configured_ports = _configured_runtime_ports(root)
        if not configured_ports:
            return self._blocked(
                "runtime_ports_not_found_in_active_config"
            )
        if all(session in existing for session in sessions.values()):
            unhealthy = _runtime_ports_with_wrong_cwd(configured_ports, root)
            if not unhealthy:
                return DirectActionResult(
                    "completed",
                    (
                        "action=start_platform_screens platform=%s project_root=%s "
                        "already_running=true screen_sessions=%s environment_changed=false"
                    )
                    % (platform, root, ",".join(sessions.values())),
                )
        conflicts = [
            session for session in sessions.values() if session in existing
        ]
        stopped_existing = []
        for session in conflicts:
            targets = self._screen_session_targets(session)
            for target in targets or [session]:
                self._command(["screen", "-S", target, "-X", "quit"], timeout=20)
            stopped_existing.extend(targets or [session])
        if stopped_existing and not _wait_screen_sessions_absent(
            self, stopped_existing, timeout=8.0
        ):
            return self._blocked(
                "screen_session_still_exists=%s" % ",".join(stopped_existing)
            )
        restart_authorized = _truthy(step.args.get("allow_stale_runtime_cleanup"))
        if stopped_existing or (
            restart_authorized
            and not all(session in existing for session in sessions.values())
            and _runtime_ports_owned_by_allowed_cwd(configured_ports, root)
        ):
            stop_result = _stop_target_runtime_after_screen_stop(
                self, root, configured_ports, timeout=min(step.timeout, 60)
            )
            if stop_result.status != "completed":
                return stop_result
            if not _wait_allowed_runtime_ports_released(
                root, configured_ports, timeout=20.0
            ):
                return self._blocked(
                    "runtime_ports_still_owned_by_target_after_screen_stop"
                )
        cleaned_stale = []
        if restart_authorized:
            cleanup = _cleanup_stale_runtime_owners(
                self, root, configured_ports, timeout=min(step.timeout, 45)
            )
            if cleanup.status != "completed":
                return cleanup
            cleaned_text = cleanup.output.split("=", 1)[1] if "=" in cleanup.output else ""
            cleaned_stale = [
                item for item in cleaned_text.split(",")
                if item and item != "none"
            ]
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
        runtime_env = _runtime_python_env(root)
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
            "web_terminal": [
                python,
                "-c",
                "from vemu_uestc.webserver.app_factory import create_web_terminal_app; "
                "from vemu_uestc.vemu_config.config import PROJ_CONFIG; "
                "from gevent import pywsgi; "
                "from geventwebsocket.handler import WebSocketHandler",
            ],
        }
        for component in _COMPONENTS:
            result = self._command(
                preflight[component],
                cwd=root,
                env=runtime_env,
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
        if restart_authorized:
            cleanup = _cleanup_stale_runtime_owners(
                self, root, configured_ports, timeout=min(step.timeout, 45)
            )
            if cleanup.status != "completed":
                return cleanup
            cleaned_text = cleanup.output.split("=", 1)[1] if "=" in cleanup.output else ""
            cleaned_stale.extend(
                item for item in cleaned_text.split(",")
                if item and item != "none" and item not in cleaned_stale
            )
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
        commands = _component_commands(python)
        started = []
        for component in _COMPONENTS:
            shell_command = "export PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}; cd %s && exec %s" % (
                shlex.quote(_runtime_pythonpath(root)),
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
                env=runtime_env,
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
        unhealthy = _wait_platform_runtime_ready(
            self,
            root,
            sessions.values(),
            configured_ports,
            timeout=30.0,
        )
        if unhealthy:
            return DirectActionResult(
                "failed",
                (
                    "platform_postcondition_failed reason=%s "
                    "project_root=%s started=%s environment_changed=unknown"
                )
                % (unhealthy, root, ",".join(started)),
                "inspect_runtime",
            )
        return DirectActionResult(
            "completed",
            (
                "action=start_platform_screens platform=%s project_root=%s "
                "python=%s screen_sessions=%s stopped_existing=%s "
                "cleaned_stale=%s environment_changed=true"
            )
            % (
                platform,
                root,
                python,
                ",".join(started),
                ",".join(stopped_existing) or "none",
                ",".join(cleaned_stale) or "none",
            ),
        )

    def _stop_frozen_component_groups(
        self,
        root: Path,
        component: str,
        frozen_pids: list[int],
        step: PrivilegedStep,
    ) -> DirectActionResult:
        """Stop proven same-root/same-role orphan groups before Screen restart.

        This is part of the existing restart Action's replacement invariant,
        not a second public lifecycle operation.  Authority is limited to PIDs
        already frozen by that Action and revalidated by cwd, role and PGID
        immediately before TERM and again before any KILL escalation.
        """

        frozen = {int(pid) for pid in frozen_pids if int(pid) > 1}
        if not frozen:
            return DirectActionResult(
                "completed", "orphan_component_groups_already_stopped=true",
            )

        def matching_groups() -> tuple[list[dict[str, int]], str]:
            processes = _klonet_runtime_processes(
                root,
                command_runner=self._command,
                allow_command_root_identity=True,
            )
            live_frozen = {
                int(proc["pid"]) for proc in processes
                if int(proc["pid"]) in frozen
            }
            if not live_frozen:
                return [], ""
            targets = [
                proc for proc in processes
                if int(proc["pid"]) in live_frozen
                and _klonet_component_for_command(str(proc["cmdline"])) == component
            ]
            if {int(proc["pid"]) for proc in targets} != live_frozen:
                return [], "orphan_component_identity_drift"
            pgids = {int(proc["pgid"]) for proc in targets}
            groups = [proc for proc in processes if int(proc["pgid"]) in pgids]
            if any(
                _klonet_component_for_command(str(proc["cmdline"])) != component
                for proc in groups
            ):
                return [], "orphan_component_process_group_mixed_roles"
            return _runtime_process_groups(groups), ""

        groups, problem = matching_groups()
        if problem:
            return self._blocked(problem)
        if not groups:
            return DirectActionResult(
                "completed", "orphan_component_groups_already_stopped=true",
            )
        for group in groups:
            result = self._command(
                _kill_argv_for_owner_pid(
                    group["owner_pid"], "TERM", "-%s" % group["pgid"],
                ),
                timeout=min(step.timeout, 30),
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "orphan_component_term_failed component=%s pgid=%s stderr=%s "
                    "environment_changed=unknown"
                    % (component, group["pgid"], _one_line(result.stderr)),
                    "inspect_process",
                )
        deadline = time.monotonic() + min(8.0, max(1.0, float(step.timeout)))
        while time.monotonic() < deadline:
            live = {
                int(proc["pid"])
                for proc in _klonet_runtime_processes(
                    root,
                    command_runner=self._command,
                    allow_command_root_identity=True,
                )
            }
            if frozen.isdisjoint(live):
                return DirectActionResult(
                    "completed",
                    "orphan_component_groups_stopped component=%s pids=%s "
                    "signal=TERM environment_changed=true"
                    % (component, ",".join(str(pid) for pid in sorted(frozen))),
                )
            time.sleep(0.2)

        remaining_groups, problem = matching_groups()
        if problem:
            return self._blocked(problem + "_before_kill")
        for group in remaining_groups:
            result = self._command(
                _kill_argv_for_owner_pid(
                    group["owner_pid"], "KILL", "-%s" % group["pgid"],
                ),
                timeout=min(step.timeout, 30),
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "orphan_component_kill_failed component=%s pgid=%s stderr=%s "
                    "environment_changed=unknown"
                    % (component, group["pgid"], _one_line(result.stderr)),
                    "inspect_process",
                )
        kill_deadline = time.monotonic() + min(
            8.0, max(1.0, float(step.timeout)),
        )
        while time.monotonic() < kill_deadline:
            if frozen.isdisjoint(_component_pids(root, component)):
                return DirectActionResult(
                    "completed",
                    "orphan_component_groups_stopped component=%s pids=%s "
                    "signal=TERM,KILL environment_changed=true"
                    % (component, ",".join(str(pid) for pid in sorted(frozen))),
                )
            time.sleep(0.2)
        return DirectActionResult(
            "failed",
            "orphan_component_not_stopped component=%s pids=%s "
            "environment_changed=true"
            % (component, ",".join(str(pid) for pid in sorted(frozen))),
            "inspect_process",
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
        suffix = _component_suffix(component, step.args)
        if not suffix or not _safe_token(session):
            return self._blocked("invalid_component_or_screen_session")
        expected = "%s_%s" % (platform, suffix)
        if session != expected:
            return self._blocked(
                "screen_session_mismatch expected=%s" % expected
            )
        run_as_uid = _runtime_run_as_uid(step.args)
        if run_as_uid is None:
            return self._blocked("invalid_run_as_uid")
        dead_targets, cleanup_error = self._clean_dead_screen_session(
            session, run_as_uid,
        )
        if cleanup_error:
            return DirectActionResult(
                "failed", cleanup_error, "inspect_runtime",
            )
        targets = self._screen_session_targets(session, run_as_uid)
        control_rc = _runtime_screen_control_path(root, session)
        control_check = self._command(
            _runtime_user_argv(
                ["test", "-f", str(control_rc)], run_as_uid,
            ),
            timeout=10,
        )
        port = component_port_arg(step.args, component)
        old_pids = (
            _listener_pids_for_port(port)
            if port else _component_pids(root, component)
        )
        if port:
            allowed = _allowed_runtime_cwds(root)
            wrong = [
                pid for pid in old_pids
                if not _proc_cwd(pid) or not any(
                    _path_is_relative_to(Path(_proc_cwd(pid)), candidate)
                    for candidate in allowed
                )
            ]
            if wrong:
                return self._blocked(
                    "component_listener_wrong_project_root=%s" % ",".join(
                        str(pid) for pid in wrong
                    )
                )
        owner_targets, ownership_observed = _screen_owner_targets_for_pids(
            old_pids,
        )
        target_owns_runtime = bool(
            set(targets).intersection(owner_targets)
        )
        foreign_owner_targets = [
            target for target in owner_targets if target not in targets
        ]
        proven_orphan_pids: list[int] = []
        for pid in old_pids:
            per_pid_owners, per_pid_observed = _screen_owner_targets_for_pids(
                [pid],
            )
            if per_pid_observed and not per_pid_owners:
                proven_orphan_pids.append(pid)
        if proven_orphan_pids:
            orphan_cleanup = self._stop_frozen_component_groups(
                root, component, proven_orphan_pids, step,
            )
            if orphan_cleanup.status != "completed":
                return DirectActionResult(
                    "failed",
                    "screen_restart_orphan_cleanup_failed component=%s reason=%s "
                    "environment_changed=unknown"
                    % (component, orphan_cleanup.output),
                    "inspect_process",
                )
        # A named Screen can outlive the foreground service it manages, and a
        # role can also be owned by a differently named Screen from an older
        # convention.  Screen presence alone is never process ownership.
        # Preserve the interactive shell only when /proc ancestry proves that
        # it owns the frozen runtime.  When ancestry is unavailable (mainly
        # restricted /proc environments), retain the prior conservative
        # behavior instead of guessing that another session owns the process.
        stale_screen = bool(targets) and (
            not old_pids
            or (ownership_observed and not target_owns_runtime)
            or bool(foreign_owner_targets)
        )
        screen_migration = bool(
            old_pids
            and ownership_observed
            and (not target_owns_runtime or bool(foreign_owner_targets))
        )
        if control_check.returncode == 0 and targets and not stale_screen:
            target = targets[0]
            interrupted = self._command(
                _runtime_user_argv(
                    ["screen", "-S", target, "-p", "0", "-X", "stuff", "\x03"],
                    run_as_uid,
                ),
                timeout=20,
            )
            if interrupted.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "interactive_screen_interrupt_failed session=%s stderr=%s environment_changed=unknown"
                    % (session, _one_line(interrupted.stderr)),
                    "inspect_runtime",
                )
            if port and not _wait_tcp_released("127.0.0.1", port, timeout=20.0):
                return self._blocked(
                    "interactive_component_port_not_released=%s" % port
                )
            restarted = self._command(
                _runtime_user_argv(
                    [
                        "screen", "-S", target, "-p", "0", "-X", "stuff",
                        "klonet_start\n",
                    ],
                    run_as_uid,
                ),
                timeout=20,
            )
            if restarted.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "interactive_screen_restart_failed session=%s stderr=%s environment_changed=unknown"
                    % (session, _one_line(restarted.stderr)),
                    "inspect_runtime",
                )
            if port and not _wait_tcp_listening(
                "127.0.0.1", port, timeout=min(float(step.timeout), 20.0),
            ):
                return DirectActionResult(
                    "failed",
                    "component_port_not_ready component=%s port=%s environment_changed=unknown"
                    % (component, port),
                    "inspect_runtime",
                )
            new_pids = (
                _listener_pids_for_port(port)
                if port else _wait_component_new_pids(
                    root, component, set(old_pids), timeout=min(float(step.timeout), 20.0),
                )
            )
            if port and (
                not new_pids
                or set(old_pids).intersection(new_pids)
                or not any(_proc_cwd(pid) == str(root) for pid in new_pids)
            ):
                return DirectActionResult(
                    "failed",
                    "component_restart_identity_failed component=%s old_pids=%s new_pids=%s environment_changed=unknown"
                    % (
                        component,
                        ",".join(str(pid) for pid in old_pids) or "none",
                        ",".join(str(pid) for pid in new_pids) or "none",
                    ),
                    "inspect_runtime",
                )
            if not port and (
                not new_pids
                or set(old_pids).intersection(new_pids)
                or not any(_proc_cwd(pid) == str(root) for pid in new_pids)
            ):
                return DirectActionResult(
                    "failed",
                    "component_restart_identity_failed component=%s old_pids=%s new_pids=%s environment_changed=unknown"
                    % (
                        component,
                        ",".join(str(pid) for pid in old_pids) or "none",
                        ",".join(str(pid) for pid in new_pids) or "none",
                    ),
                    "inspect_runtime",
                )
            return DirectActionResult(
                "completed",
                "action=restart_screen_component component=%s session=%s "
                "session_preserved=true old_pids=%s new_pids=%s "
                "cleaned_orphan_pids=%s environment_changed=true"
                % (
                    component, session,
                    ",".join(str(pid) for pid in old_pids) or "none",
                    ",".join(str(pid) for pid in new_pids) or "none",
                    ",".join(str(pid) for pid in proven_orphan_pids) or "none",
                ),
                metadata={
                    "kind": "component_restart",
                    "component": component,
                    "session": session,
                    "old_pids": ",".join(str(pid) for pid in old_pids),
                    "new_pids": ",".join(str(pid) for pid in new_pids),
                },
            )
        cleanup_targets = list(targets)
        if screen_migration:
            cleanup_targets.extend(
                target for target in owner_targets
                if target not in cleanup_targets
            )
        failed_targets = []
        for target in cleanup_targets:
            stopped = self._command(
                _runtime_user_argv(
                    ["screen", "-S", target, "-X", "quit"], run_as_uid,
                ),
                timeout=20,
            )
            if stopped.returncode != 0:
                failed_targets.append(
                    "%s:%s" % (target, _one_line(stopped.stderr))
                )
        if failed_targets:
            return DirectActionResult(
                "failed",
                "screen_restart_cleanup_failed session=%s stderr=%s "
                "environment_changed=unknown"
                % (session, ";".join(failed_targets)),
                "inspect_runtime",
            )
        if cleanup_targets:
            for _attempt in range(20):
                existing = self._existing_screen_sessions(run_as_uid)
                if not any(
                    _screen_logical_name(target) in existing
                    for target in cleanup_targets
                ):
                    break
                time.sleep(0.1)
            existing = self._existing_screen_sessions(run_as_uid)
            remaining_sessions = [
                target for target in cleanup_targets
                if _screen_logical_name(target) in existing
            ]
            if remaining_sessions:
                return DirectActionResult(
                    "failed",
                    "screen_restart_cleanup_incomplete sessions=%s "
                    "environment_changed=unknown" % ",".join(remaining_sessions),
                    "inspect_runtime",
                )
        # Once this Action deliberately removes a Screen, every old PID it
        # froze before that removal is now its lifecycle responsibility.  A
        # foreground service can survive Screen teardown (notably when an old
        # session lacks the interactive control rc).  Clean only those exact
        # frozen groups before creating the replacement; do not rediscover a
        # new target by process name or by whichever PID currently owns a
        # port.
        if cleanup_targets and old_pids:
            stopped_runtime = self._stop_frozen_component_groups(
                root, component, old_pids, step,
            )
            if stopped_runtime.status != "completed":
                return DirectActionResult(
                    "failed",
                    "screen_replacement_runtime_stop_failed component=%s reason=%s "
                    "environment_changed=unknown"
                    % (component, stopped_runtime.output),
                    "inspect_runtime",
                )
        if old_pids and port and not _wait_tcp_released(
            "127.0.0.1", port, timeout=8.0,
        ):
            return DirectActionResult(
                "failed",
                "screen_migration_port_not_released=%s environment_changed=unknown"
                % port,
                "inspect_runtime",
            )
        started = self._start_one_component(
            component, session, root, step
        )
        if started.status != "completed":
            return started
        new_pids = (
            _listener_pids_for_port(port)
            if port else _wait_component_new_pids(
                root, component, set(old_pids), timeout=min(float(step.timeout), 20.0),
            )
        )
        if (
            not new_pids
            or set(old_pids).intersection(new_pids)
            or not any(_proc_cwd(pid) == str(root) for pid in new_pids)
        ):
            return DirectActionResult(
                "failed",
                "component_restart_identity_failed component=%s old_pids=%s new_pids=%s environment_changed=unknown"
                % (
                    component,
                    ",".join(str(pid) for pid in old_pids) or "none",
                    ",".join(str(pid) for pid in new_pids) or "none",
                ),
                "inspect_runtime",
            )
        return DirectActionResult(
            "completed",
            "%s restart_state=%s old_pids=%s new_pids=%s" % (
                started.output,
                "dead_screen_replaced" if dead_targets else (
                        "runtime_screen_migrated" if screen_migration else (
                        "stale_screen_replaced" if stale_screen else (
                        "missing_screen_created" if not targets else "screen_replaced"
                    ))
                ),
                ",".join(str(pid) for pid in old_pids) or "none",
                ",".join(str(pid) for pid in new_pids) or "none",
            ),
            metadata={
                "kind": "component_restart", "component": component,
                "session": session,
                "old_pids": ",".join(str(pid) for pid in old_pids),
                "new_pids": ",".join(str(pid) for pid in new_pids),
            },
        )

    def _action_stop_screen_component(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        session = str(step.args.get("screen_session") or "").strip()
        if not _safe_token(session):
            return self._blocked("invalid_screen_session")
        targets = self._screen_session_targets(session)
        if not targets:
            return DirectActionResult(
                "completed",
                "action=stop_screen_component session=%s already_stopped=true environment_changed=false"
                % session,
            )
        failed = []
        for target in targets:
            result = self._command(
                ["screen", "-S", target, "-X", "quit"],
                timeout=min(step.timeout, 30),
            )
            if result.returncode != 0:
                failed.append("%s:%s" % (target, _one_line(result.stderr)))
        if failed:
            return DirectActionResult(
                "failed",
                "screen_stop_failed session=%s stderr=%s environment_changed=unknown"
                % (session, ";".join(failed)),
                "inspect_runtime",
            )
        return DirectActionResult(
            "completed",
            "action=stop_screen_component session=%s targets=%s environment_changed=true"
            % (session, ",".join(targets)),
        )

    def _action_stop_platform_screens(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        platform, root, problem = _platform_root(step)
        if problem:
            return self._blocked(problem)
        contracts = step.args.get("component_contracts")
        if not isinstance(contracts, list) or not contracts:
            return self._blocked("component_contracts_required")
        run_as_uid = _runtime_run_as_uid(step.args)
        if run_as_uid is None:
            return self._blocked("invalid_run_as_uid")
        frozen = []
        allowed_cwds = _allowed_runtime_cwds(root)
        for item in contracts:
            if not isinstance(item, dict):
                return self._blocked("invalid_component_contract")
            component = str(item.get("component") or "").strip()
            suffix = _component_suffix(component, item)
            session = str(item.get("screen_session") or "").strip()
            pids = [int(pid) for pid in item.get("pids") or [] if str(pid).isdigit()]
            ports = [int(port) for port in item.get("ports") or [] if str(port).isdigit()]
            if not suffix or session != "%s_%s" % (platform, suffix) or not pids:
                return self._blocked("invalid_component_contract=%s" % component)
            wrong_pids = [
                pid for pid in pids
                if not _proc_cwd(pid) or not any(
                    _path_is_relative_to(Path(_proc_cwd(pid)), candidate)
                    for candidate in allowed_cwds
                )
            ]
            if wrong_pids:
                return self._blocked(
                    "component_contract_wrong_project_root=%s:%s"
                    % (component, ",".join(str(pid) for pid in wrong_pids))
                )
            for port in ports:
                listeners = set(_listener_pids_for_port(port))
                if listeners and not listeners.intersection(pids):
                    return self._blocked(
                        "component_contract_port_owner_mismatch=%s:%s"
                        % (component, port)
                    )
            targets = self._screen_session_targets(session, run_as_uid)
            frozen.append((component, session, pids, ports, targets))
        stopped = []
        for _component, session, _pids, _ports, targets in frozen:
            for target in targets:
                result = self._command(
                    _runtime_user_argv(
                        ["screen", "-S", target, "-X", "quit"], run_as_uid,
                    ),
                    timeout=min(step.timeout, 30),
                )
                if result.returncode == 0:
                    stopped.append(target)
        remaining = [
            "%s:%s" % (component, pid)
            for component, _session, pids, _ports, _targets in frozen
            for pid in pids if Path("/proc/%s" % pid).exists()
        ]
        listening = [
            "%s:%s" % (component, port)
            for component, _session, _pids, ports, _targets in frozen
            for port in ports if _listener_pids_for_port(port)
        ]
        if remaining or listening:
            return DirectActionResult(
                "failed",
                "platform_stop_incomplete remaining=%s listening=%s environment_changed=unknown"
                % (",".join(remaining) or "none", ",".join(listening) or "none"),
                "inspect_runtime",
            )
        if not stopped:
            return DirectActionResult(
                "completed",
                "action=stop_platform_screens stopped=none environment_changed=false",
            )
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
        original = None
        if path.exists():
            if not path.is_file():
                return self._blocked("invalid_write_path")
            try:
                original = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return self._blocked("write_target_not_readable_text")
        result, backup, created = self._commit_text_candidate(
            path,
            original,
            content,
            step.timeout,
        )
        if result:
            return result
        return DirectActionResult(
            "completed",
            "action=write_ops_file path=%s backup=%s bytes=%s "
            "candidate_validation=passed environment_changed=true"
            % (path, backup or "none", len(content.encode("utf-8"))),
            metadata=_mutation_metadata(path, backup, created),
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
        if matches == 0 and not new_text:
            return DirectActionResult(
                "completed",
                "action=replace_text_in_file path=%s already_absent=true "
                "environment_changed=false" % path,
            )
        if matches != 1:
            return self._blocked(
                "replacement_match_count=%s expected=1" % matches
            )
        updated = content.replace(old_text, new_text, 1)
        result, backup, created = self._commit_text_candidate(
            path,
            content,
            updated,
            step.timeout,
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
            metadata=_mutation_metadata(path, backup, created),
        )

    def _action_insert_text_before_anchor(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        """Insert bounded text before one exact anchor and retain a backup."""

        path = _absolute_path(step.args.get("path"))
        anchor = str(step.args.get("anchor") or "")
        insertion = str(step.args.get("content") or "")
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() not in _SAFE_TEXT_SUFFIXES
            or _SENSITIVE_NAME.search(path.name)
        ):
            return self._blocked("invalid_text_insertion_target")
        if (
            not anchor
            or not insertion.strip()
            or len(anchor) > 4000
            or len(insertion) > 12000
            or _SENSITIVE_CONTENT.search(anchor)
            or _SENSITIVE_CONTENT.search(insertion)
        ):
            return self._blocked("invalid_or_sensitive_insertion")
        try:
            if path.stat().st_size > 2_000_000:
                return self._blocked("insertion_target_too_large")
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._blocked("insertion_target_not_readable_text")
        matches = original.count(anchor)
        if matches != 1:
            return self._blocked(
                "insertion_anchor_match_count=%s expected=1" % matches
            )
        if insertion in original:
            return DirectActionResult(
                "completed",
                "action=insert_text_before_anchor path=%s already_present=true "
                "environment_changed=false" % path,
            )
        separator = "" if insertion.endswith("\n") else "\n"
        updated = original.replace(anchor, insertion + separator + anchor, 1)
        result, backup, created = self._commit_text_candidate(
            path,
            original,
            updated,
            step.timeout,
        )
        if result:
            return result
        return DirectActionResult(
            "completed",
            "action=insert_text_before_anchor path=%s backup=%s matches=1 "
            "environment_changed=true" % (path, backup),
            metadata=_mutation_metadata(path, backup, created),
        )

    def _action_edit_text_file(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        """Apply one bounded text edit selected by an explicit operation."""

        path = _absolute_path(step.args.get("path"))
        operation = str(step.args.get("operation") or "").strip().lower()
        anchor = str(step.args.get("anchor") or "")
        content = str(step.args.get("content") or "")
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() not in _SAFE_TEXT_SUFFIXES
            or _SENSITIVE_NAME.search(path.name)
        ):
            return self._blocked("invalid_text_edit_target")
        if (
            operation not in {
                "replace_file",
                "replace_once",
                "insert_before",
                "insert_after",
                "append",
            }
            or not content
            or len(anchor) > 12000
            or len(content) > 24000
            or _SENSITIVE_CONTENT.search(anchor)
            or _SENSITIVE_CONTENT.search(content)
        ):
            return self._blocked("invalid_or_sensitive_text_edit")
        if operation in {"replace_once", "insert_before", "insert_after"}:
            if not anchor:
                return self._blocked("text_edit_anchor_required")
        elif anchor:
            return self._blocked("text_edit_anchor_must_be_empty")
        try:
            if path.stat().st_size > 2_000_000:
                return self._blocked("text_edit_target_too_large")
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._blocked("text_edit_target_not_readable")

        if operation == "replace_file":
            updated = content
        elif operation == "append":
            if content in original:
                return DirectActionResult(
                    "completed",
                    "action=edit_text_file operation=append path=%s "
                    "already_present=true environment_changed=false" % path,
                )
            separator = "" if original.endswith("\n") or not original else "\n"
            updated = original + separator + content
        else:
            matches = original.count(anchor)
            if matches != 1:
                return self._blocked(
                    "text_edit_anchor_match_count=%s expected=1" % matches
                )
            if operation in {"insert_before", "insert_after"} and content in original:
                return DirectActionResult(
                    "completed",
                    "action=edit_text_file operation=%s path=%s "
                    "already_present=true environment_changed=false"
                    % (operation, path),
                )
            if operation == "replace_once":
                replacement = content
            elif operation == "insert_before":
                replacement = content + ("" if content.endswith("\n") else "\n") + anchor
            else:
                replacement = anchor + ("" if anchor.endswith("\n") else "\n") + content
            updated = original.replace(anchor, replacement, 1)

        if updated == original:
            return DirectActionResult(
                "completed",
                "action=edit_text_file operation=%s path=%s unchanged=true "
                "environment_changed=false" % (operation, path),
            )
        result, backup, created = self._commit_text_candidate(
            path,
            original,
            updated,
            step.timeout,
        )
        if result:
            return result
        return DirectActionResult(
            "completed",
            "action=edit_text_file operation=%s path=%s backup=%s "
            "environment_changed=true" % (operation, path, backup),
            metadata=_mutation_metadata(path, backup, created),
        )

    def _action_upsert_python_class(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        """Upsert a top-level class without asking an LLM for text spans."""

        path = _absolute_path(step.args.get("path"))
        class_name = str(step.args.get("class_name") or "").strip()
        base_class = str(step.args.get("base_class") or "").strip()
        body = textwrap.dedent(str(step.args.get("body") or "")).strip("\n")
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() != ".py"
            or _SENSITIVE_NAME.search(path.name)
            or not re.fullmatch(r"[A-Za-z_]\w*", class_name)
            or (base_class and not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", base_class
            ))
            or (base_class and class_name == base_class)
            or not body
            or len(body) > 24000
            or _SENSITIVE_CONTENT.search(body)
        ):
            return self._blocked("invalid_python_class_contract")
        try:
            original = path.read_text(encoding="utf-8")
            tree = ast.parse(original, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            return self._blocked("python_class_target_not_parseable")

        if not base_class:
            base_class = (
                "CommonConfig"
                if class_name.endswith("Config")
                and any(
                    isinstance(node, ast.ClassDef)
                    and node.name == "CommonConfig"
                    for node in tree.body
                )
                else "object"
            )

        # Validate the body independently before editing the target.
        class_source = "class %s(%s):\n%s\n" % (
            class_name,
            base_class,
            textwrap.indent(body, "    "),
        )
        try:
            wrapped = ast.parse(class_source)
        except SyntaxError:
            return self._blocked("python_class_body_invalid")
        if any(
            isinstance(node, ast.ClassDef)
            for node in wrapped.body[0].body
        ):
            return self._blocked(
                "python_class_body_must_not_include_class_header"
            )

        lines = original.splitlines(keepends=True)
        target = next(
            (
                node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            None,
        )
        if target is not None:
            start = min(
                [target.lineno]
                + [item.lineno for item in target.decorator_list]
            ) - 1
            end = int(target.end_lineno or target.lineno)
            del lines[start:end]

        without_target = "".join(lines)
        try:
            remaining_tree = ast.parse(without_target, filename=str(path))
        except SyntaxError:
            return self._blocked("python_class_intermediate_not_parseable")
        local_base_name = base_class.split(".")[-1]
        base = next(
            (
                node for node in remaining_tree.body
                if isinstance(node, ast.ClassDef) and node.name == local_base_name
            ),
            None,
        )
        if "." not in base_class and base_class != "object" and base is None:
            return self._blocked("python_class_local_base_missing=%s" % base_class)
        insert_at = int(base.end_lineno or base.lineno) if base is not None else len(lines)
        insertion = "\n\n" + class_source.rstrip() + "\n"
        lines.insert(insert_at, insertion)
        updated = "".join(lines)
        result, backup, created = self._commit_text_candidate(
            path,
            original,
            updated,
            step.timeout,
        )
        if result:
            return result
        return DirectActionResult(
            "completed",
            "action=upsert_python_class path=%s class=%s base=%s backup=%s "
            "candidate_validation=passed environment_changed=true"
            % (path, class_name, base_class, backup),
            metadata=_mutation_metadata(path, backup, created),
        )

    def _action_set_python_config_assignment(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        """Switch one top-level config assignment using Python structure."""

        path = _absolute_path(step.args.get("path"))
        assignment_name = str(
            step.args.get("assignment_name") or "PROJ_CONFIG"
        ).strip()
        class_name = str(step.args.get("class_name") or "").strip()
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() != ".py"
            or _SENSITIVE_NAME.search(path.name)
            or not re.fullmatch(r"[A-Za-z_]\w*", assignment_name)
            or not re.fullmatch(r"[A-Za-z_]\w*", class_name)
        ):
            return self._blocked("invalid_python_config_assignment_contract")
        try:
            original = path.read_text(encoding="utf-8")
            tree = ast.parse(original, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            return self._blocked("python_config_assignment_target_not_parseable")

        classes = [
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        assignments = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == assignment_name
                for target in node.targets
            ):
                assignments.append(node)
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == assignment_name
            ):
                assignments.append(node)
        if len(classes) != 1:
            return self._blocked(
                "python_config_class_count=%s expected=1" % len(classes)
            )
        if len(assignments) != 1:
            return self._blocked(
                "python_config_assignment_count=%s expected=1"
                % len(assignments)
            )
        assignment = assignments[0]
        if int(classes[0].lineno) >= int(assignment.lineno):
            return self._blocked("python_config_class_must_precede_assignment")

        lines = original.splitlines(keepends=True)
        start = int(assignment.lineno) - 1
        end = int(assignment.end_lineno or assignment.lineno)
        replacement = "%s = %s()\n" % (assignment_name, class_name)
        updated = "".join([*lines[:start], replacement, *lines[end:]])
        if updated == original:
            return DirectActionResult(
                "completed",
                "action=set_python_config_assignment path=%s unchanged=true "
                "environment_changed=false" % path,
            )
        result, backup, created = self._commit_text_candidate(
            path,
            original,
            updated,
            step.timeout,
        )
        if result:
            return result
        return DirectActionResult(
            "completed",
            "action=set_python_config_assignment path=%s assignment=%s "
            "class=%s backup=%s candidate_validation=passed "
            "environment_changed=true"
            % (path, assignment_name, class_name, backup),
            metadata=_mutation_metadata(path, backup, created),
        )

    def _action_set_python_class_attribute(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        """Set one scalar class attribute without text anchors or line guesses."""

        path = _absolute_path(step.args.get("path"))
        class_name = str(step.args.get("class_name") or "").strip()
        attribute = str(step.args.get("attribute") or "").strip()
        raw_value = step.args.get("value")
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() != ".py"
            or _SENSITIVE_NAME.search(path.name)
            or (class_name and not re.fullmatch(r"[A-Za-z_]\w*", class_name))
            or not re.fullmatch(r"[A-Za-z_]\w*", attribute)
            or raw_value is None
        ):
            return self._blocked("invalid_python_class_attribute_contract")
        try:
            original = path.read_text(encoding="utf-8")
            tree = ast.parse(original, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            return self._blocked("python_class_attribute_target_not_parseable")

        if not class_name:
            active_classes = []
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Name) and target.id == "PROJ_CONFIG"
                    for target in node.targets
                ):
                    continue
                if (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and not node.value.args
                    and not node.value.keywords
                ):
                    active_classes.append(node.value.func.id)
            if len(active_classes) != 1:
                return self._blocked(
                    "active_python_config_class_count=%s expected=1"
                    % len(active_classes)
                )
            class_name = active_classes[0]

        classes = [
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        if len(classes) != 1:
            return self._blocked(
                "python_attribute_class_count=%s expected=1" % len(classes)
            )
        target_class = classes[0]
        assignments = []
        for node in target_class.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == attribute
                for target in node.targets
            ):
                assignments.append(node)
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == attribute
            ):
                assignments.append(node)
        if len(assignments) > 1:
            return self._blocked(
                "python_attribute_assignment_count=%s expected_at_most=1"
                % len(assignments)
            )

        value = raw_value
        if isinstance(value, str):
            stripped = value.strip()
            if re.fullmatch(r"-?(?:0|[1-9]\d*)", stripped):
                value = int(stripped)
            elif stripped.lower() in {"true", "false"}:
                value = stripped.lower() == "true"
            elif stripped.lower() in {"none", "null"}:
                value = None
            else:
                value = stripped
        if not isinstance(value, (str, int, float, bool, type(None))):
            return self._blocked("python_attribute_value_must_be_scalar")
        replacement = "    %s = %r\n" % (attribute, value)
        lines = original.splitlines(keepends=True)
        if assignments:
            assignment = assignments[0]
            start = int(assignment.lineno) - 1
            end = int(assignment.end_lineno or assignment.lineno)
            updated = "".join([*lines[:start], replacement, *lines[end:]])
        else:
            insert_at = int(target_class.end_lineno or target_class.lineno)
            lines.insert(insert_at, replacement)
            updated = "".join(lines)
        if updated == original:
            return DirectActionResult(
                "completed",
                "action=set_python_class_attribute path=%s class=%s "
                "attribute=%s unchanged=true environment_changed=false"
                % (path, class_name, attribute),
            )
        result, backup, created = self._commit_text_candidate(
            path,
            original,
            updated,
            step.timeout,
        )
        if result:
            return result
        return DirectActionResult(
            "completed",
            "action=set_python_class_attribute path=%s class=%s attribute=%s "
            "backup=%s candidate_validation=passed environment_changed=true"
            % (path, class_name, attribute, backup),
            metadata=_mutation_metadata(path, backup, created),
        )

    def _action_install_nginx_config(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        temporary_source = None
        inline_content = step.args.get("content")
        if str(inline_content or "").strip():
            content = str(inline_content)
            if _SENSITIVE_CONTENT.search(content):
                return self._blocked("invalid_nginx_inline_content")
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="klonet-nginx-",
                suffix=".conf",
                delete=False,
            ) as handle:
                handle.write(content)
                temporary_source = Path(handle.name)
            source = temporary_source
        else:
            source = _absolute_path(step.args.get("source_path"))
        name = str(step.args.get("config_name") or "").strip()
        if source is None or not source.is_file():
            return self._blocked("nginx_source_not_found")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name)
            or ".." in name
        ):
            return self._blocked("invalid_nginx_config_name")
        destination = Path("/etc/nginx/sites-available") / name
        enabled = Path("/etc/nginx/sites-enabled") / name
        if destination.exists() or enabled.exists():
            return self._blocked("nginx_config_already_exists=%s" % name)
        try:
            command = _sudo_if_needed(
                ["install", "-o", "root", "-g", "root", "-m", "0644",
                 str(source), str(destination)]
            )
            result = self._command(command, timeout=step.timeout)
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "nginx_config_install_failed stderr=%s environment_changed=false"
                    % _one_line(result.stderr),
                    "inspect_nginx_routes",
                )
            link_result = self._command(
                _sudo_if_needed(
                    ["ln", "-sfn", str(destination), str(enabled)]
                ),
                timeout=step.timeout,
            )
            if link_result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "nginx_config_enable_failed destination=%s stderr=%s "
                    "environment_changed=true"
                    % (destination, _one_line(link_result.stderr)),
                    "inspect_nginx_routes",
                )
            return DirectActionResult(
                "completed",
                "action=install_nginx_config destination=%s enabled=%s "
                "environment_changed=true"
                % (destination, enabled),
            )
        finally:
            if temporary_source is not None:
                temporary_source.unlink(missing_ok=True)

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
        pgrep = shutil.which("pgrep")
        master_running = False
        if pgrep:
            process_check = self._command(
                [pgrep, "-f", "^nginx: master process"],
                timeout=min(step.timeout, 5),
            )
            master_running = process_check.returncode == 0
        systemctl = shutil.which("systemctl")
        if master_running:
            activation = "signal-reload"
            activation_command = [nginx, "-s", "reload"]
        elif systemctl:
            activation = "start"
            activation_command = [systemctl, "start", "nginx"]
        else:
            activation = "start"
            activation_command = [nginx]
        reload_result = self._command(
            _sudo_if_needed(activation_command),
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
            "action=reload_nginx config_test=passed activation=%s "
            "environment_changed=true" % activation,
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
        signal_name = normalize_process_signal(step.args.get("signal"))
        if pid <= 1 or signal_name not in {"TERM", "KILL", "HUP", "INT"}:
            return self._blocked("pid_or_signal_not_allowed")
        expected = str(step.args.get("expected_command") or "").strip()
        cmdline = _proc_cmdline(pid)
        if not cmdline:
            return self._blocked("pid_not_found")
        if expected and expected not in cmdline:
            return self._blocked("pid_identity_mismatch")
        scope = str(step.args.get("scope") or "pid").strip().lower()
        if scope in {"process_group", "pgid"}:
            pgid = _process_group_id(step.args.get("pgid"), pid)
            if pgid is None or pgid <= 1:
                return self._blocked("process_group_not_allowed")
            kill_target = "-%s" % pgid
        elif scope == "pid":
            kill_target = str(pid)
        else:
            return self._blocked("process_scope_not_allowed")
        result = self._command(
            _kill_argv_for_owner_pid(pid, signal_name, kill_target),
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

    def _action_stop_klonet_runtime_instance(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        runtime_cwd = _absolute_path(step.args.get("runtime_cwd"))
        if runtime_cwd is None or _protected_target(runtime_cwd):
            return self._blocked("invalid_runtime_cwd")
        runtime_cwd = _normalized_klonet_runtime_cwd(runtime_cwd)
        if runtime_cwd is None:
            return self._blocked("runtime_cwd_not_klonet_backend")
        ports = _port_arg_list(step.args.get("ports"))
        if not ports:
            return self._blocked("invalid_runtime_ports")
        processes = _klonet_runtime_processes(
            runtime_cwd,
            command_runner=self._command,
        )
        if not processes:
            owned_ports = _runtime_ports_owned_by_allowed_cwd(ports, runtime_cwd)
            if owned_ports:
                return self._blocked(
                    "runtime_ports_owned_by_unmatched_process=%s"
                    % ",".join(str(port) for port in owned_ports)
                )
            return DirectActionResult(
                "completed",
                "action=stop_klonet_runtime_instance runtime_cwd=%s already_stopped=true environment_changed=false"
                % runtime_cwd,
            )
        term_groups = _runtime_process_groups(processes)
        for group in term_groups:
            result = self._command(
                _kill_argv_for_owner_pid(group["owner_pid"], "TERM", "-%s" % group["pgid"]),
                timeout=min(step.timeout, 30),
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "stop_runtime_term_failed pgid=%s stderr=%s environment_changed=unknown"
                    % (group["pgid"], _one_line(result.stderr)),
                    "inspect_process",
                )
        stability_seconds = _positive_float(step.args.get("stability_seconds"), default=3.0, maximum=10.0)
        if _runtime_stopped_stably(self, runtime_cwd, ports, timeout=8.0, stable_for=stability_seconds):
            return DirectActionResult(
                "completed",
                "action=stop_klonet_runtime_instance runtime_cwd=%s signal=TERM pgids=%s ports=%s environment_changed=true"
                % (
                    runtime_cwd,
                    ",".join(str(group["pgid"]) for group in term_groups),
                    ",".join(str(port) for port in ports),
                ),
            )
        remaining = _klonet_runtime_processes(
            runtime_cwd, command_runner=self._command,
        )
        kill_groups = _runtime_process_groups(remaining)
        for group in kill_groups:
            result = self._command(
                _kill_argv_for_owner_pid(group["owner_pid"], "KILL", "-%s" % group["pgid"]),
                timeout=min(step.timeout, 30),
            )
            if result.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "stop_runtime_kill_failed pgid=%s stderr=%s environment_changed=unknown"
                    % (group["pgid"], _one_line(result.stderr)),
                    "inspect_process",
                )
        if not _runtime_stopped_stably(self, runtime_cwd, ports, timeout=8.0, stable_for=stability_seconds):
            occupied = _ports_currently_listening(self, ports)
            return DirectActionResult(
                "failed",
                "runtime_not_stopped_stably remaining_pids=%s runtime_ports_still_listening=%s environment_changed=true"
                % (
                    ",".join(
                        str(proc["pid"])
                        for proc in _klonet_runtime_processes(
                            runtime_cwd, command_runner=self._command,
                        )
                    ) or "none",
                    ",".join(str(port) for port in occupied) or "none",
                ),
                "inspect_process",
            )
        all_groups = term_groups + [group for group in kill_groups if group not in term_groups]
        return DirectActionResult(
            "completed",
            "action=stop_klonet_runtime_instance runtime_cwd=%s signal=TERM,KILL pgids=%s ports=%s environment_changed=true"
            % (
                runtime_cwd,
                ",".join(str(group["pgid"]) for group in all_groups),
                ",".join(str(port) for port in ports),
            ),
        )

    def _action_stop_klonet_component(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        """Stop exactly one role group after rechecking PID/cwd/role/port."""

        runtime_cwd = _absolute_path(step.args.get("runtime_cwd"))
        if runtime_cwd is None or _protected_target(runtime_cwd):
            return self._blocked("invalid_runtime_cwd")
        runtime_cwd = _normalized_klonet_runtime_cwd(runtime_cwd)
        if runtime_cwd is None:
            return self._blocked("runtime_cwd_not_klonet_backend")
        component = str(step.args.get("component") or "").strip().lower()
        if component not in {"master", "worker"}:
            return self._blocked("invalid_backend_component")
        try:
            expected_pid = int(step.args.get("pid"))
            port = int(step.args.get("port"))
        except (TypeError, ValueError):
            return self._blocked("invalid_component_pid_or_port")
        if expected_pid <= 1 or not 1 <= port <= 65535:
            return self._blocked("invalid_component_pid_or_port")

        processes = _klonet_runtime_processes(
            runtime_cwd,
            command_runner=self._command,
            expected_pid=expected_pid,
            allow_command_root_identity=True,
        )
        target = next(
            (proc for proc in processes if int(proc["pid"]) == expected_pid),
            None,
        )
        if target is None:
            return self._blocked("component_pid_state_drift")
        observed_role = _klonet_component_for_command(str(target["cmdline"]))
        if observed_role != component:
            return self._blocked(
                "component_role_mismatch expected=%s observed=%s"
                % (component, observed_role or "unknown")
            )
        pgid = int(target["pgid"])
        group = [proc for proc in processes if int(proc["pgid"]) == pgid]
        if not group or any(
            _klonet_component_for_command(str(proc["cmdline"])) != component
            for proc in group
        ):
            return self._blocked("component_process_group_mixed_roles")
        listener_pids = set(_listener_pids_for_port(port))
        group_pids = {int(proc["pid"]) for proc in group}
        if not listener_pids.intersection(group_pids):
            listener_groups = []
            listener_cwds = []
            for listener_pid in sorted(listener_pids):
                try:
                    listener_group = os.getpgid(listener_pid)
                except (OSError, ProcessLookupError):
                    listener_group = 0
                if listener_group > 0 and listener_group not in listener_groups:
                    listener_groups.append(listener_group)
                listener_cwd = _proc_cwd(
                    listener_pid,
                    command_runner=self._command,
                )
                if listener_cwd and listener_cwd not in listener_cwds:
                    listener_cwds.append(listener_cwd)
            return self._blocked(
                "component_port_not_owned_by_pid_group "
                "expected_pid=%s expected_pgid=%s port=%s "
                "actual_listener_pids=%s actual_listener_pgids=%s "
                "actual_listener_cwds=%s"
                % (
                    expected_pid,
                    pgid,
                    port,
                    ",".join(str(item) for item in sorted(listener_pids)) or "unknown",
                    ",".join(str(item) for item in listener_groups) or "unknown",
                    ",".join(listener_cwds) or "unknown",
                )
            )

        owner_pid = next(
            (int(proc["pid"]) for proc in group if int(proc["pid"]) == pgid),
            min(group_pids),
        )
        result = self._command(
            _kill_argv_for_owner_pid(owner_pid, "TERM", "-%s" % pgid),
            timeout=min(step.timeout, 30),
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "stop_component_term_failed pid=%s pgid=%s stderr=%s environment_changed=unknown"
                % (expected_pid, pgid, _one_line(result.stderr)),
                "inspect_process",
            )
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if not Path("/proc/%s" % expected_pid).exists() and not set(
                _listener_pids_for_port(port)
            ).intersection(group_pids):
                return DirectActionResult(
                    "completed",
                    "action=stop_klonet_component component=%s pid=%s pgid=%s runtime_cwd=%s port=%s signal=TERM environment_changed=true"
                    % (component, expected_pid, pgid, runtime_cwd, port),
                )
            time.sleep(0.2)
        # A Gunicorn role may legitimately exceed the graceful TERM window.
        # Re-freeze the exact same role/group/port identity before escalating;
        # never fall back to a name scan or a newly appeared PID.
        remaining = _klonet_runtime_processes(
            runtime_cwd,
            command_runner=self._command,
            expected_pid=expected_pid,
            allow_command_root_identity=True,
        )
        remaining_target = next(
            (proc for proc in remaining if int(proc["pid"]) == expected_pid),
            None,
        )
        if remaining_target is None:
            if not set(_listener_pids_for_port(port)).intersection(group_pids):
                return DirectActionResult(
                    "completed",
                    "action=stop_klonet_component component=%s pid=%s pgid=%s "
                    "runtime_cwd=%s port=%s signal=TERM environment_changed=true"
                    % (component, expected_pid, pgid, runtime_cwd, port),
                )
            return self._blocked("component_pid_state_drift_after_term")
        remaining_group = [
            proc for proc in remaining if int(proc["pgid"]) == pgid
        ]
        remaining_group_pids = {int(proc["pid"]) for proc in remaining_group}
        listeners = set(_listener_pids_for_port(port))
        if (
            int(remaining_target["pgid"]) != pgid
            or _klonet_component_for_command(
                str(remaining_target["cmdline"])
            ) != component
            or not remaining_group
            or any(
                _klonet_component_for_command(str(proc["cmdline"])) != component
                for proc in remaining_group
            )
            or not listeners.intersection(remaining_group_pids)
        ):
            return self._blocked("component_identity_drift_before_kill")
        killed = self._command(
            _kill_argv_for_owner_pid(owner_pid, "KILL", "-%s" % pgid),
            timeout=min(step.timeout, 30),
        )
        if killed.returncode != 0:
            return DirectActionResult(
                "failed",
                "stop_component_kill_failed pid=%s pgid=%s stderr=%s "
                "environment_changed=unknown"
                % (expected_pid, pgid, _one_line(killed.stderr)),
                "inspect_process",
            )
        kill_deadline = time.monotonic() + 8.0
        while time.monotonic() < kill_deadline:
            if not Path("/proc/%s" % expected_pid).exists() and not set(
                _listener_pids_for_port(port)
            ).intersection(remaining_group_pids):
                return DirectActionResult(
                    "completed",
                    "action=stop_klonet_component component=%s pid=%s pgid=%s "
                    "runtime_cwd=%s port=%s signal=TERM,KILL "
                    "environment_changed=true"
                    % (component, expected_pid, pgid, runtime_cwd, port),
                )
            time.sleep(0.2)
        return DirectActionResult(
            "failed",
            "component_not_stopped_after_kill pid=%s pgid=%s port=%s "
            "environment_changed=true"
            % (expected_pid, pgid, port),
            "inspect_process",
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

    def _action_create_docker_container(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        name = str(step.args.get("name") or "").strip()
        image = str(step.args.get("image") or "").strip()
        port_bindings = _string_list(step.args.get("port_bindings"))
        environment = _string_list(step.args.get("environment"))
        command = _string_list(step.args.get("command"))
        credential_source = step.args.get("credential_source")
        if credential_source is not None:
            resolved, problem = _resolve_klonet_container_credentials(
                credential_source,
                name=name,
                image=image,
            )
            if problem:
                return self._blocked(problem)
            protected_environment = {
                item.partition("=")[0] for item in resolved.get("environment", [])
            }
            environment = [
                item for item in environment
                if item.partition("=")[0] not in protected_environment
            ]
            environment.extend(resolved.get("environment", []))
            if resolved.get("command"):
                command = resolved["command"]
        restart_policy = str(
            step.args.get("restart_policy") or "unless-stopped"
        ).strip()
        if not _SAFE_CONTAINER_NAME.fullmatch(name):
            return self._blocked("invalid_new_container_name")
        if not _SAFE_CONTAINER_IMAGE.fullmatch(image) or ".." in image:
            return self._blocked("invalid_container_image")
        if not port_bindings or any(
            not _valid_container_port_binding(item) for item in port_bindings
        ):
            return self._blocked("invalid_container_port_bindings")
        if any(not _valid_container_environment(item) for item in environment):
            return self._blocked("invalid_container_environment")
        if command and not _valid_container_command(name, image, command):
            return self._blocked("invalid_container_command")
        if restart_policy not in {"no", "always", "unless-stopped", "on-failure"}:
            return self._blocked("invalid_container_restart_policy")
        docker = shutil.which("docker")
        if not docker:
            return self._blocked("docker_not_found")
        inspect = self._command(
            _sudo_if_needed([docker, "container", "inspect", name]),
            timeout=min(step.timeout, 30),
        )
        if inspect.returncode == 0:
            return self._blocked("container_already_exists=%s" % name)
        if not re.search(
            r"no such (?:object|container)",
            str(inspect.stderr or ""),
            re.I,
        ):
            return DirectActionResult(
                "failed",
                "container_absence_check_failed name=%s stderr=%s "
                "environment_changed=false" % (name, _one_line(inspect.stderr)),
                "inspect_docker",
            )
        image_check = self._command(
            _sudo_if_needed([docker, "image", "inspect", image]),
            timeout=min(step.timeout, 30),
        )
        if image_check.returncode != 0:
            return self._blocked("container_image_not_observed=%s" % image)
        argv = [docker, "run", "-d", "--name", name, "--restart", restart_policy]
        for binding in port_bindings:
            argv.extend(["-p", binding])
        for variable in environment:
            argv.extend(["-e", variable])
        argv.append(image)
        argv.extend(command)
        result = self._command(_sudo_if_needed(argv), timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "create_docker_container_failed name=%s image=%s stderr=%s "
                "environment_changed=unknown"
                % (name, image, _one_line(result.stderr)),
                "inspect_docker",
            )
        return DirectActionResult(
            "completed",
            "action=create_docker_container name=%s image=%s "
            "environment_changed=true" % (name, image),
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
        if operation in {"clone", "clone_at_revision"}:
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
        if operation == "clone_at_revision":
            revision = str(step.args.get("revision") or "").strip()
            checkout_argv = ["checkout", "--detach", revision]
            checkout_decision = decide_ops_command(
                {"program": "git", "argv": checkout_argv, "cwd": str(repository)}
            )
            if not checkout_decision.allowed:
                return self._blocked(
                    "git_operation_not_allowed=%s" % checkout_decision.reason
                )
            checkout = self._command(
                ["git", *checkout_decision.argv],
                cwd=repository,
                timeout=step.timeout,
            )
            if checkout.returncode != 0:
                return DirectActionResult(
                    "failed",
                    "git_operation_failed operation=%s stderr=%s environment_changed=unknown"
                    % (operation, _one_line(checkout.stderr)),
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
        if destination.is_dir():
            try:
                if next(destination.iterdir(), None) is not None:
                    return self._blocked("directory_sync_destination_not_empty")
            except OSError:
                return self._blocked("directory_sync_destination_not_inspectable")
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
        if not _wait_tcp_listening("127.0.0.1", expected_port, timeout=8.0):
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

    def _action_repair_klonet_active_master_ip(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        root = _absolute_path(step.args.get("project_root"))
        if root is None or _protected_target(root):
            return self._blocked("invalid_project_root")
        config = root / "vemu_uestc" / "vemu_config" / "config.py"
        if not config.is_file():
            return self._blocked("klonet_config_not_found")
        local_ip = str(step.args.get("local_ip") or "").strip() or _local_primary_ipv4()
        if not _safe_ipv4(local_ip):
            return self._blocked("local_ip_not_found")
        try:
            current = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self._blocked("klonet_config_unreadable")
        active = _active_config_class(current)
        if not active:
            return self._blocked("active_config_not_found")
        updated, previous = _replace_class_assignment(current, active, "master_ip", local_ip)
        if updated == current:
            return DirectActionResult(
                "completed",
                "action=repair_klonet_active_master_ip active=%s master_ip=%s already_current=true environment_changed=false" % (active, local_ip),
            )
        backup = config.with_name(config.name + ".bak.%s" % int(time.time()))
        try:
            shutil.copy2(config, backup)
            config.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return DirectActionResult(
                "failed",
                "config_update_failed path=%s error=%s environment_changed=unknown" % (config, _one_line(exc)),
                "inspect_config",
            )
        return DirectActionResult(
            "completed",
            "action=repair_klonet_active_master_ip active=%s previous=%s current=%s backup=%s environment_changed=true" % (active, previous or "unknown", local_ip, backup),
        )

    def _action_ensure_klonet_redis_instance(
        self,
        step: PrivilegedStep,
    ) -> DirectActionResult:
        root = _absolute_path(step.args.get("project_root"))
        if root is None or _protected_target(root):
            return self._blocked("invalid_project_root")
        config = root / "vemu_uestc" / "vemu_config" / "config.py"
        if not config.is_file():
            return self._blocked("klonet_config_not_found")
        settings = _klonet_redis_settings(config)
        if not settings:
            return self._blocked("klonet_redis_settings_not_found")
        port = int(settings["port"])
        password = str(settings.get("password") or "")
        if not 1 <= port <= 65535 or not password:
            return self._blocked("invalid_klonet_redis_settings")
        if _tcp_listening("127.0.0.1", port):
            return DirectActionResult(
                "completed",
                "action=ensure_klonet_redis_instance port=%s already_listening=true environment_changed=false" % port,
            )
        binary = _redis_server_binary()
        if not binary:
            return self._blocked("redis_server_not_found")
        runtime_dir = root / ".klonet_runtime"
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
        redis_config = runtime_dir / ("redis-%s.conf" % port)
        redis_config.write_text(
            "bind 0.0.0.0\nprotected-mode no\nport %s\nrequirepass %s\ndir %s\ndaemonize yes\npidfile %s\nlogfile %s\n"
            % (port, password, runtime_dir, runtime_dir / ("redis-%s.pid" % port), runtime_dir / ("redis-%s.log" % port)),
            encoding="utf-8",
        )
        redis_config.chmod(0o600)
        result = self._command([binary, str(redis_config)], cwd=runtime_dir, timeout=step.timeout)
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "redis_start_failed port=%s stderr=%s environment_changed=unknown" % (port, _one_line(result.stderr, 3000)),
                "inspect_redis",
            )
        if not _wait_tcp_listening("127.0.0.1", port, timeout=8.0):
            return DirectActionResult(
                "failed",
                "redis_postcondition_failed port=%s environment_changed=unknown" % port,
                "inspect_redis",
            )
        return DirectActionResult(
            "completed",
            "action=ensure_klonet_redis_instance port=%s config=%s environment_changed=true" % (port, redis_config),
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
        try:
            runtime_env = _runtime_python_env(root)
        except OSError as exc:
            return self._blocked(
                "runtime_python_alias_failed=%s" % exc.__class__.__name__
            )
        run_as_uid = _runtime_run_as_uid(step.args)
        if run_as_uid is None:
            return self._blocked("invalid_run_as_uid")
        command = _component_command(component, python, step.args)
        if not command:
            return self._blocked("component_command_missing")
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
            "web_terminal": [
                python,
                "-c",
                "from vemu_uestc.webserver.app_factory import create_web_terminal_app; "
                "from vemu_uestc.vemu_config.config import PROJ_CONFIG; "
                "from gevent import pywsgi; "
                "from geventwebsocket.handler import WebSocketHandler",
            ],
        }.get(component) or list(step.args.get("preflight_argv") or [])
        if not preflight:
            return self._blocked("component_preflight_missing")
        checked = self._command(
            _runtime_user_argv(preflight, run_as_uid, runtime_env),
            cwd=root,
            env=runtime_env,
            timeout=min(step.timeout, 30),
        )
        if checked.returncode != 0:
            return DirectActionResult(
                "failed",
                "startup_preflight_failed component=%s returncode=%s stderr=%s "
                "environment_changed=false"
                % (
                    component,
                    checked.returncode,
                    _one_line(checked.stderr or checked.stdout, 10000),
                ),
                "inspect_project_layout",
            )
        self._emit_command(command, cwd=root, execution="screen_foreground")
        control_rc = _runtime_screen_control_path(root, session)
        rc_content = _interactive_screen_rc(
            root=root,
            command=command,
            pythonpath=str(runtime_env.get("PYTHONPATH") or ""),
            component=component,
            session=session,
        )
        write_rc = self._command(
            _runtime_user_argv(
                [
                    python,
                    "-c",
                    (
                        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                        "p.parent.mkdir(parents=True,exist_ok=True,mode=0o700); "
                        "p.write_text(sys.argv[2],encoding='utf-8'); p.chmod(0o600)"
                    ),
                    str(control_rc),
                    rc_content,
                ],
                run_as_uid,
                runtime_env,
            ),
            cwd=root,
            env=runtime_env,
            timeout=min(step.timeout, 30),
        )
        if write_rc.returncode != 0:
            return DirectActionResult(
                "failed",
                "screen_control_rc_failed component=%s stderr=%s environment_changed=false"
                % (component, _one_line(write_rc.stderr)),
                "inspect_runtime",
            )
        result = self._command(
            _runtime_user_argv(
                [
                    "screen", "-dmS", session,
                    "bash", "--noprofile", "--rcfile", str(control_rc), "-i",
                ],
                run_as_uid,
                runtime_env,
            ),
            cwd=root,
            env=runtime_env,
            timeout=min(step.timeout, 30),
        )
        if result.returncode != 0:
            return DirectActionResult(
                "failed",
                "screen_start_failed component=%s stderr=%s environment_changed=unknown"
                % (component, _one_line(result.stderr)),
                "inspect_runtime",
            )
        component_port = component_port_arg(step.args, component)
        if component_port is not None:
            ready = _wait_tcp_listening(
                "127.0.0.1",
                component_port,
                timeout=min(float(step.timeout), 20.0),
            )
            if not ready:
                return DirectActionResult(
                    "failed",
                    "component_port_not_ready component=%s port=%s environment_changed=unknown"
                    % (component, component_port),
                    "inspect_runtime",
                )
        return DirectActionResult(
            "completed",
            "action=start_screen_component component=%s session=%s "
            "session_mode=interactive_foreground control_rc=%s environment_changed=true"
            % (component, session, control_rc),
        )

    def _screen_ls_text(self, run_as_uid: str = "") -> str:
        result = self._command(
            _runtime_user_argv(["screen", "-ls"], run_as_uid), timeout=15,
        )
        return "%s\n%s" % (result.stdout or "", result.stderr or "")

    def _screen_session_targets(
        self, session: str, run_as_uid: str = "",
    ) -> list[str]:
        targets = []
        pattern = re.compile(
            r"(?:^|\s)(\d+\.%s)(?:\s|$)" % re.escape(session)
        )
        screen_text = (
            self._screen_ls_text(run_as_uid)
            if run_as_uid else self._screen_ls_text()
        )
        for match in pattern.finditer(screen_text):
            target = match.group(1)
            if target not in targets:
                targets.append(target)
        if not targets and session in self._existing_screen_sessions(run_as_uid):
            targets.append(session)
        return targets

    def _existing_screen_sessions(self, run_as_uid: str = "") -> set[str]:
        text = (
            self._screen_ls_text(run_as_uid)
            if run_as_uid else self._screen_ls_text()
        )
        sessions = set()
        for match in re.finditer(
            r"(?:^|\s)(?:\d+\.)?([A-Za-z0-9_.:-]+)(?:\s|$)",
            text,
        ):
            value = match.group(1)
            if value not in {
                "There", "No", "Socket", "Sockets", "screens",
                "on", "in", "Detached", "Use", "to", "specify",
                "a", "session",
            }:
                sessions.add(value)
        return sessions

    def _dead_screen_session_targets(
        self, session: str, run_as_uid: str = "",
    ) -> list[str]:
        targets = []
        pattern = re.compile(
            r"(?:^|\s)(\d+\.%s)\s+\(Dead" % re.escape(session),
            re.I,
        )
        screen_text = (
            self._screen_ls_text(run_as_uid)
            if run_as_uid else self._screen_ls_text()
        )
        for match in pattern.finditer(screen_text):
            target = match.group(1)
            if target not in targets:
                targets.append(target)
        return targets

    def _clean_dead_screen_session(
        self, session: str, run_as_uid: str = "",
    ) -> tuple[list[str], str]:
        """Remove exact dead sockets and decide success from observed state.

        ``screen -S <dead> -X quit`` cannot succeed because no server process
        exists.  ``screen -wipe`` is the canonical cleanup operation.  Its
        return code is advisory: a concurrent cleanup can make it non-zero,
        so the authoritative result is whether the exact dead target remains.
        """

        dead_targets = self._dead_screen_session_targets(session, run_as_uid)
        if not dead_targets:
            return [], ""
        wipe_errors = []
        for target in dead_targets:
            result = self._command(
                _runtime_user_argv(["screen", "-wipe", target], run_as_uid),
                timeout=20,
            )
            if result.returncode != 0:
                wipe_errors.append("%s:%s" % (
                    target, _one_line(result.stderr or result.stdout),
                ))
        remaining = list(dead_targets)
        for _attempt in range(10):
            remaining = self._dead_screen_session_targets(session, run_as_uid)
            if not remaining:
                return dead_targets, ""
            time.sleep(0.1)
        return dead_targets, (
            "screen_dead_cleanup_incomplete session=%s remaining=%s stderr=%s "
            "environment_changed=unknown"
            % (
                session,
                ",".join(remaining),
                ";".join(wipe_errors) or "none",
            )
        )

    def _write_file(
        self,
        path: Path,
        content: str,
        timeout: int,
    ) -> DirectActionResult | None:
        parent = path.parent
        original_stat = path.stat() if path.exists() else None
        if parent.is_dir() and os.access(parent, os.W_OK):
            temp_path = parent / (".%s.klonet-agent.tmp" % path.name)
            temp_path.write_text(content, encoding="utf-8")
            if original_stat is not None:
                os.chmod(temp_path, stat.S_IMODE(original_stat.st_mode))
                try:
                    os.chown(temp_path, original_stat.st_uid, original_stat.st_gid)
                except PermissionError:
                    temp_path.unlink(missing_ok=True)
                    return self._blocked("cannot_preserve_file_ownership")
            os.replace(temp_path, path)
            return None
        temp_dir = Path(tempfile.mkdtemp(prefix="klonet-priv-write-"))
        try:
            staged = temp_dir / path.name
            staged.write_text(content, encoding="utf-8")
            install_args = ["install"]
            if original_stat is not None:
                install_args.extend([
                    "-o", str(original_stat.st_uid),
                    "-g", str(original_stat.st_gid),
                    "-m", "%04o" % stat.S_IMODE(original_stat.st_mode),
                ])
            else:
                install_args.extend(["-m", "0644"])
            command = _sudo_if_needed([*install_args, str(staged), str(path)])
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

    def _commit_text_candidate(
        self,
        path: Path,
        original: str | None,
        updated: str,
        timeout: int,
    ) -> tuple[DirectActionResult | None, Path | None, bool]:
        """Validate a candidate, retain a backup, then atomically publish it."""

        problem = _candidate_validation_problem(path, updated)
        if problem:
            return self._blocked(problem), None, False
        created = original is None
        backup = None
        try:
            if not created:
                backup = path.with_name(
                    "%s.klonet-agent.bak.%s" % (path.name, time.time_ns())
                )
                if os.access(path.parent, os.W_OK):
                    shutil.copy2(path, backup)
                else:
                    copied = self._command(
                        _sudo_if_needed(["cp", "-p", str(path), str(backup)]),
                        timeout=timeout,
                    )
                    if copied.returncode != 0:
                        return (
                            DirectActionResult(
                                "failed",
                                "candidate_backup_failed path=%s stderr=%s "
                                "environment_changed=false"
                                % (path, _one_line(copied.stderr)),
                                "inspect_path_permissions",
                            ),
                            None,
                            False,
                        )
            result = self._write_file(path, updated, timeout)
        except OSError as exc:
            return (
                DirectActionResult(
                    "failed",
                    "candidate_commit_failed path=%s error=%s "
                    "environment_changed=unknown"
                    % (path, exc.__class__.__name__),
                    "inspect_path_permissions",
                ),
                backup,
                created,
            )
        return result, backup, created

    def _command(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess:
        self._emit_command(argv, cwd=cwd)
        return self.command_runner(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=max(1, min(int(timeout), 3600)),
        )

    def _emit_command(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        execution: str = "subprocess",
    ) -> None:
        if self.on_command is None:
            return
        self.on_command({
            "action": self._active_action,
            "argv": list(argv),
            "cwd": str(cwd or ""),
            "execution": execution,
            "changes_state": _command_changes_state(self._active_action, argv),
        })

    def _run_command(
        self,
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
        if argv[:1] == ["sudo"]:
            auth_error = self._ensure_sudo_authenticated(
                timeout=max(1, min(int(timeout), 120)),
            )
            if auth_error:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr=auth_error,
                )
            argv = _noninteractive_sudo_argv(argv)
        return subprocess.run(
            argv,
            capture_output=True,
            **options,
        )

    def _ensure_sudo_authenticated(self, *, timeout: int) -> str:
        """Validate sudo once per atomic Action and never capture a password."""

        if self._sudo_auth_state == "ready":
            return ""
        if self._sudo_auth_state == "failed":
            return self._sudo_auth_error or "sudo_authentication_failed"

        validation_options = {
            "timeout": timeout,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        }
        try:
            cached = subprocess.run(
                ["sudo", "-n", "-v"],
                capture_output=True,
                **validation_options,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._sudo_auth_state = "failed"
            self._sudo_auth_error = "sudo_authentication_unavailable=%s" % (
                exc.__class__.__name__,
            )
            return self._sudo_auth_error
        if cached.returncode == 0:
            self._sudo_auth_state = "ready"
            return ""
        if not sys.stdin.isatty():
            self._sudo_auth_state = "failed"
            self._sudo_auth_error = "sudo_authentication_required_no_tty"
            return self._sudo_auth_error

        # This is the sole interactive sudo call for the Action.  stdin and
        # stderr stay attached to the user's terminal; the password never
        # enters argv, captured evidence, prompts, plans, or memory.
        try:
            authenticated = subprocess.run(
                ["sudo", "-v"],
                stdout=subprocess.DEVNULL,
                stderr=None,
                **validation_options,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._sudo_auth_state = "failed"
            self._sudo_auth_error = "sudo_authentication_unavailable=%s" % (
                exc.__class__.__name__,
            )
            return self._sudo_auth_error
        if authenticated.returncode != 0:
            self._sudo_auth_state = "failed"
            self._sudo_auth_error = "sudo_authentication_failed"
            return self._sudo_auth_error
        self._sudo_auth_state = "ready"
        return ""

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


def _candidate_validation_problem(path: Path, content: str) -> str:
    """Return a deterministic compiler error for a staged text mutation."""

    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return "candidate_json_invalid=%s" % exc.__class__.__name__
        return ""
    if suffix != ".py":
        return ""
    try:
        tree = ast.parse(content, filename=str(path))
        compile(tree, str(path), "exec")
    except (SyntaxError, ValueError, TypeError) as exc:
        return (
            "candidate_python_compile_failed=%s "
            "text_edit_result_invalid_%s"
            % (exc.__class__.__name__, exc.__class__.__name__)
        )

    definitions: dict[str, int] = {}
    duplicates: set[str] = set()
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    for index, node in enumerate(classes):
        if node.name in definitions:
            duplicates.add(node.name)
        definitions.setdefault(node.name, index)
    if duplicates:
        return "candidate_python_duplicate_class=%s" % ",".join(
            sorted(duplicates)
        )
    for index, node in enumerate(classes):
        for base in node.bases:
            if (
                isinstance(base, ast.Name)
                and base.id in definitions
                and definitions[base.id] >= index
            ):
                return "candidate_python_base_defined_after_subclass=%s:%s" % (
                    node.name,
                    base.id,
                )
    return ""


def _mutation_metadata(
    path: Path,
    backup: Path | None,
    created: bool,
) -> dict[str, str]:
    return {
        "kind": "text_file",
        "path": str(path),
        "backup": str(backup or ""),
        "created": "true" if created else "false",
        "state": "applied_unverified",
    }


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
    if root.name == "mains":
        return platform, root, "runtime_root_must_be_instance_root"
    if all((root / name).is_file() for name in REQUIRED_ENTRY_FILES):
        return platform, root, ""
    backend = root / platform
    if (backend / "vemu_config").is_dir():
        return platform, root, "runtime_entries_not_prepared"
    if any(
        all((candidate / name).is_file() for name in REQUIRED_ENTRY_FILES)
        for candidate in (root / "mains", root / "vemu_uestc" / "mains")
    ):
        return platform, root, "runtime_entries_not_prepared"
    return platform, root, "runtime_entry_sources_missing"


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
    if requested:
        requested_path = Path(requested)
        if requested_path.is_file() and os.access(requested, os.X_OK):
            # A frozen predecessor interpreter is part of the confirmed
            # runtime identity.  Its module compatibility is verified by the
            # role-specific preflight below; silently substituting another
            # environment would violate the plan.
            return str(requested_path.resolve())
    candidates = _python_candidates(requested)
    required = ("gunicorn", "celery", "flask")
    for raw in candidates:
        if _python_has_modules(raw, required):
            return str(Path(raw).resolve())
    for raw in candidates:
        if raw and Path(raw).is_file() and os.access(raw, os.X_OK):
            return str(Path(raw).resolve())
    return ""


def _runtime_run_as_uid(args: dict) -> str | None:
    raw = str(args.get("run_as_uid") or "").strip()
    if not raw:
        return ""
    if not re.fullmatch(r"[1-9]\d{0,9}", raw):
        return None
    return raw


def _runtime_user_argv(
    argv: list[str],
    run_as_uid: str,
    runtime_env: dict[str, str] | None = None,
) -> list[str]:
    if not run_as_uid or int(run_as_uid) == os.geteuid():
        return list(argv)
    result = ["sudo", "-n", "-u", "#%s" % run_as_uid]
    pythonpath = str((runtime_env or {}).get("PYTHONPATH") or "").strip()
    if pythonpath:
        result.extend(["env", "PYTHONPATH=%s" % pythonpath])
    result.extend(argv)
    return result


def _python_candidates(requested: str) -> list[str]:
    candidates: list[str] = []
    for raw in (requested, os.environ.get("KLONET_PYTHON", "")):
        if raw and raw not in candidates:
            candidates.append(raw)
    home = Path.home()
    for env_root in (home / "miniconda3" / "envs", home / "anaconda3" / "envs"):
        if env_root.is_dir():
            for env in sorted(env_root.iterdir()):
                for name in ("python3.8", "python"):
                    candidate = str(env / "bin" / name)
                    if candidate not in candidates:
                        candidates.append(candidate)
    for raw in ("/usr/bin/python3.8", "/usr/local/python3/bin/python3.8", "/usr/local/bin/python3.8", "/usr/bin/python3"):
        if raw not in candidates:
            candidates.append(raw)
    return candidates


def _python_has_modules(raw: str, modules: tuple[str, ...]) -> bool:
    if not raw:
        return False
    path = Path(raw)
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    code = "import importlib.util; mods=%r; raise SystemExit(0 if all(importlib.util.find_spec(m) for m in mods) else 1)" % (modules,)
    try:
        result = subprocess.run([str(path), "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0

def _runtime_pythonpath(root: Path) -> str:
    instance_root = root.parent if root.name == "mains" else root
    package_name = _expected_runtime_package(root, instance_root)
    if package_name and package_name != instance_root.name:
        digest = hashlib.sha256(str(instance_root.resolve()).encode("utf-8")).hexdigest()[:16]
        alias_root = Path(tempfile.gettempdir()) / "klonet-runtime-aliases" / digest
        alias_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        # The Action may deliberately switch to the frozen predecessor UID.
        # This directory contains only a package-name symlink, not secrets;
        # it must be traversable by that runtime account.
        alias_root.chmod(0o755)
        alias = alias_root / package_name
        if alias.is_symlink():
            if alias.resolve() != instance_root.resolve():
                raise OSError("runtime package alias points to another instance")
        elif alias.exists():
            raise OSError("runtime package alias is not a symlink")
        else:
            alias.symlink_to(instance_root.resolve(), target_is_directory=True)
        return str(alias_root)
    if root.name == "mains":
        return str(instance_root.parent.resolve())
    if (root / "vemu_config").is_dir():
        return str(root.parent.resolve())
    return str(root.resolve())


def _expected_runtime_package(root: Path, instance_root: Path) -> str:
    for name in ("gun.py", "master_main.py", "celery_worker.py", "worker_gun.py"):
        path = root / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(
            r"(?:from|import)\s+([A-Za-z_]\w*)\.([A-Za-z_]\w*)",
            content,
        ):
            package_name, child = match.groups()
            if (instance_root / child).exists():
                return package_name
    return ""


def _runtime_python_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    runtime_path = _runtime_pythonpath(root)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        runtime_path if not existing else runtime_path + os.pathsep + existing
    )
    return env


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
        "web_terminal": [
            python,
            "-c",
            "from vemu_uestc.webserver.app_factory import create_web_terminal_app; "
            "from vemu_uestc.vemu_config.config import PROJ_CONFIG; "
            "from gevent import pywsgi; "
            "from geventwebsocket.handler import WebSocketHandler; "
            "app=create_web_terminal_app(); "
            "server=pywsgi.WSGIServer(('0.0.0.0', int(PROJ_CONFIG.web_terminal_port)), "
            "app, handler_class=WebSocketHandler); "
            "print('Started!'); server.serve_forever()",
        ],
        "worker": [
            python, "-m", "gunicorn", "-c", "worker_gun.py",
            "worker_main:flask_app",
        ],
    }


def _component_suffix(component: str, args: dict[str, Any]) -> str:
    if component in _COMPONENT_SUFFIX:
        return _COMPONENT_SUFFIX[component]
    suffix = str(args.get("screen_suffix") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", suffix):
        return ""
    return suffix


def _component_command(
    component: str,
    python: str,
    args: dict[str, Any],
) -> list[str]:
    known = _component_commands(python).get(component)
    if known:
        return known
    command = args.get("command_argv")
    if not isinstance(command, list):
        return []
    return [str(item) for item in command]


def _command_changes_state(action: str, argv: list[str]) -> bool:
    """Classify subprocess events for concise user-facing execution output."""

    words = [str(item) for item in argv]
    if not words:
        return False
    unwrapped = list(words)
    if unwrapped[:1] == ["sudo"]:
        try:
            marker = next(
                index for index, value in enumerate(unwrapped)
                if index and not value.startswith("-") and not value.startswith("#")
                and value not in {"env"}
            )
            unwrapped = unwrapped[marker:]
        except StopIteration:
            return True
    program = Path(unwrapped[0]).name
    joined = " ".join(unwrapped)
    if program in {"ss", "ps", "pgrep", "curl", "test", "stat", "readlink"}:
        return False
    if program == "screen" and any(flag in unwrapped for flag in ("-ls", "-Q")):
        return False
    if "--check-config" in unwrapped:
        return False
    if program.startswith("python") and "-c" in unwrapped:
        return bool(re.search(
            r"write_text|mkdir|chmod|unlink|replace|symlink_to", joined,
        ))
    if action in {
        "start_screen_component", "restart_screen_component",
        "stop_screen_component", "stop_platform_screens",
        "start_platform_screens", "stop_klonet_component",
        "stop_klonet_runtime_instance",
    }:
        return program == "screen" or "gunicorn" in joined or "celery" in joined
    return True


def _runtime_screen_control_path(root: Path, session: str) -> Path:
    identity = hashlib.sha256(
        (str(root.resolve()) + "\0" + str(session)).encode("utf-8")
    ).hexdigest()[:16]
    return (
        Path(tempfile.gettempdir()) / "klonet-runtime-controls"
        / identity / (session + ".bashrc")
    )


def _interactive_screen_rc(
    *,
    root: Path,
    command: list[str],
    pythonpath: str,
    component: str,
    session: str,
) -> str:
    """Build a bounded interactive shell contract for one Screen component."""

    command_text = shlex.join(command)
    return "\n".join([
        "# Generated by Klonet Agent; runtime control only.",
        "cd %s || return" % shlex.quote(str(root)),
        "export PYTHONPATH=%s" % shlex.quote(pythonpath),
        "klonet_command() { printf '%s\\n' %s; }" % (
            "%s", shlex.quote(command_text),
        ),
        "klonet_status() {",
        "  printf 'session=%s component=%s cwd=%%s\\n' \"$PWD\"" % (
            session, component,
        ),
        "  jobs -l",
        "}",
        "klonet_start() {",
        "  printf '启动 %s；Ctrl-C 仅停止服务并返回本 shell。\\n'" % component,
        "  %s" % command_text,
        "}",
        "printf 'Klonet %s 控制台：Ctrl-A D 脱离；Ctrl-C 停止；klonet_start 重新启动。\\n'" % component,
        "klonet_start",
        "",
    ])


def _runtime_config_path(root: Path) -> Path | None:
    candidates = []
    if root.name == "mains":
        candidates.append(root.parent / "vemu_config" / "config.py")
    candidates.append(root / "vemu_config" / "config.py")
    candidates.append(root / "vemu_uestc" / "vemu_config" / "config.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _configured_runtime_ports(root: Path) -> list[int]:
    config = _runtime_config_path(root)
    if config is None or not config.is_file():
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
    values: dict[str, int] = {}
    for match in re.finditer(
        r"\b(master_port|worker_port|web_terminal_port)\s*=\s*['\"]?(\d{1,5})",
        content,
    ):
        port = int(match.group(2))
        if 1 <= port <= 65535:
            values[match.group(1)] = port
    web_port = _web_terminal_entry_port(root) or values.get("web_terminal_port")
    ports = []
    for port in (values.get("master_port"), values.get("worker_port"), web_port):
        if port and port not in ports:
            ports.append(port)
    return ports


def _web_terminal_entry_port(root: Path) -> int | None:
    candidates = []
    if root.name == "mains":
        candidates.append(root / "web_terminal_main.py")
    candidates.append(root / "mains" / "web_terminal_main.py")
    candidates.append(root / "vemu_uestc" / "mains" / "web_terminal_main.py")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        match = re.search(r"WSGIServer\s*\(\s*\([^)]*?,\s*(\d{1,5})\s*\)", content, flags=re.DOTALL)
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                return port
    return None


def _listening_ports(text: str, configured: list[int]) -> list[int]:
    return [
        port for port in configured
        if re.search(rf":{port}\b", text)
    ]


def _wait_screen_sessions_absent(
    runner: DirectPrivilegedActionRunner,
    sessions: list[str],
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        existing = runner._existing_screen_sessions()
        if all(session not in existing for session in sessions):
            return True
        time.sleep(0.3)
    existing = runner._existing_screen_sessions()
    return all(session not in existing for session in sessions)


def _wait_platform_runtime_ready(
    runner: DirectPrivilegedActionRunner,
    root: Path,
    sessions: Iterable[str],
    ports: list[int],
    *,
    timeout: float,
) -> str:
    deadline = time.monotonic() + timeout
    session_list = list(sessions)
    while time.monotonic() < deadline:
        missing = [
            session for session in session_list
            if session not in runner._existing_screen_sessions()
        ]
        wrong_ports = _runtime_ports_with_wrong_cwd(ports, root)
        if not missing and not wrong_ports:
            return ""
        time.sleep(0.5)
    missing = [
        session for session in session_list
        if session not in runner._existing_screen_sessions()
    ]
    wrong_ports = _runtime_ports_with_wrong_cwd(ports, root)
    reasons = []
    if missing:
        reasons.append("missing_screen_sessions=%s" % ",".join(missing))
    if wrong_ports:
        reasons.append("wrong_port_owners=%s" % ",".join(wrong_ports))
    return ";".join(reasons)


def _stop_target_runtime_after_screen_stop(
    runner: DirectPrivilegedActionRunner,
    root: Path,
    ports: list[int],
    *,
    timeout: int,
) -> DirectActionResult:
    backend = root.parent if root.name == "mains" else root
    if not (backend / "vemu_config").is_dir():
        return DirectActionResult("completed", "target_runtime_stop_skipped=true")
    if not _runtime_ports_owned_by_allowed_cwd(ports, root):
        return DirectActionResult("completed", "target_runtime_already_stopped=true")
    step = PrivilegedStep(
        step_id="stop-target-runtime-after-screen-stop",
        title="Stop target runtime after screen stop",
        command="",
        action="stop_klonet_runtime_instance",
        args={"runtime_cwd": str(backend), "ports": ports},
        risk="high",
        approval_scope="plan",
        timeout=timeout,
    )
    return runner._action_stop_klonet_runtime_instance(step)


def _wait_allowed_runtime_ports_released(
    root: Path,
    ports: list[int],
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _runtime_ports_owned_by_allowed_cwd(ports, root):
            return True
        time.sleep(0.5)
    return not _runtime_ports_owned_by_allowed_cwd(ports, root)


def _runtime_ports_owned_by_allowed_cwd(ports: list[int], root: Path) -> list[int]:
    allowed = _allowed_runtime_cwds(root)
    owned = []
    for port in ports:
        for pid in _listener_pids_for_port(port):
            cwd = _proc_cwd(pid)
            if cwd and any(_path_is_relative_to(Path(cwd), base) for base in allowed):
                owned.append(port)
                break
    return owned


def _cleanup_stale_runtime_owners(
    runner: DirectPrivilegedActionRunner,
    root: Path,
    ports: list[int],
    *,
    timeout: int,
) -> DirectActionResult:
    cwds = _stale_runtime_cwds_for_ports(ports, root)
    cleaned = []
    for cwd in cwds:
        step = PrivilegedStep(
            step_id="cleanup-stale-runtime",
            title="Cleanup stale runtime",
            command="",
            action="stop_klonet_runtime_instance",
            args={"runtime_cwd": cwd, "ports": ports},
            risk="high",
            approval_scope="plan",
            timeout=timeout,
        )
        result = runner._action_stop_klonet_runtime_instance(step)
        if result.status != "completed":
            return DirectActionResult(
                "failed",
                "stale_runtime_cleanup_failed cwd=%s reason=%s environment_changed=unknown"
                % (cwd, result.output),
                "inspect_runtime",
            )
        cleaned.append(cwd)
    return DirectActionResult(
        "completed",
        "cleaned_stale=%s" % (",".join(cleaned) or "none"),
    )


def _stale_runtime_cwds_for_ports(ports: list[int], root: Path) -> list[str]:
    allowed = _allowed_runtime_cwds(root)
    cwds = []
    for port in ports:
        for pid in _listener_pids_for_port(port):
            cwd = _proc_cwd(pid)
            if not cwd:
                continue
            path = Path(cwd)
            if any(_path_is_relative_to(path, allowed_path) for allowed_path in allowed):
                continue
            if path.name != "vemu_uestc" or not (path / "vemu_config").is_dir():
                continue
            value = str(path)
            if value not in cwds:
                cwds.append(value)
    return cwds


def _runtime_ports_with_wrong_cwd(ports: list[int], root: Path) -> list[str]:
    wrong = []
    allowed = _allowed_runtime_cwds(root)
    for port in ports:
        pids = _listener_pids_for_port(port)
        if not pids:
            wrong.append("%s:not_listening" % port)
            continue
        cwd_values = []
        for pid in pids:
            cwd = _proc_cwd(pid)
            if cwd:
                cwd_values.append(cwd)
        if not cwd_values or not any(
            _path_is_relative_to(Path(cwd), allowed_path)
            for cwd in cwd_values
            for allowed_path in allowed
        ):
            wrong.append("%s:%s" % (port, ",".join(cwd_values) or "unknown"))
    return wrong


def _allowed_runtime_cwds(root: Path) -> list[Path]:
    allowed = [root.resolve()]
    if root.name == "mains":
        allowed.append(root.parent.resolve())
    elif (root / "mains").is_dir():
        allowed.append((root / "mains").resolve())
    return allowed


def _listener_pids_for_port(port: int) -> list[int]:
    if not 1 <= int(port) <= 65535:
        return []
    ss = shutil.which("ss")
    if not ss:
        return []
    commands = ([ss, "-ltnp"], ["sudo", "-n", ss, "-ltnp"])
    for argv in commands:
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        pids = []
        for line in (result.stdout or "").splitlines():
            if not re.search(rf":{port}\b", line):
                continue
            for match in re.finditer(r"pid=(\d+)", line):
                pid = int(match.group(1))
                if pid not in pids:
                    pids.append(pid)
        if pids:
            return pids
    return []


def _component_pids(root: Path, component: str) -> list[int]:
    patterns = {
        "master": ("master_main", "gun.py"),
        "worker": ("worker_main", "worker_gun.py"),
        "celery": ("celery_worker", " celery "),
        "web_terminal": ("web_terminal_main", "create_web_terminal_app"),
    }.get(component, (component,))
    allowed = _allowed_runtime_cwds(root)
    result = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cwd = _proc_cwd(pid)
        if not cwd or not any(
            _path_is_relative_to(Path(cwd), candidate) for candidate in allowed
        ):
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace",
            )
        except OSError:
            continue
        if any(pattern.lower() in command.lower() for pattern in patterns):
            result.append(pid)
    return sorted(result)


def _screen_logical_name(target: str) -> str:
    value = str(target or "").strip()
    return value.split(".", 1)[1] if value.partition(".")[0].isdigit() else value


def _screen_owner_targets_for_pids(pids: list[int]) -> tuple[list[str], bool]:
    """Return Screen socket targets proven to own any supplied runtime PID.

    Ownership is derived only from the live /proc parent chain.  The boolean
    reports whether at least one complete chain was observable; callers must
    not interpret an unreadable chain as proof that a Screen does not own the
    process.
    """

    owners: list[str] = []
    observed = False
    for raw_pid in pids:
        try:
            current = int(raw_pid)
        except (TypeError, ValueError):
            continue
        visited: set[int] = set()
        chain_observed = False
        for _depth in range(32):
            if current <= 1 or current in visited:
                break
            visited.add(current)
            proc = Path("/proc") / str(current)
            try:
                command = proc.joinpath("cmdline").read_bytes().replace(
                    b"\x00", b" ",
                ).decode("utf-8", errors="replace").strip()
                stat = proc.joinpath("stat").read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                break
            chain_observed = True
            match = re.search(r"(?:^|\s)-dmS\s+(\S+)", command)
            if (
                command.lstrip().startswith("SCREEN ")
                and match is not None
                and _safe_token(match.group(1))
            ):
                target = "%s.%s" % (current, match.group(1))
                if target not in owners:
                    owners.append(target)
                break
            closing = stat.rfind(")")
            tail = stat[closing + 1:].strip().split() if closing >= 0 else []
            if len(tail) < 2 or not tail[1].isdigit():
                break
            current = int(tail[1])
        observed = observed or chain_observed
    return owners, observed


def _wait_component_new_pids(
    root: Path,
    component: str,
    old_pids: set[int],
    *,
    timeout: float,
) -> list[int]:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        current = _component_pids(root, component)
        if current and old_pids.isdisjoint(current):
            return current
        time.sleep(0.2)
    return _component_pids(root, component)


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


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


def _valid_container_port_binding(value: str) -> bool:
    match = re.fullmatch(r"127\.0\.0\.1:([1-9]\d{0,4}):([1-9]\d{0,4})", value)
    if match is None:
        return False
    return all(1 <= int(port) <= 65535 for port in match.groups())


def _resolve_klonet_container_credentials(
    source: Any,
    *,
    name: str,
    image: str,
) -> tuple[dict[str, list[str]], str]:
    """Resolve an allowlisted subset of cloned config secrets without model exposure."""

    if not isinstance(source, dict):
        return {}, "invalid_container_credential_source"
    path = _absolute_path(source.get("path"))
    service = str(source.get("service") or "").strip().lower()
    try:
        valid_path = bool(
            path is not None
            and path.is_file()
            and path.name == "config.py"
            and path.stat().st_size <= 1_000_000
        )
    except OSError:
        valid_path = False
    if (
        not valid_path
        or service not in {"mysql", "redis"}
        or service not in ("%s %s" % (name, image)).lower()
    ):
        return {}, "invalid_container_credential_source"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return {}, "container_credential_source_not_parseable"
    classes: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        values = {}
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if not isinstance(target, ast.Name):
                continue
            try:
                values[target.id] = ast.literal_eval(statement.value)
            except (ValueError, TypeError):
                continue
        classes[node.name] = values
    if service == "mysql":
        values = classes.get("MysqlConfig", {})
        password = values.get("mysql_password")
        database = values.get("mysql_database")
        if not (
            isinstance(password, str)
            and 1 <= len(password) <= 128
            and not any(char in password for char in "\x00\n\r")
            and isinstance(database, str)
            and re.fullmatch(r"[A-Za-z0-9_]{1,64}", database)
        ):
            return {}, "mysql_container_credentials_unavailable"
        return {
            "environment": [
                "MYSQL_ROOT_PASSWORD=%s" % password,
                "MYSQL_DATABASE=%s" % database,
            ],
            "command": [],
        }, ""
    values = {
        **classes.get("RedisConfig", {}),
        **classes.get("WtxConfig", {}),
    }
    password = values.get("redis_password")
    if not (
        isinstance(password, str)
        and 8 <= len(password) <= 128
        and not any(char.isspace() for char in password)
        and not any(char in password for char in "\x00\n\r")
    ):
        return {}, "redis_container_credentials_unavailable"
    return {
        "environment": [],
        "command": ["redis-server", "--requirepass", password],
    }, ""


def _valid_container_environment(value: str) -> bool:
    key, separator, content = value.partition("=")
    return bool(
        separator
        and _SAFE_ENV_KEY.fullmatch(key)
        and len(content) <= 2048
        and not any(character in content for character in ("\x00", "\n", "\r"))
    )


def _valid_container_command(name: str, image: str, command: list[str]) -> bool:
    """Allow only the one service override required by the typed Redis contract."""

    if not ("redis" in name.lower() and "redis" in image.lower()):
        return False
    if len(command) != 3 or command[:2] != ["redis-server", "--requirepass"]:
        return False
    password = command[2]
    return bool(
        8 <= len(password) <= 128
        and not any(character.isspace() for character in password)
        and not any(character in password for character in ("\x00", "\n", "\r"))
    )


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


def _port_arg_list(value) -> list[int]:
    items = value if isinstance(value, list) else []
    result: list[int] = []
    for item in items[:20]:
        try:
            port = int(item)
        except (TypeError, ValueError):
            return []
        if not 1 <= port <= 65535:
            return []
        if port not in result:
            result.append(port)
    return result


def _klonet_runtime_processes(
    runtime_cwd: Path,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
    expected_pid: int | None = None,
    allow_command_root_identity: bool = False,
) -> list[dict[str, int | str]]:
    processes: list[dict[str, int | str]] = []
    proc_root = Path("/proc")
    allowed_cwds = {str(runtime_cwd)}
    mains = runtime_cwd / "mains"
    if mains.is_dir():
        allowed_cwds.add(str(mains.resolve()))
    expected_pgid = None
    if expected_pid is not None:
        if expected_pid <= 1:
            return []
        expected_pgid = _proc_pgid(expected_pid)
        if expected_pgid is None or expected_pgid <= 1:
            return []
    for item in (proc_root.iterdir() if proc_root.is_dir() else []):
        if not item.name.isdigit():
            continue
        pid = int(item.name)
        pgid = _proc_pgid(pid)
        if pgid is None or pgid <= 1:
            continue
        # A frozen component action owns one PID/PGID.  Filter by that
        # identity before reading cwd so unrelated cross-user runtimes cannot
        # trigger sudo or enter this Action's authority boundary.
        if expected_pgid is not None and pgid != expected_pgid:
            continue
        cmdline = _proc_cmdline(pid)
        if not _is_klonet_runtime_command(cmdline):
            continue
        cwd = _proc_cwd(pid, command_runner=command_runner)
        if cwd not in allowed_cwds and not (
            allow_command_root_identity
            and process_belongs_to_project_root(
                cwd=cwd,
                cmdline=cmdline,
                project_root=runtime_cwd,
            )
        ):
            continue
        processes.append({"pid": pid, "pgid": pgid, "cmdline": cmdline, "cwd": cwd})
    return processes


def _normalized_klonet_runtime_cwd(runtime_cwd: Path) -> Path | None:
    """Accept both package roots and instance roots with complete mains entries."""

    root = runtime_cwd.resolve()
    if root.name == "mains" and (
        (root.parent / "vemu_config").is_dir()
        or all((root / name).is_file() for name in REQUIRED_ENTRY_FILES)
    ):
        root = root.parent.resolve()
    if root.name == "vemu_uestc" and (root / "vemu_config").is_dir():
        return root
    mains = root / "mains"
    if mains.is_dir() and all((mains / name).is_file() for name in REQUIRED_ENTRY_FILES):
        return root
    return None


def _runtime_process_groups(processes: list[dict[str, int | str]]) -> list[dict[str, int]]:
    groups: dict[int, int] = {}
    for proc in processes:
        pgid = int(proc["pgid"])
        pid = int(proc["pid"])
        current = groups.get(pgid)
        if current is None or pid == pgid or pid < current:
            groups[pgid] = pid
    return [{"pgid": pgid, "owner_pid": owner_pid} for pgid, owner_pid in groups.items()]


def _runtime_stopped_stably(
    runner: DirectPrivilegedActionRunner,
    runtime_cwd: Path,
    ports: list[int],
    *,
    timeout: float,
    stable_for: float,
) -> bool:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        stopped = not _klonet_runtime_processes(
            runtime_cwd, command_runner=runner._command,
        )
        target_ports_released = not _runtime_ports_owned_by_allowed_cwd(
            ports, runtime_cwd
        )
        if stopped and target_ports_released:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= stable_for:
                return True
        else:
            stable_since = None
        time.sleep(0.2)
    return False


def _positive_float(value, *, default: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, maximum)


def _ports_currently_listening(runner: DirectPrivilegedActionRunner, ports: list[int]) -> list[int]:
    ss = shutil.which("ss")
    if not ss:
        return []
    result = runner._command([ss, "-ltn"], timeout=20)
    return _listening_ports("%s\n%s" % (result.stdout or "", result.stderr or ""), ports)


def _proc_cwd(
    pid: int,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str:
    try:
        resolved = str((Path("/proc") / str(pid) / "cwd").resolve())
        if resolved and resolved != str(Path("/proc") / str(pid) / "cwd"):
            return resolved
    except OSError:
        pass
    if not 1 < int(pid) <= 4_194_304:
        return ""
    argv = ["readlink", "-f", "/proc/%s/cwd" % int(pid)]
    try:
        if command_runner is not None:
            # Registered Actions already have a reviewable privilege boundary.
            # Reuse its TTY-aware sudo path so cross-user identity checks can
            # authenticate without placing a password in prompts, argv, or
            # captured evidence.  Read-only Discovery callers deliberately do
            # not pass this runner and retain the non-interactive probe path.
            completed = command_runner(_sudo_if_needed(argv), timeout=5)
        else:
            completed = subprocess.run(
                ["sudo", "-n", *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) != 1 or not lines[0].startswith("/"):
        return ""
    return lines[0]


def _is_klonet_runtime_command(cmdline: str) -> bool:
    text = cmdline or ""
    if "gunicorn" in text and any(
        marker in text
        for marker in (
            "master_main:flask_app",
            "worker_main:flask_app",
            "data_server_main:flask_app",
        )
    ):
        return True
    return any(
        marker in text
        for marker in (
            "web_terminal_main.py",
            "celery_worker.celery",
        )
    )


def _klonet_component_for_command(cmdline: str) -> str:
    text = cmdline or ""
    if "master_main:flask_app" in text:
        return "master"
    if "worker_main:flask_app" in text:
        return "worker"
    if "data_server_main:flask_app" in text:
        return "data_server"
    if "web_terminal_main.py" in text:
        return "web_terminal"
    if "celery_worker.celery" in text:
        return "celery"
    return ""


def _proc_uid(pid: int) -> int | None:
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                return int(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError):
        return None
    return None


def _proc_pgid(pid: int) -> int | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
        after_comm = stat.rsplit(")", 1)[1].split()
        return int(after_comm[2]) if len(after_comm) > 2 else None
    except (OSError, ValueError, IndexError):
        return None


def _process_group_id(raw_pgid, pid: int) -> int | None:
    try:
        pgid = int(raw_pgid) if raw_pgid not in (None, "") else _proc_pgid(pid)
    except (TypeError, ValueError):
        return None
    return pgid if pgid and pgid > 1 else None


def _kill_argv_for_owner_pid(pid: int, signal_name: str, target: str) -> list[str]:
    argv = ["kill", "-%s" % signal_name, target]
    if hasattr(os, "geteuid"):
        euid = os.geteuid()
        if euid == 0 or _proc_uid(pid) == euid:
            return argv
    return _sudo_if_needed(argv)


def _kill_argv_for_pid(pid: int, signal_name: str) -> list[str]:
    return _kill_argv_for_owner_pid(pid, signal_name, str(pid))


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
    if operation in {"clone", "clone_at_revision"}:
        url = str(args.get("url") or "").strip()
        repository = _absolute_path(args.get("repository"))
        if not url or repository is None:
            return []
        if operation == "clone_at_revision":
            revision = str(args.get("revision") or "").strip()
            if not ref or not revision:
                return []
            return [
                "clone",
                "--branch",
                ref,
                "--single-branch",
                url,
                repository.name,
            ]
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


def _local_primary_ipv4() -> str:
    try:
        result = subprocess.run(["hostname", "-I"], text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for item in (result.stdout or "").split():
        if _safe_ipv4(item) and not item.startswith(("127.", "172.", "10.")):
            return item
    for item in (result.stdout or "").split():
        if _safe_ipv4(item) and not item.startswith("127."):
            return item
    return ""


def _safe_ipv4(value: str) -> bool:
    parts = str(value or "").split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 and str(int(part)) == part for part in parts)
    except ValueError:
        return False


def _active_config_class(content: str) -> str:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PROJ_CONFIG" and isinstance(node.value, ast.Call):
                    func = node.value.func
                    if isinstance(func, ast.Name):
                        return func.id
    return ""


def _replace_class_assignment(content: str, class_name: str, field: str, value: str) -> tuple[str, str]:
    class_match = re.search(r"(?m)^class\s+%s\b.*?:\s*$" % re.escape(class_name), content)
    if not class_match:
        return content, ""
    next_match = re.search(r"(?m)^class\s+[A-Za-z_][A-Za-z0-9_]*\b.*?:\s*$", content[class_match.end():])
    end = class_match.end() + next_match.start() if next_match else len(content)
    block = content[class_match.end():end]
    pattern = re.compile(r'''(?m)^(?P<indent>\s*)%s\s*=\s*(?P<quote>['"])(?P<old>.*?)(?P=quote)(?P<tail>\s*(?:#.*)?$)''' % re.escape(field))
    match = pattern.search(block)
    if not match:
        return content, ""
    replacement = "%s%s = '%s'%s" % (match.group('indent'), field, value, match.group('tail'))
    new_block = block[:match.start()] + replacement + block[match.end():]
    return content[:class_match.end()] + new_block + content[end:], match.group('old')


def _redis_server_binary() -> str:
    for raw in ("/usr/local/bin/redis-server", "/usr/bin/redis-server", "redis-server"):
        found = shutil.which(raw) if raw == "redis-server" else raw
        if found and Path(found).is_file() and os.access(found, os.X_OK):
            return str(Path(found).resolve())
    return ""


def _klonet_redis_settings(config: Path) -> dict[str, str | int]:
    try:
        tree = ast.parse(config.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return {}
    classes: dict[str, dict[str, object]] = {}
    active = ""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PROJ_CONFIG" and isinstance(node.value, ast.Call):
                    func = node.value.func
                    if isinstance(func, ast.Name):
                        active = func.id
        if isinstance(node, ast.ClassDef):
            values = {}
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            try:
                                values[target.id] = ast.literal_eval(stmt.value)
                            except Exception:
                                pass
            classes[node.name] = values
    merged: dict[str, object] = {}
    for name in ("RedisConfig", active):
        merged.update(classes.get(name, {}))
    try:
        port = int(merged.get("redis_port"))
    except (TypeError, ValueError):
        return {}
    password = str(merged.get("redis_password") or "")
    return {"port": port, "password": password}


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


def _wait_tcp_listening(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_listening(host, port, timeout=0.5):
            return True
        time.sleep(0.2)
    return _tcp_listening(host, port, timeout=0.5)


def _wait_tcp_released(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _tcp_listening(host, port, timeout=0.5):
            return True
        time.sleep(0.2)
    return not _tcp_listening(host, port, timeout=0.5)


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
    if (
        argv
        and Path(str(argv[0])).name == "docker"
        and (
            bool(str(os.environ.get("DOCKER_HOST") or "").strip())
            or os.access("/var/run/docker.sock", os.R_OK | os.W_OK)
        )
    ):
        return argv
    return ["sudo", *argv]


def _noninteractive_sudo_argv(argv: list[str]) -> list[str]:
    """Make an already-authenticated sudo command unable to prompt again."""

    if argv[:1] != ["sudo"] or "-n" in argv[1:]:
        return list(argv)
    return ["sudo", "-n", *argv[1:]]


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
