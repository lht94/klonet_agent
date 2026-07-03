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
from pathlib import Path

from klonet_agent.ops.operations import OperationPlan, OperationStep, RecipeExecutionResult


RESTART_SCREEN_COMPONENT = "restart_screen_component"
MANUAL_CHECKPOINT = "manual_checkpoint"
STOP_SCREEN_COMPONENT = "stop_screen_component"
STOP_PLATFORM_SCREENS = "stop_platform_screens"
START_PLATFORM_SCREENS = "start_platform_screens"
VALIDATE_PROJECT_FILES = "validate_project_files"
PREPARE_PROJECT_FILES = "prepare_project_files"
ALLOWED_COMPONENTS = {"master", "worker", "celery", "web_terminal"}
REQUIRED_PROJECT_ENTRY_FILES = (
    "gun.py",
    "master_main.py",
    "celery_worker.py",
    "web_terminal_main.py",
    "worker_gun.py",
    "worker_main.py",
)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


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


def _looks_unsafe_path(value: str) -> bool:
    if any(part in value for part in ("\x00", "\n", "\r")):
        return True
    return not (value.startswith("/") or Path(value).is_absolute())


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


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())[:500]
