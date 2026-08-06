from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "kwargs": kwargs}
        )
        content = self.responses.pop(0)
        if tools:
            function_name = tools[0]["function"]["name"]
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name=function_name,
                            arguments=content,
                        )
                    )
                ],
            )
        else:
            message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=message
                )
            ]
        )


def _semantic_payload(*, objective="inspect the current platform state"):
    return json.dumps(
        {
            "status": "ready",
            "goal": "inspect platform",
            "assumptions": [],
            "steps": [
                {
                    "step_id": "inspect",
                    "title": "检查平台",
                    "objective": objective,
                    "reason": "the current state is required before further work",
                    "evidence_refs": ["server facts"],
                    "depends_on": [],
                    "expected_effects": [],
                    "success_criteria": ["the current state is observed"],
                    "risk_suggestion": "readonly",
                }
            ],
        }
    )


def test_binding_agent_builds_and_binds_atomic_implementation_plan(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "lht"
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="prepare a new lht instance")])
    ).plan("prepare lht")
    binder = FakeLLM(
        [
            json.dumps(
                {
                    "status": "ready",
                    "reason": "creation and validation are separate operations",
                    "implementation_steps": [
                        {
                            "id": "create-root",
                            "title": "创建实例目录",
                            "objective": "create the lht instance root directory",
                            "reason": "the instance needs a root directory",
                            "depends_on": [],
                            "expected_changes": ["the directory exists"],
                            "success_criteria": ["the directory exists"],
                            "risk_suggestion": "low",
                        },
                        {
                            "id": "verify-root",
                            "title": "验证实例目录",
                            "objective": "verify the lht instance root directory exists",
                            "reason": "the created state must be observed",
                            "depends_on": ["create-root"],
                            "expected_changes": [
                                "the directory state is confirmed"
                            ],
                            "success_criteria": ["the directory exists"],
                            # The model may accidentally inherit the outer
                            # mutation risk. No expected state transition
                            # still makes this a deterministic verifier.
                            "risk_suggestion": "medium",
                        },
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "create_directory",
                    "selection_reason": "registered directory creation is atomic",
                    "resolved_from_evidence": [],
                    "probe_requests": [],
                    "reason": "",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "reason": "",
                    "args": {"path": str(target)},
                    "binding_reason": "create the requested root",
                    "resolved_from_evidence": [],
                    "preconditions": [],
                    "postconditions": [
                        {"checker": "file_exists", "args": {"path": str(target)}}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "verification_only",
                    "action": "",
                    "selection_reason": "only observation remains",
                    "resolved_from_evidence": [],
                    "probe_requests": [],
                    "reason": "",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "reason": "",
                    "binding_reason": "observe the requested state",
                    "resolved_from_evidence": [],
                    "postconditions": [
                        {"checker": "file_exists", "args": {"path": str(target)}}
                    ],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(binder).prepare_plan(
        plan,
        grounded_context=None,
    )

    semantic = bound.steps[0]
    assert semantic.execution_binding is None
    assert semantic.implementation_plan is not None
    micro_steps = semantic.implementation_plan.steps
    assert [item.execution_binding.kind for item in micro_steps] == [
        "registered_action",
        "verification_only",
    ]
    assert micro_steps[1].depends_on == ["inspect__create-root"]
    assert micro_steps[1].risk == "readonly"
    assert "planned predecessor inspect__create-root" in (
        micro_steps[1].evidence_refs[0]
    )
    restored = PrivilegedPlan.from_dict(bound.to_dict())
    assert restored.content_hash == bound.content_hash
    assert restored.steps[0].implementation_plan.steps[0].step_id == (
        "inspect__create-root"
    )


def test_structural_class_binding_extracts_a_single_full_class_wrapper():
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    compiled = _infer_structural_action_args(
        "upsert_python_class",
        {
            "path": "/future/config.py",
            "class_name": "LhtConfig",
            "body": (
                "class LhtConfig(CommonConfig):\n"
                "    master_port = 46001\n"
                "    worker_port = 46002\n"
            ),
        },
        [],
    )

    assert compiled["base_class"] == "CommonConfig"
    assert compiled["body"] == (
        "master_port = 46001\nworker_port = 46002"
    )


def test_structural_git_binding_normalizes_compound_clone_alias():
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    compiled = _infer_structural_action_args(
        "git_operation",
        {
            "operation": "clone+checkout",
            "repository": "/srv/v4e2e",
            "url": "gitee:example/platform.git",
            "ref": "develop",
            "revision": "a" * 40,
        },
        [],
    )

    assert compiled["operation"] == "clone_at_revision"


def test_config_assignment_action_cannot_masquerade_as_port_field_edit():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_objective_fit,
    )

    edit_ports = PrivilegedStep(
        step_id="ports",
        title="写入四个端口字段",
        objective="在 LhtConfig 类中设置 master 和 worker 端口",
        expected_changes=["类字段变更"],
        risk="medium",
    )
    activate = PrivilegedStep(
        step_id="activate",
        title="切换活动配置",
        objective="将 PROJ_CONFIG 切换为 LhtConfig",
        expected_changes=["活动配置变更"],
        risk="medium",
    )

    assert "objective_is_not_config_activation" in (
        _validate_action_objective_fit(
            "set_python_config_assignment",
            edit_ports,
        )
    )
    assert not _validate_action_objective_fit(
        "set_python_config_assignment",
        activate,
    )


def test_existing_container_actions_cannot_masquerade_as_container_creation():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_objective_fit,
    )

    step = PrivilegedStep(
        step_id="mysql",
        title="Create isolated MySQL container",
        objective="Create a new v4e2e-mysql container from mysql:latest",
        expected_changes=["A previously absent container is created and started"],
        risk="high",
    )

    assert "cannot_create_new_container" in _validate_action_objective_fit(
        "manage_container", step
    )
    assert "cannot_create_new_container" in _validate_action_objective_fit(
        "start_docker_container", step
    )
    assert not _validate_action_objective_fit("create_docker_container", step)


def test_new_container_contract_matches_semantic_name_service_and_credentials():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_contract_consistency,
    )

    step = PrivilegedStep(
        step_id="rabbit",
        title="Create isolated RabbitMQ container v4e2e-rabbitmq",
        objective="Create v4e2e-rabbitmq from the RabbitMQ image",
        expected_changes=["v4e2e-rabbitmq is running"],
        risk="high",
    )

    assert "container_name_mismatch" in _validate_action_contract_consistency(
        "create_docker_container",
        {"name": "v4e2e", "image": "rabbitmq:latest"},
        step,
    )
    assert "credential_source_not_allowed" in _validate_action_contract_consistency(
        "create_docker_container",
        {
            "name": "v4e2e-rabbitmq",
            "image": "rabbitmq:latest",
            "credential_source": {"service": "rabbitmq", "path": "/x/config.py"},
        },
        step,
    )
    assert not _validate_action_contract_consistency(
        "create_docker_container",
        {"name": "v4e2e-rabbitmq", "image": "rabbitmq:latest"},
        step,
    )


def test_new_container_creation_is_routed_to_creation_capability():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _forced_registered_action_for_step,
    )

    create = PrivilegedStep(
        step_id="mysql",
        title="Provision isolated MySQL container",
        objective="Create and start a new v4e2e-mysql Docker container",
        expected_changes=["A previously absent container is created"],
        risk="high",
    )
    existing = PrivilegedStep(
        step_id="restart",
        title="Restart existing MySQL container",
        objective="Restart the already present mysql-main container",
        expected_changes=["Existing container restarts"],
        risk="medium",
    )
    verify = PrivilegedStep(
        step_id="verify",
        title="Verify the newly created MySQL container",
        objective="Verify the new container is running",
        expected_changes=[],
        risk="readonly",
    )

    assert _forced_registered_action_for_step(create) == "create_docker_container"
    assert _forced_registered_action_for_step(existing) == ""
    assert _forced_registered_action_for_step(verify) == ""


def test_new_container_binding_skips_probabilistic_capability_selection(monkeypatch):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    import klonet_agent.ops.privileged.execution_agent as execution_agent

    monkeypatch.setattr(
        execution_agent,
        "_validate_action_resource_bindings",
        lambda *_args, **_kwargs: None,
    )

    step = PrivilegedStep(
        step_id="mysql",
        title="Create isolated MySQL container",
        objective="Create a new v4e2e-mysql Docker container",
        expected_changes=["A previously absent container is created"],
        risk="high",
    )
    plan = PrivilegedPlan(
        plan_id="forced-create",
        goal="deploy isolated service",
        risk="high",
        steps=[step],
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "ready",
                    "args": {
                        "name": "v4e2e-mysql",
                        "image": "mysql:8",
                        "port_bindings": ["127.0.0.1:3308:3306"],
                    },
                    "binding_reason": "dedicated isolated container",
                    "resolved_from_evidence": [],
                    "preconditions": [],
                    "postconditions": [
                        {
                            "checker": "container_running",
                            "args": {"container": "v4e2e-mysql"},
                        }
                    ],
                }
            )
        ]
    )

    binding = PrivilegedExecutionAgent(llm).prepare_step(
        plan, step, grounded_context=None
    )

    assert binding.action == "create_docker_container"
    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"][0]["function"]["name"] == (
        "bind_action_create_docker_container"
    )


def test_container_micro_plan_collapses_redundant_start_after_create():
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_container_starts,
    )

    items = [
        {
            "id": "create",
            "title": "Create Redis container v4e2e-redis",
            "objective": "Create and start the new isolated Redis container",
            "depends_on": [],
        },
        {
            "id": "start",
            "title": "Start Redis container v4e2e-redis",
            "objective": "Start the newly created Redis container",
            "depends_on": ["create"],
        },
        {
            "id": "verify",
            "title": "Verify Redis container",
            "objective": "Verify Redis is running",
            "depends_on": ["start"],
        },
    ]

    normalized = _collapse_redundant_container_starts(items)

    assert [item["id"] for item in normalized] == ["create", "verify"]
    assert normalized[1]["depends_on"] == ["create"]


def test_container_binding_compiles_semantic_name_and_credential_policy():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_semantic_action_args,
    )

    semantic = PrivilegedStep(
        step_id="rabbit",
        title="Provision isolated RabbitMQ container v4e2e-rabbitmq",
        objective="Create and start the new v4e2e-rabbitmq container",
        expected_changes=["v4e2e-rabbitmq is running"],
        risk="high",
    )

    compiled = _infer_semantic_action_args(
        "create_docker_container",
        {
            "name": "v4e2e",
            "image": "rabbitmq:latest",
            "port_bindings": ["0.0.0.0:27701:5672"],
            "credential_source": {"path": "/srv/config.py", "service": "redis"},
        },
        semantic,
    )

    assert compiled["name"] == "v4e2e-rabbitmq"
    assert compiled["port_bindings"] == ["127.0.0.1:27701:5672"]
    assert "credential_source" not in compiled


def test_complete_config_compiler_accepts_semantic_config_py_settings_title():
    from klonet_agent.ops.privileged.contracts import (
        PlanResource,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        _deterministic_klonet_config_items,
    )

    resources = [
        PlanResource("config", "path", "frozen", "config_path",
                     "/srv/v4e2e/vemu_config/config.py", "derived"),
    ] + [
        PlanResource(role, "port", "frozen", role, value, "evidence")
        for role, value in {
            "master_port": 47001,
            "worker_port": 47002,
            "web_terminal_port": 47003,
            "mysql_port": 47004,
            "redis_port": 47005,
            "rabbitmq_port": 47006,
        }.items()
    ]
    plan = PrivilegedPlan(
        plan_id="config-semantic",
        goal="deploy a complete isolated Klonet instance",
        risk="high",
        resources=resources,
        steps=[],
    )
    semantic = PrivilegedStep(
        step_id="config",
        title="Configure v4e2e instance settings in config.py",
        objective="Write instance ports, IPs, and Celery DB endpoints",
        expected_changes=["config changes"],
        risk="medium",
    )

    items = _deterministic_klonet_config_items(plan, semantic)
    attributes = {
        item.get("attribute") for item in items if item.get("attribute")
    }

    assert {
        "master_ip", "mysql_ip", "rabbitmq_ip", "master_port", "worker_port",
        "web_terminal_port", "mysql_port", "redis_port", "rabbitmq_port",
        "celery_redis_port_db", "celery_rabbitmq_port_db",
    } <= attributes


def test_container_binding_requires_selected_image_in_discovery_evidence():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_evidence,
    )

    grounded = GroundedPlanContext(
        knowledge_evidence="",
        environment_evidence=(
            "inspect_docker_images\n"
            "REPOSITORY TAG DIGEST IMAGE ID CREATED SIZE\n"
            "rabbitmq latest sha256:a sha256:b now 1MB\n"
        ),
        action_catalog="",
    )

    assert _validate_action_evidence(
        "create_docker_container", {"image": "rabbitmq:latest"}, grounded
    ) == ""
    assert "not_observed" in _validate_action_evidence(
        "create_docker_container", {"image": "rabbitmq:3-management"}, grounded
    )


def test_nginx_activation_micro_plan_collapses_duplicate_reloads():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_nginx_activations,
    )

    semantic = PrivilegedStep(
        step_id="activate-nginx",
        title="Reload Nginx to activate klonet-v4-e2e",
        objective="Validate and reload Nginx after the application starts",
        expected_changes=["site is active"],
        risk="medium",
    )
    items = [
        {"id": "validate", "title": "Validate and reload Nginx", "depends_on": []},
        {"id": "reload", "title": "Reload Nginx", "depends_on": ["validate"]},
        {"id": "verify", "title": "Verify Nginx health", "depends_on": ["reload"]},
    ]

    normalized = _collapse_redundant_nginx_activations(items, semantic)

    assert [item["id"] for item in normalized] == ["validate", "verify"]
    assert normalized[1]["depends_on"] == ["validate"]


def test_nginx_prepare_micro_plan_does_not_activate_service():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_nginx_activation_from_prepare,
    )

    semantic = PrivilegedStep(
        step_id="prepare-nginx",
        title="Create Nginx site klonet-v4-e2e",
        objective="Write and enable the isolated site configuration",
        expected_changes=["site files exist"],
        risk="medium",
    )
    items = [
        {"id": "install", "title": "Install Nginx config", "depends_on": []},
        {"id": "reload", "title": "Validate and reload Nginx",
         "depends_on": ["install"]},
        {"id": "verify", "title": "Verify config files", "depends_on": ["reload"]},
    ]

    normalized = _drop_nginx_activation_from_prepare(items, semantic)

    assert [item["id"] for item in normalized] == ["install", "verify"]
    assert normalized[1]["depends_on"] == ["install"]


def test_complete_wtx_config_compiles_to_one_atomic_step_per_attribute():
    from klonet_agent.ops.privileged.contracts import (
        PlanResource,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        _deterministic_klonet_config_items,
    )

    resources = [
        PlanResource("config", "path", "frozen", "config_path",
                     "/srv/v4e2e/vemu_config/config.py", "derived"),
        *[
            PlanResource(name, "port", "frozen", name, value, "evidence")
            for name, value in (
                ("master_port", 47001), ("worker_port", 47002),
                ("web_terminal_port", 47003), ("public_port", 47004),
                ("redis_port", 47005), ("mysql_port", 47006),
                ("rabbitmq_port", 47007),
            )
        ],
    ]
    plan = PrivilegedPlan(
        plan_id="config", goal="deploy complete isolated Klonet",
        risk="high", resources=resources, steps=[],
    )
    semantic = PrivilegedStep(
        step_id="configure",
        title="Write isolated WtxConfig configuration",
        objective="Configure complete WtxConfig and retain PROJ_CONFIG",
        risk="medium",
        expected_changes=["all isolated config values change"],
    )

    items = _deterministic_klonet_config_items(plan, semantic)

    values = {
        item["attribute"]: item["value"]
        for item in items
        if item.get("attribute")
    }
    assert values == {
        "master_ip": "127.0.0.1",
        "mysql_ip": "127.0.0.1",
        "rabbitmq_ip": "127.0.0.1",
        "master_port": 47001,
        "worker_port": 47002,
        "web_terminal_port": 47003,
        "public_port": 47004,
        "redis_port": 47005,
        "mysql_port": 47006,
        "rabbitmq_port": 47007,
        "celery_redis_port_db": "47005/6",
        "celery_rabbitmq_port_db": "47005/7",
    }
    assert items[-1]["expected_changes"] == []


def test_new_container_port_bindings_must_use_frozen_plan_ports():
    import pytest

    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_resource_bindings,
    )

    step = PrivilegedStep(
        step_id="change-3__mysql",
        title="Create MySQL container",
        risk="high",
    )
    resources = [
        PlanResource(
            "mysql_host_port",
            "port",
            "frozen",
            "service_port",
            47005,
            "evidence",
            consumers=["change-3.mysql_port"],
        ),
        PlanResource(
            "mysql_internal_port",
            "port",
            "frozen",
            "container_internal_port",
            3306,
            "image_contract",
            consumers=["change-3.mysql_internal_port"],
        ),
        PlanResource(
            "config_path", "path", "frozen", "config_path",
            "/srv/v4e2e/vemu_config/config.py", "derived",
            consumers=["configure.path"],
        ),
    ]
    credential_source = {
        "path": "/srv/v4e2e/vemu_config/config.py",
        "service": "mysql",
    }

    _validate_action_resource_bindings(
        step,
        "create_docker_container",
        {
            "port_bindings": ["127.0.0.1:47005:3306"],
            "credential_source": credential_source,
        },
        resources,
    )
    with pytest.raises(ValueError, match="unfrozen_container_port"):
        _validate_action_resource_bindings(
            step,
            "create_docker_container",
            {
                "port_bindings": ["127.0.0.1:47999:3306"],
                "credential_source": credential_source,
            },
            resources,
        )
    with pytest.raises(ValueError, match="container_port_binding_not_loopback"):
        _validate_action_resource_bindings(
            step,
            "create_docker_container",
            {
                "port_bindings": ["47005:3306"],
                "credential_source": credential_source,
            },
            resources,
        )


def test_structural_binding_compiles_pinned_clone_active_config_and_credentials():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    resources = [
        PlanResource(
            "config_path", "path", "frozen", "config_path",
            "/srv/v4e2e/vemu_config/config.py", "derived",
            consumers=["configure.path"],
        )
    ]
    clone = _infer_structural_action_args(
        "git_operation",
        {"operation": "clone", "revision": "abc123"},
        resources,
    )
    active = _infer_structural_action_args(
        "set_python_config_assignment",
        {"path": "/srv/v4e2e/vemu_config/config.py", "class_name": "WtxConfig",
         "assignment_name": "config"},
        resources,
    )
    redis = _infer_structural_action_args(
        "create_docker_container",
        {"name": "v4e2e-redis", "image": "redis:7"},
        resources,
    )
    nginx = _infer_structural_action_args(
        "install_nginx_config",
        {
            "content": "server {\n  listen 47004;\n  location / {\n"
                       "    proxy_pass http://127.0.0.1:47001;\n  }\n}",
        },
        resources,
    )

    assert clone["operation"] == "clone_at_revision"
    assert active["assignment_name"] == "PROJ_CONFIG"
    assert redis["credential_source"] == {
        "path": "/srv/v4e2e/vemu_config/config.py",
        "service": "redis",
    }
    assert "location = /healthz" in nginx["content"]
    assert "proxy_pass http://127.0.0.1:47001" in nginx["content"]


def test_micro_predecessor_can_produce_frozen_instance_root_for_validation():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _host_fact_is_planned_future,
    )

    step = PrivilegedStep(
        step_id="copy__verify",
        title="校验复制目录",
        objective="验证项目结构",
        evidence_refs=[
            "planned semantic predecessor copy executes first; "
            "objective=copy source tree; expected_changes=instance tree exists"
        ],
        depends_on=["copy__sync"],
        risk="readonly",
    )
    resource = PlanResource(
        name="instance_root",
        kind="path",
        status="frozen",
        role="instance_root",
        value="/home/lzl/lht",
        source="derived",
    )

    assert _host_fact_is_planned_future(
        "validate_project_files",
        {"project_root": "/home/lzl/lht/vemu_uestc"},
        step,
        "grounding_failed=project_root_missing_entries:gun.py",
        [resource],
    )


def test_manual_checkpoint_cannot_replace_automatic_start_action():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_objective_fit,
    )

    start = PrivilegedStep(
        step_id="start",
        title="启动 master 组件",
        objective="启动 lht master screen 会话",
        expected_changes=["master 会话运行"],
        risk="high",
    )

    assert "cannot_produce_automatic_state_change" in (
        _validate_action_objective_fit("manual_checkpoint", start)
    )


def test_readonly_verification_reselects_instead_of_starting_a_service(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="confirm Redis is reachable")])
    ).plan("确认 Redis 可用")
    plan.steps[0].risk = "medium"
    plan.steps[0].expected_changes = ["Redis state is confirmed"]
    binder = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "ensure_klonet_redis_instance",
                    "selection_reason": "incorrectly tries to repair it",
                }
            ),
            json.dumps(
                {
                    "status": "verification_only",
                    "selection_reason": "only observe service reachability",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "binding_reason": "TCP reachability proves the criterion",
                    "postconditions": [
                        {
                                "checker": "port_listening",
                            "args": {"host": "127.0.0.1", "port": 8368},
                        }
                    ],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(
        binder,
        enable_implementation_plans=False,
    ).prepare_plan(plan, grounded_context=None)

    assert bound.steps[0].execution_binding.kind == "verification_only"


def test_micro_plan_inherits_outer_semantic_future_outputs():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    prepare = PrivilegedStep(
        step_id="prepare",
        title="prepare runtime",
        objective="create the lht runtime directory",
        expected_changes=["/home/lzl/lht exists and contains copied source"],
        risk="medium",
    )
    start = PrivilegedStep(
        step_id="start",
        title="start runtime",
        objective="start lht from /home/lzl/lht",
        depends_on=["prepare"],
        risk="medium",
    )
    plan = PrivilegedPlan(
        plan_id="priv-future-output",
        goal="deploy lht",
        risk="medium",
        steps=[prepare, start],
    )
    llm = FakeLLM([
        json.dumps(
            {
                "status": "ready",
                "reason": "verify the predecessor output before launch",
                "implementation_steps": [
                    {
                        "id": "verify-runtime",
                        "title": "验证运行目录",
                        "objective": "verify /home/lzl/lht is runnable",
                        "reason": "launch requires the copied source",
                        "depends_on": [],
                        "expected_changes": [],
                        "success_criteria": ["runtime directory exists"],
                        "risk_suggestion": "readonly",
                    }
                ],
            }
        )
    ])

    steps = PrivilegedExecutionAgent(llm)._decompose_semantic_step(
        plan,
        start,
        grounded_context=None,
    )

    assert any(
        "planned semantic predecessor prepare executes first" in item
        and "/home/lzl/lht exists" in item
        for item in steps[0].evidence_refs
    )


def test_structural_python_action_rejects_nested_duplicate_class_contract():
    from klonet_agent.ops.privileged.planner import _validate_action_semantics

    problem = _validate_action_semantics(
        "upsert_python_class",
        {
            "path": "/tmp/config.py",
            "class_name": "LhtConfig",
            "base_class": "CommonConfig",
            "body": "class LhtConfig(CommonConfig):\n    master_port = 46001",
        },
    )

    assert problem == (
        "action=upsert_python_class body_must_not_include_class_header"
    )


def test_single_component_action_rejects_aggregate_or_mismatched_contract():
    from klonet_agent.ops.privileged.planner import _validate_action_semantics

    aggregate = _validate_action_semantics(
        "start_screen_component",
        {
            "platform": "lht",
            "component": "undefined",
            "screen_session": "lht",
            "project_root": "/home/lzl",
        },
    )
    mismatch = _validate_action_semantics(
        "start_screen_component",
        {
            "platform": "lht",
            "component": "master",
            "screen_session": "lht_w",
            "project_root": "/home/lzl",
        },
    )

    assert "invalid_component" in aggregate
    assert mismatch.endswith("screen_session_mismatch")


def test_binding_keeps_full_structural_python_body():
    from klonet_agent.ops.privileged.execution_agent import _clean_binding_args

    body = "\n".join("field_%s = %s" % (index, index) for index in range(200))

    cleaned = _clean_binding_args({"body": body})

    assert cleaned["body"] == body


def test_duplicate_http_checks_compile_to_one_alternative_contract():
    from klonet_agent.ops.privileged.execution_agent import (
        _merge_alternative_checker_contracts,
    )

    checks = _merge_alternative_checker_contracts(
        [
            {
                "checker": "http_status",
                "args": {"url": "http://127.0.0.1/lht/", "status": 200},
            },
            {
                "checker": "http_status",
                "args": {"url": "http://127.0.0.1/lht/", "status": 302},
            },
        ]
    )

    assert checks == [
        {
            "checker": "http_status",
            "args": {
                "url": "http://127.0.0.1/lht/",
                "statuses": [200, 302],
            },
        }
    ]


def test_future_dependency_output_can_ground_nginx_install_source():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _host_fact_is_planned_future,
    )

    step = PrivilegedStep(
        step_id="install-nginx",
        title="install nginx config",
        objective="install generated config",
        evidence_refs=[
            "planned predecessor copy-template executes first; "
            "expected_changes=/home/lzl/lht/nginx/lht.conf exists"
        ],
        depends_on=["copy-template"],
        risk="medium",
    )

    assert _host_fact_is_planned_future(
        "install_nginx_config",
        {"source_path": "/home/lzl/lht/nginx/lht.conf"},
        step,
        "grounding_failed=nginx_source_not_file",
    )
    assert not _host_fact_is_planned_future(
        "install_nginx_config",
        {"source_path": "/etc/shadow"},
        step,
        "grounding_failed=nginx_source_not_file",
    )


def test_frozen_instance_root_is_valid_future_runtime_after_prepare_step():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _host_fact_is_planned_future,
    )

    step = PrivilegedStep(
        step_id="start__master",
        title="start master",
        objective="start copied instance",
        evidence_refs=[
            "planned semantic predecessor prepare executes first; "
            "expected_changes=isolated source tree exists"
        ],
        depends_on=["prepare"],
    )
    resource = PlanResource(
        name="instance_root",
        kind="path",
        status="frozen",
        role="instance_root",
        value="/home/lzl/lht",
        source="derived",
    )

    assert _host_fact_is_planned_future(
        "start_screen_component",
        {"project_root": "/home/lzl/lht"},
        step,
        "grounding_failed=project_root_missing_entries:gun.py",
        [resource],
    )


def test_micro_plan_cannot_add_http_health_before_backend_start():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        _validate_micro_plan_dependency_shape,
    )

    route = PrivilegedStep(
        step_id="route",
        title="configure route",
        objective="write nginx config",
        risk="medium",
    )
    start = PrivilegedStep(
        step_id="start",
        title="启动平台组件",
        objective="start platform services",
        depends_on=["route"],
        risk="high",
    )
    plan = PrivilegedPlan(
        plan_id="priv-order",
        goal="deploy",
        risk="high",
        steps=[route, start],
    )
    premature = PrivilegedStep(
        step_id="route__http",
        title="验证路由可访问",
        objective="check HTTP route response",
        success_criteria=["HTTP 200"],
        risk="readonly",
    )
    author_only = PrivilegedStep(
        step_id="route__write",
        title="编写 Nginx 配置",
        objective="write proxy_pass http://127.0.0.1:45661",
        success_criteria=["配置包含 proxy_pass http://127.0.0.1:45661"],
        risk="medium",
    )

    _validate_micro_plan_dependency_shape(plan, route, [author_only])

    with pytest.raises(ExecutionBindingError, match="requires_backend_start"):
        _validate_micro_plan_dependency_shape(plan, route, [premature])


def test_frozen_resource_consumers_override_model_guessed_action_args():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _inject_frozen_resource_args,
    )

    step = PrivilegedStep(
        step_id="prepare__sync",
        title="sync source",
        objective="copy source into isolated instance",
    )
    resources = [
        PlanResource(
            name="source_root",
            kind="path",
            status="frozen",
            role="source_repo_root",
            value="/home/lzl/vemu_uestc",
            source="evidence",
            consumers=["prepare.source"],
        ),
        PlanResource(
            name="instance_root",
            kind="path",
            status="frozen",
            role="instance_root",
            value="/home/lzl/lht",
            source="derived",
            consumers=["prepare.destination"],
        ),
    ]

    compiled = _inject_frozen_resource_args(
        step,
        {
            "source": "/wrong/source",
            "destination": "/home/lzl/lht/vemu_uestc",
        },
        resources,
    )

    assert compiled == {
        "source": "/home/lzl/vemu_uestc",
        "destination": "/home/lzl/lht",
    }


def test_generated_file_action_cannot_escape_frozen_plan_paths(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_mutating_action_paths,
    )

    instance = tmp_path / "lht"
    resource = PlanResource(
        name="instance_root",
        kind="path",
        status="frozen",
        role="instance_root",
        value=str(instance),
        source="derived",
    )

    assert not _validate_mutating_action_paths(
        "write_ops_file",
        {"path": str(instance / "nginx" / "lht.conf")},
        [resource],
    )
    assert "outside_frozen_resources" in _validate_mutating_action_paths(
        "write_ops_file",
        {"path": "/root/legacy/nginx.conf"},
        [resource],
    )


def test_structural_binding_infers_base_from_future_copied_source(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    source = tmp_path / "source"
    target = tmp_path / "lht"
    (source / "vemu_config").mkdir(parents=True)
    (source / "vemu_config" / "config.py").write_text(
        "class CommonConfig:\n    pass\n",
        encoding="utf-8",
    )
    resources = [
        PlanResource(
            name="source_root",
            kind="path",
            status="frozen",
            role="source_repo_root",
            value=str(source),
            source="evidence",
        ),
        PlanResource(
            name="instance_root",
            kind="path",
            status="frozen",
            role="instance_root",
            value=str(target),
            source="derived",
        ),
    ]

    compiled = _infer_structural_action_args(
        "upsert_python_class",
        {
            "path": str(target / "vemu_config" / "config.py"),
            "class_name": "LhtConfig",
            "body": "master_port = 46001",
        },
        resources,
    )

    assert compiled["base_class"] == "CommonConfig"


def test_planner_can_probe_then_returns_semantic_plan_without_actions():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    probes = []
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "ports",
                            "args": {"ports": [8080]},
                            "purpose": "identify the current listener",
                        }
                    ],
                }
            ),
            _semantic_payload(),
        ]
    )
    progress = []
    planner = PrivilegedPlannerAgent(
        llm,
        probe_runner=lambda requests: (
            probes.extend(requests) or "port 8080 is not listening"
        ),
        on_progress=progress.append,
    )

    plan = planner.plan("inspect platform")

    assert probes[0]["probe"] == "ports"
    assert plan.schema_version == 3
    assert plan.steps[0].execution_binding is None
    assert plan.steps[0].action == ""
    assert plan.probe_history[0]["round"] == 1
    assert "action=" not in llm.calls[0]["messages"][0]["content"]
    assert any("准备执行 1 个只读检查（ports）" in item for item in progress)
    assert progress[-1] == "规划结论：已形成 1 个语义步骤，开始匹配安全执行能力。"


def test_planner_receives_recent_dialogue_for_continuation_resolution():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_semantic_payload(objective="deploy instance lht")])
    dialogue = (
        "user: 新增一个 Klonet 平台实例\n"
        "assistant: 请提供实例名称"
    )

    PrivilegedPlannerAgent(llm).plan(
        "叫 lht 吧",
        conversation_context=dialogue,
    )

    prompt = llm.calls[0]["messages"][1]["content"]
    assert dialogue in prompt
    assert "Current request:\n叫 lht 吧" in prompt


def test_planner_reuses_prior_probe_evidence_instead_of_repeating_probe():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "ports",
                            "args": {"ports": [8080]},
                            "purpose": "repeat old check",
                        }
                    ],
                }
            ),
            _semantic_payload(),
        ]
    )
    progress = []
    planner = PrivilegedPlannerAgent(
        llm,
        probe_runner=lambda requests: (_ for _ in ()).throw(
            AssertionError("duplicate probe must not execute")
        ),
        on_progress=progress.append,
    )

    plan = planner.plan(
        "inspect platform",
        planning_feedback="port 8080 is available",
        prior_probe_history=[
            {
                "requests": [
                    {"probe": "ports", "args": {"ports": [8080]}}
                ],
                "evidence": "port 8080 is available",
            }
        ],
    )

    assert plan.steps
    assert any("已拒绝重复只读检查（ports）" in item for item in progress)


def test_planner_accepts_legacy_action_risk_labels_as_semantic_aliases():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    payload = json.loads(_semantic_payload())
    payload["risk"] = "privileged"
    payload["steps"][0]["risk_suggestion"] = "privileged"

    plan = PrivilegedPlannerAgent(
        FakeLLM([json.dumps(payload)])
    ).plan("inspect platform")

    assert plan.risk == "medium"
    assert plan.steps[0].risk == "medium"


def test_failure_packet_context_routes_rag_to_troubleshooting():
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    seen = []
    builder = PrivilegedPlanContextBuilder(
        knowledge_search=lambda query, **kwargs: (
            seen.append((query, kwargs)) or "recovery runbook"
        ),
        environment_inspector=lambda args: "environment",
    )

    builder.build(
        "上一执行失败，请根据 Failure Packet 诊断并修复",
        supplemental_environment_context="Connection refused on Redis port 9368",
    )

    assert seen[0][1]["task_type"] == "troubleshooting"
    assert "Redis port 9368" in seen[0][0]


def test_execution_agent_maps_semantic_step_to_registered_action():
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload()])
    ).plan("inspect platform")
    binder = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "manual_checkpoint",
                    "selection_reason": "registered checkpoint covers objective",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "args": {"reason": "record observed platform state"},
                    "binding_reason": "the registered checkpoint covers the objective",
                    "resolved_from_evidence": ["server facts"],
                    "postconditions": [
                        {"checker": "exit_code_zero", "args": {}}
                    ],
                }
            )
        ]
    )

    progress = []
    bound = PrivilegedExecutionAgent(
        binder,
        on_progress=progress.append,
        enable_implementation_plans=False,
    ).prepare_plan(
        plan,
        grounded_context=None,
    )

    binding = bound.steps[0].execution_binding
    assert binding.kind == "registered_action"
    assert binding.action == "manual_checkpoint"
    assert binding.approval_scope == "plan"
    assert bound.status == "awaiting_confirmation"
    assert "manual_checkpoint" in binder.calls[0]["messages"][1]["content"]
    assert len(binder.calls) == 2
    assert "frozen_action" in binder.calls[1]["messages"][1]["content"]
    assert progress[0].startswith("实现节点 1/1")
    assert progress[-1].endswith("注册 Action：manual_checkpoint。")


def test_execution_agent_repairs_selected_action_args_without_replanning(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    source = tmp_path / "source"
    destination = tmp_path / "lht"
    source.mkdir()
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="copy source for lht")])
    ).plan("deploy lht")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "sync_directory",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    # Stage 2 is not allowed to switch the frozen selection.
                    "action": "write_ops_file",
                    "args": {
                        "source": str(source),
                        "destination": str(destination),
                    },
                    "resolved_from_evidence": ["observed source directory"],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(
        binder_llm,
        on_progress=progress.append,
        enable_implementation_plans=False,
    ).prepare_plan(plan, grounded_context=None)

    binding = bound.steps[0].execution_binding
    assert binding is not None
    assert binding.action == "sync_directory"
    assert binding.args == {
        "source": str(source),
        "destination": str(destination),
    }
    assert len(binder_llm.calls) == 2
    repair_request = json.loads(binder_llm.calls[1]["messages"][1]["content"])
    assert repair_request["frozen_action"] == "sync_directory"
    assert repair_request["required_args"] == ["source", "destination"]
    contract_call = binder_llm.calls[1]
    contract_tool = contract_call["tools"][0]["function"]
    assert contract_tool["name"] == "bind_action_sync_directory"
    assert contract_tool["parameters"]["properties"]["args"]["required"] == [
        "source",
        "destination",
    ]
    assert contract_call["kwargs"]["tool_choice"]["function"]["name"] == (
        "bind_action_sync_directory"
    )
    assert contract_call["kwargs"]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert any("单独补全参数合同" in item for item in progress)


def test_execution_agent_accepts_json_stringified_registered_action_args():
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="record deployment checkpoint")])
    ).plan("record checkpoint")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "manual_checkpoint",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "args": json.dumps({"reason": "deployment prepared"}),
                }
            )
        ]
    )

    bound = PrivilegedExecutionAgent(
        binder_llm,
        enable_implementation_plans=False,
    ).prepare_plan(
        plan,
        grounded_context=None,
    )

    assert bound.steps[0].execution_binding.args == {
        "reason": "deployment prepared"
    }
    assert len(binder_llm.calls) == 2


def test_missing_action_is_reported_before_missing_args():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )

    agent = PrivilegedExecutionAgent(FakeLLM([]))

    try:
        agent._registered_binding(
            {"status": "registered_action"},
            PrivilegedStep(
                step_id="configure",
                title="修改配置",
                objective="modify config ports",
                risk="high",
            ),
            None,
        )
    except ValueError as exc:
        assert str(exc) == "action_not_directly_registered=<missing>"
    else:
        raise AssertionError("missing action must be rejected")


def test_execution_agent_repairs_invalid_status_and_accepts_case_normalization():
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload()])
    ).plan("inspect platform")
    valid_selection = {
        "status": "REGISTERED_ACTION",
        "action": "manual_checkpoint",
    }
    valid_contract = {
        "status": "ready",
        "args": {"reason": "record observed platform state"},
        "binding_reason": "registered action matches",
        "resolved_from_evidence": ["server facts"],
    }
    binder_llm = FakeLLM(
        [
            json.dumps({"status": "ready"}),
            json.dumps({"status": "success"}),
            json.dumps(valid_selection),
            json.dumps(valid_contract),
        ]
    )

    bound = PrivilegedExecutionAgent(
        binder_llm,
        enable_implementation_plans=False,
    ).prepare_plan(
        plan,
        grounded_context=None,
    )

    assert bound.steps[0].execution_binding.action == "manual_checkpoint"
    assert len(binder_llm.calls) == 4
    repair = binder_llm.calls[1]["messages"][-1]["content"]
    assert "shell_artifact" in repair
    assert "stage 2 status ready is invalid" in repair


def test_execution_selection_prompt_is_bounded_and_omits_stage2_catalog():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload()])
    ).plan("inspect platform")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "test stops after prompt capture",
                }
            )
        ]
    )
    context = GroundedPlanContext(
        knowledge_evidence="K" * 40000,
        environment_evidence="E" * 40000,
        action_catalog="summary",
        facts={"environment_model": {"platform": "lht"}},
    )

    try:
        PrivilegedExecutionAgent(
            binder_llm,
            enable_implementation_plans=False,
        ).prepare_plan(
            plan,
            grounded_context=context,
        )
    except Exception:
        pass
    content = binder_llm.calls[0]["messages"][1]["content"]
    payload = json.loads(content)

    assert payload["required_status_values"] == [
        "registered_action",
        "shell_artifact",
        "verification_only",
        "need_evidence",
        "blocked",
    ]
    assert "manual_checkpoint" in payload["registered_action_catalog"]
    assert "sync_directory" in payload["allowed_action_names"]
    assert "system_environment" in payload["registered_probe_catalog"]
    assert "registered_checker_catalog" not in payload
    assert "exact grounded values will be supplied to stage 2" in payload[
        "selection_context"
    ]
    assert "K" * 100 not in payload["selection_context"]
    assert "E" * 100 not in payload["selection_context"]
    assert len(payload["selection_context"]) < 7000
    selection_call = binder_llm.calls[0]
    selection_tool = selection_call["tools"][0]
    assert selection_call["kwargs"]["tool_choice"]["function"]["name"] == (
        "select_execution_implementation"
    )
    assert selection_tool["function"]["parameters"]["properties"][
        "action"
    ]["enum"] == ["", *payload["allowed_action_names"]]
    assert selection_tool["function"]["parameters"]["properties"][
        "status"
    ]["enum"] == payload["required_status_values"]


def test_direct_blocked_selection_must_review_shell_fallback(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "fallback-marker"
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="create a deployment marker")])
    ).plan("create marker")
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "no registered action covers this operation",
                }
            ),
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "a bounded shell fallback is safe",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "touch %s" % target,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove the marker",
                    "binding_reason": "bounded shell fallback",
                    "resolved_from_evidence": [],
                    "postconditions": [
                        {"checker": "file_exists", "args": {"path": str(target)}}
                    ],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(
        llm,
        on_progress=progress.append,
        enable_implementation_plans=False,
    ).prepare_plan(plan, grounded_context=None)

    assert bound.steps[0].execution_binding.kind == "shell_artifact"
    assert any("强制评估一次性 Shell 兜底" in item for item in progress)
    fallback_request = llm.calls[1]["messages"][-1]["content"]
    assert "absence of a registered Action is not sufficient" in fallback_request


def test_plan_resources_freeze_paths_ports_and_keep_git_remote_deferred(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.workflow import render_plan

    instance_root = tmp_path / "lht"
    payload = json.loads(
        _semantic_payload(objective="deploy a new lht platform instance")
    )
    payload["goal"] = "deploy lht"
    payload["steps"][0]["step_id"] = "start"
    payload["resources"] = [
        {
            "name": "instance_name",
            "kind": "identifier",
            "status": "frozen",
            "value": "lht",
            "source": "user_input",
            "reason": "",
            "resolve_before": "",
            "consumers": ["start.platform"],
        },
        {
            "name": "instance_root",
            "kind": "path",
            "status": "frozen",
            "value": str(instance_root),
            "source": "derived",
            "reason": "",
            "resolve_before": "",
            "consumers": ["start.project_root"],
        },
        {
            "name": "master_port",
            "kind": "port",
            "status": "frozen",
            "value": 52101,
            "source": "environment_evidence",
            "reason": "",
            "resolve_before": "",
            "consumers": [],
        },
        {
            "name": "git_remote",
            "kind": "url",
            "status": "deferred",
            "value": None,
            "source": "",
            "reason": "the user may choose local copy instead",
            "resolve_before": "start",
            "consumers": [],
        },
    ]

    plan = PrivilegedPlannerAgent(FakeLLM([json.dumps(payload)])).plan(
        "deploy lht"
    )

    assert plan.resources[1].value == str(instance_root)
    assert plan.resources[2].value == 52101
    assert plan.resources[3].status == "deferred"
    assert "start.project_root" in plan.resources[1].consumers
    assert "start.master_port" in plan.resources[2].consumers
    binding_payload = json.loads(
        PrivilegedExecutionAgent(FakeLLM([]))._selection_request_content(
            plan,
            plan.steps[0],
            grounded_context=None,
        )
    )
    assert binding_payload["frozen_plan_resources"][1]["value"] == str(
        instance_root
    )
    preview = render_plan(plan)
    assert "instance_root=%s（已冻结）" % instance_root in preview
    assert "git_remote（待补全" in preview


def test_same_value_resource_aliases_may_share_one_consumer(tmp_path):
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    payload = json.loads(
        _semantic_payload(objective="deploy a new lht platform instance")
    )
    payload["goal"] = "deploy lht"
    payload["steps"][0]["risk_suggestion"] = "medium"
    payload["resources"] = [
        {
            "name": "instance_root",
            "kind": "path",
            "status": "frozen",
            "role": "instance_root",
            "value": str(tmp_path / "lht"),
            "source": "derived",
            "reason": "",
            "resolve_before": "",
            "consumers": ["inspect.destination"],
        },
        {
            "name": "copy_destination",
            "kind": "path",
            "status": "frozen",
            "role": "copy_destination",
            "value": str(tmp_path / "lht"),
            "source": "derived",
            "reason": "",
            "resolve_before": "",
            "consumers": ["inspect.destination"],
        },
    ]

    plan = PrivilegedPlannerAgent(FakeLLM([json.dumps(payload)])).plan(
        "deploy lht"
    )

    assert len(plan.resources) == 2
    assert "inspect.destination" in plan.resources[0].consumers
    assert "inspect.destination" not in plan.resources[1].consumers


def test_mutating_deployment_plan_cannot_omit_resource_manifest(tmp_path):
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    missing = json.loads(
        _semantic_payload(objective="deploy a new lht platform instance")
    )
    missing["goal"] = "deploy lht"
    missing["steps"][0]["risk_suggestion"] = "medium"
    repaired = dict(missing)
    repaired["resources"] = [
        {
            "name": "instance_root",
            "kind": "path",
            "status": "frozen",
            "value": str(tmp_path / "lht"),
            "source": "derived",
            "reason": "",
            "resolve_before": "",
            "consumers": [],
        }
    ]
    llm = FakeLLM([json.dumps(missing), json.dumps(repaired)])

    plan = PrivilegedPlannerAgent(llm).plan("deploy lht")

    assert len(llm.calls) == 2
    assert "requires a path resource manifest" in (
        llm.calls[1]["messages"][-1]["content"]
    )
    assert plan.resources[0].name == "instance_root"


def test_frozen_instance_root_compiles_over_model_selected_old_instance(
    tmp_path,
):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )

    new_root = tmp_path / "lht"
    old_root = tmp_path / "vemu_uestc"
    old_root.mkdir()
    resource = PlanResource(
        name="instance_root",
        kind="path",
        status="frozen",
        value=str(new_root),
        source="derived",
        consumers=["start.project_root"],
    )
    step = PrivilegedStep(
        step_id="start__screens",
        title="start lht",
        objective="start the lht platform",
        risk="high",
    )
    agent = PrivilegedExecutionAgent(FakeLLM([]))
    contract = {
        "action": "start_platform_screens",
        "args": {"platform": "lht", "project_root": str(old_root)},
    }

    binding = agent._registered_binding(
        contract,
        step,
        grounded_context=None,
        plan_resources=[resource],
    )
    assert binding.args["project_root"] == str(new_root)


def test_frozen_port_is_enforced_inside_configuration_content():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_resource_bindings,
    )

    resource = PlanResource(
        name="master_port",
        kind="port",
        status="frozen",
        value=52101,
        source="environment_evidence",
        consumers=["configure.content"],
    )
    step = PrivilegedStep(
        step_id="configure__edit",
        title="configure lht",
        risk="medium",
    )

    _validate_action_resource_bindings(
        step,
        "edit_text_file",
        {"content": "master_port = 52101"},
        [resource],
    )
    with pytest.raises(ValueError, match="resource_binding_violation"):
        _validate_action_resource_bindings(
            step,
            "edit_text_file",
            {"content": "master_port = 52001"},
            [resource],
        )


def test_resolving_deferred_resource_invalidates_previous_confirmation():
    from klonet_agent.ops.privileged.contracts import (
        PlanResource,
        PrivilegedPlan,
        PrivilegedStep,
    )

    plan = PrivilegedPlan(
        plan_id="priv-resources",
        goal="deploy lht",
        risk="medium",
        steps=[PrivilegedStep(step_id="copy", title="copy", risk="medium")],
        resources=[
            PlanResource(
                name="git_remote",
                kind="url",
                status="deferred",
                reason="repository choice is not known",
                resolve_before="copy",
            )
        ],
        status="awaiting_confirmation",
    )
    plan.authorize()
    old_hash = plan.authorized_hash

    plan.resolve_resource(
        "git_remote",
        "git@github.com:example/klonet.git",
        source="user_input",
    )

    assert old_hash != plan.content_hash
    assert plan.authorized_hash == ""
    assert plan.status == "awaiting_confirmation"
    assert plan.resources[0].status == "frozen"


def test_shell_fallback_obeys_frozen_and_deferred_resources(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        PlanResource,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_shell_resource_bindings,
    )
    from klonet_agent.ops.privileged.shell_artifact import create_shell_artifact

    new_root = tmp_path / "lht"
    old_root = tmp_path / "old"
    step = PrivilegedStep(
        step_id="start__shell",
        title="start lht",
        risk="high",
    )
    root = PlanResource(
        name="instance_root",
        kind="path",
        status="frozen",
        value=str(new_root),
        source="derived",
        consumers=["start.script"],
    )
    remote = PlanResource(
        name="git_remote",
        kind="url",
        status="deferred",
        reason="remote was not selected",
        resolve_before="start",
        consumers=["start.script"],
    )

    def artifact(script):
        return create_shell_artifact(
            artifact_id="shell-resource-test",
                script=script,
                cwd=str(tmp_path),
                run_as="",
                timeout=10,
                environment_fingerprint="",
                declared_changes=[str(new_root)],
                rollback="remove test output",
                nonce="resource-test",
            )

    with pytest.raises(ValueError, match="resource_binding_violation"):
        _validate_shell_resource_bindings(
            step,
            artifact("cd %s && ./start.sh" % old_root),
            [root],
        )
    _validate_shell_resource_bindings(
        step,
        artifact("cd %s && ./start.sh" % new_root),
        [root],
    )
    with pytest.raises(ValueError, match="deferred_plan_resource_required"):
        _validate_shell_resource_bindings(
            step,
            artifact("mkdir -p %s && git clone guessed %s" % (new_root, new_root)),
            [root, remote],
        )


def test_shell_failure_cannot_block_before_candidate_budget(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "alternate-marker"
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="create a deployment marker")])
    ).plan("create marker")
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "try the first shell implementation",
                }
            ),
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "the first candidate cannot be verified",
                }
            ),
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "the previous shell candidate failed",
                }
            ),
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "use a materially different command",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "touch %s" % target,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove the marker",
                    "binding_reason": "bounded alternate shell candidate",
                    "resolved_from_evidence": [],
                    "postconditions": [
                        {"checker": "file_exists", "args": {"path": str(target)}}
                    ],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(
        llm,
        on_progress=progress.append,
        enable_implementation_plans=False,
    ).prepare_plan(plan, grounded_context=None)

    assert bound.steps[0].execution_binding.kind == "shell_artifact"
    assert len(llm.calls) == 5
    assert any("Shell 候选 1/3 校验失败" in item for item in progress)
    assert any("拒绝提前 blocked" in item for item in progress)
    assert any("候选 2/3" in item for item in progress)


def test_hard_shell_blocker_can_stop_before_candidate_budget(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="perform a forbidden mutation")])
    ).plan("perform mutation")
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "evaluate a bounded shell implementation",
                }
            ),
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "the policy forbids this mutation",
                }
            ),
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "the requested mutation is prohibited by policy",
                    "shell_blocker_category": "hard_policy",
                }
            ),
        ]
    )

    with pytest.raises(ExecutionBindingError) as captured:
        PrivilegedExecutionAgent(
            llm,
            enable_implementation_plans=False,
        ).prepare_plan(plan, grounded_context=None)

    assert captured.value.category == "capability_mismatch"
    assert len(llm.calls) == 3


def test_shell_candidate_budget_exhaustion_recommends_replan(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="create an unusual deployment file")])
    ).plan("create deployment file")
    responses = []
    for candidate in range(1, 4):
        responses.extend(
            [
                json.dumps(
                    {
                        "status": "shell_artifact",
                        "selection_reason": "shell candidate %s" % candidate,
                    }
                ),
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "candidate %s has no safe contract" % candidate,
                    }
                ),
            ]
        )
    llm = FakeLLM(responses)

    with pytest.raises(ExecutionBindingError) as captured:
        PrivilegedExecutionAgent(
            llm,
            enable_implementation_plans=False,
        ).prepare_plan(plan, grounded_context=None)

    assert captured.value.category == "capability_mismatch"
    assert captured.value.replan_recommended is True
    assert "所有 Shell 候选" in str(captured.value)
    assert len(llm.calls) == 6


def test_binding_agent_uses_unified_text_edit_contract(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    config = tmp_path / "config.py"
    config.write_text("PROJ_CONFIG = WtxConfig()\n", encoding="utf-8")
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="insert LhtConfig before active config")])
    ).plan("configure lht")
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "edit_text_file",
                    "selection_reason": "unified text editor supports anchored insertion",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "reason": "",
                    "args": {
                        "path": str(config),
                        "operation": "insert_before",
                        "anchor": "PROJ_CONFIG = WtxConfig()",
                        "content": "class LhtConfig(WtxConfig):\n    master_port = 5200\n",
                    },
                    "binding_reason": "unique active-config anchor",
                    "resolved_from_evidence": ["config tail"],
                    "preconditions": [],
                    "postconditions": [],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(
        llm,
        enable_implementation_plans=False,
    ).prepare_plan(plan, grounded_context=None)

    binding = bound.steps[0].execution_binding
    assert binding.action == "edit_text_file"
    assert binding.args["operation"] == "insert_before"
    assert binding.args["anchor"] == "PROJ_CONFIG = WtxConfig()"
    contract_request = json.loads(llm.calls[1]["messages"][1]["content"])
    assert contract_request["optional_args"] == ["anchor"]
    assert "insert_before" in contract_request["action_description"]


def test_implementation_plan_locally_rebuilds_discovery_only_step(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "instance"
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="prepare a new instance")])
    ).plan("prepare instance")
    discovery = {
        "status": "ready",
        "reason": "first locate parameters",
        "implementation_steps": [
            {
                "id": "locate",
                "title": "定位并读取现有配置",
                "objective": "locate and read existing configuration",
                "reason": "obtain arguments for a later step",
                "depends_on": [],
                "expected_changes": [],
                "success_criteria": ["configuration location is known"],
                "risk_suggestion": "readonly",
            }
        ],
    }
    actionable = {
        "status": "ready",
        "reason": "perform the observable state change directly",
        "implementation_steps": [
            {
                "id": "create",
                "title": "创建实例目录",
                "objective": "create the instance directory",
                "reason": "the deployment requires it",
                "depends_on": [],
                "expected_changes": ["the instance directory exists"],
                "success_criteria": ["the instance directory exists"],
                "risk_suggestion": "low",
            }
        ],
    }
    llm = FakeLLM(
        [
            json.dumps(discovery),
            json.dumps(actionable),
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "create_directory",
                    "selection_reason": "registered action covers the change",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "reason": "",
                    "args": {"path": str(target)},
                    "binding_reason": "create requested directory",
                    "resolved_from_evidence": [],
                    "preconditions": [],
                    "postconditions": [
                        {"checker": "file_exists", "args": {"path": str(target)}}
                    ],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(llm, on_progress=progress.append).prepare_plan(
        plan,
        grounded_context=None,
    )

    micro_steps = bound.steps[0].implementation_plan.steps
    assert [item.step_id for item in micro_steps] == ["inspect__create"]
    assert any("局部重建" in item for item in progress)


def test_unregistered_shell_requires_plan_then_exact_step_confirmation(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    target = tmp_path / "shell-result"
    planner = PrivilegedPlannerAgent(
        FakeLLM(
            [
                _semantic_payload(
                    objective="create the one-off deployment marker"
                )
            ]
        )
    )
    binder = PrivilegedExecutionAgent(
        FakeLLM(
            [
                json.dumps(
                    {
                        "status": "shell_artifact",
                        "selection_reason": "no registered action covers marker",
                    }
                ),
                json.dumps(
                    {
                        "status": "ready",
                        "script": "touch %s" % target,
                        "cwd": str(tmp_path),
                        "run_as": "",
                        "timeout": 10,
                        "declared_changes": [str(target)],
                        "rollback": "remove the marker file",
                        "binding_reason": "no registered action covers this marker",
                        "postconditions": [
                            {
                                "checker": "file_exists",
                                "args": {"path": str(target)},
                            }
                        ],
                    }
                )
            ]
        ),
        enable_implementation_plans=False,
    )
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        execution_agent=binder,
        executor=PrivilegedCommandExecutor(),
        verifier=PrivilegedVerifierAgent(None),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )

    waiting = workflow.submit("create marker")
    plan_id = waiting.plan.plan_id
    step_id = waiting.plan.steps[0].step_id
    after_plan_confirmation = workflow.handle_command(
        "confirm-priv %s" % plan_id
    )
    details = workflow.handle_command("show-priv %s" % plan_id)
    completed = workflow.handle_command(
        "confirm-priv-step %s %s" % (plan_id, step_id)
    )

    assert waiting.kind == "awaiting_confirmation"
    assert after_plan_confirmation.kind == "awaiting_step_confirmation"
    assert "固定脚本如下" in details.message
    assert "脚本 SHA-256" in details.message
    assert completed.kind == "completed"
    assert target.is_file()
    assert completed.plan.is_authorized is True
    assert (
        completed.plan.steps[0].execution_binding.shell_artifact.status
        == "executed"
    )


def test_execution_agent_completes_missing_shell_postconditions_separately(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "generated-marker"
    plan = PrivilegedPlannerAgent(
        FakeLLM(
            [
                _semantic_payload(
                    objective="create an observable deployment marker"
                )
            ]
        )
    ).plan("create marker")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "no registered action covers marker",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "touch %s" % target,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove the marker",
                    "binding_reason": "no registered action covers this",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {"path": str(target)},
                        }
                    ],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(
        binder_llm,
        on_progress=progress.append,
        enable_implementation_plans=False,
    ).prepare_plan(plan, grounded_context=None)

    binding = bound.steps[0].execution_binding
    assert binding is not None
    assert binding.kind == "shell_artifact"
    assert binding.postconditions == [
        {"checker": "file_exists", "args": {"path": str(target)}}
    ]
    assert binding.shell_artifact is not None
    assert "touch %s" % target in binding.shell_artifact.script
    assert len(binder_llm.calls) == 3
    shell_request = json.loads(
        binder_llm.calls[1]["messages"][1]["content"]
    )
    assert shell_request["frozen_implementation_kind"] == "shell_artifact"
    assert "checker=file_contains args=path,text" in shell_request[
        "registered_checker_catalog"
    ]
    verification_request = json.loads(
        binder_llm.calls[2]["messages"][1]["content"]
    )
    assert verification_request["frozen_shell_artifact"]["sha256"] == (
        binding.shell_artifact.sha256
    )
    assert any("脚本已通过安全校验并冻结" in item for item in progress)
    assert progress[-1].endswith("需要一次性脚本并单独确认。")


def test_shell_verification_repair_cannot_replace_frozen_script(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "frozen-marker"
    original_script = "touch %s" % target
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="create the frozen marker")])
    ).plan("create marker")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "no registered action covers marker",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": original_script,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove the marker",
                    "postconditions": [
                        {"checker": "file_exists", "args": {}}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "rm -f /tmp/unrelated",
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {"path": str(target)},
                        }
                    ],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(
        binder_llm,
        enable_implementation_plans=False,
    ).prepare_plan(
        plan,
        grounded_context=None,
    )

    artifact = bound.steps[0].execution_binding.shell_artifact
    assert artifact is not None
    assert original_script in artifact.script
    assert "unrelated" not in artifact.script


def test_shell_postconditions_require_observable_state_not_only_exit_code(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "observable-marker"
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="create an observable marker")])
    ).plan("create marker")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "no registered action covers marker",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "touch %s" % target,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove marker",
                    "postconditions": [
                        {"checker": "exit_code_zero", "args": {}}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "postconditions": [
                        {"checker": "exit_code_zero", "args": {}}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {"path": str(target)},
                        }
                    ],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(
        binder_llm,
        enable_implementation_plans=False,
    ).prepare_plan(
        plan,
        grounded_context=None,
    )

    assert bound.steps[0].postconditions[0]["checker"] == "file_exists"
    repair_prompt = binder_llm.calls[3]["messages"][-1]["content"]
    assert "Repair only postconditions" in repair_prompt


def test_initial_binding_failure_returns_to_planner_before_blocking(tmp_path):
    from klonet_agent.ops.privileged.contracts import ExecutionBinding
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    planner_llm = FakeLLM(
        [
            _semantic_payload(objective="deploy lht in one broad step"),
            _semantic_payload(objective="prepare the lht instance safely"),
        ]
    )
    planner = PrivilegedPlannerAgent(planner_llm)

    class BindingAgent:
        def __init__(self):
            self.calls = 0

        def prepare_plan(self, plan, *, grounded_context):
            del grounded_context
            self.calls += 1
            if self.calls == 1:
                plan.probe_history.append(
                    {
                        "phase": "execution_binding",
                        "step_id": plan.steps[0].step_id,
                        "round": 1,
                        "requests": [
                            {"probe": "ports", "args": {"ports": [8080]}}
                        ],
                        "evidence": "port 8080 is available",
                    }
                )
                raise ExecutionBindingError(
                    "no registered action safely covers the broad step"
                )
            step = plan.steps[0]
            step.execution_binding = ExecutionBinding(
                kind="registered_action",
                action="manual_checkpoint",
                args={"reason": "prepared"},
                risk="medium",
                approval_scope="plan",
                postconditions=[
                    {"checker": "exit_code_zero", "args": {}}
                ],
            )
            step.risk = "medium"
            step.postconditions = list(step.execution_binding.postconditions)
            plan.risk = "medium"
            plan.status = "awaiting_confirmation"
            return plan

    binder = BindingAgent()
    progress = []
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        execution_agent=binder,
        executor=object(),
        verifier=object(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
        on_progress=progress.append,
    )

    result = workflow.submit("部署名为 lht 的平台实例")

    assert result.kind == "awaiting_confirmation"
    assert binder.calls == 2
    assert len(planner_llm.calls) == 2
    retry_prompt = planner_llm.calls[1]["messages"][1]["content"]
    assert "execution_binding" in retry_prompt
    assert "no registered action safely covers" in retry_prompt
    assert "port 8080 is available" in retry_prompt
    assert result.plan.probe_history[0]["evidence"] == "port 8080 is available"
    assert "第 1/2 次" in progress[0]
    assert "no registered action safely covers" in progress[0]


def test_initial_binding_replan_loop_is_bounded(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    planner_llm = FakeLLM([_semantic_payload()] * 3)
    planner = PrivilegedPlannerAgent(planner_llm)

    class AlwaysFailingBindingAgent:
        def __init__(self):
            self.calls = 0

        def prepare_plan(self, plan, *, grounded_context):
            del plan, grounded_context
            self.calls += 1
            raise ExecutionBindingError("binding remains impossible")

    binder = AlwaysFailingBindingAgent()
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        execution_agent=binder,
        executor=object(),
        verifier=object(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )

    result = workflow.submit("部署名为 lht 的平台实例")

    assert result.kind == "paused"
    assert "2 次重新规划后仍失败" in result.message
    assert "请由你决定" in result.message
    assert "replan-priv" in result.message
    assert result.plan.status == "paused"
    assert binder.calls == 3
    assert len(planner_llm.calls) == 3


def test_invalid_execute_contract_does_not_consume_planner_replans(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    planner_llm = FakeLLM([_semantic_payload()])
    progress = []

    class InvalidContractBindingAgent:
        def prepare_plan(self, plan, *, grounded_context):
            del plan, grounded_context
            raise ExecutionBindingError(
                "registered action args must be an object",
                replan_recommended=False,
                category="implementation_contract_invalid",
            )

    workflow = PrivilegedOpsWorkflow(
        planner=PrivilegedPlannerAgent(planner_llm),
        execution_agent=InvalidContractBindingAgent(),
        executor=object(),
        verifier=object(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
        on_progress=progress.append,
    )

    result = workflow.submit("部署名为 lht 的平台实例")

    assert result.kind == "paused"
    assert "未触发无意义的 Planner 重规划" in result.message
    assert "请由你决定" in result.message
    assert len(planner_llm.calls) == 1
    assert any("不消耗 Planner 重规划次数" in item for item in progress)


def test_execution_agent_binds_verification_only_without_command(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    marker = tmp_path / "healthy"
    marker.write_text("ok", encoding="utf-8")
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="verify deployment health")])
    ).plan("verify deployment")
    binder = PrivilegedExecutionAgent(
        FakeLLM(
            [
                json.dumps(
                    {
                        "status": "verification_only",
                        "action": "",
                        "selection_reason": "the step only verifies state",
                    }
                ),
                json.dumps(
                    {
                        "status": "ready",
                        "binding_reason": "file state proves health",
                        "postconditions": [
                            {
                                "checker": "file_exists",
                                "args": {"path": str(marker)},
                            }
                        ],
                    }
                ),
            ]
        ),
        enable_implementation_plans=False,
    )

    bound = binder.prepare_plan(plan, grounded_context=None)
    step = bound.steps[0]
    evidence = PrivilegedCommandExecutor().execute(step)
    step.evidence = evidence
    decision = PrivilegedVerifierAgent(None).verify_deterministic_step(
        bound,
        step,
    )

    assert step.execution_binding.kind == "verification_only"
    assert evidence.return_code == 0
    assert evidence.environment_changed is False
    assert decision.status == "passed"


def test_observational_semantic_step_rejects_mutating_micro_step():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        PrivilegedExecutionAgent,
    )

    verify = PrivilegedStep(
        step_id="verify-health",
        title="验证 lht 实例端到端健康",
        objective="verify the deployed instance is healthy",
        expected_changes=["the public endpoint is reachable"],
        risk="medium",
    )
    plan = PrivilegedPlan(
        plan_id="priv-verifier-pollution",
        goal="deploy lht",
        risk="medium",
        steps=[verify],
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "ready",
                    "implementation_steps": [
                        {
                            "id": "reload-nginx",
                            "title": "Reload Nginx",
                            "objective": "reload nginx to pick up the site",
                            "depends_on": [],
                            "expected_changes": ["nginx configuration reloaded"],
                            "success_criteria": ["nginx is active"],
                            "risk_suggestion": "medium",
                        }
                    ],
                }
            )
        ]
    )

    with pytest.raises(
        ExecutionBindingError,
        match="observational_semantic_step_contains_mutation",
    ):
        PrivilegedExecutionAgent(llm)._decompose_semantic_step(
            plan,
            verify,
            grounded_context=None,
        )


def test_generic_file_write_cannot_claim_to_enable_nginx_site():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_objective_fit,
    )

    step = PrivilegedStep(
        step_id="nginx",
        title="安装并启用 lht Nginx 站点",
        objective="write the site config and enable it",
        expected_changes=[
            "/etc/nginx/sites-available/lht exists",
            "/etc/nginx/sites-enabled/lht points to it",
        ],
        risk="medium",
    )

    assert "cannot_enable_nginx_site" in _validate_action_objective_fit(
        "write_ops_file",
        step,
    )


def test_nginx_mutating_actions_declare_effects():
    from klonet_agent.ops.actions import configured_ops_action_registry

    registry = configured_ops_action_registry()

    assert registry.get("install_nginx_config").effects
    assert registry.get("reload_nginx").effects


def test_nginx_install_contract_allows_inline_content_without_source_file():
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    tool = PrivilegedExecutionAgent(FakeLLM([]))._action_contract_function_tool(
        "install_nginx_config"
    )
    args_schema = tool["function"]["parameters"]["properties"]["args"]

    assert args_schema["required"] == ["config_name"]
    assert "content" in args_schema["properties"]
    assert "source_path" in args_schema["properties"]


def test_invalid_implementation_json_triggers_local_rebuild(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    target = tmp_path / "lht"
    step = PrivilegedStep(
        step_id="prepare",
        title="创建实例目录",
        objective="create the lht instance directory",
        expected_changes=["the directory exists"],
        risk="medium",
    )
    plan = PrivilegedPlan(
        plan_id="priv-invalid-implementation-json",
        goal="prepare lht",
        risk="medium",
        steps=[step],
    )
    llm = FakeLLM(
        [
            "{invalid-json",
            json.dumps(
                {
                    "status": "ready",
                    "implementation_steps": [
                        {
                            "id": "create",
                            "title": "创建实例目录",
                            "objective": "create the lht instance directory",
                            "depends_on": [],
                            "expected_changes": ["the directory exists"],
                            "success_criteria": ["the directory exists"],
                            "risk_suggestion": "medium",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "create_directory",
                    "selection_reason": "the action is atomic",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "reason": "",
                    "args": {"path": str(target)},
                    "binding_reason": "create the frozen target",
                    "resolved_from_evidence": [],
                    "preconditions": [],
                    "postconditions": [
                        {"checker": "file_exists", "args": {"path": str(target)}}
                    ],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(
        llm,
        on_progress=progress.append,
    ).prepare_plan(plan, grounded_context=None)

    assert bound.status == "awaiting_confirmation"
    assert len(llm.calls) == 4
    assert any("局部重建" in message for message in progress)


def test_implementation_items_are_topologically_ordered_before_binding():
    from klonet_agent.ops.privileged.execution_agent import (
        _topologically_order_implementation_items,
    )

    items = [
        {"id": "verify", "depends_on": ["start"]},
        {"id": "start", "depends_on": ["create"]},
        {"id": "create", "depends_on": []},
    ]

    ordered = _topologically_order_implementation_items(items)

    assert [item["id"] for item in ordered] == ["create", "start", "verify"]


def test_outer_semantic_dependencies_are_not_micro_plan_dependencies():
    from klonet_agent.ops.privileged.execution_agent import (
        _remove_outer_semantic_dependencies,
    )

    items = [
        {"id": "create", "depends_on": ["change-1"]},
        {"id": "verify", "depends_on": ["create", "change-1"]},
    ]

    _remove_outer_semantic_dependencies(items, ["change-1"])

    assert items == [
        {"id": "create", "depends_on": []},
        {"id": "verify", "depends_on": ["create"]},
    ]


def test_screen_session_is_derived_from_platform_and_component():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    compiled = _infer_structural_action_args(
        "start_screen_component",
        {
            "platform": "klonet",
            "component": "master",
            "screen_session": "klonet_master",
            "project_root": "/home/lzl/lht",
        },
        [
            PlanResource(
                name="screen_name_prefix",
                kind="string",
                status="frozen",
                role="screen_session_name_prefix",
                value="lht",
                source="planner",
            )
        ],
    )

    assert compiled["platform"] == "lht"
    assert compiled["screen_session"] == "lht_m"


def test_screen_component_alias_is_compiled_from_frozen_session_suffix():
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    compiled = _infer_structural_action_args(
        "start_screen_component",
        {
            "platform": "v4e2e",
            "component": "web",
            "screen_session": "v4e2e_web",
            "project_root": "/home/lzl/klonet_v4_e2e",
        },
        [],
    )

    assert compiled["component"] == "web_terminal"
    assert compiled["screen_session"] == "v4e2e_web"


def test_nginx_install_source_must_prove_declared_content(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    source = tmp_path / "default"
    source.write_text("server { listen 80; }\n", encoding="utf-8")
    step = PrivilegedStep(
        step_id="nginx",
        title="安装并启用 lht Nginx 站点",
        objective="install the lht nginx site",
        expected_changes=["the site listens on 45556"],
        risk="medium",
    )

    with pytest.raises(ValueError, match="nginx_source_missing_declared_content"):
        PrivilegedExecutionAgent(FakeLLM([]))._registered_binding(
            {
                "action": "install_nginx_config",
                "args": {
                    "source_path": str(source),
                    "content": "",
                    "config_name": "lht",
                },
                "postconditions": [
                    {
                        "checker": "file_contains",
                        "args": {
                            "path": "/etc/nginx/sites-available/lht",
                            "text": "45556",
                        },
                    }
                ],
            },
            step,
            grounded_context=None,
        )


def test_backend_configuration_requires_frozen_public_port_assignment():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        _validate_semantic_resource_coverage,
    )

    semantic = PrivilegedStep(
        step_id="configure",
        title="配置 lht 后端独立端口与地址",
        objective="configure the copied backend ports",
        risk="medium",
    )
    micro_steps = [
        PrivilegedStep(
            step_id=f"configure__{attribute}",
            title=f"set {attribute}",
            action="set_python_class_attribute",
            args={"attribute": attribute},
            risk="medium",
        )
        for attribute in ("master_port", "worker_port", "web_terminal_port")
    ]
    resources = [
        PlanResource(
            name="public_port",
            kind="port",
            status="frozen",
            role="public_port",
            value=45556,
            source="planner",
        )
    ]

    with pytest.raises(ExecutionBindingError, match="missing_public_port_assignment"):
        _validate_semantic_resource_coverage(semantic, micro_steps, resources)


def test_verifier_cannot_assert_unscoped_legacy_port_state():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_checker_resource_scope,
    )

    step = PrivilegedStep(
        step_id="ready",
        title="验证 lht 后端全部就绪",
        objective="verify the new backend ports are listening",
        risk="readonly",
    )
    resources = [
        PlanResource(
            name="master_port",
            kind="port",
            status="frozen",
            role="master_port",
            value=45554,
            source="planner",
        )
    ]

    assert "unscoped_port_check=45551" in _validate_checker_resource_scope(
        step,
        [{"checker": "port_not_listening", "args": {"port": 45551}}],
        resources,
    )


def test_verifier_can_observe_services_that_are_already_started():
    from klonet_agent.ops.privileged.execution_agent import (
        _implementation_item_is_verification,
    )

    assert _implementation_item_is_verification(
        {
            "title": "验证 lht 四个后端端口均处于监听",
            "objective": "verify all backend services are started and listening",
        }
    )
    assert not _implementation_item_is_verification(
        {
            "title": "验证并启动 lht 后端",
            "objective": "verify and start the backend services",
        }
    )


def test_git_checkout_and_pin_is_a_mutation_not_a_check_verifier():
    from klonet_agent.ops.privileged.execution_agent import (
        _implementation_item_is_verification,
    )

    assert not _implementation_item_is_verification(
        {
            "title": "Checkout and pin target revision",
            "objective": "checkout af418698 and pin the repository revision",
        }
    )
    assert _implementation_item_is_verification(
        {
            "title": "Check pinned target revision",
            "objective": "verify the repository is at af418698",
        }
    )


def test_frozen_port_selection_step_becomes_readonly_verification():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_resolved_resource_semantic_step,
    )

    step = PrivilegedStep(
        step_id="select-ports",
        title="自动选择 lht 空闲端口",
        objective="scan listeners and choose available backend ports",
        expected_changes=["port values are selected"],
        risk="medium",
    )
    resources = [
        PlanResource(
            name="master_port",
            kind="port",
            status="frozen",
            role="master_port",
            value=45661,
            source="planner",
        )
    ]

    _normalize_resolved_resource_semantic_step(step, resources)

    assert step.title == "验证已冻结端口仍可用"
    assert step.risk == "readonly"
    assert step.expected_changes == []
    assert "45661" in step.objective


def test_reload_step_cannot_degrade_to_verification_only():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="reload",
        title="校验并重新加载 Nginx 配置",
        objective="validate and reload nginx",
        risk="readonly",
    )
    plan = PrivilegedPlan(
        plan_id="priv-reload-no-degrade",
        goal="enable nginx site",
        risk="medium",
        steps=[step],
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "verification_only",
                    "selection_reason": "syntax can be checked",
                }
            ),
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "reload_nginx",
                    "selection_reason": "reload is required",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "reason": "",
                    "args": {},
                    "binding_reason": "validate then reload",
                    "resolved_from_evidence": [],
                    "preconditions": [],
                    "postconditions": [
                        {"checker": "nginx_config_valid", "args": {}},
                        {"checker": "service_active", "args": {"service": "nginx"}},
                    ],
                }
            ),
        ]
    )

    binding = PrivilegedExecutionAgent(llm).prepare_step(
        plan,
        step,
        grounded_context=None,
    )

    assert binding.action == "reload_nginx"


def test_sync_binding_rejects_existing_nonempty_destination(tmp_path):
    from klonet_agent.ops.privileged.planner import _validate_host_facts

    source = tmp_path / "source"
    destination = tmp_path / "existing"
    source.mkdir()
    destination.mkdir()
    (destination / "legacy.txt").write_text("keep", encoding="utf-8")

    assert "destination_not_empty" in _validate_host_facts(
        "sync_directory",
        {"source": str(source), "destination": str(destination)},
    )


def test_copy_semantic_rejects_nonempty_frozen_instance_root(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        _validate_semantic_destination_availability,
    )

    destination = tmp_path / "lht"
    destination.mkdir()
    (destination / "legacy.txt").write_text("keep", encoding="utf-8")
    step = PrivilegedStep(
        step_id="copy",
        title="复制源码到新实例目录",
        objective="copy the source repository into the lht instance root",
        expected_changes=["the instance tree exists"],
        risk="medium",
    )
    resources = [
        PlanResource(
            name="instance_root",
            kind="path",
            status="frozen",
            role="instance_root",
            value=str(destination),
            source="planner",
        )
    ]

    with pytest.raises(ExecutionBindingError, match="instance_root_not_empty"):
        _validate_semantic_destination_availability(step, resources)


def test_execution_agent_reselects_after_action_contract_is_not_grounded(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    marker = tmp_path / "healthy"
    progress = []
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="verify deployment health")])
    ).plan("verify deployment")
    binder = PrivilegedExecutionAgent(
        FakeLLM(
            [
                json.dumps(
                    {
                        "status": "registered_action",
                        "action": "run_ops_command",
                        "selection_reason": "initial but unsuitable choice",
                    }
                ),
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "no grounded program and argv",
                    }
                ),
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "no grounded program and argv",
                    }
                ),
                json.dumps(
                    {
                        "status": "verification_only",
                        "action": "",
                        "selection_reason": "no execution is required",
                    }
                ),
                json.dumps(
                    {
                        "status": "ready",
                        "binding_reason": "use a deterministic state check",
                        "postconditions": [
                            {
                                "checker": "file_exists",
                                "args": {"path": str(marker)},
                            }
                        ],
                    }
                ),
            ]
        ),
        on_progress=progress.append,
        enable_implementation_plans=False,
    )

    bound = binder.prepare_plan(plan, grounded_context=None)

    assert bound.steps[0].execution_binding.kind == "verification_only"
    assert any("正在重新选择" in item for item in progress)


def test_workflow_safely_retries_one_transient_idempotent_action(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
        VerificationDecision,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    class Executor:
        def __init__(self):
            self.calls = 0

        def execute(self, step):
            del step
            self.calls += 1
            if self.calls == 1:
                return ExecutionEvidence(
                    return_code=1,
                    stderr="connection refused environment_changed=false",
                    environment_changed=False,
                )
            return ExecutionEvidence(return_code=0, environment_changed=False)

    class Verifier:
        def __init__(self):
            self.calls = 0

        def verify_step(self, plan, step):
            del plan, step
            self.calls += 1
            if self.calls == 1:
                return VerificationDecision(
                    status="failed",
                    reason="service temporarily unavailable",
                )
            return VerificationDecision(status="passed", reason="healthy")

    step = PrivilegedStep(
        step_id="start",
        title="启动组件",
        objective="start component",
        risk="medium",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="start_screen_component",
            args={"project_root": str(tmp_path), "component": "worker"},
            risk="medium",
        ),
    )
    plan = PrivilegedPlan(
        plan_id="priv-transient-retry",
        goal="start platform",
        risk="medium",
        steps=[step],
        status="awaiting_confirmation",
    )
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")
    store.save(plan)
    executor = Executor()
    progress = []
    workflow = PrivilegedOpsWorkflow(
        planner=object(),
        execution_agent=object(),
        executor=executor,
        verifier=Verifier(),
        store=store,
        on_progress=progress.append,
    )

    result = workflow.approve_plan(plan.plan_id)

    assert result.kind == "completed"
    assert executor.calls == 2
    assert result.plan.steps[0].execution_attempts == 2
    assert any("有限重试" in item for item in progress)


def test_runtime_failure_rebinds_implementation_and_requires_confirmation(
    tmp_path,
):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
        VerificationDecision,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    class Executor:
        def execute(self, step):
            del step
            return ExecutionEvidence(
                return_code=1,
                stderr="implementation did not satisfy objective",
                environment_changed=False,
            )

    class Verifier:
        def verify_step(self, plan, step):
            del plan, step
            return VerificationDecision(
                status="failed",
                reason="expected state is absent",
            )

    class Context:
        def build(self, goal, supplemental_environment_context=""):
            del goal, supplemental_environment_context
            return SimpleNamespace(planning_blocker=lambda: "")

    class Binder:
        def prepare_step(
            self,
            plan,
            step,
            *,
            grounded_context,
            implementation_feedback="",
        ):
            del plan, step, grounded_context
            assert "rejected_implementation" in implementation_feedback
            return ExecutionBinding(
                kind="verification_only",
                risk="readonly",
                postconditions=[
                    {"checker": "file_exists", "args": {"path": str(tmp_path)}}
                ],
                binding_reason="materially different implementation",
            )

    step = PrivilegedStep(
        step_id="repair",
        title="修复实例",
        objective="repair instance",
        risk="medium",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="stop_klonet_runtime_instance",
            args={"project_root": str(tmp_path)},
            risk="medium",
        ),
    )
    plan = PrivilegedPlan(
        plan_id="priv-runtime-rebind",
        goal="repair platform",
        risk="medium",
        steps=[step],
        status="awaiting_confirmation",
    )
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")
    store.save(plan)
    workflow = PrivilegedOpsWorkflow(
        planner=object(),
        execution_agent=Binder(),
        executor=Executor(),
        verifier=Verifier(),
        store=store,
        context_builder=Context(),
    )

    result = workflow.approve_plan(plan.plan_id)

    assert result.kind == "awaiting_confirmation"
    assert result.plan.is_authorized is False
    assert result.plan.steps[0].execution_binding.kind == "verification_only"
    assert "旧授权已经失效" in result.message
    assert "confirm-priv" in result.message


def test_unfinished_plan_options_prevent_plain_continue_from_replanning(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    plan = PrivilegedPlan(
        plan_id="priv-resume-choice",
        goal="deploy lht",
        risk="medium",
        steps=[PrivilegedStep(step_id="deploy", title="部署", risk="medium")],
        status="awaiting_confirmation",
    )
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")
    store.save(plan)
    workflow = PrivilegedOpsWorkflow(
        planner=object(),
        execution_agent=object(),
        executor=object(),
        verifier=object(),
        store=store,
    )

    result = workflow.unfinished_plan_options()
    classifier_context = workflow.unfinished_plan_context()

    assert result.kind == "recovery_options"
    assert "不会自动执行" in result.message
    assert "confirm-priv priv-resume-choice" in result.message
    assert "plan_id=priv-resume-choice" in classifier_context
    assert "goal=deploy lht" in classifier_context


def test_shell_policy_hard_denies_dynamic_egress_secrets_and_agent_changes(
    tmp_path,
):
    from klonet_agent.ops.privileged.shell_artifact import (
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    policy = ShellArtifactPolicy()
    scripts = (
        "echo $(id)",
        "curl https://example.test/upload",
        "cat /etc/shadow",
        "sed -i 's/x/y/' ops/privileged/policy.py",
        "PASSWORD=plain-text command true",
        "python3 -c 'print(1)'",
    )
    for index, script in enumerate(scripts):
        artifact = create_shell_artifact(
            artifact_id="shell-%s" % index,
            script=script,
            cwd=str(tmp_path),
            run_as="",
            timeout=10,
            environment_fingerprint="",
            declared_changes=[],
            rollback="",
            nonce="nonce-%s" % index,
        )
        assert policy.validate(artifact), script


def test_shell_policy_rejects_normalized_empty_artifact(tmp_path):
    from klonet_agent.ops.privileged.shell_artifact import (
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    artifact = create_shell_artifact(
        artifact_id="shell-empty",
        script="",
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env",
        declared_changes=[str(tmp_path / "marker")],
        rollback="none",
        nonce="nonce",
    )

    assert ShellArtifactPolicy().validate(artifact) == "shell_artifact_empty"


def test_shell_policy_allows_bounded_multiline_configuration(tmp_path):
    from klonet_agent.ops.privileged.shell_artifact import (
        MAX_SCRIPT_LINES,
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    script = "\n".join("printf '%s\\n' line-%s" % ("%s", index) for index in range(80))
    artifact = create_shell_artifact(
        artifact_id="shell-multiline",
        script=script,
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env",
        declared_changes=[str(tmp_path / "config")],
        rollback="remove config",
        nonce="nonce",
    )

    assert MAX_SCRIPT_LINES >= 80
    assert ShellArtifactPolicy().validate(artifact) == ""


def test_shell_policy_allows_dependency_produced_cwd_only_during_compilation(
    tmp_path,
):
    from klonet_agent.ops.privileged.shell_artifact import (
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    future = tmp_path / "future-instance"
    artifact = create_shell_artifact(
        artifact_id="shell-future-cwd",
        script="touch marker",
        cwd=str(future),
        run_as="",
        timeout=10,
        environment_fingerprint="env",
        declared_changes=[str(future / "marker")],
        rollback="remove marker",
        nonce="nonce",
    )
    policy = ShellArtifactPolicy()

    assert policy.validate(artifact) == (
        "shell_cwd_not_existing_absolute_directory"
    )
    assert policy.validate(
        artifact,
        allowed_future_cwds=(future,),
    ) == ""
    future.mkdir()
    assert policy.validate(artifact) == ""


def test_shell_policy_reports_actual_line_limit(tmp_path):
    from klonet_agent.ops.privileged.shell_artifact import (
        MAX_SCRIPT_LINES,
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    actual_lines = MAX_SCRIPT_LINES + 1
    script = "\n".join("true" for _ in range(actual_lines))
    artifact = create_shell_artifact(
        artifact_id="shell-too-many-lines",
        script=script,
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env",
        declared_changes=[str(tmp_path / "config")],
        rollback="remove config",
        nonce="nonce",
    )

    assert ShellArtifactPolicy().validate(artifact) == (
        "shell_artifact_too_many_lines=%s>%s"
        % (actual_lines, MAX_SCRIPT_LINES)
    )


def test_executor_refuses_changed_expired_drifted_or_reused_shell(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.shell_artifact import create_shell_artifact

    artifact = create_shell_artifact(
        artifact_id="shell-once",
        script="touch %s" % (tmp_path / "once"),
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env-a",
        declared_changes=[str(tmp_path / "once")],
        rollback="remove once",
        nonce="nonce",
    )
    artifact.status = "approved"
    artifact.approved_contract_hash = artifact.contract_hash
    step = PrivilegedStep(
        step_id="once",
        title="one time",
        objective="create marker once",
        reason="test",
        success_criteria=["marker exists"],
        risk="high",
        approval_scope="step",
        execution_binding=ExecutionBinding(
            kind="shell_artifact",
            risk="high",
            approval_scope="step",
            shell_artifact=artifact,
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        ),
    )
    drifted = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-b"
    ).execute(step)
    assert drifted.stderr == "shell_artifact_environment_fingerprint_changed"

    artifact.environment_fingerprint = "env-a"
    completed = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    reused = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert completed.return_code == 0
    assert reused.stderr == "shell_artifact_not_exactly_approved"

    artifact.status = "approved"
    artifact.expires_at = "2000-01-01T00:00:00+00:00"
    artifact.approved_contract_hash = artifact.contract_hash
    expired = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert expired.stderr == "shell_artifact_expired"

    artifact.status = "approved"
    artifact.expires_at = "2999-01-01T00:00:00+00:00"
    artifact.approved_contract_hash = artifact.contract_hash
    artifact.script += "echo changed\n"
    changed = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert changed.stderr == "shell_artifact_contract_not_exactly_approved"


def test_verifier_probe_evidence_is_persisted_and_cannot_override_failure():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = PrivilegedStep(
        step_id="verify",
        title="verify",
        objective="verify service",
        reason="test",
        success_criteria=["service is active"],
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
        evidence=ExecutionEvidence(return_code=9, stderr="failed"),
    )
    plan = PrivilegedPlan(
        plan_id="priv-v3",
        goal="verify service",
        risk="medium",
        steps=[step],
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "service",
                            "args": {"services": ["nginx"]},
                            "purpose": "observe service state",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "passed",
                    "summary": "service looks active",
                    "confirmed_facts": ["nginx active"],
                    "failed_criteria": [],
                    "missing_evidence": [],
                    "reflection": "the command itself still failed",
                    "recommended_next_focus": "inspect command failure",
                }
            ),
        ]
    )
    verifier = PrivilegedVerifierAgent(
        llm,
        probe_runner=lambda requests: "nginx active",
    )

    decision = verifier.verify_step(plan, step)

    assert decision.status == "failed"
    assert decision.probe_history[0]["requests"][0]["probe"] == "service"
    assert "return_code=9" in decision.failures


def test_replan_rejects_same_failed_remaining_route(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
        VerificationDecision,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    def step(step_id):
        return PrivilegedStep(
            step_id=step_id,
            title="启动平台",
            objective="start the Klonet platform",
            reason="the user requested deployment",
            success_criteria=["platform is healthy"],
            risk="medium",
            status="approved",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                action="manual_checkpoint",
                args={"reason": "same route"},
                risk="medium",
                postconditions=[{"checker": "exit_code_zero", "args": {}}],
            ),
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        )

    original = PrivilegedPlan(
        plan_id="priv-loop",
        goal="deploy platform",
        risk="medium",
        status="approved",
        steps=[step("start")],
    )
    original.authorize()
    replacement = PrivilegedPlan(
        plan_id="priv-replacement",
        goal="deploy platform",
        risk="medium",
        steps=[step("start-again")],
    )

    class Planner:
        def plan(self, goal, **kwargs):
            del goal, kwargs
            return replacement

    class PreboundExecutionAgent:
        @staticmethod
        def prepare_plan(plan, *, grounded_context):
            del grounded_context
            plan.status = "awaiting_confirmation"
            return plan

    class ContextBuilder:
        @staticmethod
        def current_environment_fingerprint():
            return "env"

        @staticmethod
        def build(goal, **kwargs):
            del goal, kwargs
            return GroundedPlanContext(
                knowledge_evidence="recovery runbook",
                environment_evidence="same failure evidence",
                action_catalog="capability summary",
            )

    class Executor:
        @staticmethod
        def execute(current_step):
            del current_step
            return ExecutionEvidence(return_code=1, stderr="same failure")

    class Verifier:
        @staticmethod
        def verify_step(plan, current_step):
            del plan, current_step
            return VerificationDecision(
                status="failed",
                reason="same failure",
                reflection="the route did not change the prerequisite",
            )

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        execution_agent=PreboundExecutionAgent(),
        executor=Executor(),
        verifier=Verifier(),
        context_builder=ContextBuilder(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )
    workflow.store.save(original)

    result = workflow.execute("priv-loop")

    assert result.kind == "paused"
    assert "没有实质差异" in result.message
    assert result.plan.replan_attempts == 1


def test_schema_v2_raw_command_migrates_to_non_executable_audit_record():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan

    raw = {
        "schema_version": 2,
        "plan_id": "priv-old",
        "goal": "deploy",
        "risk": "medium",
        "status": "approved",
        "authorized_hash": hashlib.sha256(b"old").hexdigest(),
        "steps": [
            {
                "step_id": "legacy",
                "title": "legacy",
                "command": "klonet deploy",
                "risk": "medium",
                "status": "approved",
            }
        ],
    }

    plan = PrivilegedPlan.from_dict(raw)

    assert plan.schema_version == 3
    assert plan.authorized_hash == ""
    assert plan.steps[0].execution_binding.kind == "legacy_command"
    assert plan.steps[0].status == "blocked"
