from __future__ import annotations


def test_exact_text_deletion_compiles_to_replace_with_empty_content():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    unwanted = (
        "def KLONET_E2E_INJECTED_FAILURE():\n"
        '    raise RuntimeError("KLONET_E2E_INJECTED_FAILURE")\n\n'
        "KLONET_E2E_INJECTED_FAILURE()\n\n"
    )
    step = PrivilegedStep(
        step_id="remove-injected",
        title="Remove injected boot failure",
        objective="Delete the exact injected block from /srv/102/mains/master_main.py",
        risk="medium",
    )

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "edit_text_file",
            "args": {
                "path": "/srv/102/mains/master_main.py",
                "operation": "replace_once",
                "anchor": unwanted,
                "content": "unrelated file prefix must not be used",
            },
        },
        step,
        None,
        [],
    )

    assert binding.action == "replace_text_in_file"
    assert binding.args == {
        "path": "/srv/102/mains/master_main.py",
        "old_text": unwanted,
        "new_text": "",
    }
    assert binding.postconditions == [
        {
            "checker": "file_not_contains",
            "args": {"path": "/srv/102/mains/master_main.py", "text": unwanted},
        },
        {
            "checker": "python_file_syntax_valid",
            "args": {"path": "/srv/102/mains/master_main.py"},
        },
    ]


def test_python_source_edit_replaces_import_execution_check_with_syntax_check():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    path = "/srv/102/mains/master_main.py"
    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "replace_text_in_file",
            "args": {"path": path, "old_text": "bad()", "new_text": "good()"},
            "postconditions": [
                {"checker": "file_contains", "args": {"path": path, "text": "good()"}},
                {
                    "checker": "python_import_succeeds",
                    "args": {"module": "master_main", "cwd": "/srv/102/mains"},
                },
            ],
        },
        PrivilegedStep(
            step_id="repair",
            title="Repair Python startup entry",
            objective="Replace the broken startup call",
            risk="medium",
        ),
        None,
        [],
    )

    assert binding.postconditions == [
        {"checker": "file_contains", "args": {"path": path, "text": "good()"}},
        {"checker": "python_file_syntax_valid", "args": {"path": path}},
    ]


def test_grounded_function_removal_rejects_model_whole_file_rewrite():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    path = "/srv/102/mains/master_main.py"
    block = (
        "\n\ndef KLONET_E2E_INJECTED_FAILURE():\n"
        "    raise RuntimeError('boom')\n\n\n"
        "KLONET_E2E_INJECTED_FAILURE()\n"
    )
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence=(
            "read_ops_file\nresolved_path=%s\nimport app\n%s\napp = create_app()\n"
            % (path, block)
        ),
        action_catalog="catalog",
    )
    step = PrivilegedStep(
        step_id="repair",
        title="Remove KLONET_E2E_INJECTED_FAILURE function",
        objective="Delete its definition and module-level call from %s" % path,
        risk="medium",
    )

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "edit_text_file",
            "args": {
                "path": path,
                "operation": "replace_file",
                "content": "import app\nSECRET_KEY = [REDACTED]\napp = create_app()\n",
            },
        },
        step,
        context,
        [],
    )

    assert binding.action == "replace_text_in_file"
    assert binding.args == {
        "path": path,
        "old_text": (
            "\ndef KLONET_E2E_INJECTED_FAILURE():\n"
            "    raise RuntimeError('boom')\n\n\n"
            "KLONET_E2E_INJECTED_FAILURE()\n\n"
        ),
        "new_text": "",
    }


def test_redacted_placeholder_cannot_become_file_write_action():
    from klonet_agent.ops.privileged.action_contracts import _validate_action_semantics

    assert _validate_action_semantics(
        "edit_text_file",
        {
            "path": "/srv/app.py",
            "operation": "replace_file",
            "content": "SECRET_KEY = [REDACTED]",
        },
    ) == "action=edit_text_file redacted_placeholder_cannot_be_written"


def test_partial_python_function_header_cannot_become_confirmable_deletion():
    from klonet_agent.ops.privileged.action_contracts import _validate_action_semantics

    assert _validate_action_semantics(
        "replace_text_in_file",
        {
            "path": "/srv/102/master_main.py",
            "old_text": "def KLONET_E2E_INJECTED_FAILURE",
            "new_text": "",
        },
    ) == "action=replace_text_in_file incomplete_python_function_deletion"

    assert not _validate_action_semantics(
        "replace_text_in_file",
        {
            "path": "/srv/102/master_main.py",
            "old_text": "def injected_failure():\n    raise RuntimeError('boom')",
            "new_text": "",
        },
    )

    assert _validate_action_semantics(
        "replace_text_in_file",
        {
            "path": "/srv/102/master_main.py",
            "old_text": "KLONET_E2E_INJECTED_FAILURE",
            "new_text": "",
        },
    ) == "action=replace_text_in_file ungrounded_python_identifier_deletion"


def test_existing_runtime_start_rejects_nonexistent_mains_without_predecessor(tmp_path):
    from klonet_agent.ops.privileged.action_contracts import _validate_host_facts
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import _host_fact_is_planned_future

    missing = tmp_path / "instance" / "mains"
    args = {
        "project_root": str(missing),
        "platform": "instance",
        "component": "master",
        "screen_session": "instance_m",
    }
    problem = _validate_host_facts("start_screen_component", args)

    assert problem == "grounding_failed=project_root_not_directory"
    assert not _host_fact_is_planned_future(
        "start_screen_component",
        args,
        PrivilegedStep(
            step_id="start", title="Start master", objective="Start existing master"
        ),
        problem,
        [],
    )


def test_forced_start_compiles_new_screen_from_frozen_project_root(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    root = tmp_path / "102"
    mains = root / "mains"
    mains.mkdir(parents=True)
    for name in ("master_main.py", "worker_main.py"):
        (mains / name).write_text("# entry\n", encoding="utf-8")
    step = PrivilegedStep(
        step_id="repair__start",
        title="Start master screen component",
        objective="Start the master role after repairing its source",
        risk="medium",
    )
    resources = [
        PlanResource(
            name="project_root",
            kind="path",
            status="frozen",
            role="project_root",
            value=str(root),
            source="running_platforms",
            consumers=["repair.project_root"],
        )
    ]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {"action": "start_screen_component", "args": {}},
        step,
        None,
        resources,
    )

    assert binding.args["component"] == "master"
    assert binding.args["platform"] == "102"
    assert binding.args["screen_session"] == "102_m"
    assert binding.args["project_root"] == str(mains)


def test_start_consumes_frozen_predecessor_uid_and_python(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    root = tmp_path / "102"
    mains = root / "mains"
    mains.mkdir(parents=True)
    for name in ("master_main.py", "worker_main.py"):
        (mains / name).write_text("# entry\n", encoding="utf-8")
    step = PrivilegedStep(
        step_id="repair__start-master",
        title="Start master screen component",
        objective="Start master for %s after source repair" % root,
        risk="medium",
    )
    resources = [
        PlanResource("project_root", "path", "frozen", "project_root", str(root), "running_platforms", consumers=["repair.project_root"]),
        PlanResource("master_uid", "identifier", "frozen", "master_uid", 997, "running_platforms", consumers=["repair.run_as_uid"]),
        PlanResource("master_python", "path", "frozen", "master_python_executable", "/usr/bin/python3.8", "running_platforms", consumers=["repair.python_executable"]),
    ]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {"action": "start_screen_component", "args": {}},
        step,
        None,
        resources,
    )

    assert binding.args["run_as_uid"] == "997"
    assert binding.args["python_executable"] == "/usr/bin/python3.8"


def test_exact_role_stop_is_forced_without_vemu_named_root():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _forced_registered_action_for_step,
    )

    step = PrivilegedStep(
        step_id="stop",
        title="Stop current master runtime process",
        objective="Precisely stop master for /srv/102",
        risk="high",
    )

    assert _forced_registered_action_for_step(step) == "stop_klonet_component"


def test_source_recovery_orders_edit_stop_start_then_verification():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _order_source_runtime_recovery_items,
    )

    semantic = PrivilegedStep(
        step_id="repair",
        title="Repair master startup source",
        objective="Remove injected failure; recover master for /srv/102",
        expected_changes=[
            "restart unhealthy master role at 27694 and backend health succeeds"
        ],
        risk="high",
    )
    items = [
        {"id": "edit", "title": "Remove injected function", "objective": "Edit source", "depends_on": []},
        {"id": "restart", "title": "Restart master screen component", "objective": "Restart master", "depends_on": ["edit"]},
        {"id": "stop", "title": "Stop current master runtime process", "objective": "Stop master", "depends_on": ["restart"]},
        {"id": "verify", "title": "Verify backend health", "objective": "Verify master health", "depends_on": ["stop"]},
    ]

    result = _order_source_runtime_recovery_items(items, semantic)

    assert [item["id"] for item in result] == ["edit", "stop", "restart", "verify"]
    assert [item["depends_on"] for item in result] == [
        [], ["edit"], ["stop"], ["restart"],
    ]


def test_source_mutation_coverage_rejects_stop_start_without_edit():
    import pytest

    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        _validate_source_mutation_action_coverage,
    )

    path = "/srv/102/mains/master_main.py"
    semantic = PrivilegedStep(
        step_id="repair",
        title="Remove injected function",
        objective="Delete the injected function from %s" % path,
        risk="high",
    )
    runtime_only = [
        PrivilegedStep(
            step_id="stop",
            title="Stop master",
            objective="Stop master",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                risk="high",
                action="stop_klonet_component",
                args={"runtime_cwd": "/srv/102", "component": "master", "pid": 1, "port": 27694},
            ),
        ),
        PrivilegedStep(
            step_id="start",
            title="Start master",
            objective="Start master",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                risk="medium",
                action="start_screen_component",
                args={"component": "master", "project_root": "/srv/102/mains"},
            ),
        ),
    ]

    with pytest.raises(ExecutionBindingError, match="source_mutation_action_missing"):
        _validate_source_mutation_action_coverage(semantic, runtime_only)

    runtime_only.insert(
        0,
        PrivilegedStep(
            step_id="edit",
            title="Edit source",
            objective="Edit source",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                risk="medium",
                action="replace_text_in_file",
                args={"path": path, "old_text": "bad", "new_text": "good"},
            ),
        ),
    )
    _validate_source_mutation_action_coverage(semantic, runtime_only)


def test_preserved_healthy_worker_is_not_required_as_recovery_action():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _required_runtime_recovery_roles,
    )

    step = PrivilegedStep(
        step_id="repair",
        title="Restart master and verify worker",
        objective="Recover master for /srv/102",
        expected_changes=[
            "restart unhealthy master role",
            "preserve healthy worker role without restart",
        ],
    )

    assert _required_runtime_recovery_roles(step) == {"master"}


def test_grounded_function_removal_includes_adjacent_call_and_local_whitespace():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        _expand_grounded_python_function_removal,
    )

    definition = "def injected_failure():\n    raise RuntimeError('boom')"
    source = (
        "import app\n\n\n"
        + definition
        + "\n\ninjected_failure()\n\n# startup\napp.run()\n"
    )
    context = GroundedPlanContext(
        knowledge_evidence="frozen",
        environment_evidence="[ev probe=ops_file]\n" + source,
        action_catalog="audited",
    )

    block = _expand_grounded_python_function_removal(definition, context)
    repaired = source.replace(block, "", 1)

    assert block == (
        "\ndef injected_failure():\n    raise RuntimeError('boom')"
        "\n\ninjected_failure()\n\n"
    )
    assert repaired == "import app\n\n# startup\napp.run()\n"


def test_grounded_bare_function_name_expands_before_contract_validation():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    name = "KLONET_E2E_INJECTED_FAILURE"
    path = "/srv/102/mains/master_main.py"
    source = (
        "import app\n\n\n"
        "def %s():\n    raise RuntimeError('boom')\n\n\n%s()\n\n"
        "app = create_app()\n"
    ) % (name, name)
    context = GroundedPlanContext(
        "frozen", "[ev probe=ops_file]\n" + source, "audited"
    )

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "replace_text_in_file",
            "args": {"path": path, "old_text": name, "new_text": ""},
        },
        PrivilegedStep(
            step_id="remove",
            title="Remove injected function",
            objective="Delete %s and its call" % name,
            risk="medium",
        ),
        context,
        [],
    )

    repaired = source.replace(binding.args["old_text"], "", 1)
    assert repaired == "import app\n\napp = create_app()\n"


def test_call_only_anchor_expands_when_semantics_promise_definition_and_call():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    name = "KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE"
    path = "/srv/102/mains/master_main.py"
    source = (
        "import app\n\n\n"
        "def %s():\n    raise RuntimeError('boom')\n\n\n%s()\n\n"
        "app = create_app()\n"
    ) % (name, name)
    context = GroundedPlanContext(
        "frozen", "[ev probe=ops_file]\n" + source, "audited"
    )

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "replace_text_in_file",
            "args": {"path": path, "old_text": "%s()" % name, "new_text": ""},
        },
        PrivilegedStep(
            step_id="remove",
            title="Remove injected startup failure",
            objective="Delete the %s function definition and its unconditional call" % name,
            risk="medium",
        ),
        context,
        [],
    )

    assert "def %s" % name in binding.args["old_text"]
    assert "%s()" % name in binding.args["old_text"]
    assert source.replace(binding.args["old_text"], "", 1) == (
        "import app\n\napp = create_app()\n"
    )


def test_grounded_function_removal_discards_model_anchor_with_unrelated_prefix():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        _expand_grounded_python_function_removal,
    )

    source = (
        "from app import create\n\n\n"
        "def injected_failure():\n    raise RuntimeError('boom')\n\n"
        "injected_failure()\n\n# startup\napp = create()\n"
    )
    context = GroundedPlanContext(
        "frozen", "[ev probe=ops_file]\n" + source, "audited"
    )
    overbroad = (
        "from app import create\n\n\n"
        "def injected_failure():\n    raise RuntimeError('boom')\n\n"
        "injected_failure()\n\n# startup"
    )

    block = _expand_grounded_python_function_removal(overbroad, context)
    repaired = source.replace(block, "", 1)

    assert "from app import create" not in block
    assert "# startup" not in block
    assert repaired == "from app import create\n\n# startup\napp = create()\n"


def test_repeated_identical_source_evidence_still_yields_one_exact_removal():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        _expand_grounded_python_function_removal,
    )

    definition = "def injected_failure():\n    raise RuntimeError('boom')"
    source = (
        "import app\n\n\n" + definition
        + "\n\ninjected_failure()\n\n# startup\napp.run()\n"
    )
    context = GroundedPlanContext(
        "frozen",
        "[ev-1 probe=ops_file]\n%s\n[ev-2 probe=ops_file]\n%s" % (source, source),
        "audited",
    )

    block = _expand_grounded_python_function_removal(definition, context)

    assert source.replace(block, "", 1) == "import app\n\n# startup\napp.run()\n"


def test_python_class_attribute_postcondition_is_canonicalized_from_action_args():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    checks = _canonical_action_postconditions(
        "set_python_class_attribute",
        {
            "path": "/srv/app/vemu_config/config.py",
            "class_name": "WtxConfig",
            "attribute": "master_port",
            "value": "47001",
        },
        [
            {
                "checker": "python_attribute_equals",
                "args": {
                    "module": "vemu_config.config",
                    "attribute": "master_port",
                    "expected": "47001",
                    "cwd": "/srv/app",
                },
            }
        ],
    )

    assert checks == [
        {
            "checker": "python_attribute_equals",
            "args": {
                "module": "vemu_config.config",
                "attribute": "WtxConfig.master_port",
                "expected": 47001,
                "cwd": "/srv/app",
            },
        }
    ]


def test_screen_postconditions_are_canonical_for_component_role():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    common = {
        "screen_session": "v4e2e_c",
        "component": "celery",
        "port_47001": "47001",
        "port_47002": "47002",
        "port_47003": "47003",
    }
    celery = _canonical_action_postconditions(
        "start_screen_component",
        common,
        [{"checker": "port_listening", "args": {"port": 47002}}],
    )
    master = _canonical_action_postconditions(
        "start_screen_component",
        {**common, "screen_session": "v4e2e_m", "component": "master"},
        [],
    )

    assert celery == [
        {
            "checker": "screen_session_exists",
            "args": {"session": "v4e2e_c"},
        }
    ]
    assert master[-1] == {
        "checker": "port_listening",
        "args": {"port": 47001, "host": "127.0.0.1"},
    }

    worker = _canonical_action_postconditions(
        "restart_screen_component",
        {"screen_session": "test_w", "component": "worker", "worker_port": "45555"},
        [],
    )
    assert worker[-1] == {
        "checker": "port_listening",
        "args": {"port": 45555, "host": "127.0.0.1"},
    }


def test_screen_preconditions_are_scoped_to_exact_session_and_role_port():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_preconditions,
    )

    start = _canonical_action_preconditions(
        "start_screen_component",
        {
            "screen_session": "vemu_uestc_w",
            "component": "worker",
            "worker_port": "45552",
        },
        [
            {"checker": "process_not_running", "args": {"pattern": "worker_main"}},
            {"checker": "screen_session_absent", "args": {"session": "vemu_uestc_worker"}},
        ],
    )
    restart = _canonical_action_preconditions(
        "restart_screen_component",
        {"screen_session": "vemu_uestc_m", "component": "master"},
        [{"checker": "port_not_listening", "args": {"port": 45551}}],
    )

    assert start == [
        {"checker": "screen_session_absent", "args": {"session": "vemu_uestc_w"}},
        {"checker": "port_not_listening", "args": {"port": 45552}},
    ]
    assert restart == []


def test_structured_config_preconditions_do_not_require_old_listener_after_stop():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_preconditions,
    )

    result = _canonical_action_preconditions(
        "set_python_class_attribute",
        {"path": "/srv/test/vemu_config/config.py", "attribute": "worker_port", "value": "45555"},
        [
            {"checker": "file_exists", "args": {"path": "/srv/test/worker_gun.py"}},
            {"checker": "port_listening", "args": {"port": 45552}},
        ],
    )

    assert result == [
        {"checker": "file_exists", "args": {"path": "/srv/test/vemu_config/config.py"}},
    ]

import pytest


def test_semantic_step_with_recovery_and_start_is_not_observational():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _semantic_step_is_observational,
    )

    step = PrivilegedStep(
        step_id="formal-worker",
        title="恢复 formal 实例的 worker 角色并确认 master 可达",
        objective="为 formal 实例启动 worker screen 组件并验证接口",
        risk="medium",
        expected_changes=["worker screen starts"],
        postconditions=[{"checker": "port_listening", "args": {"port": 45552}}],
    )

    assert _semantic_step_is_observational(step) is False


def test_verification_title_does_not_inherit_migration_as_a_new_mutation():
    from klonet_agent.ops.privileged.execution_agent import (
        _implementation_item_is_verification,
    )

    assert _implementation_item_is_verification({
        "title": "验证 test 平台迁移完成与端口隔离",
        "objective": "Verify the migrated master and worker are healthy",
    }) is True
    assert _implementation_item_is_verification({
        "title": "验证配置并重启 worker",
        "objective": "Verify then restart the component",
    }) is False


def test_orphan_runtime_stop_forces_cwd_scoped_action_not_screen_stop():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _forced_registered_action_for_step,
    )

    step = PrivilegedStep(
        step_id="stop-test-runtime",
        title="Stop current test instance worker process",
        objective="Stop the orphan runtime process owned by /home/lzl/test/vemu_uestc",
        risk="high",
        expected_changes=["old runtime port is released"],
        postconditions=[{"checker": "port_not_listening", "args": {"port": 45552}}],
    )

    assert _forced_registered_action_for_step(step) == "stop_klonet_component"


def test_atomic_runtime_restart_forces_role_screen_action():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _forced_registered_action_for_step,
        _validate_action_objective_fit,
    )

    step = PrivilegedStep(
        step_id="restart-test-worker",
        title="Restart test worker process on new port 45555",
        objective="Restart worker for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=["worker starts on 45555"],
    )

    assert _forced_registered_action_for_step(step) == "restart_screen_component"
    assert "contradicts" in _validate_action_objective_fit(
        "stop_klonet_runtime_instance", step,
    )


def test_runtime_recovery_requires_bound_action_for_every_promised_role():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        _validate_runtime_recovery_action_coverage,
    )

    semantic = PrivilegedStep(
        step_id="formal",
        title="Restore formal master/worker",
        objective="Restore master and worker for /home/lzl/vemu_uestc",
        risk="high",
        expected_changes=[
            "restart unhealthy master role and backend health succeeds",
            "start missing worker role and backend health succeeds",
        ],
    )
    master = PrivilegedStep(
        step_id="master", title="Restart master", objective="Restart master", risk="medium",
        execution_binding=ExecutionBinding(
            kind="registered_action", action="restart_screen_component",
            args={"component": "master"}, risk="medium",
        ),
    )

    with pytest.raises(ExecutionBindingError, match="worker"):
        _validate_runtime_recovery_action_coverage(semantic, [master])


def test_semantic_backend_health_is_canonicalized_for_each_recovered_role():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_semantic_backend_health_contract,
    )

    step = PrivilegedStep(
        step_id="formal",
        title="Restore formal master/worker",
        objective="Start master and worker for /home/lzl/vemu_uestc",
        risk="high",
        expected_changes=["master and worker become healthy"],
        postconditions=[
            {"checker": "backend_health", "args": {"url": "http://192.168.1.124:45551"}},
            {"checker": "http_status", "args": {"url": "http://127.0.0.1:45552"}},
            {"checker": "backend_health", "args": {"url": "http://127.0.0.1:49999/server_health/"}},
        ],
    )
    resources = [
        PlanResource("formal_master", "port", "frozen", "master_port", 45551, "existing", consumers=["formal.master_port"]),
        PlanResource("formal_worker", "port", "frozen", "worker_port", 45552, "existing", consumers=["formal.worker_port"]),
    ]

    _normalize_semantic_backend_health_contract(step, resources)

    assert step.postconditions == [
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45551/server_health/", "expected_code": 1}},
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45552/server_health/", "expected_code": 1}},
    ]


def test_semantic_backend_health_prefers_new_worker_port_over_old_port():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_semantic_backend_health_contract,
    )

    step = PrivilegedStep(
        step_id="change-1",
        title="Migrate test worker",
        objective="Start worker for /home/lzl/test/vemu_uestc",
        expected_changes=["start missing worker role and backend health succeeds"],
        postconditions=[
            {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45552/server_health/"}},
        ],
        risk="high",
    )
    resources = [
        PlanResource("test_worker_old_port", "port", "frozen", "worker_port", 45552, "existing_config", consumers=["change-1.worker_port_2"]),
        PlanResource("test_worker_new_port", "port", "frozen", "worker_port", 45555, "evidence", consumers=["change-1.worker_port"]),
    ]

    _normalize_semantic_backend_health_contract(step, resources)

    assert step.postconditions == [
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45555/server_health/", "expected_code": 1}},
    ]


def test_unhealthy_role_disposition_rewrites_prefixed_start_title_to_restart():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_runtime_role_recovery_verbs,
    )

    semantic = PrivilegedStep(
        step_id="formal",
        title="Restore formal roles",
        objective="Restore /home/lzl/vemu_uestc",
        expected_changes=[
            "restart unhealthy master role at 45551 and backend health succeeds",
            "start missing worker role at 45552 and backend health succeeds",
        ],
        risk="high",
    )
    items = [
        {"id": "master", "title": "确保 master 的 screen 会话不存在后启动 master 组件", "objective": "restore roles", "depends_on": []},
        {"id": "worker", "title": "确保 worker 的 screen 会话不存在后启动 worker 组件", "objective": "restore roles", "depends_on": ["master"]},
    ]

    result = _normalize_runtime_role_recovery_verbs(items, semantic)

    assert result[0]["title"] == "Restart master screen component"
    assert result[1]["title"] == "Start worker screen component"


def test_backend_port_repair_does_not_inherit_public_port_deployment_requirement():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, PlanResource, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_semantic_resource_coverage,
    )

    semantic = PrivilegedStep(
        step_id="migrate",
        title="Migrate backend ports",
        objective="Configure master/worker ports for /home/lzl/test/vemu_uestc",
        risk="high",
    )
    micro_steps = []
    for role in ("master", "worker"):
        micro_steps.append(PrivilegedStep(
            step_id=role,
            title="Set WtxConfig %s_port" % role,
            objective="Set backend port",
            risk="medium",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                action="set_python_class_attribute",
                args={"attribute": "%s_port" % role, "value": 45554},
                risk="medium",
            ),
        ))
    resources = [
        PlanResource("active_public", "port", "frozen", "public_port", 45553, "existing_config", consumers=[]),
    ]

    _validate_semantic_resource_coverage(semantic, micro_steps, resources)


def test_stop_component_pid_must_be_frozen_for_same_semantic_step():
    from klonet_agent.ops.privileged.contracts import ExecutionBinding, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        _validate_semantic_resource_coverage,
    )

    semantic = PrivilegedStep(
        step_id="migrate",
        title="Stop worker on port 45552",
        objective="Stop /home/lzl/test/vemu_uestc worker before port migration",
        risk="high",
    )
    stop = PrivilegedStep(
        step_id="migrate__stop",
        title="Stop worker",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="stop_klonet_component",
            args={"pid": 2562051, "runtime_cwd": "/home/lzl/test/vemu_uestc", "component": "worker", "port": 45552},
            risk="high",
        ),
        risk="high",
    )

    with pytest.raises(ExecutionBindingError, match="pid_not_frozen"):
        _validate_semantic_resource_coverage(semantic, [stop], [])


def test_multi_attribute_config_micro_step_is_split_into_atomic_updates():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _split_multi_attribute_config_items,
    )

    semantic = PrivilegedStep(
        step_id="change-1",
        title="Migrate test instance configuration",
        objective="Update WtxConfig master_port and worker_port for the isolated test instance",
        risk="medium",
        expected_changes=["master_port becomes 45553", "worker_port becomes 45554"],
    )
    resources = [
        PlanResource(
            name="test_master_port", kind="port", status="frozen",
            role="master_port", value=45553, consumers=["change-1.master_port"],
        ),
        PlanResource(
            name="test_worker_port", kind="port", status="frozen",
            role="worker_port", value=45554, consumers=["change-1.worker_port"],
        ),
    ]
    items = [{
        "id": "update-config",
        "title": "Update test instance config ports",
        "objective": "Set WtxConfig master_port and worker_port to the frozen ports",
        "reason": "remove the collision",
        "depends_on": ["stop-runtime"],
        "expected_changes": ["master_port and worker_port are updated"],
        "success_criteria": ["both configured ports match the plan"],
        "risk_suggestion": "medium",
    }, {
        "id": "start-runtime",
        "title": "Start test runtime",
        "objective": "Start the isolated runtime",
        "reason": "restore service",
        "depends_on": ["update-config"],
        "expected_changes": ["runtime starts"],
        "success_criteria": ["health endpoint succeeds"],
        "risk_suggestion": "medium",
    }]

    result = _split_multi_attribute_config_items(items, semantic, resources)

    assert [item["id"] for item in result] == [
        "update-config-master-port",
        "update-config-worker-port",
        "start-runtime",
    ]
    assert result[0]["title"] == "Set WtxConfig master_port"
    assert result[1]["title"] == "Set WtxConfig worker_port"
    assert result[1]["depends_on"] == ["update-config-master-port"]
    assert result[2]["depends_on"] == ["update-config-worker-port"]


def test_preserved_config_fields_are_not_compiled_as_mutations():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_preservation_only_config_mutations,
    )

    semantic = PrivilegedStep(
        step_id="migrate-test",
        title="Migrate test backend ports",
        objective="Update master_port and worker_port for the test instance",
        risk="high",
        expected_changes=[
            "master_port becomes 45554 and worker_port becomes 45555",
            "public_port remains 45553 and web_terminal_port remains 5114 unchanged",
        ],
    )
    items = [
        {"id": "master", "title": "Set WtxConfig master_port", "objective": "Set master_port", "depends_on": []},
        {"id": "worker", "title": "Set WtxConfig worker_port", "objective": "Set worker_port", "depends_on": ["master"]},
        {"id": "web", "title": "Set WtxConfig web_terminal_port", "objective": "Set web_terminal_port", "depends_on": ["worker"]},
        {"id": "public", "title": "Set WtxConfig public_port", "objective": "Set public_port", "depends_on": ["web"]},
        {"id": "start", "title": "Start worker", "objective": "Start worker", "depends_on": ["public"]},
    ]

    result = _drop_preservation_only_config_mutations(items, semantic)

    assert [item["id"] for item in result] == ["master", "worker", "start"]
    assert result[-1]["depends_on"] == ["worker"]


def test_unmentioned_master_ip_repair_is_dropped_from_runtime_recovery():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_unauthorized_config_mutations,
    )

    semantic = PrivilegedStep(
        step_id="recover-formal",
        title="Recover formal master and worker",
        objective="Restore master and worker on their existing ports",
        risk="high",
        expected_changes=["master and worker become healthy"],
    )
    items = [
        {"id": "repair-ip", "title": "Repair active master_ip", "objective": "Repair WtxConfig master_ip", "depends_on": []},
        {"id": "master", "title": "Restart master screen component", "objective": "Restart master", "depends_on": ["repair-ip"]},
    ]

    result = _drop_unauthorized_config_mutations(items, semantic)

    assert [item["id"] for item in result] == ["master"]
    assert result[0]["depends_on"] == []


def test_chinese_duplicate_scalar_config_writes_are_collapsed():
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_config_mutations,
    )

    items = [
        {"id": "master-a", "title": "Set WtxConfig master_port", "objective": "Set master_port", "depends_on": []},
        {"id": "master-b", "title": "将 test master_port 改为 45554", "objective": "修改 master_port", "depends_on": ["master-a"]},
        {"id": "start", "title": "Restart master", "objective": "Restart master", "depends_on": ["master-b"]},
    ]

    result = _collapse_redundant_config_mutations(items)

    assert [item["id"] for item in result] == ["master-a", "start"]
    assert result[1]["depends_on"] == ["master-a"]


def test_structural_attribute_inference_prefers_scoped_injected_value():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    resources = [
        PlanResource(
            name="test_master_port", kind="port", status="frozen",
            role="master_port", value=45553, consumers=["change-1.master_port"],
        ),
        PlanResource(
            name="formal_master_port", kind="port", status="frozen",
            role="master_port", value=45551, consumers=["change-2.master_port"],
        ),
    ]

    args = _infer_structural_action_args(
        "set_python_class_attribute",
        {"attribute": "master_port", "master_port": 45553},
        resources,
    )

    assert args["value"] == 45553


def test_plural_screen_start_is_split_into_one_step_per_component():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _split_multi_component_runtime_items,
    )

    semantic = PrivilegedStep(
        step_id="change-1",
        title="Restore test runtime",
        objective="Start /home/lzl/test/vemu_uestc as an isolated runtime",
        risk="medium",
    )
    items = [{
        "id": "start-runtime",
        "title": "Restart test master/worker/web_terminal roles",
        "objective": "Start master, worker and web_terminal screen components",
        "reason": "restore backend",
        "depends_on": ["update-config"],
        "expected_changes": ["master, worker and web_terminal start"],
        "success_criteria": ["all roles are running"],
        "risk_suggestion": "medium",
    }, {
        "id": "verify",
        "title": "Verify runtime health",
        "objective": "Verify backend health",
        "reason": "acceptance",
        "depends_on": ["start-runtime"],
        "expected_changes": [],
        "success_criteria": ["healthy"],
        "risk_suggestion": "readonly",
    }]

    result = _split_multi_component_runtime_items(items, semantic)

    assert [item["id"] for item in result[:3]] == [
        "start-runtime-master",
        "start-runtime-worker",
        "start-runtime-web-terminal",
    ]
    assert result[3]["depends_on"] == ["start-runtime-web-terminal"]


def test_screen_action_contract_must_match_role_and_runtime_root():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_contract_consistency,
    )

    step = PrivilegedStep(
        step_id="change-2__start-worker",
        title="Start worker role",
        objective="Start worker for /home/lzl/vemu_uestc",
        risk="medium",
        expected_changes=["worker starts"],
    )

    assert "component_mismatch" in _validate_action_contract_consistency(
        "start_screen_component",
        {
            "component": "master",
            "screen_session": "vemu_uestc_m",
            "project_root": "/home/lzl/vemu_uestc/mains",
        },
        step,
    )
    assert "project_root_mismatch" in _validate_action_contract_consistency(
        "start_screen_component",
        {
            "component": "worker",
            "screen_session": "test_w",
            "project_root": "/home/lzl/test/vemu_uestc/mains",
        },
        step,
    )
    assert "platform_mismatch" in _validate_action_contract_consistency(
        "start_screen_component",
        {
            "component": "worker",
            "platform": "vemu_uestc",
            "screen_session": "vemu_uestc_w",
            "project_root": "/home/lzl/test/vemu_uestc/mains",
        },
        PrivilegedStep(
            step_id="test-worker",
            title="Start worker screen component",
            objective="Start worker for /home/lzl/test/vemu_uestc",
            risk="medium",
        ),
    )


def test_screen_action_args_are_compiled_from_atomic_role_and_runtime_root():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_semantic_action_args,
    )

    step = PrivilegedStep(
        step_id="change-2__start-worker",
        title="Start worker screen component",
        objective="Start worker for /home/lzl/vemu_uestc",
        risk="medium",
        expected_changes=["worker starts"],
    )

    args = _infer_semantic_action_args(
        "start_screen_component",
        {
            "platform": "vemu_uestc",
            "component": "master",
            "screen_session": "vemu_uestc_m",
            "project_root": "/home/lzl/test/vemu_uestc/mains",
        },
        step,
    )

    assert args["component"] == "worker"
    assert args["screen_session"] == "vemu_uestc_w"
    assert args["project_root"] == "/home/lzl/vemu_uestc/mains"


def test_test_instance_root_overrides_ambiguous_global_screen_identity():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import _infer_semantic_action_args

    step = PrivilegedStep(
        step_id="change-1__start-master",
        title="Start master screen component",
        objective="Start master for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=["master starts"],
    )
    args = _infer_semantic_action_args(
        "start_screen_component",
        {
            "platform": "vemu_uestc",
            "component": "master",
            "screen_session": "vemu_uestc_m",
            "project_root": "/home/lzl/vemu_uestc/mains",
        },
        step,
    )

    assert args["platform"] == "test"
    assert args["screen_session"] == "test_m"
    assert args["project_root"] == "/home/lzl/test/vemu_uestc/mains"


def test_forced_screen_start_compiles_new_session_without_existing_session_evidence():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="change-test__start-worker",
        title="Start worker screen component",
        objective="Start worker for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=["worker starts"],
    )
    plan = PrivilegedPlan(
        plan_id="screen-start",
        goal="restore test worker",
        risk="medium",
        steps=[step],
    )

    binding = PrivilegedExecutionAgent(None).prepare_step(
        plan,
        step,
        grounded_context=None,
    )

    assert binding.action == "start_screen_component"
    assert binding.args["platform"] == "test"
    assert binding.args["component"] == "worker"
    assert binding.args["screen_session"] == "test_w"
    assert binding.args["project_root"] == "/home/lzl/test/vemu_uestc/mains"


def test_frozen_screen_consumer_wins_after_semantic_fallback_in_binding():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="change-2__start-worker",
        title="Start worker screen component",
        objective="Start worker for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=["worker starts"],
    )
    resources = [
        PlanResource(
            name="test_master_screen",
            kind="identifier",
            status="frozen",
            role="screen_session",
            value="test_vemu_uestc_m",
            consumers=["change-2.screen_session"],
        ),
        PlanResource(
            name="test_worker_screen",
            kind="identifier",
            status="frozen",
            role="screen_session",
            value="test_vemu_uestc_w",
            consumers=["change-2.screen_session"],
        ),
        PlanResource(
            name="test_runtime_mains",
            kind="path",
            status="frozen",
            role="runtime_mains_root",
            value="/home/lzl/test/vemu_uestc/mains",
            consumers=["change-2.project_root"],
        ),
    ]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "start_screen_component",
            "args": {
                "platform": "vemu_uestc",
                "component": "master",
                "screen_session": "vemu_uestc_m",
                "project_root": "/home/lzl/vemu_uestc/mains",
            },
        },
        step,
        None,
        resources,
    )

    assert binding.args["component"] == "worker"
    assert binding.args["screen_session"] == "test_vemu_uestc_w"
    assert binding.args["project_root"] == "/home/lzl/test/vemu_uestc/mains"


def test_screen_start_falls_back_to_frozen_instance_root_and_real_mains(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    instance = tmp_path / "102"
    mains = instance / "mains"
    mains.mkdir(parents=True)
    for name in REQUIRED_ENTRY_FILES:
        (mains / name).write_text("# entry\n", encoding="utf-8")
    step = PrivilegedStep(
        step_id="repair__start-master",
        title="Start master screen component",
        objective="Start corrected master",
        expected_changes=["master starts"],
        risk="medium",
    )
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root",
            str(instance), "running_platforms", consumers=["repair.instance_root"],
        )
    ]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "start_screen_component",
            "args": {
                "platform": "change-1", "component": "master",
                "screen_session": "change-1_m", "project_root": str(instance),
            },
        },
        step,
        None,
        resources,
    )

    assert binding.args["platform"] == "102"
    assert binding.args["screen_session"] == "102_m"
    assert binding.args["project_root"] == str(mains)


def test_screen_start_keeps_frozen_instance_identifier_after_canonicalization(
    tmp_path,
):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    instance = tmp_path / "directory-name-is-not-the-instance-id"
    mains = instance / "mains"
    mains.mkdir(parents=True)
    for name in REQUIRED_ENTRY_FILES:
        (mains / name).write_text("# entry\n", encoding="utf-8")
    step = PrivilegedStep(
        step_id="restart-backend-roles__start-master",
        title="Start master screen component",
        objective=f"Start master for {instance}",
        expected_changes=["master starts"],
        risk="medium",
    )
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root",
            str(instance), "running_platforms",
            consumers=["restart-backend-roles.project_root"],
        ),
        PlanResource(
            "instance_identifier", "identifier", "frozen",
            "instance_identifier", "v4e2e", "running_platforms",
            consumers=["restart-backend-roles.platform"],
        ),
    ]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "start_screen_component",
            "args": {
                "platform": "directory-name-is-not-the-instance-id",
                "component": "master",
                "screen_session": "directory-name-is-not-the-instance-id_m",
                "project_root": str(instance),
            },
        },
        step,
        None,
        resources,
    )

    assert binding.args["platform"] == "v4e2e"
    assert binding.args["screen_session"] == "v4e2e_m"
    assert binding.args["project_root"] == str(mains)


def test_runtime_binding_progress_titles_are_presented_in_chinese():
    from klonet_agent.ops.privileged.execution_agent import _progress_text

    assert _progress_text("Start master screen component") == "启动 master Screen 组件"
    assert _progress_text("Start worker screen component") == "启动 worker Screen 组件"


def test_runtime_migration_order_accepts_instance_qualified_start_titles():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import _order_runtime_migration_items

    semantic = PrivilegedStep(
        step_id="change-1",
        title="迁移 test 实例端口",
        objective="迁移 /home/lzl/test/vemu_uestc 的运行端口",
        risk="high",
    )
    items = [
        {"id": "config-master", "title": "Set WtxConfig master_port", "objective": "Set port", "depends_on": []},
        {"id": "config-worker", "title": "Set WtxConfig worker_port", "objective": "Set port", "depends_on": ["config-master"]},
        {"id": "stop", "title": "Stop old test instance master/worker runtime", "objective": "Stop old process", "depends_on": ["config-worker"]},
        {"id": "start-master", "title": "Start test instance master component on new port", "objective": "Start master", "depends_on": ["stop"]},
        {"id": "start-worker", "title": "Start test instance worker component on new port", "objective": "Start worker", "depends_on": ["start-master"]},
    ]

    result = _order_runtime_migration_items(items, semantic)

    assert [item["id"] for item in result] == [
        "stop", "config-master", "config-worker", "start-master", "start-worker",
    ]


def test_frozen_attribute_value_is_recomputed_after_final_resource_injection():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="change-1__set-master",
        title="Set WtxConfig master_port",
        objective="Set master_port for /home/lzl/test/vemu_uestc/vemu_config/config.py",
        risk="medium",
        expected_changes=["master_port becomes 45553"],
    )
    resources = [
        PlanResource(
            "test_master_port", "port", "frozen", "master_port", 45553,
            consumers=["change-1.master_port"],
        ),
        PlanResource(
            "test_config", "path", "frozen", "config_path",
            "/home/lzl/test/vemu_uestc/vemu_config/config.py",
            consumers=["change-1.path"],
        ),
    ]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "set_python_class_attribute",
            "args": {
                "path": "/home/lzl/test/vemu_uestc/vemu_config/config.py",
                "class_name": "WtxConfig",
                "attribute": "master_port",
                "master_port": 45551,
                "value": 45551,
            },
        },
        step,
        None,
        resources,
    )

    assert binding.args["master_port"] == "45553"
    assert binding.args["value"] == "45553"


def test_destination_port_wins_over_old_same_role_resource_in_binding():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="change-2__set-master",
        title="Set WtxConfig master_port",
        objective="Migrate master for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=["master_port becomes 45555"],
    )
    resources = [
        PlanResource("formal_master_port", "port", "frozen", "master_port", 45551, "existing_config", consumers=["change-2.port_45551"]),
        PlanResource("test_master_new_port", "port", "frozen", "master_port", 45555, "planner_decision", consumers=["change-2.test_master_new_port"]),
    ]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "set_python_class_attribute",
            "args": {
                "path": "/home/lzl/test/vemu_uestc/vemu_config/config.py",
                "attribute": "master_port",
                "value": 45551,
            },
        },
        step,
        None,
        resources,
    )

    assert binding.args["master_port"] == "45555"
    assert binding.args["value"] == "45555"


def test_resource_validation_ignores_old_port_when_destination_is_frozen():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_resource_bindings,
    )

    step = PrivilegedStep(
        step_id="change-1__worker",
        title="Set WtxConfig worker_port",
        objective="Move /home/lzl/test/vemu_uestc worker_port to 45555",
        risk="medium",
        expected_changes=["worker_port becomes 45555"],
    )
    resources = [
        PlanResource("test_old_worker_port", "port", "frozen", "worker_port", 45552, "existing_runtime", consumers=["change-1.worker_port"]),
        PlanResource("test_worker_port", "port", "frozen", "worker_port", 45555, "planner_decision", consumers=["change-1.worker_port"]),
    ]

    _validate_action_resource_bindings(
        step,
        "set_python_class_attribute",
        {"worker_port": 45555},
        resources,
    )


def test_test_instance_start_does_not_reuse_unscoped_formal_dead_screen():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="start-test-worker",
        title="Start worker screen component",
        objective="Start worker for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=["test worker starts"],
    )
    resources = [PlanResource(
        "test_worker_session", "identifier", "frozen", "screen_session",
        "vemu_uestc_w", "screen_unknown_root",
        consumers=["start-test-worker.screen_session"],
    )]

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "start_screen_component",
            "args": {
                "component": "worker",
                "project_root": "/home/lzl/test/vemu_uestc/mains",
                "platform": "vemu_uestc",
                "screen_session": "vemu_uestc_w",
            },
        },
        step,
        None,
        resources,
    )

    assert binding.args["platform"] == "test"
    assert binding.args["screen_session"] == "test_w"


def test_klonet_port_write_ignores_model_instance_label_as_class_name():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="set-worker",
        title="Set WtxConfig worker_port",
        objective="Update /home/lzl/test/vemu_uestc worker_port",
        risk="medium",
        expected_changes=["worker_port becomes 45556"],
    )
    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "set_python_class_attribute",
            "args": {
                "path": "/home/lzl/test/vemu_uestc/vemu_config/config.py",
                "class_name": "test",
                "attribute": "worker_port",
                "value": 45556,
            },
        },
        step,
        None,
        [],
    )

    assert "class_name" not in binding.args


def test_python_attribute_check_is_recompiled_from_final_action_args():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    result = _canonical_action_postconditions(
        "set_python_class_attribute",
        {
            "path": "/home/lzl/test/vemu_uestc/vemu_config/config.py",
            "attribute": "master_port",
            "value": "45554",
        },
        [{
            "checker": "python_attribute_equals",
            "args": {
                "module": "config",
                "attribute": "master_port",
                "expected": 45551,
                "cwd": "/home/lzl/vemu_uestc",
            },
        }],
    )

    assert result == [{
        "checker": "python_attribute_equals",
        "args": {
            "module": "vemu_config.config",
            "attribute": "PROJ_CONFIG.master_port",
            "expected": 45554,
            "cwd": "/home/lzl/test/vemu_uestc",
        },
    }]


def test_component_stop_check_does_not_inherit_global_process_pattern():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    result = _canonical_action_postconditions(
        "stop_klonet_component",
        {"pid": 3035965, "port": 45552, "component": "worker"},
        [{
            "checker": "process_not_running",
            "args": {"pattern": "worker_gun.py worker_main"},
        }],
    )

    assert result == [
        {"checker": "process_pid_absent", "args": {"pid": 3035965}},
        {"checker": "port_not_listening", "args": {"port": 45552}},
    ]


def test_config_attribute_path_is_compiled_from_scoped_instance_root():
    from klonet_agent.ops.privileged.execution_agent import _infer_structural_action_args

    args = _infer_structural_action_args(
        "set_python_class_attribute",
        {
            "path": "/home/lzl/vemu_uestc/vemu_config/config.py",
            "attribute": "master_port",
            "master_port": 45553,
            "instance_root": "/home/lzl/test/vemu_uestc",
        },
        [],
    )

    assert args["path"] == "/home/lzl/test/vemu_uestc/vemu_config/config.py"
    assert args["value"] == 45553


def test_final_config_binding_normalizes_root_path_and_active_assignment_alias():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="test-config",
        title="Set WtxConfig worker_port",
        objective="Set worker_port for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=["worker_port becomes 45555"],
    )
    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {
            "action": "set_python_class_attribute",
            "args": {
                "path": "/home/lzl/test/vemu_uestc",
                "class_name": "PROJ_CONFIG",
                "attribute": "worker_port",
                "value": 45555,
            },
        },
        step,
        None,
        [],
    )

    assert binding.args["path"] == "/home/lzl/test/vemu_uestc/vemu_config/config.py"
    assert "class_name" not in binding.args


def test_final_readonly_review_is_classified_as_verification():
    from klonet_agent.ops.privileged.execution_agent import (
        _implementation_item_is_verification,
    )

    assert _implementation_item_is_verification({
        "title": "最终只读复核端口隔离与健康状态",
        "objective": "Review the migrated runtime",
    }) is True


def test_mutating_micro_plan_drops_redundant_future_state_verifications():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_redundant_implementation_verifications,
    )

    semantic = PrivilegedStep(
        step_id="formal",
        title="Restore formal worker",
        objective="Start worker for /home/lzl/vemu_uestc",
        risk="high",
        expected_changes=["worker starts"],
        postconditions=[
            {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45552/server_health/"}},
        ],
    )
    items = [
        {"id": "verify-port", "title": "Verify formal worker port 45552 is released", "objective": "Confirm port free", "depends_on": []},
        {"id": "start-worker", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["verify-port"]},
        {"id": "verify-health", "title": "Verify formal worker health", "objective": "Verify health", "depends_on": ["start-worker"]},
    ]

    result = _drop_redundant_implementation_verifications(items, semantic)

    assert [item["id"] for item in result] == ["start-worker"]
    assert result[0]["depends_on"] == []


def test_missing_unhealthy_role_gets_an_atomic_recovery_action():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_role_recovery_items,
    )

    semantic = PrivilegedStep(
        step_id="change-2",
        title="Restore formal runtime",
        objective="Repair /home/lzl/vemu_uestc",
        risk="medium",
        expected_changes=[
            "restart unhealthy master role at 45551 and backend health succeeds",
            "start missing worker role at 45552 and backend health succeeds",
        ],
    )
    items = [{
        "id": "start-worker",
        "title": "Start worker screen component",
        "objective": "Start worker for /home/lzl/vemu_uestc",
        "reason": "restore worker",
        "depends_on": [],
        "expected_changes": ["worker starts"],
        "success_criteria": ["worker healthy"],
        "risk_suggestion": "medium",
    }, {
        "id": "verify",
        "title": "Verify formal runtime",
        "objective": "Verify health",
        "reason": "acceptance",
        "depends_on": ["start-worker"],
        "expected_changes": [],
        "success_criteria": ["healthy"],
        "risk_suggestion": "readonly",
    }]

    result = _ensure_runtime_role_recovery_items(items, semantic)

    assert result[0]["title"] == "Start worker screen component"
    assert result[1]["title"] == "Restart master screen component"
    assert result[1]["depends_on"] == ["start-worker"]
    assert result[-1]["depends_on"] == ["change-2-recover-master"]


def test_unhealthy_role_disposition_compiles_start_title_to_restart():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_runtime_role_recovery_verbs,
    )

    semantic = PrivilegedStep(
        step_id="formal",
        title="Restore formal master/worker",
        objective="Restore /home/lzl/vemu_uestc",
        risk="high",
        expected_changes=[
            "restart unhealthy master role at 45551 and backend health succeeds",
            "start missing worker role at 45552 and backend health succeeds",
        ],
    )
    items = [
        {"id": "master", "title": "Start formal master screen component", "objective": "Start master"},
        {"id": "worker", "title": "Restart formal worker screen component", "objective": "Restart worker"},
    ]

    result = _normalize_runtime_role_recovery_verbs(items, semantic)

    assert result[0]["title"] == "Restart master screen component"
    assert result[1]["title"] == "Start worker screen component"


def test_synthesized_role_recovery_runs_after_config_and_before_verification():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_role_recovery_items,
    )

    semantic = PrivilegedStep(
        step_id="change-test",
        title="Migrate test runtime",
        objective="Migrate /home/lzl/test/vemu_uestc to isolated ports",
        risk="high",
        expected_changes=["restart unhealthy worker role on its new port"],
    )
    items = [
        {
            "id": "set-master",
            "title": "Set WtxConfig master_port",
            "objective": "Set master_port to 45554",
            "depends_on": [],
        },
        {
            "id": "set-worker",
            "title": "Set WtxConfig worker_port",
            "objective": "Set worker_port to 45555",
            "depends_on": ["set-master"],
        },
        {
            "id": "verify",
            "title": "Verify test runtime health",
            "objective": "Verify master and worker health",
            "depends_on": ["set-worker"],
        },
    ]

    result = _ensure_runtime_role_recovery_items(items, semantic)

    assert [item["id"] for item in result] == [
        "set-master",
        "set-worker",
        "change-test-recover-worker",
        "verify",
    ]
    assert result[2]["depends_on"] == ["set-worker"]
    assert result[3]["depends_on"] == ["change-test-recover-worker"]


def test_partial_worker_recovery_does_not_restart_preserved_healthy_master():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_role_recovery_items,
    )

    semantic = PrivilegedStep(
        step_id="finish-test-worker",
        title="Finish test worker recovery",
        objective="Start worker for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=[
            "start missing worker role at 45555 and backend health succeeds",
            "test master on 45554 remains healthy and untouched",
        ],
    )

    result = _ensure_runtime_role_recovery_items([], semantic)

    assert [item["title"] for item in result] == ["Start worker screen component"]


def test_explicit_restart_requested_compiles_to_restart_screen_actions():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_runtime_role_recovery_verbs,
    )

    semantic = PrivilegedStep(
        step_id="restart-backend-roles",
        title="重启 v4e2e 的后端角色",
        objective="按项目根目录 /home/lzl/klonet_workflow_e2e 重启 master 和 worker",
        risk="medium",
        expected_changes=[
            "restart requested master role at 47001 and backend health succeeds",
            "restart requested worker role at 47002 and backend health succeeds",
        ],
    )
    items = [
        {"id": "master", "title": "Start master screen component"},
        {"id": "worker", "title": "Start worker screen component"},
    ]

    result = _normalize_runtime_role_recovery_verbs(items, semantic)

    assert [item["title"] for item in result] == [
        "Restart master screen component",
        "Restart worker screen component",
    ]


def test_partial_worker_binding_drops_model_start_for_preserved_master():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _filter_unauthorized_runtime_role_mutations,
    )

    semantic = PrivilegedStep(
        step_id="finish-test-worker",
        title="Reconfigure and start test worker",
        objective="Start test worker, preserving the healthy test master on 45554",
        risk="medium",
        expected_changes=[
            "test worker starts on 45555",
            "test master on 45554 remains healthy and unchanged",
        ],
    )
    items = [
        {"id": "master", "title": "Start master screen component", "objective": "Start master", "depends_on": []},
        {"id": "worker", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["master"]},
    ]

    result = _filter_unauthorized_runtime_role_mutations(items, semantic)

    assert [item["id"] for item in result] == ["worker"]
    assert result[0]["depends_on"] == []

    from klonet_agent.ops.privileged.execution_agent import (
        _required_runtime_recovery_roles,
    )
    assert _required_runtime_recovery_roles(semantic) == {"worker"}


def test_verifying_still_healthy_master_does_not_authorize_master_start():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _filter_unauthorized_runtime_role_mutations,
        _required_runtime_recovery_roles,
    )

    semantic = PrivilegedStep(
        step_id="start-test-worker",
        title="启动 test worker",
        objective="启动 worker 到 45555，并验证 master 45554 仍健康",
        risk="medium",
        expected_changes=["worker 启动并健康", "master 45554 保持健康"],
    )
    items = [
        {"id": "worker", "title": "Start worker screen component", "objective": "Start worker", "depends_on": []},
        {"id": "master", "title": "Start master screen component", "objective": "Start master", "depends_on": ["worker"]},
    ]

    result = _filter_unauthorized_runtime_role_mutations(items, semantic)

    assert [item["id"] for item in result] == ["worker"]
    assert _required_runtime_recovery_roles(semantic) == {"worker"}


def test_missing_worker_evidence_drops_stale_stop_before_start():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_runtime_stops_for_roles_known_missing,
    )

    semantic = PrivilegedStep(
        step_id="finish-test-worker",
        title="Recover test worker",
        objective="Configure and start worker for /home/lzl/test/vemu_uestc",
        risk="medium",
        expected_changes=[
            "start missing worker role at 45555 and backend health succeeds",
        ],
    )
    items = [
        {"id": "stop", "title": "Stop old test worker process", "objective": "Stop worker on 45552", "depends_on": []},
        {"id": "config", "title": "Set WtxConfig worker_port", "objective": "Set worker_port", "depends_on": ["stop"]},
        {"id": "start", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["config"]},
    ]

    result = _drop_runtime_stops_for_roles_known_missing(items, semantic)

    assert [item["id"] for item in result] == ["config", "start"]
    assert result[0]["depends_on"] == []


def test_runtime_recovery_does_not_checkout_observed_existing_revision():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_unauthorized_source_mutations,
    )

    semantic = PrivilegedStep(
        step_id="recover-formal",
        title="Restore production master and worker",
        objective="Restore runtime using the existing source revision on branch develop",
        risk="medium",
        expected_changes=["master restarts", "worker starts"],
    )
    items = [
        {"id": "checkout", "title": "Ensure source tree is at production revision", "objective": "Checkout branch develop", "depends_on": []},
        {"id": "master", "title": "Restart master screen component", "objective": "Restart master", "depends_on": ["checkout"]},
        {"id": "worker", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["master"]},
    ]

    result = _drop_unauthorized_source_mutations(items, semantic)

    assert [item["id"] for item in result] == ["master", "worker"]
    assert result[0]["depends_on"] == []


def test_config_precedes_component_start_without_migration_keyword_or_stop():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _order_runtime_migration_items,
    )

    semantic = PrivilegedStep(
        step_id="finish-worker",
        title="Repair test worker",
        objective="Configure and start worker for /home/lzl/test/vemu_uestc",
        risk="medium",
    )
    items = [
        {"id": "start", "title": "Start worker screen component", "objective": "Start worker", "depends_on": []},
        {"id": "config", "title": "Set test worker_port in config", "objective": "Update worker_port to 45555", "depends_on": []},
    ]

    result = _order_runtime_migration_items(items, semantic)

    assert [item["id"] for item in result] == ["config", "start"]
    assert result[1]["depends_on"] == ["config"]


def test_runtime_migration_synthesizes_stop_and_preserves_natural_worker_restart():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_migration_stop_item,
        _ensure_runtime_role_recovery_items,
    )

    semantic = PrivilegedStep(
        step_id="change-1",
        title="迁移 test 实例端口并重启 master",
        objective="迁移 /home/lzl/test/vemu_uestc 到独立端口",
        risk="high",
        expected_changes=[
            "test 实例 master gunicorn 进程重启并监听 45553",
            "test 实例 worker 进程重启并监听 45554",
        ],
    )
    items = [{
        "id": "start-master",
        "title": "Restart test master component",
        "objective": "Restart master for /home/lzl/test/vemu_uestc",
        "reason": "apply port",
        "depends_on": [],
        "expected_changes": ["master restarts"],
        "success_criteria": ["master healthy"],
        "risk_suggestion": "medium",
    }]

    with_stop = _ensure_runtime_migration_stop_item(items, semantic)
    result = _ensure_runtime_role_recovery_items(with_stop, semantic)

    assert result[-1]["title"] == "Restart worker screen component"
    assert result[-1]["depends_on"] == ["start-master"]
    assert any(item["title"] == "Stop current worker runtime process" for item in result)
    assert any("master" in item["title"].lower() and "restart" in item["title"].lower() for item in result)


def test_explicit_stop_reconfigure_start_synthesizes_stop_without_migrate_word():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_migration_stop_item,
    )

    semantic = PrivilegedStep(
        step_id="repair-worker",
        title="修复 test worker：停止旧 45552、改端口 45555、重启",
        objective="停止 /home/lzl/test/vemu_uestc worker 监听并修改 worker_port",
        risk="high",
        expected_changes=["worker 在 45555 启动"],
    )
    items = [
        {"id": "config", "title": "修改 worker_port 配置", "objective": "将 worker_port 改为 45555", "depends_on": []},
        {"id": "start", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["config"]},
    ]

    result = _ensure_runtime_migration_stop_item(items, semantic)

    assert result[0]["title"] == "Stop current worker runtime process"


def test_explicit_stop_config_start_extracts_worker_role_without_restart_word():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_migration_stop_item,
    )

    semantic = PrivilegedStep(
        step_id="repair-worker",
        title="修复 test worker：停止旧 worker、改端口 45555、启动新 worker",
        objective=(
            "精确停止 /home/lzl/test/vemu_uestc worker 监听 45552，"
            "修改 worker_port 后启动于 45555"
        ),
        risk="high",
        expected_changes=["worker 在 45555 启动"],
    )
    items = [
        {"id": "config", "title": "Set WtxConfig worker_port", "objective": "Set worker_port", "depends_on": []},
        {"id": "start", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["config"]},
    ]

    result = _ensure_runtime_migration_stop_item(items, semantic)

    assert result[0]["title"] == "Stop current worker runtime process"
    assert result[0]["objective"].endswith("before changing its port")


def test_runtime_migration_orders_pid_titled_stop_before_config_and_start():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _order_runtime_migration_items,
    )

    semantic = PrivilegedStep(
        step_id="migrate",
        title="Migrate worker from 45552 to 45555",
        objective="Stop worker, update worker_port, start worker",
        risk="high",
    )
    items = [
        {"id": "config", "title": "Set WtxConfig worker_port", "objective": "Set worker_port to 45555", "depends_on": []},
        {"id": "stop", "title": "Stop test worker PIDs 3049962, 3049898", "objective": "Stop exact worker group", "depends_on": ["config"]},
        {"id": "start", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["stop"]},
    ]

    result = _order_runtime_migration_items(items, semantic)

    assert [item["id"] for item in result] == ["stop", "config", "start"]
    assert result[0]["depends_on"] == []
    assert result[1]["depends_on"] == ["stop"]
    assert result[2]["depends_on"] == ["config"]


def test_stop_binding_supports_json_encoded_pid_resource_group():
    import inspect
    from klonet_agent.ops.privileged import execution_agent

    source = inspect.getsource(execution_agent.PrivilegedExecutionAgent._registered_binding)

    assert "json.loads(pid)" in source
    assert "pid = min(numeric_pids)" in source


def test_port_config_mutation_forces_structured_class_attribute_action():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _forced_registered_action_for_step,
    )

    step = PrivilegedStep(
        step_id="config-worker",
        title="修改 worker_port 配置从 45552 为 45555",
        objective="Edit /home/lzl/test/vemu_uestc config worker_port",
        risk="medium",
    )

    assert _forced_registered_action_for_step(step) == "set_python_class_attribute"


def test_runtime_migration_does_not_repeat_a_stop_semantic_predecessor():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_migration_stop_item,
    )

    semantic = PrivilegedStep(
        step_id="migrate-test",
        title="Migrate test worker port",
        objective="Migrate /home/lzl/test/vemu_uestc worker to 45554",
        depends_on=["stop-test"],
        risk="medium",
        expected_changes=["worker restarts on 45554"],
    )
    items = [{"id": "configure", "title": "Set WtxConfig worker_port", "objective": "Set port"}]

    assert _ensure_runtime_migration_stop_item(items, semantic) == items


def test_runtime_migration_drops_model_stop_when_predecessor_handles_it():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_runtime_stops_covered_by_semantic_predecessor,
    )

    semantic = PrivilegedStep(
        step_id="migrate-test",
        title="Migrate test master/worker ports",
        objective="Migrate /home/lzl/test/vemu_uestc to 45554/45555",
        depends_on=["stop-test-worker"],
        risk="high",
    )
    items = [
        {"id": "stop-master", "title": "Stop test old master process", "objective": "Stop instance on 45551", "depends_on": []},
        {"id": "config", "title": "Set WtxConfig master_port", "objective": "Set port", "depends_on": ["stop-master"]},
        {"id": "start", "title": "Start master screen component", "objective": "Start master", "depends_on": ["config"]},
    ]

    result = _drop_runtime_stops_covered_by_semantic_predecessor(items, semantic)

    assert [item["id"] for item in result] == ["config", "start"]
    assert result[0]["depends_on"] == []
    assert result[1]["depends_on"] == ["config"]


def test_duplicate_runtime_role_starts_are_collapsed_and_dependencies_rewired():
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_runtime_role_mutations,
    )

    items = [
        {"id": "master-a", "title": "Start master screen component", "objective": "Start master", "depends_on": []},
        {"id": "master-b", "title": "Restore master screen component", "objective": "Restore master", "depends_on": ["master-a"]},
        {"id": "worker-a", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["master-b"]},
        {"id": "verify", "title": "Verify health", "objective": "Verify health", "depends_on": ["worker-a"]},
    ]

    result = _collapse_redundant_runtime_role_mutations(items)

    assert [item["id"] for item in result] == ["master-a", "worker-a", "verify"]
    assert result[1]["depends_on"] == ["master-a"]


def test_restart_role_drops_redundant_broad_pre_stop_without_config_change():
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_runtime_stops_covered_by_same_step_restart,
    )

    items = [
        {"id": "stop", "title": "Stop production master screen component", "objective": "Stop master on 45551", "depends_on": []},
        {"id": "restart", "title": "Restart master screen component", "objective": "Restart master on 45551", "depends_on": ["stop"]},
        {"id": "worker", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["restart"]},
    ]

    result = _drop_runtime_stops_covered_by_same_step_restart(items)

    assert [item["id"] for item in result] == ["restart", "worker"]
    assert result[0]["depends_on"] == []
    assert result[1]["depends_on"] == ["restart"]


def test_unhealthy_live_role_restart_expands_to_exact_stop_then_start():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _expand_unhealthy_role_restarts,
    )

    semantic = PrivilegedStep(
        step_id="repair-master",
        title="Recover master",
        objective="Recover master for /srv/102",
        expected_changes=[
            "restart unhealthy master role at 27694 and backend health succeeds"
        ],
        risk="high",
    )
    items = [
        {
            "id": "restart-master",
            "title": "Restart master screen component",
            "objective": "Restart master for /srv/102",
            "depends_on": ["edit-source"],
            "expected_changes": ["master restarts"],
        }
    ]

    result = _expand_unhealthy_role_restarts(items, semantic)

    assert [item["id"] for item in result] == [
        "restart-master-stop-current", "restart-master"
    ]
    assert result[0]["title"] == "Stop current master runtime process"
    assert result[0]["depends_on"] == ["edit-source"]
    assert result[1]["title"] == "Start master screen component"
    assert result[1]["depends_on"] == ["restart-master-stop-current"]


def test_missing_role_start_drops_model_invented_pre_stop_without_pid():
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_runtime_stops_covered_by_same_step_restart,
    )

    items = [
        {"id": "stop-worker", "title": "Stop current worker runtime process", "objective": "Stop worker", "depends_on": []},
        {"id": "restart-master", "title": "Restart master screen component", "objective": "Restart master", "depends_on": ["stop-worker"]},
        {"id": "start-worker", "title": "Start worker screen component", "objective": "Start missing worker", "depends_on": ["restart-master"]},
    ]

    result = _drop_runtime_stops_covered_by_same_step_restart(items)

    assert [item["id"] for item in result] == ["restart-master", "start-worker"]
    assert result[0]["depends_on"] == []


def test_synthesized_missing_worker_start_then_drops_pre_stop():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_runtime_stops_covered_by_same_step_restart,
        _ensure_runtime_role_recovery_items,
    )

    semantic = PrivilegedStep(
        step_id="formal",
        title="Restart formal master",
        objective="Restart master for /home/lzl/vemu_uestc",
        expected_changes=[
            "restart unhealthy master role at 45551 and backend health succeeds",
            "start missing worker role at 45552 and backend health succeeds",
        ],
        risk="high",
    )
    initial = [
        {"id": "stop-worker", "title": "Stop current worker runtime process", "objective": "Stop worker", "depends_on": []},
        {"id": "restart-master", "title": "Restart master screen component", "objective": "Restart master", "depends_on": ["stop-worker"]},
    ]

    with_recovery = _ensure_runtime_role_recovery_items(initial, semantic)
    result = _drop_runtime_stops_covered_by_same_step_restart(with_recovery)

    assert all(not item["id"].endswith("stop-worker") for item in result)
    assert any("worker" in item["title"].lower() and "start" in item["title"].lower() for item in result)


def test_duplicate_master_starts_collapse_when_objective_mentions_both_roles():
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_runtime_role_mutations,
    )

    shared = "Restore master 45551 and worker 45552 for /home/lzl/vemu_uestc"
    items = [
        {"id": "master-a", "title": "Restart master screen component", "objective": shared, "depends_on": []},
        {"id": "master-b", "title": "Restart master screen component", "objective": shared, "depends_on": ["master-a"]},
        {"id": "worker", "title": "Start worker screen component", "objective": shared, "depends_on": ["master-b"]},
    ]

    result = _collapse_redundant_runtime_role_mutations(items)

    assert [item["id"] for item in result] == ["master-a", "worker"]
    assert result[1]["depends_on"] == ["master-a"]


def test_runtime_implementation_drops_roles_that_semantics_only_preserve():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_runtime_role_mutations,
        _filter_unauthorized_runtime_role_mutations,
    )

    semantic = PrivilegedStep(
        step_id="formal",
        title="Restore formal master/worker",
        objective="Restore master and worker for /home/lzl/vemu_uestc",
        risk="high",
        expected_changes=[
            "master and worker become healthy",
            "celery and web_terminal remain running and unchanged",
        ],
    )
    items = [
        {"id": "master-a", "title": "Start master screen component", "objective": "Start master", "depends_on": []},
        {"id": "worker-a", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["master-a"]},
        {"id": "celery", "title": "Start celery screen component", "objective": "Start celery", "depends_on": ["worker-a"]},
        {"id": "web", "title": "Start web_terminal screen component", "objective": "Start web_terminal", "depends_on": ["celery"]},
        {"id": "master-b", "title": "Start master screen component", "objective": "Start master for formal", "depends_on": ["web"]},
        {"id": "verify", "title": "Verify backend health", "objective": "Verify health", "depends_on": ["master-b"]},
    ]

    filtered = _filter_unauthorized_runtime_role_mutations(items, semantic)
    result = _collapse_redundant_runtime_role_mutations(filtered)

    assert [item["id"] for item in result] == ["master-a", "worker-a", "verify"]
    assert result[-1]["depends_on"] == ["worker-a"]


def test_health_acceptance_does_not_authorize_mutating_healthy_worker():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _filter_unauthorized_runtime_role_mutations,
    )

    semantic = PrivilegedStep(
        step_id="repair-master",
        title="Repair master startup source",
        objective="Recover master for /srv/102",
        expected_changes=[
            "restart unhealthy master role at 27694 and backend health succeeds",
            "worker backend health succeeds on 27695",
        ],
        risk="high",
    )
    items = [
        {"id": "master", "title": "Restart master screen component", "objective": "Restart master", "depends_on": []},
        {"id": "worker", "title": "Restart worker via gunicorn", "objective": "Restart worker", "depends_on": ["master"]},
    ]

    result = _filter_unauthorized_runtime_role_mutations(items, semantic)

    assert [item["id"] for item in result] == ["master"]


def test_source_only_semantic_step_drops_all_runtime_mutations():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _filter_unauthorized_runtime_role_mutations,
    )

    semantic = PrivilegedStep(
        step_id="repair-source",
        title="Remove injected master boot failure",
        objective="Delete the injected function so master_main.py can be imported",
        expected_changes=["master_main.py no longer contains the injected code"],
        risk="medium",
    )
    items = [
        {"id": "edit", "title": "Edit master_main.py", "objective": "Delete function", "depends_on": []},
        {"id": "wrong-master", "title": "Start master screen component", "objective": "Start master", "depends_on": ["edit"]},
        {"id": "wrong-worker", "title": "Start worker screen component", "objective": "Start worker", "depends_on": ["wrong-master"]},
        {"id": "verify", "title": "Verify Python syntax", "objective": "Verify file", "depends_on": ["wrong-worker"]},
    ]

    result = _filter_unauthorized_runtime_role_mutations(items, semantic)

    assert [item["id"] for item in result] == ["edit", "verify"]
    assert result[1]["depends_on"] == ["edit"]


def test_stop_only_semantic_step_keeps_its_exact_role_stop():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _filter_unauthorized_runtime_role_mutations,
    )

    semantic = PrivilegedStep(
        step_id="stop-worker",
        title="Stop worker",
        objective="Precisely stop worker for /srv/instance",
        expected_changes=["worker process stops"],
        risk="high",
    )
    items = [
        {"id": "stop-worker", "title": "Stop worker process", "objective": "Stop worker", "depends_on": []},
        {"id": "stop-master", "title": "Stop master process", "objective": "Stop master", "depends_on": []},
    ]

    result = _filter_unauthorized_runtime_role_mutations(items, semantic)

    assert [item["id"] for item in result] == ["stop-worker"]


def test_duplicate_python_symbol_removals_collapse_and_rewire():
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_python_removal_items,
    )

    path = "/srv/102/mains/master_main.py"
    items = [
        {
            "id": "remove-a",
            "title": "Remove KLONET_E2E_INJECTED_FAILURE function",
            "objective": "Delete it from %s" % path,
            "depends_on": [],
        },
        {
            "id": "remove-b",
            "title": "Delete KLONET_E2E_INJECTED_FAILURE call",
            "objective": "Remove it from %s" % path,
            "depends_on": ["remove-a"],
        },
        {
            "id": "verify",
            "title": "Verify Python syntax",
            "objective": "Verify repaired source",
            "depends_on": ["remove-b"],
        },
    ]

    result = _collapse_redundant_python_removal_items(items)

    assert [item["id"] for item in result] == ["remove-a", "verify"]
    assert result[1]["depends_on"] == ["remove-a"]


def test_duplicate_atomic_config_writes_are_collapsed_and_rewired():
    from klonet_agent.ops.privileged.execution_agent import (
        _collapse_redundant_config_mutations,
    )

    items = [
        {"id": "master-a", "title": "Set test active config master_port", "objective": "Set WtxConfig master_port to 45554", "depends_on": []},
        {"id": "worker-a", "title": "Set test active config worker_port", "objective": "Set WtxConfig worker_port to 45555", "depends_on": ["master-a"]},
        {"id": "master-b", "title": "Set WtxConfig master_port", "objective": "Set config master_port to 45554", "depends_on": ["worker-a"]},
        {"id": "worker-b", "title": "Set WtxConfig worker_port", "objective": "Set config worker_port to 45555", "depends_on": ["master-b"]},
        {"id": "start", "title": "Start test master", "objective": "Start master", "depends_on": ["worker-b"]},
    ]

    result = _collapse_redundant_config_mutations(items)

    assert [item["id"] for item in result] == ["master-a", "worker-a", "start"]
    assert result[-1]["depends_on"] == ["worker-a"]


def test_root_bound_worker_stop_has_one_deterministic_atomic_action():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _deterministic_runtime_stop_items,
    )

    semantic = PrivilegedStep(
        step_id="stop-test",
        title="精确停止 root-bound worker on port 45552",
        objective=(
            "Stop only worker processes whose cwd belongs to "
            "/home/lzl/test/vemu_uestc and whose listener owns port 45552"
        ),
        risk="high",
        expected_changes=["worker listener stops"],
    )

    result = _deterministic_runtime_stop_items(semantic)

    assert len(result) == 1
    assert result[0]["title"] == "Stop root-bound worker runtime"
    assert "/home/lzl/test/vemu_uestc" in result[0]["objective"]
    assert "45552" in result[0]["objective"]


def test_runtime_migration_orders_stop_before_config_and_component_starts():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _order_runtime_migration_items,
    )

    semantic = PrivilegedStep(
        step_id="change-1",
        title="Migrate test runtime",
        objective="Migrate /home/lzl/test/vemu_uestc to isolated ports and restart it",
        risk="high",
    )
    items = [
        {"id": "set-master", "title": "Set WtxConfig master_port", "depends_on": []},
        {"id": "set-worker", "title": "Set WtxConfig worker_port", "depends_on": ["set-master"]},
        {"id": "stop", "title": "Stop test runtime processes", "depends_on": ["set-worker"]},
        {"id": "start-master", "title": "Start master screen component", "depends_on": ["stop"]},
        {"id": "start-worker", "title": "Start worker screen component", "depends_on": ["start-master"]},
        {"id": "verify", "title": "Verify runtime health", "depends_on": ["start-worker"]},
    ]

    result = _order_runtime_migration_items(items, semantic)

    assert [item["id"] for item in result] == [
        "stop", "set-master", "set-worker", "start-master", "start-worker", "verify",
    ]
    assert result[1]["depends_on"] == ["stop"]
    assert result[-1]["depends_on"] == ["start-worker"]


def _plan():
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    return ChangePlan(
        plan_id="priv-ops-bind",
        goal="deploy isolated instance",
        risk="high",
        steps=[
            ChangeStep(
                step_id="deploy",
                title="deploy instance",
                objective="deploy an isolated instance",
                risk="high",
                expected_changes=["instance is created"],
                postconditions=[{"checker": "exit_code_zero"}],
            )
        ],
    )


class FakeLegacyBinder:
    def __init__(self, apply):
        self.apply = apply
        self.received = None

    def prepare_plan(self, plan, *, grounded_context):
        self.received = plan
        self.apply(plan)
        return plan


def test_workflow_binder_maps_direct_registered_action_back_to_change_plan():
    from klonet_agent.ops.privileged.contracts import ExecutionBinding
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBinder

    def apply(plan):
        plan.steps[0].execution_binding = ExecutionBinding(
            kind="registered_action",
            risk="high",
            action="service_control",
            args={"service": "v4e2e", "operation": "start"},
            postconditions=[{"checker": "exit_code_zero"}],
        )

    legacy = FakeLegacyBinder(apply)
    plan = _plan()

    bound = ChangeBinder(legacy).bind(plan)

    assert legacy.received.schema_version == 3
    assert bound is plan
    assert bound.steps[0].execution_binding.kind == "registered_action"
    assert bound.steps[0].implementation_plan is None
    assert bound.status == "awaiting_confirmation"


def test_workflow_binder_preserves_action_shell_hierarchical_implementation():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
        ShellArtifact,
    )
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBinder

    def apply(plan):
        action = PrivilegedStep(
            step_id="deploy-1",
            title="clone",
            risk="high",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                risk="high",
                action="git_operation",
                args={"operation": "clone"},
                postconditions=[{"checker": "exit_code_zero"}],
            ),
        )
        artifact = ShellArtifact(
            artifact_id="script-1",
            script="true",
            sha256="x",
            declared_changes=["config is written"],
        )
        shell = PrivilegedStep(
            step_id="deploy-2",
            title="configure",
            risk="high",
            depends_on=["deploy-1"],
            execution_binding=ExecutionBinding(
                kind="shell_artifact",
                risk="high",
                shell_artifact=artifact,
                postconditions=[{"checker": "exit_code_zero"}],
            ),
        )
        plan.steps[0].implementation_plan = ImplementationPlan(
            implementation_id="impl-deploy",
            semantic_step_id="deploy",
            objective="deploy",
            steps=[action, shell],
            status="awaiting_confirmation",
        )

    plan = ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())

    implementation = plan.steps[0].implementation_plan
    assert implementation is not None
    assert [item.execution_binding.kind for item in implementation.steps] == [
        "registered_action",
        "shell_artifact",
    ]
    restored = type(plan).from_dict(plan.to_dict())
    assert restored.steps[0].implementation_plan.steps[1].step_id == "deploy-2"


def test_workflow_binder_rejects_direct_verification_only_as_a_change_implementation():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError, ChangeBinder

    def apply(plan):
        binding = ExecutionBinding(
            kind="verification_only",
            risk="readonly",
            postconditions=[{"checker": "exit_code_zero"}],
        )
        plan.steps[0].execution_binding = binding

    with pytest.raises(ChangeBindingError, match="verification_only"):
        ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())


def test_workflow_binder_lifts_hierarchical_verification_out_of_execution_plan():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBinder

    def apply(plan):
        plan.steps[0].implementation_plan = ImplementationPlan(
            implementation_id="impl-deploy",
            semantic_step_id="deploy",
            objective="deploy",
            steps=[
                PrivilegedStep(
                    step_id="deploy-action",
                    title="deploy",
                    risk="high",
                    execution_binding=ExecutionBinding(
                        kind="registered_action",
                        risk="high",
                        action="service_control",
                        args={"service": "v4e2e", "operation": "start"},
                        postconditions=[{"checker": "exit_code_zero"}],
                    ),
                ),
                PrivilegedStep(
                    step_id="deploy-verify",
                    title="verify deployment",
                    risk="readonly",
                    depends_on=["deploy-action"],
                    execution_binding=ExecutionBinding(
                        kind="verification_only",
                        risk="readonly",
                        postconditions=[
                            {"checker": "service_active", "args": {"service": "v4e2e"}}
                        ],
                    ),
                ),
            ],
        )

    plan = ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())

    assert [
        item.step_id for item in plan.steps[0].implementation_plan.steps
    ] == ["deploy-action"]
    assert {item["checker"] for item in plan.steps[0].postconditions} == {
        "exit_code_zero",
        "service_active",
    }


def test_workflow_binder_rewires_precondition_verification_without_lifting_it():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBinder

    def apply(plan):
        plan.steps[0].implementation_plan = ImplementationPlan(
            implementation_id="impl-deploy",
            semantic_step_id="deploy",
            objective="deploy",
            steps=[
                PrivilegedStep(
                    step_id="check-absent",
                    title="verify target is absent",
                    risk="readonly",
                    execution_binding=ExecutionBinding(
                        kind="verification_only",
                        risk="readonly",
                        postconditions=[{
                            "checker": "container_absent",
                            "args": {"container": "v4e2e-redis"},
                        }],
                    ),
                ),
                PrivilegedStep(
                    step_id="create",
                    title="create target",
                    risk="high",
                    depends_on=["check-absent"],
                    execution_binding=ExecutionBinding(
                        kind="registered_action",
                        risk="high",
                        action="create_docker_container",
                        args={"name": "v4e2e-redis"},
                        postconditions=[{
                            "checker": "container_running",
                            "args": {"container": "v4e2e-redis"},
                        }],
                    ),
                ),
            ],
        )

    plan = ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())

    implementation = plan.steps[0].implementation_plan
    assert [item.step_id for item in implementation.steps] == ["create"]
    assert implementation.steps[0].depends_on == []
    assert not any(
        check["checker"] == "container_absent"
        for check in plan.steps[0].postconditions
    )


def test_workflow_binder_translates_shared_binding_failure_to_workflow_boundary():
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError, ChangeBinder

    class FailingSharedBinder:
        def prepare_plan(self, plan, *, grounded_context):
            raise ExecutionBindingError("clone target could not be grounded")

    with pytest.raises(ChangeBindingError, match="clone target could not be grounded"):
        ChangeBinder(FailingSharedBinder()).bind(_plan())
