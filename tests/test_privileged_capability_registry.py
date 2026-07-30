from __future__ import annotations


def test_domain_workflow_registry_covers_platform_lifecycle():
    from klonet_agent.ops.privileged.workflows import DEFAULT_DOMAIN_WORKFLOWS

    expected = {
        "deploy_platform",
        "start_platform",
        "stop_platform",
        "restart_platform",
        "restart_component",
        "configure_platform",
        "configure_nginx",
        "verify_platform",
        "diagnose_platform",
        "deploy_environment",
        "upgrade_platform_source",
        "rollback_platform",
        "acquire_platform_source",
        "install_base_environment",
        "recover_shared_services",
        "configure_docker_runtime",
        "recover_runtime_redis",
        "diagnose_worker_registration",
        "diagnose_topology_progress",
        "diagnose_kvm_network",
        "manage_kvm_runtime",
        "diagnose_onos",
        "cleanup_orphan_resources",
    }

    assert expected <= {spec.name for spec in DEFAULT_DOMAIN_WORKFLOWS.describe()}
    for name in expected:
        spec = DEFAULT_DOMAIN_WORKFLOWS.get(name)
        assert spec is not None
        assert spec.phases
        assert spec.required_facts
        assert spec.success_evidence


def test_direct_action_specs_publish_machine_readable_contracts():
    from klonet_agent.ops.actions import DEFAULT_OPS_ACTION_REGISTRY
    from klonet_agent.ops.privileged.action_runner import DIRECT_PRIVILEGED_ACTIONS

    expected = {
        "replace_text_in_file",
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
        "extract_archive",
        "run_install_script",
        "ensure_shared_services",
        "start_screen_component",
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

    assert expected <= DIRECT_PRIVILEGED_ACTIONS
    for name in expected:
        spec = DEFAULT_OPS_ACTION_REGISTRY.get(name)
        assert spec is not None
        assert spec.category
        assert spec.description
        assert spec.preconditions
        assert spec.effects
        assert spec.postconditions
        assert "ops-privilege" in spec.backends


def test_every_direct_privileged_action_has_spec_handler_and_schema():
    from klonet_agent.ops.actions import DEFAULT_OPS_ACTION_REGISTRY
    from klonet_agent.ops.privileged.action_runner import (
        DIRECT_PRIVILEGED_ACTIONS,
        DirectPrivilegedActionRunner,
    )
    from klonet_agent.ops.privileged.planner_schema import REQUIRED_ACTION_ARGS

    runner = DirectPrivilegedActionRunner()
    for name in DIRECT_PRIVILEGED_ACTIONS:
        spec = DEFAULT_OPS_ACTION_REGISTRY.get(name)
        assert spec is not None, name
        assert hasattr(runner, "_action_" + name), name
        assert name in REQUIRED_ACTION_ARGS or not spec.path_args, name


def test_every_workflow_preferred_action_is_directly_executable():
    from klonet_agent.ops.actions import DEFAULT_OPS_ACTION_REGISTRY
    from klonet_agent.ops.privileged.action_runner import DIRECT_PRIVILEGED_ACTIONS
    from klonet_agent.ops.privileged.workflows import DEFAULT_DOMAIN_WORKFLOWS

    for workflow in DEFAULT_DOMAIN_WORKFLOWS.describe():
        for action in workflow.preferred_actions:
            assert DEFAULT_OPS_ACTION_REGISTRY.get(action) is not None, (
                workflow.name,
                action,
            )
            assert action in DIRECT_PRIVILEGED_ACTIONS, (
                workflow.name,
                action,
            )


def test_common_knowledge_domains_have_probe_action_and_checker_coverage():
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
    from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES

    probes = {item.name for item in DEFAULT_READONLY_PROBES.describe()}
    checkers = set(DefaultCheckerRegistry().names)

    assert {
        "project_layout",
        "klonet_config_consistency",
        "service",
        "ports",
        "process",
        "screen",
        "docker",
        "docker_images",
        "docker_networks",
        "nginx",
        "redis",
        "git_repository",
        "virtualization",
        "libvirt",
        "ovs",
        "network_links",
        "file_integrity",
        "archive_inventory",
    } <= probes
    assert {
        "file_exists",
        "file_absent",
        "service_active",
        "service_inactive",
        "process_pid_absent",
        "port_listening",
        "screen_session_exists",
        "container_running",
        "container_restart_policy",
        "docker_image_state",
        "docker_network_state",
        "network_link_state",
        "libvirt_domain_state",
        "ovs_resource_state",
        "nginx_config_valid",
        "git_revision",
    } <= checkers


def test_ordinary_ops_runner_refuses_privilege_only_actions():
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledActionRunner

    result = ControlledActionRunner()(
        OperationPlan("p1", "restart_platform", "nginx", "restart"),
        OperationStep(
            "s1",
            "restart nginx",
            "test backend separation",
            action="manage_service",
            args={"service": "nginx", "operation": "restart"},
        ),
    )

    assert result.status == "blocked"
    assert "backend" in result.output


def test_grounded_context_exposes_workflows_probes_and_action_contracts(tmp_path):
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    context = PrivilegedPlanContextBuilder(
        knowledge_search=lambda *_args, **_kwargs: "可靠 Klonet 部署知识",
        environment_inspector=lambda _args: "readonly environment",
    ).build(
        "部署并检查平台",
    )
    rendered = context.render()

    assert "workflow=deploy_platform" in rendered
    assert "python_import:" in rendered
    assert "action=manage_service" in rendered
    assert "postconditions=" in rendered
