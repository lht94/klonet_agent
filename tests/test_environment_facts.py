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
        knowledge_search=lambda *args, **kwargs: (
            "knowledge example /root/obsolete/platform"
        ),
        environment_inspector=lambda args: "environment",
    ).build("閮ㄧ讲骞冲彴")

    prompt = context.render()
    model = context.facts["environment_model"]
    assert "Structured environment facts" in prompt
    assert "/root/obsolete/platform" not in prompt
    assert "knowledge-path omitted" in prompt
    assert model["projects"][0]["platform_root"] == str(backend.parent)
    assert context.audit_summary()["environment_schema_version"] == 1



def test_environment_model_allows_future_project_root_only_with_prior_producer(
    tmp_path,
):
    import pytest

    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )
    from klonet_agent.ops.privileged.action_contracts import _validate_environment_model

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


def test_environment_model_requires_a_prior_producer_for_future_shell_cwd(
    tmp_path,
):
    import pytest

    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import (
        EnvironmentFactCollector,
    )
    from klonet_agent.ops.privileged.action_contracts import _validate_environment_model

    backend = _backend_repo(tmp_path)
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence="environment",
        action_catalog="actions",
        facts={
            "environment_model": EnvironmentFactCollector()
            .collect([str(backend)])
            .to_dict()
        },
    )
    future_root = tmp_path / "lht"
    start = PrivilegedStep(
        step_id="start",
        title="start from future root",
        action="shell_artifact",
        args={
            "cwd": str(future_root),
            "declared_changes": ["lht screen sessions"],
        },
        risk="high",
    )

    with pytest.raises(
        ValueError,
        match="shell_future_cwd_has_no_prior_producer",
    ):
        _validate_environment_model([start], context)

    producer = PrivilegedStep(
        step_id="copy",
        title="produce lht root",
        action="shell_artifact",
        args={
            "cwd": str(tmp_path),
            "declared_changes": [str(future_root)],
        },
        risk="medium",
    )
    _validate_environment_model([producer, start], context)



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
