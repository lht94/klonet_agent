"""Schema shared by grounded context rendering and planner validation."""

from __future__ import annotations


REQUIRED_ACTION_ARGS = {
    "restart_screen_component": ("platform", "component", "screen_session", "project_root"),
    "start_screen_component": ("platform", "component", "screen_session", "project_root"),
    "stop_screen_component": ("screen_session",),
    "stop_platform_screens": (
        "platform", "project_root", "component_contracts", "run_as_uid",
    ),
    "start_platform_screens": ("platform", "project_root"),
    "validate_project_files": ("project_root",),
    "prepare_project_files": ("project_root",),
    "extract_archive": ("archive_path", "destination_dir"),
    "run_install_script": ("script_dir", "script_name"),
    "ensure_shared_services": ("script_dir",),
    "write_ops_file": ("path", "content"),
    "replace_text_in_file": ("path", "old_text", "new_text"),
    "insert_text_before_anchor": ("path", "anchor", "content"),
    "edit_text_file": ("path", "operation", "content"),
    "upsert_python_class": ("path", "class_name", "body"),
    "set_python_config_assignment": ("path", "class_name"),
    "set_python_class_attribute": ("path", "attribute", "value"),
    "install_nginx_config": ("config_name",),
    "start_docker_container": ("name",),
    "ensure_user_group": ("user", "group"),
    "remove_python_package_entries": ("site_packages_dir", "package", "entries"),
    "run_ops_command": ("program", "argv"),
    "copy_files": ("sources", "destination"),
    "move_path": ("source", "destination"),
    "create_directory": ("path",),
    "remove_path": ("path",),
    "manage_service": ("service", "operation"),
    "manage_process": ("pid", "signal"),
    "stop_klonet_component": ("runtime_cwd", "component", "pid", "port"),
    "stop_klonet_runtime_instance": ("runtime_cwd", "ports"),
    "manage_container": ("name", "operation"),
    "install_system_packages": ("packages",),
    "install_python_packages": ("python_executable", "operation", "packages"),
    "manage_file_permissions": ("path",),
    "git_operation": ("operation", "repository"),
    "sync_directory": ("source", "destination"),
    "merge_json_file": ("path", "patch"),
    "start_redis_instance": ("binary", "config_path", "expected_port"),
    "ensure_klonet_redis_instance": ("project_root",),
    "create_docker_container": ("name", "image", "port_bindings"),
    "repair_klonet_active_master_ip": ("project_root",),
    "run_reviewed_script": ("script_path", "cwd", "sha256"),
    "manage_libvirt_domain": ("domain", "operation"),
    "manage_docker_network": ("network", "operation"),
    "manage_docker_image": ("operation",),
    "manage_network_link": ("name", "operation"),
    "manage_ovs_resource": ("resource_type", "name", "operation"),
}


PROCESS_SIGNAL_ALIASES = {
    "1": "HUP",
    "2": "INT",
    "15": "TERM",
    "TERM": "TERM",
    "SIGTERM": "TERM",
    "KILL": "KILL",
    "SIGKILL": "KILL",
    "HUP": "HUP",
    "SIGHUP": "HUP",
    "INT": "INT",
    "SIGINT": "INT",
}


PROCESS_TERMINATING_SIGNALS = frozenset({"TERM", "KILL", "INT"})
SEMANTIC_RISK_LEVELS = ("readonly", "low", "medium", "high", "destructive")
SEMANTIC_RISK_ALIASES = {
    "normal": "readonly",
    "controlled": "medium",
    "privileged": "medium",
    "dangerous": "high",
}


def normalize_process_signal(value) -> str | None:
    """Return the bounded kill(1) signal name accepted by manage_process."""

    return PROCESS_SIGNAL_ALIASES.get(str(value or "").strip().upper())


def normalize_semantic_risk(value, default: str = "") -> str:
    """Translate legacy Action risk labels into the semantic-plan vocabulary."""

    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return SEMANTIC_RISK_ALIASES.get(normalized, normalized)
