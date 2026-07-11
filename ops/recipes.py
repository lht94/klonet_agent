"""Controlled Ops recipes.

Recipes in this module are allowlisted operations. They validate structured
arguments and return auditable results; they do not execute model-authored
shell commands.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

from klonet_agent.ops.actions import OpsActionRegistry, configured_ops_action_registry
from klonet_agent.ops.command_policy import command_exists, decide_ops_command
from klonet_agent.ops.operations import OperationPlan, OperationStep, RecipeExecutionResult


RESTART_SCREEN_COMPONENT = "restart_screen_component"
MANUAL_CHECKPOINT = "manual_checkpoint"
STOP_SCREEN_COMPONENT = "stop_screen_component"
STOP_PLATFORM_SCREENS = "stop_platform_screens"
START_PLATFORM_SCREENS = "start_platform_screens"
VALIDATE_PROJECT_FILES = "validate_project_files"
PREPARE_PROJECT_FILES = "prepare_project_files"
EXTRACT_ARCHIVE = "extract_archive"
RUN_INSTALL_SCRIPT = "run_install_script"
ENSURE_SHARED_SERVICES = "ensure_shared_services"
WRITE_OPS_FILE = "write_ops_file"
INSTALL_NGINX_CONFIG = "install_nginx_config"
RELOAD_NGINX = "reload_nginx"
START_DOCKER_CONTAINER = "start_docker_container"
RUN_OPS_COMMAND = "run_ops_command"
RUN_OPS_COMMAND_TIMEOUT_SECONDS = 120
ALLOWED_COMPONENTS = {"master", "worker", "celery", "web_terminal"}
ALLOWED_INSTALL_SCRIPTS = {
    "base_requ_setup.sh": ("NORMAL",),
    "docker_service.sh": (),
}
REQUIRED_PROJECT_ENTRY_FILES = (
    "gun.py",
    "master_main.py",
    "celery_worker.py",
    "web_terminal_main.py",
    "worker_gun.py",
    "worker_main.py",
)
SHARED_SERVICE_PORTS = {
    "redis": "6379",
    "mysql": "3306",
    "rabbitmq": "5672",
}
SAFE_OPS_WRITE_SUFFIXES = {
    ".py",
    ".conf",
    ".cfg",
    ".ini",
    ".json",
    ".js",
    ".md",
    ".service",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SAFE_OPS_WRITE_NAMES = {
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
SENSITIVE_OPS_WRITE_NAME_PARTS = (
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
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
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


class ControlledActionRunner:
    """Validate and dispatch allowlisted structured Ops actions."""

    def __init__(
        self,
        *,
        dry_run: bool = True,
        helper_path: str = "/usr/local/bin/klonet-agent-op",
        command_runner=None,
        action_registry: OpsActionRegistry | None = None,
        execution_config: str = "enabled",
    ):
        self.dry_run = dry_run
        self.helper_path = helper_path
        self._uses_default_command_runner = command_runner is None
        self.command_runner = command_runner or _run_command
        self.action_registry = action_registry or configured_ops_action_registry()
        self.execution_config = execution_config

    def _helper_command(self, action: str, *args: str) -> list:
        command = [
            self.helper_path,
            action,
            "--dry-run" if self.dry_run else "--execute",
            *args,
        ]
        if self.dry_run:
            return command
        return ["sudo", "-n", *command]

    def __call__(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        action = step.action or step.recipe_id
        action_args = step.args or step.recipe_args
        spec = self.action_registry.get(action)
        if spec is None:
            return RecipeExecutionResult(
                "blocked",
                f"action_not_allowlisted={action or 'missing'}; environment unchanged",
            )
        if spec.requires_confirmation and self.execution_config != "enabled":
            return RecipeExecutionResult(
                "blocked",
                (
                    "ops_real_execution_not_configured "
                    f"config_status={self.execution_config} "
                    "required=KLONET_AGENT_OPS_REAL_EXECUTION=1; "
                    "environment unchanged"
                ),
                "configure /etc/klonet-agent/klonet-agent.env and restart the Agent process",
            )
        problem = self.action_registry.validate_args(spec, action_args)
        if problem:
            return RecipeExecutionResult("blocked", f"{problem}; environment unchanged")
        # Keep legacy handlers working while action/args are the canonical model.
        step.action = spec.name
        step.args = dict(action_args)
        step.recipe_id = spec.name
        step.recipe_args = dict(action_args)
        handler = getattr(self, spec.handler_name, None)
        if handler is None:
            return RecipeExecutionResult(
                "blocked",
                f"action_handler_unavailable={action}; environment unchanged",
            )
        if spec.name == MANUAL_CHECKPOINT:
            return handler(step)
        step_only_actions = {
            VALIDATE_PROJECT_FILES,
            PREPARE_PROJECT_FILES,
            EXTRACT_ARCHIVE,
            RUN_INSTALL_SCRIPT,
            ENSURE_SHARED_SERVICES,
            WRITE_OPS_FILE,
            INSTALL_NGINX_CONFIG,
            START_DOCKER_CONTAINER,
            RUN_OPS_COMMAND,
        }
        if spec.name in step_only_actions:
            return handler(step)
        if spec.name == RELOAD_NGINX:
            return handler()
        return handler(plan, step)

    def _manual_checkpoint(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        reason = _one_line(str(args.get("reason") or "manual checkpoint confirmed"))
        project_root = _one_line(str(args.get("project_root") or ""))
        parts = [
            f"recipe_id={MANUAL_CHECKPOINT}",
            f"reason={reason}",
        ]
        if project_root:
            parts.append(f"project_root={project_root}")
        parts.append("environment unchanged")
        return RecipeExecutionResult("completed", " ".join(parts))

    def _restart_screen_component(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        platform = str(args.get("platform") or plan.target or "").strip()
        component = str(args.get("component") or "").strip()
        screen_session = str(args.get("screen_session") or "").strip()
        project_root = str(args.get("project_root") or "").strip()
        problem = _validate_restart_args(platform, component, screen_session, project_root)
        if problem:
            return RecipeExecutionResult("blocked", f"{problem}; environment unchanged")
        command = self._helper_command(
            "restart-screen-component",
            "--platform",
            platform,
            "--component",
            component,
            "--screen",
            screen_session,
            "--project-root",
            project_root,
        )
        if not self.dry_run:
            try:
                output = self.command_runner(command)
            except subprocess.CalledProcessError as exc:
                return _helper_failure_result(exc)
            return RecipeExecutionResult(
                "completed",
                (
                    f"dry_run=false "
                    f"recipe_id={RESTART_SCREEN_COMPONENT} "
                    f"command={_format_command(command)} "
                    f"helper_output={_one_line(output)}"
                ),
            )
        return RecipeExecutionResult(
            "completed",
            (
                f"dry_run={str(self.dry_run).lower()} "
                f"recipe_id={RESTART_SCREEN_COMPONENT} "
                f"command_preview={_format_command(command)} "
                "environment unchanged"
            ),
        )

    def _stop_screen_component(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        platform = str(args.get("platform") or plan.target or "").strip()
        component = str(args.get("component") or "").strip()
        screen_session = str(args.get("screen_session") or "").strip()
        problem = _validate_screen_component_args(platform, component, screen_session)
        if problem:
            return RecipeExecutionResult("blocked", f"{problem}; environment unchanged")
        command = self._helper_command(
            "stop-screen-component",
            "--platform",
            platform,
            "--component",
            component,
            "--screen",
            screen_session,
        )
        return self._helper_result(STOP_SCREEN_COMPONENT, command)

    def _stop_platform_screens(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        platform = str(args.get("platform") or plan.target or "").strip()
        if not _safe_token(platform):
            return RecipeExecutionResult("blocked", f"invalid_platform={platform or 'missing'}; environment unchanged")
        command = self._helper_command(
            "stop-platform-screens",
            "--platform",
            platform,
        )
        return self._helper_result(STOP_PLATFORM_SCREENS, command)

    def _start_platform_screens(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        platform = str(args.get("platform") or plan.target or "").strip()
        project_root = str(args.get("project_root") or "").strip()
        problem = _validate_start_platform_args(platform, project_root)
        if problem:
            return RecipeExecutionResult("blocked", f"{problem}; environment unchanged")
        command = self._helper_command(
            "start-platform-screens",
            "--platform",
            platform,
            "--project-root",
            project_root,
        )
        if not self.dry_run:
            try:
                output = self.command_runner(command)
            except subprocess.CalledProcessError as exc:
                return _helper_failure_result(exc)
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=false "
                    f"recipe_id={START_PLATFORM_SCREENS} "
                    f"command={_format_command(command)} "
                    f"helper_output={_one_line(output)}"
                ),
            )
        return RecipeExecutionResult(
            "completed",
            (
                f"dry_run={str(self.dry_run).lower()} "
                f"recipe_id={START_PLATFORM_SCREENS} "
                f"command_preview={_format_command(command)} "
                "environment unchanged"
            ),
        )

    def _validate_project_files(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        project_root = str(args.get("project_root") or "").strip()
        if not project_root or _looks_unsafe_path(project_root):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={VALIDATE_PROJECT_FILES} invalid_project_root={project_root or 'missing'}; environment unchanged",
            )
        root = Path(project_root)
        found = _project_entry_file_sources(root)
        missing = [filename for filename in REQUIRED_PROJECT_ENTRY_FILES if filename not in found]
        if missing:
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={VALIDATE_PROJECT_FILES} "
                    f"project_root={_one_line(project_root)} "
                    f"missing_files={','.join(missing)} "
                    "environment unchanged"
                ),
            )
        return RecipeExecutionResult(
            "completed",
            (
                f"recipe_id={VALIDATE_PROJECT_FILES} "
                f"project_root={_one_line(project_root)} "
                f"found_files={','.join(found[filename] for filename in REQUIRED_PROJECT_ENTRY_FILES)} "
                "environment unchanged"
            ),
        )

    def _prepare_project_files(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        project_root = str(args.get("project_root") or "").strip()
        if not project_root or _looks_unsafe_path(project_root):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={PREPARE_PROJECT_FILES} invalid_project_root={project_root or 'missing'}; environment unchanged",
            )
        root = Path(project_root)
        mains = root / "mains"
        missing_sources = [
            filename
            for filename in REQUIRED_PROJECT_ENTRY_FILES
            if not (mains / filename).is_file()
        ]
        if missing_sources:
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={PREPARE_PROJECT_FILES} "
                    f"project_root={_one_line(project_root)} "
                    f"missing_sources={','.join('mains/' + filename for filename in missing_sources)} "
                    "environment unchanged"
                ),
            )
        previews = [f"mains/{filename}->{filename}" for filename in REQUIRED_PROJECT_ENTRY_FILES]
        if self.dry_run:
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=true "
                    f"recipe_id={PREPARE_PROJECT_FILES} "
                    f"project_root={_one_line(project_root)} "
                    f"copy_preview={','.join(previews)} "
                    "environment unchanged"
                ),
            )
        try:
            for filename in REQUIRED_PROJECT_ENTRY_FILES:
                shutil.copy2(mains / filename, root / filename)
        except OSError as exc:
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={PREPARE_PROJECT_FILES} "
                    f"copy_failed={_one_line(str(exc))} "
                    "environment_changed=unknown"
                ),
                "inspect_runtime",
            )
        return RecipeExecutionResult(
            "completed",
            (
                "dry_run=false "
                f"recipe_id={PREPARE_PROJECT_FILES} "
                f"project_root={_one_line(project_root)} "
                f"copied_files={','.join(REQUIRED_PROJECT_ENTRY_FILES)} "
                "environment_changed=true"
            ),
        )

    def _extract_archive(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        archive_path = str(args.get("archive_path") or "").strip()
        destination_dir = str(args.get("destination_dir") or "").strip()
        if not archive_path or _looks_unsafe_path(archive_path):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={EXTRACT_ARCHIVE} invalid_archive_path={archive_path or 'missing'}; environment unchanged",
            )
        if not destination_dir or _looks_unsafe_path(destination_dir):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={EXTRACT_ARCHIVE} invalid_destination_dir={destination_dir or 'missing'}; environment unchanged",
            )
        archive = Path(archive_path)
        destination = Path(destination_dir)
        if not archive.is_file():
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={EXTRACT_ARCHIVE} archive_not_found={_one_line(archive_path)}; environment unchanged",
            )
        try:
            members = _archive_members(archive)
            unsafe_member = _first_unsafe_archive_member(destination, members)
            if unsafe_member:
                return RecipeExecutionResult(
                    "blocked",
                    f"recipe_id={EXTRACT_ARCHIVE} unsafe_archive_member={_one_line(unsafe_member)}; environment unchanged",
                )
            member_preview = ",".join(members[:50])
            if self.dry_run:
                return RecipeExecutionResult(
                    "completed",
                    (
                        "dry_run=true "
                        f"recipe_id={EXTRACT_ARCHIVE} "
                        f"archive_path={_one_line(archive_path)} "
                        f"destination_dir={_one_line(destination_dir)} "
                        f"archive_members={member_preview} "
                        "environment unchanged"
                    ),
                )
            if _requires_sudo_helper_path(destination):
                command = self._helper_command(
                    "extract-archive",
                    "--archive-path",
                    archive_path,
                    "--destination-dir",
                    destination_dir,
                )
                output = self.command_runner(command)
                return RecipeExecutionResult(
                    "completed",
                    (
                        "dry_run=false "
                        f"recipe_id={EXTRACT_ARCHIVE} "
                        f"command={_format_command(command)} "
                        f"helper_output={_one_line(output)}"
                    ),
                )
            destination.mkdir(parents=True, exist_ok=True)
            _extract_archive_to(archive, destination)
        except subprocess.CalledProcessError as exc:
            return _helper_failure_result(exc)
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={EXTRACT_ARCHIVE} "
                    f"extract_failed={_one_line(str(exc))} "
                    "environment_changed=unknown"
                ),
                "inspect_runtime",
            )
        return RecipeExecutionResult(
            "completed",
            (
                "dry_run=false "
                f"recipe_id={EXTRACT_ARCHIVE} "
                f"archive_path={_one_line(archive_path)} "
                f"destination_dir={_one_line(destination_dir)} "
                f"extracted_members={len(members)} "
                "environment_changed=true"
            ),
        )

    def _run_install_script(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        script_dir = str(args.get("script_dir") or "").strip()
        script_name = str(args.get("script_name") or "").strip()
        script_args = _split_script_args(args.get("script_args"))
        if not script_dir or _looks_unsafe_path(script_dir):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={RUN_INSTALL_SCRIPT} invalid_script_dir={script_dir or 'missing'}; environment unchanged",
            )
        if script_name not in ALLOWED_INSTALL_SCRIPTS:
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={RUN_INSTALL_SCRIPT} unsupported_script={script_name or 'missing'}; environment unchanged",
            )
        allowed_args = ALLOWED_INSTALL_SCRIPTS[script_name]
        if tuple(script_args) != allowed_args:
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={RUN_INSTALL_SCRIPT} "
                    f"unsupported_script_args={','.join(script_args) or 'none'} "
                    "environment unchanged"
                ),
            )
        command = _install_script_command(script_dir, script_name, script_args)
        if self.dry_run:
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=true "
                    f"recipe_id={RUN_INSTALL_SCRIPT} "
                    f"command_preview={_format_command(command)} "
                        "environment unchanged"
                    ),
                )
        try:
            if _requires_sudo_helper_path(Path(script_dir)):
                command = self._helper_command(
                    "run-install-script",
                    "--script-dir",
                    script_dir,
                    "--script-name",
                    script_name,
                )
                if script_args:
                    command.extend(["--script-args", " ".join(script_args)])
            else:
                script_path = Path(script_dir) / script_name
                if not script_path.is_file():
                    return RecipeExecutionResult(
                        "blocked",
                        f"recipe_id={RUN_INSTALL_SCRIPT} script_not_found={_one_line(str(script_path))}; environment unchanged",
                    )
            output = self._run_install_command(command)
        except subprocess.CalledProcessError as exc:
            return _script_failure_result(exc)
        postcondition_problem = _install_script_postcondition_problem(script_name, script_args)
        if postcondition_problem:
            return RecipeExecutionResult(
                "blocked",
                (
                    "dry_run=false "
                    f"recipe_id={RUN_INSTALL_SCRIPT} "
                    f"command={_format_command(command)} "
                    f"postcondition_failed={postcondition_problem} "
                    f"script_output={_one_line(output)} "
                    "environment_changed=unknown"
                ),
                "inspect_runtime",
            )
        return RecipeExecutionResult(
            "completed",
            (
                "dry_run=false "
                f"recipe_id={RUN_INSTALL_SCRIPT} "
                f"command={_format_command(command)} "
                f"script_output={_one_line(output)} "
                "environment_changed=true"
            ),
        )

    def _run_install_command(self, command: list) -> str:
        if self._uses_default_command_runner and not self.dry_run:
            return _run_command_streaming(command)
        return self.command_runner(command)

    def _ensure_shared_services(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        script_dir = str(args.get("script_dir") or "/root/vemu_install_new_gen").strip()
        if not script_dir or _looks_unsafe_path(script_dir):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={ENSURE_SHARED_SERVICES} invalid_script_dir={script_dir or 'missing'}; environment unchanged",
            )
        check_command = _shared_service_check_command()
        if self.dry_run:
            install_command = self._helper_command(
                "run-install-script",
                "--script-dir",
                script_dir,
                "--script-name",
                "docker_service.sh",
            )
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=true "
                    f"recipe_id={ENSURE_SHARED_SERVICES} "
                    f"check_command_preview={_format_command(check_command)} "
                    f"fallback_command_preview={_format_command(install_command)} "
                    "environment unchanged"
                ),
            )
        try:
            status_output = self.command_runner(check_command)
        except subprocess.CalledProcessError as exc:
            return _script_failure_result(exc)
        missing = _missing_shared_services(status_output)
        if not missing:
            return RecipeExecutionResult(
                "completed",
                (
                    f"dry_run=false recipe_id={ENSURE_SHARED_SERVICES} "
                    f"service_status={_one_line(status_output)} "
                    "environment unchanged"
                ),
            )
        install_step = OperationStep(
            step_id=step.step_id,
            title=step.title,
            purpose=step.purpose,
            recipe_id=RUN_INSTALL_SCRIPT,
            recipe_args={
                "script_dir": script_dir,
                "script_name": "docker_service.sh",
            },
        )
        result = self._run_install_script(install_step)
        result.output = (
            f"recipe_id={ENSURE_SHARED_SERVICES} "
            f"missing_services={','.join(missing)} "
            f"service_status={_one_line(status_output)} "
            f"fallback_result=({result.output})"
        )
        return result

    def _write_ops_file(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.args or step.recipe_args or {}
        raw_path = str(args.get("path") or "").strip()
        content = str(args.get("content") or "")
        mode = str(args.get("mode") or "replace_file").strip().lower()
        anchor = str(args.get("anchor") or "")
        if not raw_path or _looks_unsafe_path(raw_path):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={WRITE_OPS_FILE} invalid_path={raw_path or 'missing'}; environment unchanged",
            )
        path = Path(raw_path)
        if _is_sensitive_ops_write_path(path):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={WRITE_OPS_FILE} refused_sensitive_path={path.name}; environment unchanged",
            )
        if not _is_supported_ops_write_path(path):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={WRITE_OPS_FILE} unsupported_file_type={path.name or raw_path}; environment unchanged",
            )

        if mode not in {"replace_file", "insert_after", "insert_before", "replace_text"}:
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={WRITE_OPS_FILE} unsupported_write_mode={mode}; environment unchanged",
            )
        old_content = ""
        new_content = content
        already_applied = False
        if mode != "replace_file":
            if not path.is_file():
                return RecipeExecutionResult(
                    "blocked",
                    f"recipe_id={WRITE_OPS_FILE} incremental_target_missing={path}; environment unchanged",
                )
            try:
                old_content = path.read_text(encoding="utf-8")
            except OSError as exc:
                return RecipeExecutionResult(
                    "blocked",
                    f"recipe_id={WRITE_OPS_FILE} read_failed={_one_line(str(exc))}; environment unchanged",
                )
            edit_result = _apply_incremental_edit(
                old_content,
                mode=mode,
                anchor=anchor,
                content=content,
                expected_matches=args.get("expected_matches", 1),
            )
            if edit_result[0]:
                return RecipeExecutionResult(
                    "blocked",
                    f"recipe_id={WRITE_OPS_FILE} {edit_result[0]}; environment unchanged",
                )
            new_content = edit_result[1]
            already_applied = edit_result[2]

        preview = _one_line(_redact_sensitive_text(content))
        if self.dry_run:
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=true "
                    f"recipe_id={WRITE_OPS_FILE} "
                    f"path={_one_line(raw_path)} "
                    f"mode={mode} "
                    f"already_applied={str(already_applied).lower()} "
                    f"preview={preview} "
                    "environment unchanged"
                ),
            )
        try:
            if already_applied:
                return RecipeExecutionResult(
                    "completed",
                    (
                        f"dry_run=false recipe_id={WRITE_OPS_FILE} "
                        f"path={_one_line(raw_path)} mode={mode} "
                        "already_applied=true environment_changed=false"
                    ),
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            backup_path = ""
            if path.exists():
                backup = _ops_backup_path(path)
                shutil.copy2(path, backup)
                backup_path = str(backup)
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={WRITE_OPS_FILE} "
                    f"write_failed={_one_line(str(exc))} "
                    "environment_changed=unknown"
                ),
                "inspect_runtime",
            )
        parts = [
            "dry_run=false",
            f"recipe_id={WRITE_OPS_FILE}",
            f"path={_one_line(raw_path)}",
            f"mode={mode}",
            f"bytes_written={len(new_content.encode('utf-8'))}",
        ]
        if backup_path:
            parts.append(f"backup_path={_one_line(backup_path)}")
        parts.append("environment_changed=true")
        return RecipeExecutionResult("completed", " ".join(parts))

    def _install_nginx_config(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.args or step.recipe_args or {}
        source_path = str(args.get("source_path") or "").strip()
        config_name = str(args.get("config_name") or "").strip()
        if not source_path or _looks_unsafe_path(source_path):
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={INSTALL_NGINX_CONFIG} "
                    f"invalid_source_path={source_path or 'missing'}; "
                    "environment unchanged"
                ),
            )
        if not _safe_nginx_config_name(config_name):
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={INSTALL_NGINX_CONFIG} "
                    f"invalid_config_name={config_name or 'missing'}; "
                    "environment unchanged"
                ),
            )
        command = self._helper_command(
            "install-nginx-config",
            "--source-path",
            source_path,
            "--config-name",
            config_name,
        )
        return self._helper_result(INSTALL_NGINX_CONFIG, command)

    def _reload_nginx(self) -> RecipeExecutionResult:
        command = self._helper_command(
            "reload-nginx",
        )
        if self.dry_run:
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=true "
                    f"recipe_id={RELOAD_NGINX} "
                    f"command_preview={_format_command(command)} "
                    "environment unchanged"
                ),
            )
        try:
            output = self.command_runner(command)
        except subprocess.CalledProcessError as exc:
            return _nginx_helper_failure_result(exc)
        return RecipeExecutionResult(
            "completed",
            (
                "dry_run=false "
                f"recipe_id={RELOAD_NGINX} "
                f"command={_format_command(command)} "
                f"helper_output={_one_line(output)}"
            ),
        )

    def _start_docker_container(self, step: OperationStep) -> RecipeExecutionResult:
        name = str((step.args or step.recipe_args or {}).get("name") or "").strip()
        if not _safe_container_name(name):
            return RecipeExecutionResult(
                "blocked",
                f"invalid_container_name={name or 'missing'}; environment unchanged",
            )
        command = self._helper_command("start-docker-container", "--name", name)
        return self._helper_result(START_DOCKER_CONTAINER, command)

    def _run_ops_command(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.args or step.recipe_args or {}
        decision = decide_ops_command(args)
        if not decision.allowed:
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={RUN_OPS_COMMAND} command_not_allowed={decision.reason}; environment unchanged",
            )
        if not command_exists(decision.program):
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={RUN_OPS_COMMAND} program_not_found={decision.program}; environment unchanged",
            )
        if decision.requires_sudo:
            command = self._helper_command(
                "run-ops-command",
                "--program",
                decision.program,
                "--argv-json",
                decision.argv_json(),
            )
            if decision.cwd:
                command.extend(["--cwd", decision.cwd])
        else:
            command = [decision.program, *decision.argv]
        if self.dry_run:
            return RecipeExecutionResult(
                "completed",
                (
                    f"dry_run=true recipe_id={RUN_OPS_COMMAND} "
                    f"category={decision.category} risk={decision.risk} "
                    f"requires_sudo={str(decision.requires_sudo).lower()} "
                    f"command_preview={_format_command(command)} "
                    f"cwd={_one_line(decision.cwd or '.')} "
                    "environment unchanged"
                ),
            )
        try:
            if decision.requires_sudo:
                output = self.command_runner(command)
            elif self._uses_default_command_runner:
                output = _run_command(command, cwd=decision.cwd or None)
            else:
                output = self.command_runner(command)
        except subprocess.CalledProcessError as exc:
            return _helper_failure_result(exc)
        except subprocess.TimeoutExpired as exc:
            return RecipeExecutionResult(
                "blocked",
                (
                    f"recipe_id={RUN_OPS_COMMAND} command_timed_out "
                    f"timeout_seconds={int(exc.timeout or RUN_OPS_COMMAND_TIMEOUT_SECONDS)} "
                    "environment_changed=unknown"
                ),
                "inspect_runtime",
            )
        except OSError as exc:
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={RUN_OPS_COMMAND} command_os_error={_one_line(str(exc))}; environment unchanged",
            )
        return RecipeExecutionResult(
            "completed",
            (
                f"dry_run=false recipe_id={RUN_OPS_COMMAND} "
                f"category={decision.category} risk={decision.risk} "
                f"requires_sudo={str(decision.requires_sudo).lower()} "
                f"command={_format_command(command)} "
                f"cwd={_one_line(decision.cwd or '.')} "
                f"command_output={_one_line(output)} "
                "environment_changed=unknown"
            ),
        )

    def _helper_result(self, recipe_id: str, command: list) -> RecipeExecutionResult:
        if not self.dry_run:
            try:
                output = self.command_runner(command)
            except subprocess.CalledProcessError as exc:
                return _helper_failure_result(exc)
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=false "
                    f"recipe_id={recipe_id} "
                    f"command={_format_command(command)} "
                    f"helper_output={_one_line(output)}"
                ),
            )
        return RecipeExecutionResult(
            "completed",
            (
                "dry_run=true "
                f"recipe_id={recipe_id} "
                f"command_preview={_format_command(command)} "
                "environment unchanged"
            ),
        )


# Backward-compatible import for existing integrations and persisted terminology.
ControlledRecipeRunner = ControlledActionRunner


def _validate_restart_args(platform: str, component: str, screen_session: str, project_root: str) -> str:
    problem = _validate_screen_component_args(platform, component, screen_session)
    if problem:
        return problem
    if not project_root or _looks_unsafe_path(project_root):
        return f"invalid_project_root={project_root or 'missing'}"
    return ""


def _validate_screen_component_args(platform: str, component: str, screen_session: str) -> str:
    if not _safe_token(platform):
        return f"invalid_platform={platform or 'missing'}"
    if component not in ALLOWED_COMPONENTS:
        return f"unsupported_component={component or 'missing'}"
    if not _safe_token(screen_session):
        return f"invalid_screen_session={screen_session or 'missing'}"
    if not screen_session.startswith(f"{platform}_") and f".{platform}_" not in screen_session:
        return "screen_session_does_not_match_platform"
    return ""


def _validate_start_platform_args(platform: str, project_root: str) -> str:
    if not _safe_token(platform):
        return f"invalid_platform={platform or 'missing'}"
    if not project_root or _looks_unsafe_path(project_root):
        return f"invalid_project_root={project_root or 'missing'}"
    return ""


def _safe_token(value: str) -> bool:
    return bool(value and _SAFE_NAME.match(value))


def _safe_container_name(value: str) -> bool:
    return bool(value and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value))


def _safe_nginx_config_name(value: str) -> bool:
    return bool(value and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}\.conf", value))


def _apply_incremental_edit(
    original: str,
    *,
    mode: str,
    anchor: str,
    content: str,
    expected_matches,
) -> tuple[str, str, bool]:
    """Return (problem, new_content, already_applied) for a deterministic edit."""

    if not anchor:
        return "missing_anchor", original, False
    try:
        expected = int(expected_matches)
    except (TypeError, ValueError):
        return "invalid_expected_matches", original, False
    if expected < 1 or expected > 20:
        return "invalid_expected_matches", original, False
    newline = "\r\n" if "\r\n" in original else "\n"
    snippet = content.rstrip("\r\n")
    matches = original.count(anchor)
    if mode == "insert_after":
        replacement = anchor + newline + snippet
        if matches != expected:
            return f"anchor_match_count={matches} expected={expected}", original, False
        if replacement in original:
            return "", original, True
    elif mode == "insert_before":
        replacement = snippet + newline + anchor
        if matches != expected:
            return f"anchor_match_count={matches} expected={expected}", original, False
        if replacement in original:
            return "", original, True
    else:
        replacement = content
        if anchor not in original and content and content in original:
            return "", original, True
        if matches != expected:
            return f"anchor_match_count={matches} expected={expected}", original, False
    return "", original.replace(anchor, replacement, expected), False


def _project_entry_file_sources(root: Path) -> dict:
    found = {}
    for filename in REQUIRED_PROJECT_ENTRY_FILES:
        if (root / filename).is_file():
            found[filename] = filename
        elif (root / "mains" / filename).is_file():
            found[filename] = f"mains/{filename}"
    return found


def _archive_members(archive: Path) -> list:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            return [name for name in handle.namelist() if name and not name.endswith("/")]
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as handle:
            return [member.name for member in handle.getmembers() if member.name and member.isfile()]
    raise OSError(f"unsupported_archive={archive.name}")


def _extract_archive_to(archive: Path, destination: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(destination)
        return
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            if not member.isfile():
                continue
            source = handle.extractfile(member)
            if source is None:
                continue
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _split_script_args(value) -> list:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return shlex.split(text)


def _install_script_postcondition_problem(script_name: str, script_args: list[str]) -> str:
    if script_name != "base_requ_setup.sh" or tuple(script_args) != ("NORMAL",):
        return ""
    required = {
        "python3.8": ("/usr/local/python3/bin/python3.8",),
        "pip3.8": ("/usr/local/python3/bin/pip3.8",),
        "gunicorn": ("/usr/local/bin/gunicorn",),
        "celery": ("/usr/local/bin/celery",),
    }
    missing = [
        name
        for name, paths in required.items()
        if not _command_or_known_path_exists(name, paths)
    ]
    return "missing_commands=" + ",".join(missing) if missing else ""


def _command_or_known_path_exists(command: str, paths: tuple[str, ...]) -> bool:
    if shutil.which(command):
        return True
    return any(Path(path).is_file() for path in paths)


def _install_script_command(script_dir: str, script_name: str, script_args: list) -> list:
    args = " ".join(shlex.quote(item) for item in script_args)
    command = f"cd {shlex.quote(script_dir)} && bash ./{shlex.quote(script_name)}"
    if args:
        command = f"{command} {args}"
    return ["bash", "-lc", command]


def _shared_service_check_command() -> list:
    checks = []
    for service, port in SHARED_SERVICE_PORTS.items():
        checks.append(
            "if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '(^|:)"
            + re.escape(port)
            + "$'; then echo service="
            + service
            + " port="
            + port
            + " status=listen; else echo service="
            + service
            + " port="
            + port
            + " status=missing; fi"
        )
    return ["bash", "-lc", " ".join(checks)]


def _missing_shared_services(status_output: str) -> list:
    missing = []
    for line in str(status_output or "").splitlines():
        service_match = re.search(r"\bservice=([A-Za-z0-9_-]+)\b", line)
        if "status=missing" in line and service_match:
            missing.append(service_match.group(1))
    return missing


def _first_unsafe_archive_member(destination: Path, members: list) -> str:
    destination_root = destination.resolve()
    for member in members:
        if Path(member).is_absolute():
            return member
        target = (destination / member).resolve()
        if not _path_is_relative_to(target, destination_root):
            return member
    return ""


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _looks_unsafe_path(value: str) -> bool:
    if any(part in value for part in ("\x00", "\n", "\r")):
        return True
    return not (value.startswith("/") or Path(value).is_absolute())


def _requires_sudo_helper_path(path: Path) -> bool:
    normalized = str(path).replace("\\", "/")
    return normalized == "/root" or normalized.startswith("/root/")


def _redact_sensitive_text(text: str) -> str:
    redacted = text or ""
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def _redact_match(match: re.Match) -> str:
    if match.lastindex:
        return f"{match.group(1)} [REDACTED]"
    return "[REDACTED]"


def _is_sensitive_ops_write_path(path: Path) -> bool:
    lower_name = path.name.lower()
    return any(part in lower_name for part in SENSITIVE_OPS_WRITE_NAME_PARTS)


def _is_supported_ops_write_path(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name in SAFE_OPS_WRITE_NAMES or lower_name.startswith("dockerfile"):
        return True
    if path.suffix.lower() == ".py":
        return False
    return path.suffix.lower() in SAFE_OPS_WRITE_SUFFIXES


def _ops_backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return path.with_name(f"{path.name}.bak.{stamp}")


def _format_command(command: list) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def _run_command(command: list, cwd: str | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_command_env(command),
        timeout=RUN_OPS_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def _command_env(command: list) -> dict | None:
    if not command or Path(str(command[0])).name != "git":
        return None
    import os

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault(
        "GIT_SSH_COMMAND",
        "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15",
    )
    return env


def _run_command_streaming(command: list) -> str:
    subprocess.run(
        command,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return "streamed_to_console=true"


def _helper_failure_result(exc: subprocess.CalledProcessError) -> RecipeExecutionResult:
    stderr = _one_line(exc.stderr)
    stdout = _one_line(exc.output)
    output = (
        f"helper_failed returncode={exc.returncode} "
        f"stderr={stderr} "
        f"stdout={stdout}"
    )
    if "environment_changed=unknown" in stderr:
        return RecipeExecutionResult(
            "blocked",
            f"helper_environment_unknown {output}",
            "inspect_runtime",
        )
    return RecipeExecutionResult("failed", output)


def _nginx_helper_failure_result(exc: subprocess.CalledProcessError) -> RecipeExecutionResult:
    stderr = _one_line(exc.stderr)
    stdout = _one_line(exc.output)
    output = (
        f"nginx_helper_failed returncode={exc.returncode} "
        f"stderr={stderr} "
        f"stdout={stdout}"
    )
    if "nginx_test_failed" in stderr or "environment_changed=false" in stderr:
        return RecipeExecutionResult("blocked", output)
    if "environment_changed=unknown" in stderr:
        return RecipeExecutionResult("blocked", output, "inspect_runtime")
    return RecipeExecutionResult("failed", output)


def _script_failure_result(exc: subprocess.CalledProcessError) -> RecipeExecutionResult:
    stderr = _one_line(exc.stderr)
    stdout = _one_line(exc.output)
    return RecipeExecutionResult(
        "blocked",
        (
            f"script_failed returncode={exc.returncode} "
            f"stderr={stderr} "
            f"stdout={stdout} "
            "environment_changed=unknown"
        ),
        "inspect_runtime",
    )


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())[:500]
