from __future__ import annotations


def test_operational_experience_is_a_rag_runbook_not_a_workflow_registry():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runbook = root / "knowledge" / "klonet" / "ops" / "agentic_operations_runbook.md"
    text = runbook.read_text(encoding="utf-8")

    assert "不定义必须执行的工作流" in text
    assert "Failure Packet" in text
    assert "一次性 Shell" in text
    assert not (root / "ops" / "privileged" / "workflows.py").exists()


def test_direct_action_specs_publish_machine_readable_contracts():
    from klonet_agent.ops.actions import DEFAULT_OPS_ACTION_REGISTRY
    from klonet_agent.ops.privileged.action_runner import DIRECT_PRIVILEGED_ACTIONS

    expected = {
        "replace_text_in_file",
        "insert_text_before_anchor",
        "edit_text_file",
        "copy_files",
        "move_path",
        "create_directory",
        "remove_path",
        "manage_service",
        "manage_process",
        "manage_container",
        "create_docker_container",
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


def test_shared_action_contracts_do_not_import_runtime_registries():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "ops"
        / "privileged"
        / "action_contracts.py"
    ).read_text(encoding="utf-8")

    assert "DEFAULT_DOMAIN_WORKFLOWS" not in source
    assert "configured_ops_action_registry" not in source


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


def test_grounded_context_exposes_runbook_evidence_probes_and_capability_summary(tmp_path):
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    context = PrivilegedPlanContextBuilder(
        knowledge_search=lambda *_args, **_kwargs: "可靠 Klonet 部署知识",
        environment_inspector=lambda _args: "readonly environment",
    ).build(
        "部署并检查平台",
    )
    rendered = context.render()

    assert "可靠 Klonet 部署知识" in rendered
    assert "python_import:" in rendered
    assert "Available execution capability summary" in rendered
    assert "action=manage_service" not in rendered
    assert "category=" in rendered
    assert "postconditions=" in rendered
