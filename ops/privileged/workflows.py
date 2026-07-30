"""Domain workflow registry for privileged Klonet operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainWorkflowSpec:
    name: str
    description: str
    phases: tuple[str, ...]
    required_facts: tuple[str, ...]
    preferred_actions: tuple[str, ...]
    success_evidence: tuple[str, ...]


class DomainWorkflowRegistry:
    def __init__(self, specs: tuple[DomainWorkflowSpec, ...]) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def get(self, name: str) -> DomainWorkflowSpec | None:
        return self._specs.get(str(name or "").strip())

    def describe(self) -> tuple[DomainWorkflowSpec, ...]:
        return tuple(self._specs.values())

    def render(self) -> str:
        lines = []
        for spec in self.describe():
            lines.append(
                "- workflow=%s description=%s phases=%s required_facts=%s "
                "preferred_actions=%s success_evidence=%s"
                % (
                    spec.name,
                    spec.description,
                    ",".join(spec.phases),
                    ",".join(spec.required_facts),
                    ",".join(spec.preferred_actions),
                    ",".join(spec.success_evidence),
                )
            )
        return "\n".join(lines)


DEFAULT_DOMAIN_WORKFLOWS = DomainWorkflowRegistry(
    (
        DomainWorkflowSpec(
            "deploy_platform",
            "创建并启动一个 Klonet 平台实例",
            ("discover", "prepare", "configure", "start", "verify"),
            (
                "project_layout",
                "python_runtime",
                "platform_instances",
                "ports",
                "shared_services",
            ),
            (
                "validate_project_files",
                "prepare_project_files",
                "write_ops_file",
                "start_platform_screens",
            ),
            ("screen_sessions", "processes", "listening_ports", "health_endpoint"),
        ),
        DomainWorkflowSpec(
            "start_platform",
            "启动已配置的 Klonet 平台",
            ("discover", "preflight", "start", "verify"),
            ("project_layout", "python_runtime", "ports", "screen_sessions"),
            (
                "validate_project_files",
                "prepare_project_files",
                "start_platform_screens",
            ),
            ("screen_sessions", "listening_ports", "process_cwd"),
        ),
        DomainWorkflowSpec(
            "stop_platform",
            "停止一个明确 Klonet 平台",
            ("discover", "stop", "verify"),
            ("platform_instances", "screen_sessions", "processes"),
            ("stop_platform_screens",),
            ("screen_sessions_absent", "platform_processes_absent"),
        ),
        DomainWorkflowSpec(
            "restart_platform",
            "重启一个明确 Klonet 平台并验收",
            ("discover", "stop", "prepare", "start", "verify"),
            ("project_layout", "platform_instances", "ports", "python_runtime"),
            (
                "stop_platform_screens",
                "prepare_project_files",
                "start_platform_screens",
            ),
            ("new_processes", "screen_sessions", "listening_ports"),
        ),
        DomainWorkflowSpec(
            "restart_component",
            "只重启一个 Klonet 组件",
            ("discover", "restart", "verify"),
            ("component_process", "screen_session", "project_layout"),
            ("restart_screen_component",),
            ("component_process", "component_port"),
        ),
        DomainWorkflowSpec(
            "configure_platform",
            "更新后端、前端或共享服务配置",
            ("discover", "render", "write", "validate", "activate", "verify"),
            ("project_layout", "ports", "service_endpoints"),
            (
                "replace_text_in_file",
                "write_ops_file",
                "run_ops_command",
            ),
            ("config_parse", "runtime_health"),
        ),
        DomainWorkflowSpec(
            "configure_nginx",
            "安装或更新 Nginx 路由并安全重载",
            ("discover", "render", "install", "syntax_check", "reload", "verify"),
            ("nginx_paths", "ports", "routes"),
            ("install_nginx_config", "reload_nginx"),
            ("nginx_syntax", "route_response"),
        ),
        DomainWorkflowSpec(
            "verify_platform",
            "只读验收 Klonet 运行状态",
            ("inspect", "probe", "summarize"),
            ("project_layout", "processes", "ports", "routes"),
            (),
            ("screen_sessions", "processes", "ports", "http_health"),
        ),
        DomainWorkflowSpec(
            "diagnose_platform",
            "基于证据诊断平台故障",
            ("collect", "hypothesize", "probe", "conclude"),
            ("failure_evidence", "environment_facts"),
            (),
            ("confirmed_cause_or_remaining_uncertainty",),
        ),
        DomainWorkflowSpec(
            "deploy_environment",
            "安装或修复 Klonet 所需 Ubuntu 基础环境",
            ("inventory", "packages", "services", "verify"),
            ("os", "disk", "python_runtime", "services", "virtualization"),
            (
                "install_system_packages",
                "install_python_packages",
                "manage_service",
                "manage_container",
            ),
            ("package_state", "service_state", "tool_versions"),
        ),
        DomainWorkflowSpec(
            "upgrade_platform_source",
            "更新平台源码并重新验收",
            ("inspect_git", "fetch", "checkout_or_pull", "prepare", "restart", "verify"),
            ("git_repository", "project_layout", "runtime_state"),
            ("git_operation", "prepare_project_files"),
            ("git_revision", "runtime_health"),
        ),
        DomainWorkflowSpec(
            "rollback_platform",
            "回退到已知源码或配置版本",
            ("inspect", "select_revision", "apply", "restart", "verify"),
            ("git_repository", "previous_revision", "runtime_state"),
            (
                "git_operation",
                "replace_text_in_file",
                "write_ops_file",
                "prepare_project_files",
            ),
            ("expected_revision", "runtime_health"),
        ),
        DomainWorkflowSpec(
            "acquire_platform_source",
            "从明确 Git 仓库、归档或本地验证副本获取平台源码",
            ("discover", "acquire", "verify_layout", "record_revision"),
            ("git_repository_or_source", "destination", "disk"),
            ("git_operation", "extract_archive", "sync_directory"),
            ("project_layout", "git_revision_or_file_integrity"),
        ),
        DomainWorkflowSpec(
            "install_base_environment",
            "按当前安装包运行 Klonet 标准基础环境脚本",
            ("inspect_bundle", "extract", "review_scripts", "install", "verify"),
            ("archive_inventory", "install_scripts", "disk", "os"),
            (
                "extract_archive",
                "run_install_script",
                "run_reviewed_script",
            ),
            ("commands_available", "services", "virtualization"),
        ),
        DomainWorkflowSpec(
            "recover_shared_services",
            "恢复 Docker、MySQL、Celery Redis、RabbitMQ 和镜像仓库",
            ("inspect", "logs", "start_missing", "verify"),
            ("service", "docker", "ports", "logs"),
            (
                "manage_service",
                "manage_container",
                "manage_docker_image",
                "ensure_shared_services",
            ),
            ("container_running", "service_ports"),
        ),
        DomainWorkflowSpec(
            "configure_docker_runtime",
            "合并 Docker daemon 配置并安全重启验收",
            ("inspect", "backup_merge", "validate", "restart", "verify"),
            ("json_file", "docker", "docker_networks"),
            ("merge_json_file", "manage_service"),
            ("json_valid", "docker_info", "containers_recovered"),
        ),
        DomainWorkflowSpec(
            "recover_runtime_redis",
            "按当前 redis.conf 和项目配置恢复 Klonet 独立运行态 Redis",
            ("discover_config", "compare_ports", "start", "verify"),
            (
                "klonet_config_consistency",
                "ops_file",
                "ports",
                "process",
            ),
            ("start_redis_instance", "replace_text_in_file"),
            ("configured_port_listening", "platform_import_preflight"),
        ),
        DomainWorkflowSpec(
            "diagnose_worker_registration",
            "定位 Worker 监听、双向连通、注册、心跳和 Redis 状态断点",
            ("inspect_worker", "probe_both_directions", "inspect_state", "conclude"),
            ("platform_health", "ports", "http_endpoint", "network", "redis"),
            ("replace_text_in_file", "restart_screen_component"),
            ("worker_health", "registration_stable", "heartbeat_observed"),
        ),
        DomainWorkflowSpec(
            "diagnose_topology_progress",
            "按 Celery、Redis 进度、Worker 分发和真实资源定位拓扑卡点",
            ("identify_task", "inspect_celery", "inspect_workers", "compare_resources", "conclude"),
            ("logs", "redis", "platform_health", "docker", "ovs", "libvirt"),
            (),
            ("last_successful_stage", "first_failed_boundary"),
        ),
        DomainWorkflowSpec(
            "diagnose_kvm_network",
            "按 domain、tap、bridge/OVS、宿主链路和虚机网络栈诊断",
            ("inspect_domain", "inspect_links", "inspect_ovs", "compare_state", "conclude"),
            ("virtualization", "libvirt", "network_links", "ovs", "disk"),
            (),
            ("fault_layer_identified", "database_and_resource_state_compared"),
        ),
        DomainWorkflowSpec(
            "manage_kvm_runtime",
            "管理明确 Klonet libvirt domain 或经审查的初始化脚本",
            ("prove_ownership", "change", "verify"),
            ("libvirt", "file_integrity", "platform_state"),
            ("manage_libvirt_domain", "run_reviewed_script"),
            ("domain_state", "network_state"),
        ),
        DomainWorkflowSpec(
            "diagnose_onos",
            "按容器、Docker 网络、端口、应用和设备连接诊断 ONOS",
            ("inspect_container", "inspect_network", "probe_ports", "inspect_logs", "conclude"),
            ("docker", "docker_networks", "ports", "logs", "ovs"),
            (
                "manage_container",
                "manage_docker_image",
                "manage_docker_network",
            ),
            ("container_running", "management_port", "control_connection"),
        ),
        DomainWorkflowSpec(
            "cleanup_orphan_resources",
            "只清理已有归属证据的容器、domain、OVS、文件或进程",
            ("inventory", "prove_ownership", "remove_one_layer", "verify_absence"),
            ("docker", "libvirt", "ovs", "network_links", "process", "path_permissions"),
            (
                "manage_container",
                "manage_libvirt_domain",
                "manage_ovs_resource",
                "manage_network_link",
                "manage_process",
                "remove_path",
            ),
            ("target_absent", "unrelated_resources_unchanged"),
        ),
    )
)
