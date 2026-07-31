from __future__ import annotations

import json


ENTRY_FILES = (
    "gun.py",
    "master_main.py",
    "celery_worker.py",
    "web_terminal_main.py",
    "worker_gun.py",
    "worker_main.py",
)


def _backend_repo(tmp_path):
    backend = tmp_path / "demo_project" / "vemu_uestc"
    mains = backend / "mains"
    config = backend / "vemu_config" / "config.py"
    mains.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    (backend / "__init__.py").write_text("", encoding="utf-8")
    config.write_text(
        (
            "class DemoConfig:\n"
            "    master_port = 5000\n"
            "    redis_port = 6380\n"
            "    redis_password = 'never-render-this'\n\n"
            "    mysql_port = 3307\n"
            "    mysql_password = 'mysql-never-render-this'\n"
            "    rabbitmq_port = 5673\n"
            "    rabbitmq_password = 'rabbit-never-render-this'\n\n"
            "PROJ_CONFIG = DemoConfig()\n"
        ),
        encoding="utf-8",
    )
    for name in ENTRY_FILES:
        (mains / name).write_text("# entry\n", encoding="utf-8")
    return backend


def test_environment_facts_separate_source_package_and_runtime_root(tmp_path):
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )

    backend = _backend_repo(tmp_path)
    facts = EnvironmentFactCollector().collect([str(backend)])
    project = facts.projects[0]

    assert project.source_repo_root == str(backend)
    assert project.backend_package_root == str(backend)
    assert project.platform_root == str(backend.parent)
    assert project.runtime_cwd == str(backend.parent)
    assert project.entry_source_root == str(backend / "mains")
    assert project.readiness == "preparable"


def test_environment_facts_keep_redis_secret_as_metadata_only(tmp_path):
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )

    backend = _backend_repo(tmp_path)
    facts = EnvironmentFactCollector().collect([str(backend)])
    rendered = facts.render_for_planner()

    assert facts.redis.ports == (6380,)
    assert facts.redis.authentication.configured is True
    assert facts.redis.authentication.access == "reference_only"
    assert "never-render-this" not in rendered
    assert '"configured": true' in rendered


def test_environment_facts_cover_common_service_endpoints_without_secrets(
    tmp_path,
):
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )

    backend = _backend_repo(tmp_path)
    facts = EnvironmentFactCollector().collect([str(backend)])
    rendered = facts.render_for_planner()

    assert facts.mysql.ports == (3307,)
    assert facts.mysql.authentication.configured is True
    assert facts.rabbitmq.ports == (5673,)
    assert facts.rabbitmq.authentication.configured is True
    assert "mysql-never-render-this" not in rendered
    assert "rabbit-never-render-this" not in rendered
    assert hasattr(facts.capabilities, "docker_binary")
    assert isinstance(facts.nginx.config_directories, tuple)


def test_context_renders_structured_environment_model(tmp_path):
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    backend = _backend_repo(tmp_path)

    class Builder(PrivilegedPlanContextBuilder):
        @staticmethod
        def _candidate_project_roots():
            return [str(backend)]

    context = Builder(
        knowledge_search=lambda *args, **kwargs: "knowledge",
        environment_inspector=lambda args: "environment",
    ).build("部署平台")

    prompt = context.render()
    model = context.facts["environment_model"]
    assert "Structured environment facts" in prompt
    assert model["projects"][0]["platform_root"] == str(backend.parent)
    assert context.audit_summary()["environment_schema_version"] == 1


def test_planner_replaces_source_repo_runtime_with_grounded_parent_plan(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.tests.test_privileged_agents import FakeLLM

    backend = _backend_repo(tmp_path)
    facts = EnvironmentFactCollector().collect([str(backend)])
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence="environment",
        action_catalog="actions",
        facts={"environment_model": facts.to_dict()},
    )
    semantic = json.dumps(
        {
            "status": "ready",
            "goal": "部署平台",
            "steps": [
                {
                    "step_id": "prepare",
                    "title": "准备运行入口",
                    "objective": "make all platform entry files available in the runtime directory",
                    "reason": "the environment model marks the runtime layout as preparable",
                    "success_criteria": ["all entry files exist in runtime cwd"],
                    "risk_suggestion": "medium",
                },
                {
                    "step_id": "start",
                    "title": "启动平台",
                    "objective": "start all Klonet platform components",
                    "reason": "runtime entries will be prepared by the preceding step",
                    "depends_on": ["prepare"],
                    "success_criteria": ["platform components are running"],
                    "risk_suggestion": "medium",
                },
            ],
        }
    )
    plan = PrivilegedPlannerAgent(FakeLLM([semantic])).plan(
        "部署平台",
        grounded_context=context,
    )
    assert all(step.execution_binding is None for step in plan.steps)
    binder = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "prepare_project_files",
                    "selection_reason": "registered preparation action",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "args": {
                        "project_root": str(backend.parent),
                        "source_root": str(backend / "mains"),
                    },
                    "binding_reason": "grounded runtime layout",
                }
            ),
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "start_platform_screens",
                    "selection_reason": "registered platform startup action",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "args": {
                        "platform": "demo",
                        "project_root": str(backend.parent),
                    },
                    "binding_reason": "grounded runtime cwd",
                }
            ),
        ]
    )
    bound = PrivilegedExecutionAgent(
        binder,
        enable_implementation_plans=False,
    ).prepare_plan(
        plan,
        grounded_context=context,
    )
    assert bound.grounding["planner_source"] == "llm_agentic_v3"
    assert [
        step.execution_binding.args["project_root"]
        for step in bound.steps
    ] == [str(backend.parent), str(backend.parent)]


def test_invalid_planner_output_has_no_deterministic_workflow_fallback(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.tests.test_privileged_grounded_planning import FakeLLM

    backend = _backend_repo(tmp_path)
    facts = EnvironmentFactCollector().collect([str(backend)])
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence="environment",
        action_catalog="actions",
        facts={
            "candidate_project_roots": [str(backend)],
            "environment_model": facts.to_dict(),
        },
    )
    invalid = json.dumps({"status": "ready", "steps": []})
    import pytest

    with pytest.raises(ValueError, match="valid semantic plan"):
        PrivilegedPlannerAgent(FakeLLM([invalid, invalid])).plan(
            "部署平台",
            grounded_context=context,
        )


def test_environment_model_allows_future_project_root_only_with_prior_producer(
    tmp_path,
):
    import pytest

    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )
    from klonet_agent.ops.privileged.planner import _validate_environment_model

    backend = _backend_repo(tmp_path)
    facts = EnvironmentFactCollector().collect([str(backend)])
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence="environment",
        action_catalog="actions",
        facts={"environment_model": facts.to_dict()},
    )
    future_root = tmp_path / "codexsim"
    start = PrivilegedStep(
        step_id="start",
        title="start future instance",
        action="start_screen_component",
        args={
            "platform": "codexsim",
            "component": "master",
            "screen_session": "codexsim_m",
            "project_root": str(future_root),
        },
        risk="medium",
    )

    with pytest.raises(
        ValueError,
        match="future_project_root_has_no_prior_producer",
    ):
        _validate_environment_model([start], context)

    producer = PrivilegedStep(
        step_id="copy",
        title="copy runtime",
        action="sync_directory",
        args={
            "source": str(backend.parent),
            "destination": str(future_root),
        },
        risk="medium",
    )
    _validate_environment_model([producer, start], context)


def test_new_platform_deployment_replay_compiles_to_confirmable_plan(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.tests.test_privileged_agents import FakeLLM

    backend = _backend_repo(tmp_path)
    source_root = backend.parent
    target_root = tmp_path / "codexsim"
    target_config = target_root / "vemu_uestc" / "vemu_config" / "config.py"
    nginx_config = tmp_path / "nginx.conf"
    nginx_config.write_text("http {\n}\n", encoding="utf-8")
    facts = EnvironmentFactCollector().collect([str(backend)])
    context = GroundedPlanContext(
        knowledge_evidence="Klonet deployment knowledge",
        environment_evidence="source and ports are grounded",
        action_catalog="registered actions",
        facts={"environment_model": facts.to_dict()},
    )
    semantic = json.dumps(
        {
            "status": "ready",
            "goal": "deploy codexsim",
            "steps": [
                {
                    "step_id": "copy",
                    "title": "复制源码",
                    "objective": "copy the current platform source into codexsim",
                    "reason": "a separate instance needs its own tree",
                    "expected_effects": ["codexsim project tree exists"],
                    "success_criteria": ["copied project tree exists"],
                    "risk_suggestion": "medium",
                },
                {
                    "step_id": "configure",
                    "title": "配置独立端口",
                    "objective": "add and activate a codexsim configuration",
                    "reason": "the new instance needs non-conflicting ports",
                    "depends_on": ["copy"],
                    "expected_effects": ["codexsim configuration is active"],
                    "success_criteria": ["new ports are present in config"],
                    "risk_suggestion": "medium",
                },
                {
                    "step_id": "start",
                    "title": "启动平台组件",
                    "objective": "start all codexsim runtime components",
                    "reason": "the configured instance must run",
                    "depends_on": ["configure"],
                    "expected_effects": ["codexsim screen sessions exist"],
                    "success_criteria": ["all runtime sessions exist"],
                    "risk_suggestion": "high",
                },
                {
                    "step_id": "route",
                    "title": "配置反向代理",
                    "objective": "add the codexsim Nginx route",
                    "reason": "the platform needs an external route",
                    "depends_on": ["start"],
                    "expected_effects": ["codexsim route is configured"],
                    "success_criteria": ["Nginx config contains codexsim route"],
                    "risk_suggestion": "high",
                },
                {
                    "step_id": "verify",
                    "title": "验收新平台",
                    "objective": "verify the codexsim master session exists",
                    "reason": "deployment needs an observable result",
                    "depends_on": ["route"],
                    "expected_effects": [],
                    "success_criteria": ["codexsim_m screen session exists"],
                    "risk_suggestion": "readonly",
                },
            ],
        }
    )
    plan = PrivilegedPlannerAgent(FakeLLM([semantic])).plan(
        "部署一个名为 codexsim 的新平台",
        grounded_context=context,
    )

    def decomposition(step_id, title, objective, risk, changes):
        return json.dumps(
            {
                "status": "ready",
                "reason": "one atomic capability realizes this semantic step",
                "implementation_steps": [
                    {
                        "id": step_id,
                        "title": title,
                        "objective": objective,
                        "reason": "bounded deployment operation",
                        "depends_on": [],
                        "expected_changes": changes,
                        "success_criteria": [objective],
                        "risk_suggestion": risk,
                    }
                ],
            }
        )

    def selection(action):
        return json.dumps(
            {
                "status": "registered_action",
                "action": action,
                "selection_reason": "registered capability directly covers objective",
            }
        )

    def contract(args, checks):
        return json.dumps(
            {
                "status": "ready",
                "reason": "",
                "args": args,
                "binding_reason": "arguments come from the deployment plan",
                "resolved_from_evidence": ["deployment evidence"],
                "preconditions": [],
                "postconditions": checks,
            }
        )

    binder = FakeLLM(
        [
            decomposition("sync", "复制项目树", "copy project tree", "medium", [str(target_root)]),
            selection("sync_directory"),
            contract(
                {"source": str(source_root), "destination": str(target_root)},
                [{"checker": "file_exists", "args": {"path": str(target_root)}}],
            ),
            decomposition("edit", "修改配置", "activate codexsim config", "medium", [str(target_config)]),
            selection("edit_text_file"),
            contract(
                {
                    "path": str(target_config),
                    "operation": "insert_before",
                    "anchor": "PROJ_CONFIG = DemoConfig()",
                    "content": "class CodexsimConfig(DemoConfig):\n    master_port = 60001\n",
                },
                [{"checker": "file_contains", "args": {"path": str(target_config), "text": "class CodexsimConfig"}}],
            ),
            decomposition("screens", "启动组件", "start codexsim screens", "high", ["codexsim sessions exist"]),
            selection("start_platform_screens"),
            contract(
                {"platform": "codexsim", "project_root": str(target_root)},
                [{"checker": "screen_session_exists", "args": {"session": "codexsim_m"}}],
            ),
            decomposition("nginx", "添加路由", "insert codexsim Nginx route", "high", [str(nginx_config)]),
            selection("edit_text_file"),
            contract(
                {
                    "path": str(nginx_config),
                    "operation": "insert_before",
                    "anchor": "}",
                    "content": "    location /codexsim/ { proxy_pass http://127.0.0.1:60001; }",
                },
                [{"checker": "file_contains", "args": {"path": str(nginx_config), "text": "/codexsim/"}}],
            ),
            decomposition("check", "验收会话", "verify codexsim master session", "readonly", []),
            json.dumps({"status": "verification_only", "selection_reason": "registered checker proves result"}),
            json.dumps(
                {
                    "status": "ready",
                    "reason": "",
                    "binding_reason": "screen checker observes deployment result",
                    "resolved_from_evidence": ["planned session name"],
                    "postconditions": [
                        {"checker": "screen_session_exists", "args": {"session": "codexsim_m"}}
                    ],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(binder).prepare_plan(
        plan,
        grounded_context=context,
    )

    assert bound.status == "awaiting_confirmation"
    assert len(bound.steps) == 5
    actions = [
        step.implementation_plan.steps[0].execution_binding.action
        for step in bound.steps[:4]
    ]
    assert actions == [
        "sync_directory",
        "edit_text_file",
        "start_platform_screens",
        "edit_text_file",
    ]
    assert bound.steps[4].implementation_plan.steps[0].execution_binding.kind == (
        "verification_only"
    )
    bound.authorize()
    assert bound.is_authorized


def test_validate_and_prepare_actions_support_nested_backend_layout(tmp_path):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )
    from klonet_agent.ops.privileged.contracts import PrivilegedStep

    backend = _backend_repo(tmp_path)
    platform_root = backend.parent
    runner = DirectPrivilegedActionRunner()
    validate = PrivilegedStep(
        step_id="validate",
        title="validate",
        action="validate_project_files",
        args={"project_root": str(platform_root)},
        risk="readonly",
    )
    prepare = PrivilegedStep(
        step_id="prepare",
        title="prepare",
        action="prepare_project_files",
        args={
            "project_root": str(platform_root),
            "source_root": str(backend / "mains"),
        },
        risk="medium",
    )

    validate_result = runner(validate)
    prepare_result = runner(prepare)

    assert validate_result.status == "completed"
    assert "vemu_uestc/mains/gun.py" in validate_result.output
    assert prepare_result.status == "completed"
    assert (platform_root / "gun.py").is_file()


def test_helper_reads_nested_backend_config_from_platform_root(tmp_path):
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    backend = _backend_repo(tmp_path)
    helper_path = Path(__file__).resolve().parents[1] / "scripts" / "klonet-agent-op"
    loader = importlib.machinery.SourceFileLoader(
        "klonet_agent_op_environment_facts_test",
        str(helper_path),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    assert module.configured_ports(str(backend.parent)) == [5000]
