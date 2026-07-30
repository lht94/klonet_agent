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
    category: str = "general"
    description: str = ""
    readonly: bool = False
    preconditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    backends: tuple[str, ...] = ("ops", "ops-privilege")


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
        category="platform_runtime",
        description="启动一个明确的 Klonet screen 组件",
        preconditions=("project_layout_valid", "screen_session_absent"),
        effects=("component_process_started",),
        postconditions=("screen_session_exists", "component_port_listening"),
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
        ("project_root", "source_root"),
    ),
    OpsActionSpec(
        "extract_archive",
        "_extract_archive",
        "privileged",
        True,
        ("archive_path", "destination_dir"),
        category="environment",
        description="校验路径穿越、链接和设备节点后解压安装包",
        preconditions=("archive_exists", "members_are_safe"),
        effects=("archive_extracted",),
        postconditions=("destination_exists",),
    ),
    OpsActionSpec(
        "run_install_script",
        "_run_install_script",
        "dangerous",
        True,
        ("script_dir",),
        confirmation_scope="step",
        category="environment",
        description="执行知识库允许的标准 Klonet 环境安装脚本及固定参数",
        preconditions=("script_allowlisted", "arguments_exactly_match"),
        effects=("base_environment_changed",),
        postconditions=("script_exit_zero", "dependencies_rechecked"),
    ),
    OpsActionSpec(
        "ensure_shared_services",
        "_ensure_shared_services",
        "privileged",
        True,
        ("script_dir",),
        category="environment",
        description="运行当前安装包的 docker_service.sh 准备共享基础服务",
        preconditions=("docker_service_script_exists",),
        effects=("shared_service_containers_changed",),
        postconditions=("required_containers_running",),
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
        "replace_text_in_file",
        "_replace_text_in_file",
        "controlled",
        True,
        ("path",),
        category="filesystem",
        description="在明确文本配置文件中精确替换唯一匹配内容，并保留备份",
        preconditions=(
            "target_exists",
            "old_text_matches_exactly_once",
            "content_is_not_sensitive",
        ),
        effects=("target_content_changed", "backup_created"),
        postconditions=("new_text_present", "old_text_absent"),
        backends=("ops-privilege",),
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
        category="identity",
        description="把明确用户加入明确系统组",
        preconditions=("user_exists", "group_exists"),
        effects=("supplementary_group_changed",),
        postconditions=("group_membership_present",),
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
        category="package",
        description="删除明确 site-packages 下已证明属于目标包的条目",
        preconditions=("entries_are_direct_children", "package_ownership_matches"),
        effects=("package_entries_removed",),
        postconditions=("entries_absent",),
    ),
    OpsActionSpec(
        "run_ops_command",
        "_run_ops_command",
        "controlled",
        True,
        ("cwd",),
        ("run_controlled_argv",),
    ),
    OpsActionSpec(
        "copy_files",
        "_copy_files",
        "controlled",
        True,
        ("destination",),
        category="filesystem",
        description="复制一组明确文件到目标目录",
        preconditions=("sources_exist", "destination_parent_exists"),
        effects=("destination_files_updated",),
        postconditions=("destination_files_exist",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "move_path",
        "_move_path",
        "controlled",
        True,
        ("source", "destination"),
        category="filesystem",
        description="移动一个明确文件或目录",
        preconditions=("source_exists",),
        effects=("source_relocated",),
        postconditions=("destination_exists", "source_absent"),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "create_directory",
        "_create_directory",
        "controlled",
        True,
        ("path",),
        category="filesystem",
        description="创建明确目录",
        preconditions=("target_resolved", "target_not_protected_root"),
        effects=("directory_created",),
        postconditions=("directory_exists",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "remove_path",
        "_remove_path",
        "dangerous",
        True,
        ("path",),
        confirmation_scope="step",
        category="filesystem",
        description="删除明确文件或空目录；递归删除单独标记高风险",
        preconditions=("target_resolved", "target_not_protected_root"),
        effects=("target_removed",),
        postconditions=("target_absent",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_service",
        "_manage_service",
        "privileged",
        True,
        category="service",
        description="查询之外的 systemd 服务启停、重启、重载或启禁用",
        preconditions=("service_name_valid",),
        effects=("service_state_changed",),
        postconditions=("requested_service_state_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_process",
        "_manage_process",
        "dangerous",
        True,
        confirmation_scope="step",
        category="process",
        description="向明确 PID 发送允许的信号",
        preconditions=("pid_exists", "pid_identity_observed"),
        effects=("process_signalled",),
        postconditions=("process_state_rechecked",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_container",
        "_manage_container",
        "privileged",
        True,
        category="container",
        description="启动、停止、重启或删除明确 Docker 容器",
        preconditions=("container_name_valid",),
        effects=("container_state_changed",),
        postconditions=("requested_container_state_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "install_system_packages",
        "_install_system_packages",
        "dangerous",
        True,
        confirmation_scope="step",
        category="package",
        description="通过 apt 安装明确包名",
        preconditions=("package_names_valid",),
        effects=("system_packages_changed",),
        postconditions=("packages_installed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "install_python_packages",
        "_install_python_packages",
        "dangerous",
        True,
        confirmation_scope="step",
        category="package",
        description="通过明确 Python 解释器安装或卸载明确包",
        preconditions=("python_executable_exists", "package_names_valid"),
        effects=("python_environment_changed",),
        postconditions=("package_state_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_file_permissions",
        "_manage_file_permissions",
        "dangerous",
        True,
        ("path",),
        confirmation_scope="step",
        category="filesystem",
        description="修改明确路径的 mode 或 owner/group",
        preconditions=("target_exists", "identity_values_valid"),
        effects=("permissions_or_owner_changed",),
        postconditions=("requested_metadata_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "git_operation",
        "_git_operation",
        "dangerous",
        True,
        ("repository",),
        confirmation_scope="step",
        category="source",
        description="在明确仓库执行结构化 Git 操作",
        preconditions=("repository_or_parent_exists",),
        effects=("repository_state_may_change",),
        postconditions=("git_state_rechecked",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "sync_directory",
        "_sync_directory",
        "controlled",
        True,
        ("source", "destination"),
        category="source",
        description="把明确的本地已验证源码目录同步到目标目录",
        preconditions=("source_directory_exists", "destination_not_protected"),
        effects=("destination_tree_updated",),
        postconditions=("destination_tree_exists",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "merge_json_file",
        "_merge_json_file",
        "dangerous",
        True,
        ("path",),
        confirmation_scope="step",
        category="configuration",
        description="深度合并 JSON 对象，修改前备份并在写入后重新解析校验",
        preconditions=("target_is_json_object", "patch_is_json_object"),
        effects=("json_configuration_changed", "backup_created"),
        postconditions=("json_valid", "requested_keys_present"),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "start_redis_instance",
        "_start_redis_instance",
        "privileged",
        True,
        ("config_path", "binary"),
        category="service",
        description="从明确 redis.conf 启动一个独立 Redis 实例并验证配置端口",
        preconditions=("redis_config_exists", "configured_port_not_listening"),
        effects=("redis_process_started",),
        postconditions=("configured_redis_port_listening",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "run_reviewed_script",
        "_run_reviewed_script",
        "dangerous",
        True,
        ("script_path", "cwd"),
        confirmation_scope="step",
        category="environment",
        description="执行内容哈希已在计划中固定的本地维护脚本",
        preconditions=("script_exists", "sha256_matches", "arguments_are_bounded"),
        effects=("script_declared_environment_changes",),
        postconditions=("script_exit_zero", "workflow_specific_checks_pass"),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_libvirt_domain",
        "_manage_libvirt_domain",
        "dangerous",
        True,
        confirmation_scope="step",
        category="virtualization",
        description="启动、正常关机、重启或强制处理一个明确 libvirt domain",
        preconditions=("domain_exists", "domain_identity_confirmed"),
        effects=("domain_state_changed",),
        postconditions=("requested_domain_state_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_docker_network",
        "_manage_docker_network",
        "dangerous",
        True,
        confirmation_scope="step",
        category="network",
        description="创建、删除或连接一个明确 Docker 网络",
        preconditions=("network_and_targets_explicit",),
        effects=("docker_network_state_changed",),
        postconditions=("requested_network_state_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_docker_image",
        "_manage_docker_image",
        "dangerous",
        True,
        confirmation_scope="step",
        category="container",
        description="从明确归档加载、标记或删除一个 Docker 镜像",
        preconditions=("image_or_archive_explicit", "destructive_scope_confirmed"),
        effects=("docker_image_state_changed",),
        postconditions=("requested_image_state_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_network_link",
        "_manage_network_link",
        "dangerous",
        True,
        confirmation_scope="step",
        category="network",
        description="启停或删除一个身份明确的宿主机网络链路",
        preconditions=("link_exists", "link_identity_confirmed"),
        effects=("host_network_link_state_changed",),
        postconditions=("requested_link_state_observed",),
        backends=("ops-privilege",),
    ),
    OpsActionSpec(
        "manage_ovs_resource",
        "_manage_ovs_resource",
        "dangerous",
        True,
        confirmation_scope="step",
        category="network",
        description="创建或删除明确 OVS bridge/port",
        preconditions=("resource_name_valid", "ownership_evidence_available"),
        effects=("ovs_topology_changed",),
        postconditions=("requested_ovs_state_observed",),
        backends=("ops-privilege",),
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
