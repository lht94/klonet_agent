from __future__ import annotations

import pytest


def test_binding_timeout_checkpoints_and_resumes_only_unbound_atomic_step():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.change_binding import (
        ChangeBinder, ChangeBindingError,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    def atomic(step_id):
        return PrivilegedStep(
            step_id=step_id,
            title=step_id,
            objective="start one service",
            risk="medium",
            expected_changes=["service starts"],
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        )

    binding = ExecutionBinding(
        kind="registered_action",
        risk="medium",
        action="manage_service",
        args={"service": "example", "operation": "start"},
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
    )

    class CapabilityBinder:
        calls = 0
        first_binding_calls = 0
        second_binding_calls = 0

        def prepare_plan(self, legacy, *, grounded_context, checkpoint):
            self.calls += 1
            semantic = legacy.steps[0]
            if semantic.implementation_plan is None:
                semantic.implementation_plan = ImplementationPlan(
                    implementation_id="impl-deploy",
                    semantic_step_id="deploy",
                    objective="deploy",
                    steps=[atomic("first"), atomic("second")],
                    status="draft",
                )
            first, second = semantic.implementation_plan.steps
            if first.execution_binding is None:
                self.first_binding_calls += 1
                first.execution_binding = binding
                checkpoint(legacy, "deploy", 1)
            if self.calls == 1:
                checkpoint(legacy, "deploy", 1)
                raise TimeoutError("provider timed out")
            assert first.execution_binding is not None
            self.second_binding_calls += 1
            second.execution_binding = binding
            semantic.implementation_plan.status = "awaiting_confirmation"
            checkpoint(legacy, "deploy", 2)
            legacy.status = "awaiting_confirmation"
            return legacy

    capability = CapabilityBinder()
    binder = ChangeBinder(capability)
    plan = ChangePlan.new(
        goal="deploy",
        risk="medium",
        steps=[ChangeStep(
            step_id="deploy", title="deploy", objective="deploy",
            risk="medium", expected_changes=["deployed"],
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        )],
    )
    checkpoints = []

    with pytest.raises(ChangeBindingError) as captured:
        binder.bind(plan, grounded_context=None, checkpoint=lambda item: checkpoints.append(item.to_dict()))

    assert captured.value.category == "binding_provider_transient"
    assert captured.value.replan_recommended is False
    assert captured.value.replan_context["resume_binding"] is True
    assert plan.binding_cursor == {
        "phase": "binding", "semantic_step_id": "deploy",
        "atomic_step_index": 1,
    }
    assert plan.steps[0].implementation_plan.steps[0].execution_binding is not None
    assert plan.steps[0].implementation_plan.steps[1].execution_binding is None
    assert checkpoints

    resumed = binder.bind(plan, grounded_context=None)

    assert resumed.status == "awaiting_confirmation"
    assert resumed.binding_cursor == {}
    assert capability.first_binding_calls == 1
    assert capability.second_binding_calls == 1
    assert all(
        step.execution_binding is not None
        for step in resumed.steps[0].implementation_plan.steps
    )


def test_execution_agent_resumes_draft_implementation_without_redecomposing():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, PrivilegedPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    class Agent(PrivilegedExecutionAgent):
        def __init__(self):
            super().__init__(None)
            self.decompositions = 0
            self.binding_calls = {"start-a": 0, "start-b": 0}
            self.fail_second = True

        def _decompose_semantic_step(self, *args, **kwargs):
            self.decompositions += 1
            return [
                PrivilegedStep(
                    step_id="start-a", title="Start service a",
                    objective="Start service a", risk="medium",
                    expected_changes=["service a starts"],
                    postconditions=[{"checker": "exit_code_zero", "args": {}}],
                ),
                PrivilegedStep(
                    step_id="start-b", title="Start service b",
                    objective="Start service b", risk="medium",
                    depends_on=["start-a"],
                    expected_changes=["service b starts"],
                    postconditions=[{"checker": "exit_code_zero", "args": {}}],
                ),
            ]

        def prepare_step(self, plan, step, *, grounded_context):
            self.binding_calls[step.step_id] += 1
            if step.step_id == "start-b" and self.fail_second:
                self.fail_second = False
                raise TimeoutError("provider timeout")
            return ExecutionBinding(
                kind="registered_action", risk="medium",
                action="manage_service",
                args={
                    "service": step.step_id.removeprefix("start-"),
                    "operation": "start",
                },
                postconditions=[{"checker": "exit_code_zero", "args": {}}],
            )

    plan = PrivilegedPlan(
        plan_id="priv-ops-checkpoint",
        goal="start services a and b",
        risk="medium",
        steps=[PrivilegedStep(
            step_id="start-services", title="Start services a and b",
            objective="Start services a and b", risk="medium",
            expected_changes=["services a and b start"],
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        )],
    )
    agent = Agent()
    checkpoints = []

    with pytest.raises(TimeoutError):
        agent.prepare_plan(
            plan, grounded_context=None,
            checkpoint=lambda _plan, semantic, atomic: checkpoints.append(
                (semantic, atomic)
            ),
        )

    implementation = plan.steps[0].implementation_plan
    assert implementation is not None
    assert implementation.status == "draft"
    assert implementation.steps[0].execution_binding is not None
    assert implementation.steps[1].execution_binding is None

    resumed = agent.prepare_plan(
        plan, grounded_context=None,
        checkpoint=lambda _plan, semantic, atomic: checkpoints.append(
            (semantic, atomic)
        ),
    )

    assert resumed.status == "awaiting_confirmation"
    assert agent.decompositions == 1
    assert agent.binding_calls == {"start-a": 1, "start-b": 2}
    assert resumed.steps[0].implementation_plan.status == "awaiting_confirmation"
    assert ("start-services", 1) in checkpoints
    assert ("start-services", 2) in checkpoints


def test_binding_contradicted_fact_replans_only_the_resource_consumer_step():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.workflow.change_binding import (
        ChangeBinder, ChangeBindingError,
    )
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )

    class ForbiddenCapabilityBinder:
        def prepare_plan(self, *args, **kwargs):
            raise AssertionError("invalidated frozen facts must stop before Binding")

    plan = ChangePlan.new(
        goal="create platform",
        risk="medium",
        steps=[
            ChangeStep(
                step_id="prepare-source", title="prepare", objective="prepare",
                risk="medium", expected_changes=["source prepared"],
                postconditions=[{"checker": "exit_code_zero", "args": {}}],
            ),
            ChangeStep(
                step_id="start-master", title="start", objective="start",
                risk="medium", expected_changes=["master started"],
                postconditions=[{"checker": "exit_code_zero", "args": {}}],
            ),
        ],
        resources=[PlanResource(
            "master_port", "port", "frozen", "master_port", 47001,
            "checked_free", consumers=["start-master.master_port"],
        )],
    )
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence="ports",
        action_catalog="actions",
        facts={"evidence_resolutions": [{
            "gap_id": "gap-master-port",
            "contradicted_fact_ids": ["fact-master-port-free"],
            "requirements": [{
                "fact_id": "fact-master-port-free",
                "expected": 47001,
            }],
            "affected_steps": [],
        }]},
    )

    with pytest.raises(ChangeBindingError) as captured:
        ChangeBinder(ForbiddenCapabilityBinder()).bind(
            plan, grounded_context=context,
        )

    assert captured.value.category == "binding_evidence_invalidated"
    assert captured.value.replan_recommended is True
    assert captured.value.replan_context["affected_steps"] == ["start-master"]
    assert captured.value.replan_context["step_id"] == "start-master"
    assert "prepare-source" not in captured.value.replan_context["affected_steps"]


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
    from klonet_agent.ops.privileged.contracts import (
        PlanResource, PrivilegedPlan, PrivilegedStep,
    )
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
    assert binding.args["project_root"] == str(root)


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


def test_prepare_project_files_replaces_model_guesses_with_registered_contract():
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    root = "/srv/klonet/test"
    checks = _canonical_action_postconditions(
        "prepare_project_files",
        {
            "project_root": root,
            "entry_sha256s": {"master_main.py": "a" * 64},
        },
        [
            {"checker": "file_exists", "args": {"path": root + "/manage.py"}},
            {"checker": "file_exists", "args": {"path": root + "/mains"}},
        ],
    )

    assert [item["args"]["path"] for item in checks] == [
        root + "/" + name for name in REQUIRED_ENTRY_FILES
    ]
    assert all("manage.py" not in item["args"]["path"] for item in checks)
    master = next(
        item for item in checks
        if item["args"]["path"].endswith("/master_main.py")
    )
    assert master == {
        "checker": "file_sha256",
        "args": {"path": root + "/master_main.py", "sha256": "a" * 64},
    }


def test_shared_klonet_context_is_role_scoped_and_not_runtime_evidence():
    from klonet_agent.ops.privileged.context import klonet_domain_context
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES

    binding = klonet_domain_context("binding")
    discovery = klonet_domain_context("discovery")

    assert "KLONET_DOMAIN_CONTEXT" in binding
    assert "never current-host evidence" in binding
    assert all(name in binding for name in REQUIRED_ENTRY_FILES)
    assert "manage.py are not Klonet readiness criteria" in binding
    assert "canonical pre/postconditions" in binding
    assert "policy-validated read-only commands" in discovery
    assert "canonical pre/postconditions" not in discovery


def test_screen_postconditions_are_canonical_for_component_role():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    common = {
        "screen_session": "v4e2e_c",
        "component": "celery",
        "master_port": "47001",
        "worker_port": "47002",
        "web_terminal_port": "47003",
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
    assert worker[0] == {
        "checker": "screen_session_exists",
        "args": {"session": "test_w"},
    }


def test_screen_postconditions_keep_session_acceptance_with_run_as_uid():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    checks = _canonical_action_postconditions(
        "restart_screen_component",
        {
            "screen_session": "test_m",
            "component": "master",
            "project_root": "/home/lzl/test/vemu_uestc",
            "master_port": 45554,
            "run_as_uid": 1000,
        },
        [{"checker": "screen_session_exists", "args": {"session": "test_m"}}],
    )

    assert checks[0] == {
        "checker": "screen_session_exists",
        "args": {"session": "test_m"},
    }
    assert sum(item["checker"] == "screen_session_exists" for item in checks) == 1


def test_each_atomic_component_consumes_only_its_runtime_identity(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    root = tmp_path / "vemu_uestc"
    root.mkdir()
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root", str(root),
            "running_platforms", consumers=["restart.project_root"],
        ),
        PlanResource(
            "master_uid", "identifier", "frozen", "master_uid", 1000,
            "process_detail", consumers=["restart.run_as_uid"],
        ),
        PlanResource(
            "master_python", "path", "frozen", "master_python_executable",
            "/envs/test/bin/python3.8", "process_detail",
            consumers=["restart.python_executable"],
        ),
        PlanResource(
            "worker_uid", "identifier", "frozen", "worker_uid", 1000,
            "process_detail", consumers=["restart.run_as_uid"],
        ),
        PlanResource(
            "worker_python", "path", "frozen", "worker_python_executable",
            "/envs/klonet-py38/bin/python3.8", "process_detail",
            consumers=["restart.python_executable"],
        ),
    ]
    agent = PrivilegedExecutionAgent(None)

    master = agent._registered_binding(
        {"action": "restart_screen_component", "args": {}},
        PrivilegedStep(
            step_id="restart__master", title="Restart master screen component",
            objective="Restart master for %s" % root, risk="medium",
        ),
        None,
        resources,
    )
    worker = agent._registered_binding(
        {"action": "restart_screen_component", "args": {}},
        PrivilegedStep(
            step_id="restart__worker", title="Restart worker screen component",
            objective="Restart worker for %s" % root, risk="medium",
        ),
        None,
        resources,
    )

    assert master.args["python_executable"] == "/envs/test/bin/python3.8"
    assert worker.args["python_executable"] == (
        "/envs/klonet-py38/bin/python3.8"
    )


def test_binding_rejects_restart_and_preserve_for_the_same_role():
    import pytest

    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError, _validate_semantic_runtime_consistency,
    )

    step = PrivilegedStep(
        step_id="restart",
        title="Restart test runtime",
        objective="Restart master for /srv/test",
        expected_changes=[
            "restart requested master role",
            "preserve healthy master role without restart",
        ],
        risk="medium",
    )

    with pytest.raises(ExecutionBindingError, match="restart_and_preserve"):
        _validate_semantic_runtime_consistency(step)


def test_stop_platform_action_schema_requires_structured_component_contracts():
    from klonet_agent.ops.privileged.execution_agent import (
        _action_arg_json_schema,
    )

    schema = _action_arg_json_schema("component_contracts")

    assert schema["type"] == "array"
    assert schema["items"]["required"] == [
        "component", "screen_session", "pids", "ports",
    ]
    assert schema["items"]["properties"]["pids"]["items"] == {
        "type": "integer",
    }


def test_stop_platform_contracts_are_compiled_from_runtime_inventory_not_model_prose():
    import base64
    import json

    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        _compile_stop_platform_contracts,
        _validate_action_contract_consistency,
    )
    from klonet_agent.ops.privileged.contracts import PrivilegedStep

    specs = [
        {"name": "master", "managed": True, "screen_suffix": "m"},
        {"name": "celery", "managed": True, "screen_suffix": "c"},
        {"name": "web_terminal", "managed": True, "screen_suffix": "web"},
        {"name": "worker", "managed": True, "screen_suffix": "w"},
    ]
    encoded = base64.urlsafe_b64encode(
        json.dumps(specs).encode("utf-8")
    ).decode("ascii").rstrip("=")
    context = GroundedPlanContext(
        knowledge_evidence="knowledge",
        environment_evidence="runtime",
        action_catalog="actions",
        facts={"runtime_instances": [{
            "platform": "test",
            "project_root": "/srv/test",
            "roles": ["master", "celery", "web_terminal", "worker"],
            "configured_ports": {
                "master_port": 45554,
                "web_terminal_port": 5114,
                "worker_port": 45555,
            },
            "fields": {
                "component_specs_b64": encoded,
                "master_identities": "10:1000:/env/test/python3.8,11:1000:/env/test/python3.8",
                "celery_identities": "20:1000:/env/test/python3.8",
                "web_terminal_identities": "30:1000:/env/test/python3.8",
                "worker_identities": "40:1000:/env/worker/python3.8,41:1000:/env/worker/python3.8",
            },
        }]},
    )

    compiled, problem = _compile_stop_platform_contracts(
        {
            "platform": "test",
            "project_root": "/srv/test",
            "component_contracts": "model-authored prose must be replaced",
        },
        context,
    )

    assert problem == ""
    assert compiled["run_as_uid"] == 1000
    assert compiled["component_contracts"] == [
        {
            "component": "master", "screen_session": "test_m",
            "pids": [10, 11], "ports": [45554],
        },
        {
            "component": "celery", "screen_session": "test_c",
            "pids": [20], "ports": [],
        },
        {
            "component": "web_terminal", "screen_session": "test_web",
            "pids": [30], "ports": [5114],
        },
        {
            "component": "worker", "screen_session": "test_w",
            "pids": [40, 41], "ports": [45555],
        },
    ]
    step = PrivilegedStep(
        step_id="stop-test", title="Stop test platform screens",
        objective="Stop the existing test Screen-managed roles",
        expected_changes=["test Screen sessions stop"], risk="high",
    )
    assert _validate_action_contract_consistency(
        "stop_platform_screens", compiled, step,
    ) == ""


def test_stop_platform_contract_compiler_rejects_mixed_runtime_users():
    import base64
    import json

    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        _compile_stop_platform_contracts,
    )

    encoded = base64.urlsafe_b64encode(json.dumps([
        {"name": "master", "managed": True, "screen_suffix": "m"},
        {"name": "worker", "managed": True, "screen_suffix": "w"},
    ]).encode("utf-8")).decode("ascii").rstrip("=")
    context = GroundedPlanContext(
        "knowledge", "runtime", "actions",
        facts={"runtime_instances": [{
            "platform": "test", "project_root": "/srv/test",
            "roles": ["master", "worker"], "configured_ports": {},
            "fields": {
                "component_specs_b64": encoded,
                "master_identities": "10:1000:/env/test/python3.8",
                "worker_identities": "20:1001:/env/worker/python3.8",
            },
        }]},
    )

    _compiled, problem = _compile_stop_platform_contracts(
        {"platform": "test", "project_root": "/srv/test"}, context,
    )

    assert problem == "stop_platform_mixed_or_missing_run_as_uid"


def test_binding_does_not_confuse_preserved_port_owner_with_preserved_role():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_semantic_runtime_consistency,
    )

    step = PrivilegedStep(
        step_id="restart",
        title="重启 vemu_uestc 的应用组件",
        objective=(
            "按项目根目录 /home/lzl/vemu_uestc 重启 master 和 worker；"
            "修改端口 worker:45552→45556，保留冲突端口的当前占用者"
        ),
        expected_changes=[
            "restart requested master role at 45551",
            "worker_port changes to 45556 while the existing listener on "
            "45552 is preserved",
            "restart requested worker role at 45556",
        ],
        risk="medium",
    )

    _validate_semantic_runtime_consistency(step)


def test_binding_still_rejects_role_preservation_written_after_role():
    import pytest

    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError, _validate_semantic_runtime_consistency,
    )

    step = PrivilegedStep(
        step_id="restart",
        title="Restart application roles",
        objective="Restart master and worker",
        expected_changes=["master remains unchanged without restart"],
        risk="medium",
    )

    with pytest.raises(ExecutionBindingError, match="master restart_and_preserve"):
        _validate_semantic_runtime_consistency(step)


def test_binding_allows_preserving_an_unrelated_runtime_role():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_semantic_runtime_consistency,
    )

    step = PrivilegedStep(
        step_id="restart-worker",
        title="Restart worker only",
        objective="Restart worker and preserve healthy master role",
        expected_changes=["restart requested worker role"],
        risk="medium",
    )

    _validate_semantic_runtime_consistency(step)


def test_restart_screen_identity_uses_the_canonical_component_port():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    for component, port_key, port in [
        ("master", "master_port", "45554"),
        ("worker", "worker_port", 45555),
    ]:
        checks = _canonical_action_postconditions(
            "restart_screen_component",
            {
                "screen_session": "test_%s" % component[0],
                "component": component,
                "project_root": "/home/lzl/test/vemu_uestc",
                port_key: port,
            },
            [],
        )

        assert checks[-1] == {
            "checker": "component_restart_identity",
            "args": {
                "component": component,
                "project_root": "/home/lzl/test/vemu_uestc",
                "port": int(port),
            },
        }


def test_restart_screen_identity_without_a_component_port_uses_process_identity():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    checks = _canonical_action_postconditions(
        "restart_screen_component",
        {
            "screen_session": "test_c",
            "component": "celery",
            "project_root": "/home/lzl/test/vemu_uestc",
        },
        [],
    )

    assert checks == [
        {
            "checker": "screen_session_exists",
            "args": {"session": "test_c"},
        },
        {
            "checker": "component_process_project_root",
            "args": {
                "component": "celery",
                "project_root": "/home/lzl/test/vemu_uestc",
            },
        },
        {
            "checker": "component_restart_identity",
            "args": {
                "component": "celery",
                "project_root": "/home/lzl/test/vemu_uestc",
            },
        },
    ]


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


def test_stop_component_preconditions_drop_cross_role_model_checks():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_preconditions,
    )

    result = _canonical_action_preconditions(
        "stop_klonet_component",
        {
            "component": "worker", "pid": 1698329, "port": "45552",
            "runtime_cwd": "/home/lzl/vemu_uestc",
        },
        [
            {"checker": "process_pid_absent", "args": {"pid": 1669188}},
            {"checker": "port_listening", "args": {"port": 45552}},
        ],
    )

    assert result == [
        {
            "checker": "component_process_project_root",
            "args": {
                "component": "worker",
                "project_root": "/home/lzl/vemu_uestc",
            },
        },
        {
            "checker": "port_listener_project_root",
            "args": {
                "port": 45552,
                "project_root": "/home/lzl/vemu_uestc",
            },
        },
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

    with pytest.raises(ExecutionBindingError, match="worker") as captured:
        _validate_runtime_recovery_action_coverage(semantic, [master])

    assert captured.value.failed_criteria == [
        "语义计划要求恢复 worker，但 Implementation Plan 没有为该角色"
        "绑定启动或重启动作。"
    ]
    assert captured.value.missing_decisions == []


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


def test_missing_runtime_with_existing_screen_uses_restart_lifecycle():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_runtime_role_recovery_verbs,
    )

    semantic = PrivilegedStep(
        step_id="restore-vemu",
        title="Restore vemu roles",
        objective="Restore /srv/vemu",
        expected_changes=[
            "start missing web_terminal role at 5115 and listener readiness succeeds",
        ],
        risk="medium",
    )
    items = [{
        "id": "web", "title": "Start web_terminal screen component",
        "objective": "restore web", "depends_on": [],
    }]
    resources = [PlanResource(
        "restore_vemu_web_screen", "identifier", "frozen",
        "screen_session", "vemu_web", "running_platforms",
        consumers=["restore-vemu.screen_session"],
    )]

    result = _normalize_runtime_role_recovery_verbs(items, semantic, resources)

    assert result[0]["title"] == "Restart web_terminal screen component"


def test_missing_runtime_without_existing_screen_keeps_start_lifecycle():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_runtime_role_recovery_verbs,
    )

    semantic = PrivilegedStep(
        step_id="restore-vemu", title="Restore vemu roles",
        objective="Restore /srv/vemu",
        expected_changes=[
            "start missing worker role at 45556 and backend health succeeds",
        ],
        risk="medium",
    )
    items = [{
        "id": "worker", "title": "Start worker screen component",
        "objective": "restore worker", "depends_on": [],
    }]

    result = _normalize_runtime_role_recovery_verbs(items, semantic, [])

    assert result[0]["title"] == "Start worker screen component"


def test_screen_from_another_semantic_step_cannot_change_action_lifecycle():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_runtime_role_recovery_verbs,
    )

    semantic = PrivilegedStep(
        step_id="restore-target", title="Restore target roles",
        objective="Restore /srv/target",
        expected_changes=[
            "start missing worker role at 45556 and backend health succeeds",
        ],
        risk="medium",
    )
    items = [{
        "id": "worker", "title": "Start worker screen component",
        "objective": "restore worker", "depends_on": [],
    }]
    resources = [PlanResource(
        "other_worker_screen", "identifier", "frozen",
        "screen_session", "other_w", "running_platforms",
        consumers=["restore-other.screen_session"],
    )]

    result = _normalize_runtime_role_recovery_verbs(items, semantic, resources)

    assert result[0]["title"] == "Start worker screen component"


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


def test_role_qualified_worker_pid_compiles_to_stop_action_pid():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _inject_frozen_resource_args,
    )

    step = PrivilegedStep(
        step_id="restart-test__stop-worker",
        title="Stop current worker runtime process",
        objective="Stop worker for /home/lzl/test/vemu_uestc",
        risk="high",
    )
    resources = [PlanResource(
        name="test_worker_pid", kind="identifier", status="frozen",
        role="worker_pid", value="3075451", source="running_platforms",
        consumers=["restart-test.worker_pid"],
    )]

    compiled = _inject_frozen_resource_args(step, {}, resources)

    assert compiled["pid"] == "3075451"
    assert "worker_pid" not in compiled


def test_role_qualified_pid_never_leaks_across_components():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _inject_frozen_resource_args,
    )

    step = PrivilegedStep(
        step_id="restart-test__stop-master",
        title="Stop current master runtime process",
        objective="Stop master for /home/lzl/test/vemu_uestc",
        risk="high",
    )
    resources = [PlanResource(
        name="test_worker_pid", kind="identifier", status="frozen",
        role="worker_pid", value="3075451", source="running_platforms",
        consumers=["restart-test.worker_pid"],
    )]

    compiled = _inject_frozen_resource_args(step, {}, resources)

    assert "pid" not in compiled
    assert "worker_pid" not in compiled


def test_stop_component_compiles_port_from_same_semantic_scope_not_step_text():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _compile_stop_component_args,
    )

    step = PrivilegedStep(
        step_id="restart-v4__stop-worker",
        title="Stop current worker runtime process",
        objective=(
            "Stop worker for /home/lzl/klonet_v4_e2e; "
            "unrelated instance uses port 45552"
        ),
        risk="high",
    )
    resources = [
        PlanResource(
            name="v4_root", kind="path", status="frozen",
            role="instance_root", value="/home/lzl/klonet_v4_e2e",
            source="running_platforms",
            consumers=["restart-v4.project_root"],
        ),
        PlanResource(
            name="v4_worker_pid", kind="identifier", status="frozen",
            role="worker_pid", value="3943652", source="running_platforms",
            consumers=["restart-v4.worker_pid"],
        ),
        PlanResource(
            name="v4_worker_port", kind="port", status="frozen",
            role="worker_port", value=47002, source="running_platforms",
            consumers=["restart-v4.worker_port"],
        ),
        PlanResource(
            name="other_worker_port", kind="port", status="frozen",
            role="worker_port", value=45552, source="running_platforms",
            consumers=["restart-other.worker_port"],
        ),
    ]

    compiled = _compile_stop_component_args(
        step, {"port": 45552, "pid": 999}, resources,
    )

    assert compiled["component"] == "worker"
    assert compiled["runtime_cwd"] == "/home/lzl/klonet_v4_e2e"
    assert compiled["pid"] == 3943652
    assert compiled["port"] == 47002


def test_stop_component_rejects_model_port_when_no_frozen_role_port_exists():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _compile_stop_component_args,
    )

    step = PrivilegedStep(
        step_id="restart-v4__stop-worker",
        title="Stop current worker runtime process",
        objective="Stop worker for /home/lzl/klonet_v4_e2e on guessed port 45552",
        risk="high",
    )

    compiled = _compile_stop_component_args(step, {"port": 45552}, [])

    assert "port" not in compiled


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


def test_compact_role_port_migration_authorizes_only_the_named_config_writes():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_unauthorized_config_mutations,
    )

    semantic = PrivilegedStep(
        step_id="migrate-runtime-ports",
        title="Restart the selected runtime roles",
        objective=(
            "Keep the existing listeners and move the target runtime to "
            "web_terminal:5114→5115,worker:45552→45556"
        ),
        risk="high",
        expected_changes=["restart requested worker role at 45556"],
    )
    items = [
        {
            "id": "worker-port",
            "title": "Set WtxConfig worker_port",
            "objective": "Set worker_port",
            "depends_on": [],
        },
        {
            "id": "web-port",
            "title": "Set WtxConfig web_terminal_port",
            "objective": "Set web_terminal_port",
            "depends_on": ["worker-port"],
        },
        {
            "id": "master-ip",
            "title": "Set WtxConfig master_ip",
            "objective": "Set master_ip",
            "depends_on": ["web-port"],
        },
        {
            "id": "restart-worker",
            "title": "Restart worker screen component",
            "objective": "Restart worker",
            "depends_on": ["master-ip"],
        },
    ]

    result = _drop_unauthorized_config_mutations(items, semantic)

    assert [item["id"] for item in result] == [
        "worker-port", "web-port", "restart-worker",
    ]
    assert result[-1]["depends_on"] == ["web-port"]


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


def test_structural_container_inference_uses_matching_service_ports():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    resources = [
        PlanResource(
            name="mysql_host_port", kind="port", status="frozen",
            role="service_port", value=47009,
            consumers=["change-2.mysql_host_port"],
        ),
        PlanResource(
            name="mysql_container_internal_port", kind="port", status="frozen",
            role="container_internal_port", value=3306,
            consumers=["change-2.mysql_container_port"],
        ),
        PlanResource(
            name="redis_host_port", kind="port", status="frozen",
            role="service_port", value=47010,
            consumers=["change-2.redis_host_port"],
        ),
        PlanResource(
            name="redis_container_internal_port", kind="port", status="frozen",
            role="container_internal_port", value=6379,
            consumers=["change-2.redis_container_port"],
        ),
    ]

    args = _infer_structural_action_args(
        "create_docker_container",
        {
            "name": "create_e2e-mysql",
            "image": "mysql:latest",
            "port_bindings": ["127.0.0.1:47010:3306"],
        },
        resources,
    )

    assert args["port_bindings"] == ["127.0.0.1:47009:3306"]


def test_structural_config_inference_uses_redis_ip_and_rabbitmq_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    resources = [
        PlanResource(
            name="redis_port", kind="port", status="frozen",
            role="redis_port", value=47010, consumers=["change-3.redis_port"],
        ),
        PlanResource(
            name="rabbitmq_port", kind="port", status="frozen",
            role="rabbitmq_port", value=47011,
            consumers=["change-3.rabbitmq_port"],
        ),
    ]

    redis_ip = _infer_structural_action_args(
        "set_python_class_attribute", {"attribute": "redis_ip"}, resources,
    )
    rabbitmq_db = _infer_structural_action_args(
        "set_python_class_attribute",
        {"attribute": "celery_rabbitmq_port_db"},
        resources,
    )

    assert redis_ip["value"] == "127.0.0.1"
    assert rabbitmq_db["value"] == "47011/7"


def test_sync_directory_step_with_clone_prohibition_forces_sync_action():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _forced_registered_action_for_step,
    )

    step = PrivilegedStep(
        step_id="change-1",
        title="Sync source project tree to target directory",
        objective=(
            "Use sync_directory to copy the complete working tree from the source. "
            "No git_operation or Git clone."
        ),
        risk="medium",
        expected_changes=["target tree exists"],
    )

    assert _forced_registered_action_for_step(step) == "sync_directory"


def test_atomic_redis_step_prefers_title_service_over_parent_objective():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_semantic_action_args,
        _validate_action_contract_consistency,
    )

    step = PrivilegedStep(
        step_id="change-2__redis",
        title="Create Redis container create_e2e-redis",
        objective=(
            "Parent deployment creates MySQL, Redis, and RabbitMQ containers; "
            "this atomic effect creates create_e2e-redis"
        ),
        risk="medium",
        expected_changes=["Redis container exists"],
    )
    args = _infer_semantic_action_args(
        "create_docker_container",
        {"name": "create_e2e-redis", "image": "redis:7"},
        step,
    )

    assert _validate_action_contract_consistency(
        "create_docker_container", args, step,
    ) == ""
    assert args["name"] == "create_e2e-redis"


def test_deterministic_klonet_config_compiles_all_thirteen_attributes():
    from types import SimpleNamespace

    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _deterministic_klonet_config_items,
    )

    step = PrivilegedStep(
        step_id="change-3",
        title="Configure WtxConfig attributes in copied config.py",
        objective=(
            "Set master_port, worker_port, web_terminal_port, public_port, "
            "mysql_port, redis_port, rabbitmq_port, master_ip, mysql_ip, redis_ip, "
            "rabbitmq_ip, celery_redis_port_db, and celery_rabbitmq_port_db"
        ),
        risk="medium",
        expected_changes=["config is written"],
    )
    resources = [
        PlanResource(
            name="config_file_path", kind="path", status="frozen",
            role="config_file", value="/srv/create/vemu_config/config.py",
            consumers=["change-3.path"],
        ),
    ]
    for name, role, value in (
        ("master_port", "service_port", 47001),
        ("worker_port", "service_port", 47002),
        ("web_terminal_port", "service_port", 47003),
        ("public_port", "service_port", 47008),
        ("mysql_host_port", "service_port", 47009),
        ("redis_host_port", "service_port", 47010),
        ("rabbitmq_host_port", "service_port", 47011),
    ):
        resources.append(PlanResource(
            name=name, kind="port", status="frozen", role=role, value=value,
            consumers=["change-3.%s" % name.replace("_host", "")],
        ))
    plan = SimpleNamespace(
        goal="配置 Klonet 平台",
        resources=resources,
    )

    items = _deterministic_klonet_config_items(plan, step)

    attributes = [
        item["attribute"] for item in items if "attribute" in item
    ]
    assert attributes == [
        "master_ip", "mysql_ip", "redis_ip", "rabbitmq_ip",
        "master_port", "worker_port", "web_terminal_port", "public_port",
        "redis_port", "mysql_port", "rabbitmq_port",
        "celery_redis_port_db", "celery_rabbitmq_port_db",
    ]
    values = {
        item["attribute"]: item["value"]
        for item in items if "attribute" in item
    }
    assert values["celery_redis_port_db"] == "47010/6"
    assert values["celery_rabbitmq_port_db"] == "47011/7"


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
        "start-runtime-web-terminal",
        "start-runtime-worker",
    ]
    assert result[3]["depends_on"] == ["start-runtime-worker"]


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
    assert "session_mismatch" in _validate_action_contract_consistency(
        "start_screen_component",
        {
            "component": "worker",
            "platform": "test",
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
    assert args["project_root"] == "/home/lzl/vemu_uestc"


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
    assert args["project_root"] == "/home/lzl/test/vemu_uestc"


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
    assert binding.args["project_root"] == "/home/lzl/test/vemu_uestc"


def test_forced_component_stop_compiles_frozen_contract_without_llm(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        PlanResource, PrivilegedPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    root = tmp_path / "vemu_uestc"
    root.mkdir()
    step = PrivilegedStep(
        step_id="restart-test__stop-worker",
        title="Stop current worker runtime process",
        objective="Stop worker for %s" % root,
        risk="high", expected_changes=["current worker process stops"],
    )
    resources = [
        PlanResource(
            name="test_root", kind="path", status="frozen",
            role="instance_root", value=str(root), source="running_platforms",
            consumers=["restart-test.project_root"],
        ),
        PlanResource(
            name="test_worker_pid", kind="identifier", status="frozen",
            role="worker_pid", value="3075451", source="running_platforms",
            consumers=["restart-test.worker_pid"],
        ),
        PlanResource(
            name="test_worker_port", kind="port", status="frozen",
            role="worker_port", value=45555, source="running_platforms",
            consumers=["restart-test.worker_port"],
        ),
    ]
    plan = PrivilegedPlan(
        plan_id="stop-worker", goal="restart test worker", risk="high",
        steps=[step], resources=resources,
    )

    binding = PrivilegedExecutionAgent(None).prepare_step(
        plan, step, grounded_context=None,
    )

    assert binding.action == "stop_klonet_component"
    assert binding.args["runtime_cwd"] == str(root)
    assert binding.args["component"] == "worker"
    assert binding.args["pid"] == "3075451"
    assert int(binding.args["port"]) == 45555


def test_frozen_entry_source_never_overrides_screen_runtime_root():
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
            consumers=["change-2.source_root"],
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

    assert binding.args["project_root"] == "/home/lzl/test/vemu_uestc"
    assert binding.args["screen_session"] == "test_vemu_uestc_w"

    assert binding.args["component"] == "worker"


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
    assert binding.args["project_root"] == str(instance)


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
    assert binding.args["project_root"] == str(instance)


def test_scoped_resource_name_does_not_hide_instance_root_role(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _runtime_root_from_frozen_paths,
    )

    instance = tmp_path / "102"
    instance.mkdir()
    resource = PlanResource(
        "instance_102_instance_root", "path", "frozen", "instance_root",
        str(instance), "running_platforms",
        consumers=["restart-102.project_root"],
    )

    assert _runtime_root_from_frozen_paths([resource]) == str(instance)


def test_frozen_runtime_root_is_scoped_to_owning_semantic_step(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _runtime_root_from_frozen_paths,
    )

    first = tmp_path / "102"
    second = tmp_path / "v4e2e"
    first.mkdir()
    second.mkdir()
    resources = [
        PlanResource(
            "instance_102_instance_root", "path", "frozen", "instance_root",
            str(first), "running_platforms",
            consumers=["restart-102.project_root"],
        ),
        PlanResource(
            "v4e2e_instance_root", "path", "frozen", "instance_root",
            str(second), "running_platforms",
            consumers=["restart-v4e2e.project_root"],
        ),
    ]

    assert _runtime_root_from_frozen_paths(
        resources,
        semantic_step_id="restart-v4e2e__start-master",
    ) == str(second)
    assert _runtime_root_from_frozen_paths(
        resources,
        semantic_step_id="restart-102__start-master",
    ) == str(first)
    # An unscoped multi-instance lookup is ambiguous and must not select the
    # first platform merely because of resource ordering.
    assert _runtime_root_from_frozen_paths(resources) == ""


def test_runtime_start_inserts_explicit_entry_preparation_before_screen(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_entry_preparation_items,
    )

    instance = tmp_path / "v4e2e"
    mains = instance / "mains"
    mains.mkdir(parents=True)
    for name in REQUIRED_ENTRY_FILES:
        (mains / name).write_text("# %s\n" % name, encoding="utf-8")
    semantic = PrivilegedStep(
        step_id="restart",
        title="Restart v4e2e master",
        objective="Restart master from the frozen instance root",
        expected_changes=["master restarts"],
        risk="medium",
    )
    resources = [PlanResource(
        "instance_root", "path", "frozen", "instance_root",
        str(instance), "running_platforms", consumers=["restart.project_root"],
    )]
    items = [
        {
            "id": "stop-master",
            "title": "Stop master runtime",
            "objective": "Stop master",
            "depends_on": [],
        },
        {
            "id": "start-master",
            "title": "Start master screen component",
            "objective": "Start master",
            "depends_on": ["stop-master"],
        },
    ]

    result = _ensure_runtime_entry_preparation_items(items, semantic, resources)

    assert [item["id"] for item in result] == [
        "stop-master", "prepare-runtime-entries", "start-master",
    ]
    assert "source_root=%s" % mains in result[1]["objective"]
    assert "project_root=%s" % instance in result[1]["objective"]
    assert result[1]["depends_on"] == ["stop-master"]
    assert result[2]["depends_on"] == ["prepare-runtime-entries"]


def test_entry_preparation_breaks_model_back_edge_from_stop_to_start(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_entry_preparation_items,
        _topologically_order_implementation_items,
    )

    instance = tmp_path / "102"
    mains = instance / "mains"
    mains.mkdir(parents=True)
    for name in REQUIRED_ENTRY_FILES:
        (mains / name).write_text("# entry\n", encoding="utf-8")
    semantic = PrivilegedStep(
        step_id="restart-102", title="Restart all runtime roles",
        objective="Restart master celery web_terminal worker",
        expected_changes=["restart unhealthy worker role"], risk="medium",
    )
    resources = [PlanResource(
        "instance_102_instance_root", "path", "frozen", "instance_root",
        str(instance), "running_platforms",
        consumers=["restart-102.project_root"],
    )]
    items = [
        {
            "id": "start-master", "title": "Start master screen component",
            "objective": "Start master", "depends_on": [],
        },
        {
            "id": "start-web", "title": "Start web_terminal screen component",
            "objective": "Start web_terminal", "depends_on": ["start-master"],
        },
        {
            "id": "stop-worker", "title": "Stop current worker runtime process",
            "objective": "Stop unhealthy worker while recovering master celery "
            "web_terminal and worker", "depends_on": ["start-web"],
        },
        {
            "id": "start-worker", "title": "Start worker screen component",
            "objective": "Start worker", "depends_on": ["stop-worker"],
        },
    ]

    prepared = _ensure_runtime_entry_preparation_items(
        items, semantic, resources,
    )
    ordered = _topologically_order_implementation_items(prepared)

    assert [item["id"] for item in ordered] == [
        "stop-worker", "prepare-runtime-entries", "start-master",
        "start-web", "start-worker",
    ]


def test_runtime_restart_keeps_explicit_preparation_when_entries_already_match(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import (
        _ensure_runtime_entry_preparation_items,
        _forced_registered_action_for_step,
    )

    instance = tmp_path / "v4e2e"
    mains = instance / "mains"
    mains.mkdir(parents=True)
    for name in REQUIRED_ENTRY_FILES:
        content = "# %s\n" % name
        (mains / name).write_text(content, encoding="utf-8")
        (instance / name).write_text(content, encoding="utf-8")
    semantic = PrivilegedStep(
        step_id="restart", title="重启 v4e2e 平台",
        objective="从冻结的项目根目录重启全部应用组件",
        expected_changes=["重启 master、celery、web_terminal、worker"],
        risk="medium",
    )
    resources = [PlanResource(
        "instance_root", "path", "frozen", "instance_root",
        str(instance), "running_platforms", consumers=["restart.project_root"],
    )]

    result = _ensure_runtime_entry_preparation_items(
        [{"id": "master", "title": "重启 master Screen 组件", "objective": "重启 master"}],
        semantic,
        resources,
    )

    assert result[0]["title"] == "准备项目根目录入口文件"
    prepare_step = PrivilegedStep(
        step_id="prepare", title=result[0]["title"], objective=result[0]["objective"],
    )
    assert _forced_registered_action_for_step(prepare_step) == "prepare_project_files"


def test_canonical_runtime_dispositions_lower_without_implementation_llm(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        PlanResource, PrivilegedPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        _deterministic_runtime_recovery_items,
    )

    root = tmp_path / "vemu"
    root.mkdir()
    semantic = PrivilegedStep(
        step_id="restart-vemu",
        title="重启 vemu 的应用组件",
        objective=(
            "按项目根目录 %s 重启全部应用角色；修改端口 "
            "web_terminal:5114→5115,worker:45552→45556" % root
        ),
        expected_changes=[
            "restart requested master role at 45551",
            "worker_port changes from 45552 to checked-free port 45556",
            "restart requested worker role at 45556",
        ],
        risk="medium",
    )
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root", str(root),
            "running_platforms", consumers=["restart-vemu.project_root"],
        ),
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 45556,
            "planner_decision", consumers=["restart-vemu.worker_port"],
        ),
        PlanResource(
            "web_terminal_port", "port", "frozen", "web_terminal_port", 5115,
            "planner_decision", consumers=["restart-vemu.web_terminal_port"],
        ),
    ]
    plan = PrivilegedPlan(
        plan_id="p-vemu", goal="重启 vemu 全部角色", risk="medium",
        steps=[semantic], resources=resources,
    )

    items = _deterministic_runtime_recovery_items(plan, semantic)

    assert [item["title"] for item in items] == [
        "Set WtxConfig worker_port",
        "Set WtxConfig web_terminal_port",
        "Restart master screen component",
        "Restart worker screen component",
    ]
    assert items[0]["objective"].endswith("/vemu_config/config.py")
    assert items[-1]["depends_on"] == ["restart-master"]


def test_noncanonical_runtime_prose_still_uses_normal_binding_selection(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _deterministic_runtime_recovery_items,
    )

    semantic = PrivilegedStep(
        step_id="custom", title="Handle unusual runtime concern",
        objective="Use operator judgment for an unfamiliar component",
        expected_changes=["custom outcome"], risk="medium",
    )
    plan = PrivilegedPlan(
        plan_id="p", goal="custom", risk="medium", steps=[semantic],
    )

    assert _deterministic_runtime_recovery_items(plan, semantic) == []


def test_registered_runtime_lowering_binds_without_any_llm_call(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        PlanResource, PrivilegedPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    root = tmp_path / "v4e2e"
    mains = root / "mains"
    mains.mkdir(parents=True)
    for name in REQUIRED_ENTRY_FILES:
        (mains / name).write_text("# %s\n" % name, encoding="utf-8")
    semantic = PrivilegedStep(
        step_id="restart-runtime",
        title="Restart v4e2e application roles",
        objective="Restart application roles for %s" % root,
        expected_changes=["restart requested worker role at 47002"],
        postconditions=[{
            "checker": "backend_health",
            "args": {"url": "http://127.0.0.1:47002/server_health/"},
        }],
        risk="medium",
    )
    resources = [
        PlanResource(
            "root", "path", "frozen", "instance_root", str(root),
            "running_platforms", consumers=["restart-runtime.project_root"],
        ),
        PlanResource(
            "platform", "identifier", "frozen", "instance_identifier", "v4e2e",
            "running_platforms", consumers=["restart-runtime.platform"],
        ),
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 47002,
            "running_platforms", consumers=["restart-runtime.worker_port"],
        ),
        PlanResource(
            "worker_uid", "identifier", "frozen", "worker_uid", 1000,
            "running_platforms", consumers=["restart-runtime.worker_run_as_uid"],
        ),
        PlanResource(
            "worker_python", "path", "frozen", "worker_python_executable",
            "/env/worker/python3.8", "running_platforms",
            consumers=["restart-runtime.worker_python_executable"],
        ),
    ]
    plan = PrivilegedPlan(
        plan_id="p-runtime", goal="restart", risk="medium",
        steps=[semantic], resources=resources,
    )

    bound = PrivilegedExecutionAgent(None).prepare_plan(
        plan, grounded_context=None,
    )

    implementation = bound.steps[0].implementation_plan
    assert implementation is not None
    assert [
        step.execution_binding.action for step in implementation.steps
    ] == ["prepare_project_files", "restart_screen_component"]


def test_nested_frozen_config_path_binds_without_llm_or_root_path_guess(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        PlanResource, PrivilegedPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    root = tmp_path / "outer"
    config = root / "runtime" / "vemu_config" / "config.py"
    config.parent.mkdir(parents=True)
    config.write_text(
        "class WtxConfig:\n    worker_port = 46552\n"
        "PROJ_CONFIG = WtxConfig()\n",
        encoding="utf-8",
    )
    semantic = PrivilegedStep(
        step_id="restart-runtime",
        title="Restart target worker",
        objective=(
            "Restart worker for %s; change worker:46552→46554"
            % root
        ),
        expected_changes=[
            "worker_port changes from 46552 to checked-free port 46554",
            "start missing worker role at 46554",
        ],
        postconditions=[{
            "checker": "backend_health",
            "args": {"url": "http://127.0.0.1:46554/server_health/"},
        }],
        risk="medium",
    )
    resources = [
        PlanResource(
            "root", "path", "frozen", "instance_root", str(root),
            "running_platforms", consumers=["restart-runtime.project_root"],
        ),
        PlanResource(
            "config", "path", "frozen", "config_path", str(config),
            "existing_layout", consumers=["restart-runtime.path"],
        ),
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 46554,
            "checked_free_replacement",
            consumers=["restart-runtime.worker_port"],
        ),
        PlanResource(
            "platform", "identifier", "frozen", "instance_identifier",
            "target", "running_platforms",
            consumers=["restart-runtime.platform"],
        ),
        PlanResource(
            "uid", "identifier", "frozen", "run_as_uid", 1000,
            "runtime_evidence", consumers=["restart-runtime.worker_run_as_uid"],
        ),
        PlanResource(
            "python", "path", "frozen", "python_executable",
            "/usr/bin/python3", "runtime_evidence",
            consumers=["restart-runtime.worker_python_executable"],
        ),
    ]
    plan = PrivilegedPlan(
        plan_id="p-nested-config", goal="migrate worker", risk="medium",
        steps=[semantic], resources=resources,
    )

    bound = PrivilegedExecutionAgent(None).prepare_plan(
        plan, grounded_context=None,
    )

    implementation = bound.steps[0].implementation_plan
    assert implementation is not None
    config_binding = implementation.steps[0].execution_binding
    assert config_binding is not None
    assert config_binding.action == "set_python_class_attribute"
    assert config_binding.args["path"] == str(config)
    assert config_binding.args["attribute"] == "worker_port"
    assert config_binding.args["value"] == "46554"


def test_entry_preparation_binding_freezes_source_hashes(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    instance = tmp_path / "v4e2e"
    mains = instance / "mains"
    mains.mkdir(parents=True)
    for name in REQUIRED_ENTRY_FILES:
        (mains / name).write_text("# %s\n" % name, encoding="utf-8")
    step = PrivilegedStep(
        step_id="restart__prepare-runtime-entries",
        title="Prepare project root entry files",
        objective=(
            "Copy canonical entries; source_root=%s project_root=%s"
            % (mains, instance)
        ),
        expected_changes=["root entries match mains"],
        risk="medium",
    )

    binding = PrivilegedExecutionAgent(None)._registered_binding(
        {"action": "prepare_project_files", "args": {}},
        step,
        None,
        [],
    )

    assert binding.args["project_root"] == str(instance)
    assert binding.args["source_root"] == str(mains)
    assert set(binding.args["entry_sha256s"]) == set(REQUIRED_ENTRY_FILES)
    assert all(
        check["checker"] == "file_sha256"
        for check in binding.postconditions
    )


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


def test_missing_worker_drops_stop_when_objective_mentions_every_role():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _drop_runtime_stops_for_roles_known_missing,
    )

    semantic = PrivilegedStep(
        step_id="recover-all-roles",
        title="Recover stopped platform",
        objective="Start missing master, celery, web_terminal and worker roles",
        risk="medium",
        expected_changes=[
            "start missing master role at 47001 and backend health succeeds",
            "start missing worker role at 47002 and backend health succeeds",
        ],
    )
    items = [
        {
            "id": "stop-worker", "title": "Stop current worker runtime process",
            "objective": "Stop worker before recovering master, celery, web_terminal and worker",
            "depends_on": [],
        },
        {
            "id": "start-worker", "title": "Start worker screen component",
            "objective": "Start worker", "depends_on": ["stop-worker"],
        },
    ]

    result = _drop_runtime_stops_for_roles_known_missing(items, semantic)

    assert [item["id"] for item in result] == ["start-worker"]
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
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _compile_stop_component_args,
    )

    step = PrivilegedStep(
        step_id="restart-test__stop-worker", title="Stop worker",
        objective="Stop worker for /home/lzl/test/vemu_uestc", risk="high",
    )
    resources = [
        PlanResource(
            name="test_worker_pids", kind="identifier", status="frozen",
            role="worker_pid", value="[3075490, 3075451]",
            source="running_platforms",
            consumers=["restart-test.worker_pid"],
        ),
        PlanResource(
            name="test_worker_port", kind="port", status="frozen",
            role="worker_port", value=45555, source="running_platforms",
            consumers=["restart-test.worker_port"],
        ),
    ]

    compiled = _compile_stop_component_args(step, {}, resources)

    assert compiled["pid"] == 3075451
    assert compiled["port"] == 45555


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
    assert result[1]["title"] == "启动 master Screen 组件"
    assert result[1]["depends_on"] == ["restart-master-stop-current"]


def test_runtime_component_dispositions_cover_dynamic_managed_roles():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _normalize_runtime_role_recovery_verbs,
        _split_multi_component_runtime_items,
    )

    semantic = PrivilegedStep(
        step_id="restart-runtime",
        title="Restart application components",
        objective="Restart application components for /srv/v4e2e",
        expected_changes=[
            "restart requested master role at 47001 and backend health succeeds",
            "restart requested celery role and process readiness succeeds",
            "start missing web_terminal role at 47003 and listener readiness succeeds",
            "restart requested managed component metrics and component readiness succeeds",
            "restart requested worker role at 47002 and backend health succeeds",
        ],
        risk="medium",
    )
    items = [{
        "id": "runtime",
        "title": "Restart platform runtime components",
        "objective": "Restore every requested runtime role",
        "depends_on": [],
        "expected_changes": ["application components recover"],
    }]

    result = _split_multi_component_runtime_items(items, semantic)
    result = _normalize_runtime_role_recovery_verbs(result, semantic)

    assert [item["title"] for item in result] == [
        "Restart master screen component",
        "Restart celery screen component",
        "Start web_terminal screen component",
        "Restart worker screen component",
        "Restart metrics screen component",
    ]


def test_dynamic_component_progress_and_plan_text_are_localized_generically():
    from klonet_agent.ops.privileged.execution_agent import _progress_text
    from klonet_agent.ops.privileged.workflow.mutation import _localized_plan_text

    assert _progress_text("Restart metrics screen component") == "重启 metrics Screen 组件"
    assert _progress_text("Start audit_sink screen component") == "启动 audit_sink Screen 组件"
    assert _localized_plan_text("Restart metrics screen component") == "重启 metrics Screen 组件"
    assert _localized_plan_text("Start audit_sink screen component") == "启动 audit_sink Screen 组件"


def test_runtime_binding_rejects_weak_rag_even_when_it_mentions_generic_runtime_terms():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        _validate_runtime_knowledge_contract,
    )

    plan = PrivilegedPlan(
        plan_id="rag-relevance",
        goal="重启目标平台",
        risk="medium",
        steps=[PrivilegedStep(
            step_id="start", title="Start master screen component",
            objective="启动 master", risk="medium",
        )],
    )
    context = GroundedPlanContext(
        knowledge_evidence=(
            "[ev probe=klonet_knowledge]\n"
            "- retrieval_status: weak\n"
            "unrelated note mentioning <project_root>, mains and screen"
        ),
        environment_evidence="runtime facts",
        action_catalog="catalog",
    )

    with pytest.raises(ExecutionBindingError) as exc:
        _validate_runtime_knowledge_contract(plan, context)

    assert exc.value.category == "knowledge_evidence_irrelevant"


def test_runtime_binding_accepts_reliable_rag_without_literal_layout_keywords():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_runtime_knowledge_contract,
    )

    plan = PrivilegedPlan(
        plan_id="rag-structured-contract",
        goal="将 test Master 收编到 Screen",
        risk="medium",
        steps=[PrivilegedStep(
            step_id="master", title="Restart master screen component",
            objective="重新启动已确认的 Master", risk="medium",
        )],
    )
    context = GroundedPlanContext(
        knowledge_evidence=(
            "[ev probe=klonet_knowledge]\n"
            "- retrieval_status: reliable\n"
            "Use the verified component command and validate health after restart."
        ),
        environment_evidence="structured runtime facts",
        action_catalog="catalog",
    )

    _validate_runtime_knowledge_contract(plan, context)


def test_screen_runtime_project_root_cannot_be_rebound_to_mains_source_directory():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _validate_action_resource_bindings,
    )

    step = PrivilegedStep(
        step_id="master", title="Restart master screen component",
        objective="Restart selected master", risk="medium",
    )
    resource = PlanResource(
        "instance_root", "path", "frozen", "instance_root",
        "/srv/test", "running_platforms", consumers=["master.project_root"],
    )

    _validate_action_resource_bindings(
        step, "restart_screen_component", {"project_root": "/srv/test"}, [resource],
    )
    with pytest.raises(ValueError, match="resource_binding_violation"):
        _validate_action_resource_bindings(
            step,
            "restart_screen_component",
            {"project_root": "/srv/test/mains"},
            [resource],
        )


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


def test_runtime_migration_orders_every_wtx_config_write_before_start_without_cycle():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        _order_runtime_migration_items,
        _topologically_order_implementation_items,
    )

    semantic = PrivilegedStep(
        step_id="migrate-runtime",
        title="Migrate runtime ports",
        objective="Move web_terminal and worker to checked-free ports",
        risk="high",
    )
    items = [
        {
            "id": "worker-port",
            "title": "Set WtxConfig worker_port",
            "depends_on": ["web-port"],
        },
        {
            "id": "start-master",
            "title": "Restart master screen component",
            "depends_on": ["worker-port"],
        },
        {
            "id": "start-worker",
            "title": "Start worker screen component",
            "depends_on": ["start-master"],
        },
        {
            "id": "web-port",
            "title": "Set WtxConfig web_terminal_port",
            "depends_on": ["start-worker"],
        },
    ]

    ordered = _order_runtime_migration_items(items, semantic)
    result = _topologically_order_implementation_items(ordered)

    assert [item["id"] for item in result] == [
        "worker-port", "web-port", "start-master", "start-worker",
    ]
    assert result[1]["depends_on"] == ["worker-port"]
    assert result[2]["depends_on"] == ["web-port"]


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
def test_structural_binding_compiles_frozen_future_component_spec():
    import json

    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.execution_agent import (
        _infer_structural_action_args,
    )

    resources = [
        PlanResource(
            name="instance_identifier", kind="identifier", status="frozen",
            role="instance_identifier", value="v4e2e",
            consumers=["restart-backend-roles.platform"],
        ),
        PlanResource(
            name="component_metrics_spec", kind="string", status="frozen",
            role="runtime_component_spec:metrics",
            value=json.dumps({
                "name": "metrics", "screen_suffix": "metrics",
                "command_argv": ["/opt/python", "-m", "metrics_service"],
                "preflight_argv": ["/opt/python", "-c", "import metrics_service"],
                "ports": [47009],
            }),
            consumers=["restart-backend-roles.component_spec"],
        ),
    ]

    args = _infer_structural_action_args(
        "restart_screen_component",
        {"component": "metrics", "platform": "v4e2e"},
        resources,
    )

    assert args["screen_session"] == "v4e2e_metrics"
    assert args["command_argv"][-1] == "metrics_service"
    assert args["metrics_port"] == 47009
def test_binding_preserves_long_tail_evidence_need_for_discovery():
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    requests = PrivilegedExecutionAgent._probe_requests([{
        "probe": "proc_environment_identity",
        "args": {"pid": 1234},
        "purpose": "resolve interpreter environment",
        "required_facts": ["python executable", "run_as uid"],
        "freshness": "refresh",
    }])

    assert requests == [{
        "probe": "proc_environment_identity",
        "args": {"pid": 1234},
        "purpose": "resolve interpreter environment",
        "required_facts": ["python executable", "run_as uid"],
        "freshness": "refresh",
    }]


def test_authorization_accepts_compacted_role_qualified_runtime_identity():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PlanResource, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        validate_authorizable_change_plan,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    micro = PrivilegedStep(
        step_id="restart-test__start-master",
        title="Start master Screen component",
        objective="Start test master in Screen",
        risk="high",
        expected_changes=["master role starts in test_m"],
        execution_binding=ExecutionBinding(
            kind="registered_action",
            risk="high",
            action="start_screen_component",
            args={
                "component": "master", "screen_session": "test_m",
                "run_as_uid": 1000, "python_executable": "/opt/test/bin/python",
            },
            postconditions=[{
                "checker": "screen_session_exists",
                "args": {"session": "test_m"},
            }],
        ),
    )
    change = ChangeStep(
        step_id="restart-test",
        title="Restart test master",
        objective="Restart test master in Screen",
        risk="high",
        expected_changes=["master role runs in Screen"],
        postconditions=[{"checker": "screen_session_exists", "args": {"session": "test_m"}}],
        implementation_plan=ImplementationPlan(
            implementation_id="impl-restart-test",
            semantic_step_id="restart-test",
            objective="Restart test master in Screen",
            steps=[micro],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-shared-identity",
        goal="Restart test master in Screen",
        risk="high",
        steps=[change],
        resources=[
            PlanResource(
                "test_uid", "identifier", "frozen", "run_as_uid", 1000,
                "process_detail", consumers=["restart-test.master_run_as_uid"],
            ),
            PlanResource(
                "test_python", "path", "frozen", "python_executable",
                "/opt/test/bin/python", "process_detail",
                consumers=["restart-test.master_python_executable"],
            ),
        ],
    )

    validate_authorizable_change_plan(plan)


def test_successor_authorization_does_not_refreeze_completed_component_identity():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        validate_authorizable_change_plan,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    completed = PrivilegedStep(
        step_id="restart-test__completed-master",
        title="Previously completed master Screen restart",
        objective="Preserve the proved master Screen effect",
        risk="medium",
        status="completed",
        expected_changes=["master already runs in test_m"],
        execution_binding=ExecutionBinding(
            kind="registered_action", risk="medium",
            action="restart_screen_component",
            args={
                "component": "master", "screen_session": "test_m",
                "run_as_uid": 1000,
                "python_executable": "/opt/test/bin/python",
                "project_root": "/srv/test",
            },
            postconditions=[{
                "checker": "screen_session_exists",
                "args": {"session": "test_m"},
            }],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-successor-progress",
        goal="Restart all test roles in Screen", risk="medium",
        steps=[ChangeStep(
            step_id="restart-test", title="Restart remaining test roles",
            objective="Restart remaining test roles", risk="medium",
            expected_changes=["remaining roles run in Screen"],
            postconditions=[{
                "checker": "screen_session_exists",
                "args": {"session": "test_m"},
            }],
            implementation_plan=ImplementationPlan(
                implementation_id="impl-successor-progress",
                semantic_step_id="restart-test",
                objective="Restart remaining test roles",
                steps=[completed],
            ),
        )],
        # The identity resources belonged to the predecessor authorization;
        # this successor will never execute the completed node.
        resources=[],
    )

    validate_authorizable_change_plan(plan)


def test_authorization_rejects_compacted_identity_owned_by_other_role():
    import pytest

    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PlanResource, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError, validate_authorizable_change_plan,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    micro = PrivilegedStep(
        step_id="restart-test__start-master",
        title="Start master Screen component",
        objective="Start test master in Screen",
        risk="high",
        expected_changes=["master role starts in test_m"],
        execution_binding=ExecutionBinding(
            kind="registered_action", risk="high",
            action="start_screen_component",
            args={"component": "master", "screen_session": "test_m", "run_as_uid": 1000},
            postconditions=[{"checker": "screen_session_exists", "args": {"session": "test_m"}}],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-wrong-role-identity",
        goal="Restart test master in Screen", risk="high",
        steps=[ChangeStep(
            step_id="restart-test", title="Restart test master",
            objective="Restart test master in Screen", risk="high",
            expected_changes=["master role runs in Screen"],
            postconditions=[{"checker": "screen_session_exists", "args": {"session": "test_m"}}],
            implementation_plan=ImplementationPlan(
                implementation_id="impl-restart-test",
                semantic_step_id="restart-test",
                objective="Restart test master in Screen", steps=[micro],
            ),
        )],
        resources=[PlanResource(
            "test_uid", "identifier", "frozen", "run_as_uid", 1000,
            "process_detail", consumers=["restart-test.worker_run_as_uid"],
        )],
    )

    with pytest.raises(
        ExecutionBindingError,
        match="component_runtime_identity_not_frozen=master.run_as_uid",
    ):
        validate_authorizable_change_plan(plan)


def test_observational_step_rejects_mutating_registered_action_even_without_effect_metadata():
    import pytest

    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="verify-klonet",
        title="终验 klonet 全部角色就绪",
        objective="校验 master、celery、web_terminal、worker 四角色均处于就绪状态",
        risk="readonly",
        expected_changes=[],
    )

    with pytest.raises(
        ValueError,
        match="readonly_verification_cannot_bind_mutating_action=start_screen_component",
    ):
        PrivilegedExecutionAgent(None)._registered_binding(
            {
                "action": "start_screen_component",
                "args": {
                    "component": "worker",
                    "platform": "klonet",
                    "screen_session": "klonet_w",
                    "project_root": "/home/lzl/xxy/klonet",
                },
            },
            step,
            None,
            [],
        )


def test_observational_step_can_bind_existing_shell_artifact_as_readonly(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="verify-processes",
        title="检查运行进程",
        objective="只读检查当前 Klonet 进程",
        risk="readonly",
        expected_changes=[],
    )
    binding = PrivilegedExecutionAgent(None)._shell_binding(
        {
            "script": "ps -ef",
            "cwd": str(tmp_path),
            "run_as": "",
            "timeout": 10,
            "declared_changes": [],
            "rollback": "",
            "postconditions": [{"checker": "exit_code_zero", "args": {}}],
        },
        step,
        None,
        [],
        observational=True,
    )

    assert binding.kind == "shell_artifact"
    assert binding.risk == "readonly"
    assert binding.approval_scope == "plan"
    assert binding.shell_artifact.declared_changes == []


def test_observational_shell_rejects_state_changing_command(tmp_path):
    import pytest

    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    step = PrivilegedStep(
        step_id="verify-service",
        title="检查服务状态",
        objective="只读检查服务状态",
        risk="readonly",
        expected_changes=[],
    )

    with pytest.raises(ValueError, match="readonly shell line rejected"):
        PrivilegedExecutionAgent(None)._shell_binding(
            {
                "script": "systemctl restart nginx",
                "cwd": str(tmp_path),
                "run_as": "",
                "timeout": 10,
                "declared_changes": [],
                "rollback": "",
                "postconditions": [{"checker": "exit_code_zero", "args": {}}],
            },
            step,
            None,
            [],
            observational=True,
        )


def test_hierarchical_binding_failure_identifies_owning_semantic_step():
    import pytest

    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        ExecutionBindingError,
        PrivilegedExecutionAgent,
    )

    class FailingAgent(PrivilegedExecutionAgent):
        def _decompose_semantic_step(self, *args, **kwargs):
            return [PrivilegedStep(
                step_id="change-2__create-mysql",
                title="Create MySQL container",
                objective="Create an isolated MySQL container",
                expected_changes=["new MySQL container"],
                risk="high",
            )]

        def prepare_step(self, *args, **kwargs):
            raise ExecutionBindingError(
                "docker_image_not_observed=mysql:5.7",
                category="capability_contract_invalid",
                failed_criteria=["observed Docker image required"],
                replan_context={"action": "create_docker_container"},
            )

    plan = PrivilegedPlan(
        plan_id="priv-ops-deploy",
        goal="create a platform",
        risk="high",
        steps=[PrivilegedStep(
            step_id="change-2",
            title="Create isolated dependencies",
            objective="Create MySQL, Redis and RabbitMQ containers",
            expected_changes=["three new containers"],
            success_criteria=["containers are running"],
            risk="high",
        )],
    )

    with pytest.raises(ExecutionBindingError) as raised:
        FailingAgent(None).prepare_plan(plan, grounded_context=None)

    assert raised.value.replan_context == {
        "action": "create_docker_container",
        "step_id": "change-2",
    }
    assert raised.value.failed_criteria == ["observed Docker image required"]


def test_binding_selection_schema_uses_absence_instead_of_empty_enum_values():
    from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent

    tool = PrivilegedExecutionAgent(None)._selection_function_tool()
    parameters = tool["function"]["parameters"]
    properties = parameters["properties"]

    assert "" not in properties["action"]["enum"]
    assert "" not in properties["shell_blocker_category"]["enum"]
    assert "action" not in parameters["required"]
    assert "shell_blocker_category" not in parameters["required"]
