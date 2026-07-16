"""Central allowlist for structured Ops actions.

The model selects an action and supplies structured arguments. It never
supplies the shell command that implements the action.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class OpsActionSpec:
    name: str
    handler_name: str
    risk: str = "normal"
    requires_confirmation: bool = False
    path_args: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    confirmation_scope: str = "plan"


class OpsActionRegistry:
    """Resolve and validate model-selected operations before dispatch."""

    def __init__(
        self,
        specs: Iterable[OpsActionSpec],
        *,
        allowed_path_roots: Iterable[str | Path] | None = None,
    ):
        self._specs = {spec.name: spec for spec in specs}
        self._aliases = {
            alias: spec.name
            for spec in self._specs.values()
            for alias in spec.aliases
        }
        self._allowed_path_roots = tuple(
            _normalized_absolute_path_text(str(root))
            for root in (allowed_path_roots or ())
        )

    def get(self, action: str) -> OpsActionSpec | None:
        normalized = str(action or "").strip()
        canonical = self._aliases.get(normalized, normalized)
        return self._specs.get(canonical)

    def canonical_name(self, action: str) -> str:
        spec = self.get(action)
        return spec.name if spec else ""

    def require(self, action: str) -> OpsActionSpec:
        spec = self.get(action)
        if spec is None:
            raise ValueError(f"action_not_allowlisted={action or 'missing'}")
        return spec

    def validate_args(self, spec: OpsActionSpec, args: Mapping | None) -> str:
        values = args if isinstance(args, Mapping) else {}
        for field in spec.path_args:
            raw_value = str(values.get(field) or "").strip()
            if not raw_value:
                continue
            if any(char in raw_value for char in ("\x00", "\n", "\r")):
                return f"invalid_path_arg={field}"
            resolved = _normalized_absolute_path_text(raw_value)
            if not resolved:
                return f"invalid_path_arg={field}"
            if self._allowed_path_roots and not any(
                _path_text_is_relative_to(resolved, root)
                for root in self._allowed_path_roots
            ):
                return f"path_not_allowlisted={field}"
        return ""

    def describe(self) -> tuple[OpsActionSpec, ...]:
        return tuple(self._specs.values())


def _normalized_absolute_path_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    expanded = str(Path(raw).expanduser())
    if raw.startswith("/"):
        expanded = raw
    elif not Path(expanded).is_absolute():
        return ""
    return expanded.replace("\\", "/").rstrip("/") or "/"


def _path_text_is_relative_to(path: str, root: str) -> bool:
    if not path or not root:
        return False
    return path == root or path.startswith(root.rstrip("/") + "/")


DEFAULT_OPS_ACTIONS = (
    OpsActionSpec("manual_checkpoint", "_manual_checkpoint"),
    OpsActionSpec(
        "restart_screen_component",
        "_restart_screen_component",
        "privileged",
        True,
        ("project_root",),
        ("restart_component",),
    ),
    OpsActionSpec(
        "start_screen_component",
        "_start_screen_component",
        "privileged",
        True,
        ("project_root",),
        ("start_component",),
    ),
    OpsActionSpec(
        "stop_screen_component",
        "_stop_screen_component",
        "dangerous",
        True,
        aliases=("stop_component",),
        confirmation_scope="step",
    ),
    OpsActionSpec(
        "stop_platform_screens",
        "_stop_platform_screens",
        "dangerous",
        True,
        aliases=("stop_platform",),
        confirmation_scope="step",
    ),
    OpsActionSpec(
        "start_platform_screens",
        "_start_platform_screens",
        "privileged",
        True,
        ("project_root",),
        ("start_platform",),
    ),
    OpsActionSpec(
        "validate_project_files",
        "_validate_project_files",
        path_args=("project_root",),
    ),
    OpsActionSpec(
        "prepare_project_files",
        "_prepare_project_files",
        "privileged",
        True,
        ("project_root",),
    ),
    OpsActionSpec(
        "extract_archive",
        "_extract_archive",
        "privileged",
        True,
        ("archive_path", "destination_dir"),
    ),
    OpsActionSpec(
        "run_install_script",
        "_run_install_script",
        "privileged",
        True,
        ("script_dir",),
    ),
    OpsActionSpec(
        "ensure_shared_services",
        "_ensure_shared_services",
        "privileged",
        True,
        ("script_dir",),
    ),
    OpsActionSpec(
        "write_ops_file",
        "_write_ops_file",
        "privileged",
        True,
        ("path",),
        ("write_file",),
    ),
    OpsActionSpec(
        "install_nginx_config",
        "_install_nginx_config",
        "privileged",
        True,
    ),
    OpsActionSpec("reload_nginx", "_reload_nginx", "privileged", True),
    OpsActionSpec(
        "start_docker_container",
        "_start_docker_container",
        "privileged",
        True,
    ),
    OpsActionSpec(
        "ensure_user_group",
        "_ensure_user_group",
        "dangerous",
        True,
        aliases=("ensure-user-group", "add_user_to_group", "add-user-to-group"),
        confirmation_scope="step",
    ),
    OpsActionSpec(
        "remove_python_package_entries",
        "_remove_python_package_entries",
        "dangerous",
        True,
        ("site_packages_dir",),
        aliases=(
            "remove-python-package-entries",
            "cleanup_python_package_entries",
            "cleanup-python-package-entries",
        ),
        confirmation_scope="step",
    ),
    OpsActionSpec(
        "run_ops_command",
        "_run_ops_command",
        "controlled",
        True,
        ("cwd",),
    ),
)

DEFAULT_OPS_ACTION_REGISTRY = OpsActionRegistry(DEFAULT_OPS_ACTIONS)


def configured_ops_action_registry() -> OpsActionRegistry:
    """Build the registry with optional production path roots from the environment."""

    raw_roots = os.getenv("KLONET_AGENT_OPS_ALLOWED_ROOTS", "").strip()
    roots = [item for item in raw_roots.split(os.pathsep) if item] if raw_roots else None
    return OpsActionRegistry(DEFAULT_OPS_ACTIONS, allowed_path_roots=roots)


def default_action_bindings(
    operation: str,
    target: str,
    operation_args: Mapping | None,
) -> tuple[dict[str, dict], bool]:
    """Return deterministic step bindings and whether a shared-service step is needed."""

    args = operation_args if isinstance(operation_args, Mapping) else {}
    bindings: dict[str, dict] = {}
    add_shared_services = False
    if operation == "restart_platform" and target:
        project_root = _text(args.get("project_root"))
        if project_root:
            for step_id, component, suffix in (
                ("restart-master", "master", "m"),
                ("restart-worker", "worker", "w"),
                ("restart-celery", "celery", "c"),
                ("restart-web-terminal", "web_terminal", "web"),
            ):
                bindings[step_id] = _binding(
                    "restart_screen_component",
                    platform=target,
                    component=component,
                    screen_session=f"{target}_{suffix}",
                    project_root=project_root,
                )
    elif operation == "deploy_platform" and target:
        project_root = _text(args.get("project_root"))
        archive_path = _text(args.get("archive_path"))
        destination_dir = _text(args.get("destination_dir"))
        script_dir = _text(args.get("script_dir"))
        script_name = _text(args.get("script_name"))
        script_args = _text(args.get("script_args"))
        if project_root:
            bindings["precheck"] = _binding(
                "validate_project_files", project_root=project_root
            )
            bindings["prepare-files"] = _binding(
                "prepare_project_files", project_root=project_root
            )
            bindings["start-services"] = _binding(
                "start_platform_screens",
                platform=target,
                project_root=project_root,
            )
            if not _truthy(args.get("skip_shared_services")):
                shared_dir = _text(
                    args.get("shared_services_script_dir")
                    or args.get("docker_service_script_dir")
                )
                if not shared_dir and script_name == "docker_service.sh":
                    shared_dir = script_dir
                bindings["start-shared-services"] = _binding(
                    "ensure_shared_services",
                    script_dir=shared_dir or "/root/vemu_install_new_gen",
                )
                add_shared_services = True
        elif archive_path and destination_dir:
            bindings["prepare-files"] = _binding(
                "extract_archive",
                archive_path=archive_path,
                destination_dir=destination_dir,
            )
        elif script_dir and script_name:
            action_args = {"script_dir": script_dir, "script_name": script_name}
            if script_args:
                action_args["script_args"] = script_args
            bindings["prepare-files"] = {
                "action": "run_install_script",
                "args": action_args,
            }
    elif operation == "destroy_platform" and target:
        bindings["stop-services"] = _binding(
            "stop_platform_screens", platform=target
        )
    return bindings, add_shared_services


def _binding(action: str, **args: str) -> dict:
    return {"action": action, "args": {key: value for key, value in args.items() if value}}


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
