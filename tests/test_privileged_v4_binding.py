from __future__ import annotations


def test_python_class_attribute_postcondition_is_canonicalized_from_action_args():
    from klonet_agent.ops.privileged.execution_agent import (
        _canonical_action_postconditions,
    )

    checks = _canonical_action_postconditions(
        "set_python_class_attribute",
        {
            "path": "/srv/v4/vemu_config/config.py",
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
                    "cwd": "/srv/v4",
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
                "cwd": "/srv/v4",
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

import pytest


def _plan():
    from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, ChangeStepV4

    return ChangePlanV4(
        plan_id="priv-v4-bind",
        goal="deploy isolated instance",
        risk="high",
        steps=[
            ChangeStepV4(
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


def test_v4_binder_maps_direct_registered_action_back_to_change_plan():
    from klonet_agent.ops.privileged.contracts import ExecutionBinding
    from klonet_agent.ops.privileged.v4.binding import V4ChangeBinder

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

    bound = V4ChangeBinder(legacy).bind(plan)

    assert legacy.received.schema_version == 3
    assert bound is plan
    assert bound.steps[0].execution_binding.kind == "registered_action"
    assert bound.steps[0].implementation_plan is None
    assert bound.status == "awaiting_confirmation"


def test_v4_binder_preserves_action_shell_hierarchical_implementation():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
        ShellArtifact,
    )
    from klonet_agent.ops.privileged.v4.binding import V4ChangeBinder

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

    plan = V4ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())

    implementation = plan.steps[0].implementation_plan
    assert implementation is not None
    assert [item.execution_binding.kind for item in implementation.steps] == [
        "registered_action",
        "shell_artifact",
    ]
    restored = type(plan).from_dict(plan.to_dict())
    assert restored.steps[0].implementation_plan.steps[1].step_id == "deploy-2"


def test_v4_binder_rejects_direct_verification_only_as_a_change_implementation():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.v4.binding import V4BindingError, V4ChangeBinder

    def apply(plan):
        binding = ExecutionBinding(
            kind="verification_only",
            risk="readonly",
            postconditions=[{"checker": "exit_code_zero"}],
        )
        plan.steps[0].execution_binding = binding

    with pytest.raises(V4BindingError, match="verification_only"):
        V4ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())


def test_v4_binder_lifts_hierarchical_verification_out_of_execution_plan():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.v4.binding import V4ChangeBinder

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

    plan = V4ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())

    assert [
        item.step_id for item in plan.steps[0].implementation_plan.steps
    ] == ["deploy-action"]
    assert {item["checker"] for item in plan.steps[0].postconditions} == {
        "exit_code_zero",
        "service_active",
    }


def test_v4_binder_rewires_precondition_verification_without_lifting_it():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.v4.binding import V4ChangeBinder

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

    plan = V4ChangeBinder(FakeLegacyBinder(apply)).bind(_plan())

    implementation = plan.steps[0].implementation_plan
    assert [item.step_id for item in implementation.steps] == ["create"]
    assert implementation.steps[0].depends_on == []
    assert not any(
        check["checker"] == "container_absent"
        for check in plan.steps[0].postconditions
    )


def test_v4_binder_translates_shared_binding_failure_to_v4_boundary():
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.v4.binding import V4BindingError, V4ChangeBinder

    class FailingSharedBinder:
        def prepare_plan(self, plan, *, grounded_context):
            raise ExecutionBindingError("clone target could not be grounded")

    with pytest.raises(V4BindingError, match="clone target could not be grounded"):
        V4ChangeBinder(FailingSharedBinder()).bind(_plan())
