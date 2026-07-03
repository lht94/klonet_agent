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
WRITE_OPS_FILE = "write_ops_file"
RELOAD_NGINX = "reload_nginx"
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


class ControlledRecipeRunner:
    """Dispatch allowlisted Ops recipes."""

    def __init__(
        self,
        *,
        dry_run: bool = True,
        helper_path: str = "/usr/local/bin/klonet-agent-op",
        command_runner=None,
    ):
        self.dry_run = dry_run
        self.helper_path = helper_path
        self.command_runner = command_runner or _run_command

    def __call__(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        if step.recipe_id == MANUAL_CHECKPOINT:
            return self._manual_checkpoint(step)
        if step.recipe_id == RESTART_SCREEN_COMPONENT:
            return self._restart_screen_component(plan, step)
        if step.recipe_id == STOP_SCREEN_COMPONENT:
            return self._stop_screen_component(plan, step)
        if step.recipe_id == STOP_PLATFORM_SCREENS:
            return self._stop_platform_screens(plan, step)
        if step.recipe_id == START_PLATFORM_SCREENS:
            return self._start_platform_screens(plan, step)
        if step.recipe_id == VALIDATE_PROJECT_FILES:
            return self._validate_project_files(step)
        if step.recipe_id == PREPARE_PROJECT_FILES:
            return self._prepare_project_files(step)
        if step.recipe_id == EXTRACT_ARCHIVE:
            return self._extract_archive(step)
        if step.recipe_id == RUN_INSTALL_SCRIPT:
            return self._run_install_script(step)
        if step.recipe_id == WRITE_OPS_FILE:
            return self._write_ops_file(step)
        if step.recipe_id == RELOAD_NGINX:
            return self._reload_nginx()
        return RecipeExecutionResult("blocked", f"unknown_recipe={step.recipe_id}; environment unchanged")

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
        command = [
            self.helper_path,
            "restart-screen-component",
            "--dry-run" if self.dry_run else "--execute",
            "--platform",
            platform,
            "--component",
            component,
            "--screen",
            screen_session,
            "--project-root",
            project_root,
        ]
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
        command = [
            self.helper_path,
            "stop-screen-component",
            "--dry-run" if self.dry_run else "--execute",
            "--platform",
            platform,
            "--component",
            component,
            "--screen",
            screen_session,
        ]
        return self._helper_result(STOP_SCREEN_COMPONENT, command)

    def _stop_platform_screens(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        platform = str(args.get("platform") or plan.target or "").strip()
        if not _safe_token(platform):
            return RecipeExecutionResult("blocked", f"invalid_platform={platform or 'missing'}; environment unchanged")
        command = [
            self.helper_path,
            "stop-platform-screens",
            "--dry-run" if self.dry_run else "--execute",
            "--platform",
            platform,
        ]
        return self._helper_result(STOP_PLATFORM_SCREENS, command)

    def _start_platform_screens(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        platform = str(args.get("platform") or plan.target or "").strip()
        project_root = str(args.get("project_root") or "").strip()
        problem = _validate_start_platform_args(platform, project_root)
        if problem:
            return RecipeExecutionResult("blocked", f"{problem}; environment unchanged")
        command = [
            self.helper_path,
            "start-platform-screens",
            "--dry-run" if self.dry_run else "--execute",
            "--platform",
            platform,
            "--project-root",
            project_root,
        ]
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
            destination.mkdir(parents=True, exist_ok=True)
            _extract_archive_to(archive, destination)
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
        script_path = Path(script_dir) / script_name
        if not script_path.is_file():
            return RecipeExecutionResult(
                "blocked",
                f"recipe_id={RUN_INSTALL_SCRIPT} script_not_found={_one_line(str(script_path))}; environment unchanged",
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
            output = self.command_runner(command)
        except subprocess.CalledProcessError as exc:
            return _script_failure_result(exc)
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

    def _write_ops_file(self, step: OperationStep) -> RecipeExecutionResult:
        args = step.recipe_args or {}
        raw_path = str(args.get("path") or "").strip()
        content = str(args.get("content") or "")
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

        preview = _one_line(_redact_sensitive_text(content))
        if self.dry_run:
            return RecipeExecutionResult(
                "completed",
                (
                    "dry_run=true "
                    f"recipe_id={WRITE_OPS_FILE} "
                    f"path={_one_line(raw_path)} "
                    f"preview={preview} "
                    "environment unchanged"
                ),
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            backup_path = ""
            if path.exists():
                backup = _ops_backup_path(path)
                shutil.copy2(path, backup)
                backup_path = str(backup)
            path.write_text(content, encoding="utf-8")
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
            f"bytes_written={len(content.encode('utf-8'))}",
        ]
        if backup_path:
            parts.append(f"backup_path={_one_line(backup_path)}")
        parts.append("environment_changed=true")
        return RecipeExecutionResult("completed", " ".join(parts))

    def _reload_nginx(self) -> RecipeExecutionResult:
        command = [
            self.helper_path,
            "reload-nginx",
            "--dry-run" if self.dry_run else "--execute",
        ]
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


def _install_script_command(script_dir: str, script_name: str, script_args: list) -> list:
    args = " ".join(shlex.quote(item) for item in script_args)
    command = f"cd {shlex.quote(script_dir)} && bash ./{shlex.quote(script_name)}"
    if args:
        command = f"{command} {args}"
    return ["bash", "-lc", command]


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
    return path.suffix.lower() in SAFE_OPS_WRITE_SUFFIXES


def _ops_backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return path.with_name(f"{path.name}.bak.{stamp}")


def _format_command(command: list) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def _run_command(command: list) -> str:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


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
