"""Controlled Ops recipes.

Recipes in this module are allowlisted operations. They validate structured
arguments and return auditable results; they do not execute model-authored
shell commands.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from klonet_agent.ops.operations import OperationPlan, OperationStep, RecipeExecutionResult


RESTART_SCREEN_COMPONENT = "restart_screen_component"
ALLOWED_COMPONENTS = {"master", "worker", "celery", "web_terminal"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


class ControlledRecipeRunner:
    """Dispatch allowlisted Ops recipes."""

    def __init__(self, *, dry_run: bool = True, helper_path: str = "/usr/local/bin/klonet-agent-op"):
        self.dry_run = dry_run
        self.helper_path = helper_path

    def __call__(self, plan: OperationPlan, step: OperationStep) -> RecipeExecutionResult:
        if step.recipe_id == RESTART_SCREEN_COMPONENT:
            return self._restart_screen_component(plan, step)
        return RecipeExecutionResult("blocked", f"unknown_recipe={step.recipe_id}; environment unchanged")

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
            "--platform",
            platform,
            "--component",
            component,
            "--screen",
            screen_session,
            "--project-root",
            project_root,
        ]
        return RecipeExecutionResult(
            "completed",
            (
                f"dry_run={str(self.dry_run).lower()} "
                f"recipe_id={RESTART_SCREEN_COMPONENT} "
                f"command_preview={_format_command(command)} "
                "environment unchanged"
            ),
        )


def _validate_restart_args(platform: str, component: str, screen_session: str, project_root: str) -> str:
    if not _safe_token(platform):
        return f"invalid_platform={platform or 'missing'}"
    if component not in ALLOWED_COMPONENTS:
        return f"unsupported_component={component or 'missing'}"
    if not _safe_token(screen_session):
        return f"invalid_screen_session={screen_session or 'missing'}"
    if not screen_session.startswith(f"{platform}_") and f".{platform}_" not in screen_session:
        return "screen_session_does_not_match_platform"
    if not project_root or _looks_unsafe_path(project_root):
        return f"invalid_project_root={project_root or 'missing'}"
    return ""


def _safe_token(value: str) -> bool:
    return bool(value and _SAFE_NAME.match(value))


def _looks_unsafe_path(value: str) -> bool:
    if any(part in value for part in ("\x00", "\n", "\r")):
        return True
    return not (value.startswith("/") or Path(value).is_absolute())


def _format_command(command: list) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)
