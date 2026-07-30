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
    from klonet_agent.tests.test_privileged_grounded_planning import (
        FakeLLM,
        _action_payload,
    )

    backend = _backend_repo(tmp_path)
    facts = EnvironmentFactCollector().collect([str(backend)])
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence="environment",
        action_catalog="actions",
        facts={"environment_model": facts.to_dict()},
    )
    payload = _action_payload(
        action="start_platform_screens",
        args={"platform": "demo", "project_root": str(backend)},
    )

    plan = PrivilegedPlannerAgent(FakeLLM([payload, payload])).plan(
        "部署平台",
        grounded_context=context,
    )

    assert plan.grounding["planner_source"] == "deterministic_grounded_resolver"
    assert all(
        step.args.get("project_root") != str(backend)
        for step in plan.steps
        if step.action in {
            "validate_project_files",
            "prepare_project_files",
            "start_platform_screens",
        }
    )
    assert plan.steps[-1].args["project_root"] == str(backend.parent)


def test_deterministic_plan_prepares_parent_runtime_from_nested_mains(tmp_path):
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
    invalid = json.dumps({"steps": []})
    plan = PrivilegedPlannerAgent(FakeLLM([invalid, invalid])).plan(
        "部署平台",
        grounded_context=context,
    )

    assert [step.action for step in plan.steps] == [
        "validate_project_files",
        "prepare_project_files",
        "start_platform_screens",
    ]
    assert plan.steps[1].args == {
        "project_root": str(backend.parent),
        "source_root": str(backend / "mains"),
    }
    assert plan.steps[2].args["project_root"] == str(backend.parent)


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
