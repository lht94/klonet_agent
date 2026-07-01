"""Controlled Ops operation planning tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_ops_operation_tools_are_registered_and_profile_allowed():
    from klonet_agent.agents import get_profile
    from klonet_agent.tools.registry import TOOLS

    tool_names = {item["function"]["name"] for item in TOOLS}
    profile = get_profile("ops")

    assert "create_ops_operation_plan" in tool_names
    assert "approve_ops_operation_plan" in tool_names
    assert "execute_ops_operation_step" in tool_names
    assert "create_ops_operation_plan" in profile.allowed_tools
    assert "approve_ops_operation_plan" in profile.allowed_tools
    assert "execute_ops_operation_step" in profile.allowed_tools
    assert "run_command" not in profile.allowed_tools


def test_create_operation_plan_schema_exposes_recipe_bindings_without_execution():
    from klonet_agent.tools.registry import TOOLS

    tool = next(
        item
        for item in TOOLS
        if item["function"]["name"] == "create_ops_operation_plan"
    )
    properties = tool["function"]["parameters"]["properties"]
    description = tool["function"]["description"]

    assert "recipe_bindings" in properties
    assert "只保存" in description
    assert "确认后才能执行" in description


def test_mentor_profile_cannot_create_or_approve_operation_plans():
    from klonet_agent.agents import get_profile

    profile = get_profile("mentor")

    assert "create_ops_operation_plan" not in profile.allowed_tools
    assert "approve_ops_operation_plan" not in profile.allowed_tools
    assert "execute_ops_operation_step" not in profile.allowed_tools


def test_create_operation_plan_persists_but_does_not_execute():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        result = store.create_plan(
            operation="restart_platform",
            target="102",
            evidence=["screen 102_m exists", "ports detected"],
            objective="restart platform 102",
        )
        loaded = store.load_plan(result.plan_id)

    assert result.status == "pending"
    assert loaded.plan_id == result.plan_id
    assert loaded.operation == "restart_platform"
    assert loaded.target == "102"
    assert loaded.status == "pending"
    assert [step.status for step in loaded.steps] == ["pending", "pending", "pending"]


def test_render_plan_includes_execution_state_and_next_step():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
        )
        rendered = render_plan(plan)

    assert "execution_state:" in rendered
    assert "total_steps=3" in rendered
    assert "pending=3" in rendered
    assert "next_step=precheck-runtime" in rendered
    assert "execution_order=precheck-runtime -> restart-master -> verify-health" in rendered


def test_executor_refuses_plan_approval_without_user_confirm_text():
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u1", project_id="p1", mode="ops")
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        executor = ToolExecutor(
            session=session,
            allowed_tools={
                "create_ops_operation_plan",
                "approve_ops_operation_plan",
            },
            memory_store=store,
        )
        created = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
            },
        )
        plan_id = _extract_plan_id(created)

        executor.set_user_authorization_context("请你直接确认")
        denied = executor.run(
            "approve_ops_operation_plan",
            {"plan_id": plan_id, "scope": "plan"},
        )

    assert denied.startswith("Error:")
    assert "用户原文必须是 confirm" in denied


def test_executor_accepts_exact_user_confirm_text_and_requires_step_confirmation():
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u1", project_id="p1", mode="ops")
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        executor = ToolExecutor(
            session=session,
            allowed_tools={
                "create_ops_operation_plan",
                "approve_ops_operation_plan",
                "execute_ops_operation_step",
            },
            memory_store=store,
        )
        created = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
            },
        )
        plan_id = _extract_plan_id(created)

        executor.set_user_authorization_context(f"confirm {plan_id}")
        approved = executor.run(
            "approve_ops_operation_plan",
            {"plan_id": plan_id, "scope": "plan"},
        )
        blocked = executor.run(
            "execute_ops_operation_step",
            {"plan_id": plan_id, "step_id": "restart-master"},
        )
        loaded_after_execute = executor._operation_plan_store().load_plan(plan_id)

    assert "status=approved" in approved
    assert blocked.startswith("Error:")
    assert "step requires explicit confirm-step" in blocked
    assert [step.status for step in loaded_after_execute.steps] == [
        "pending",
        "pending",
        "pending",
    ]


def test_executor_marks_confirmed_step_blocked_when_recipe_is_missing():
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u1", project_id="p1", mode="ops")
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        executor = ToolExecutor(
            session=session,
            allowed_tools={
                "create_ops_operation_plan",
                "approve_ops_operation_plan",
                "execute_ops_operation_step",
            },
            memory_store=store,
        )
        created = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
            },
        )
        plan_id = _extract_plan_id(created)
        executor.set_user_authorization_context(f"confirm {plan_id}")
        executor.run("approve_ops_operation_plan", {"plan_id": plan_id, "scope": "plan"})
        executor.set_user_authorization_context(f"confirm-step {plan_id} restart-master")
        executor.run(
            "approve_ops_operation_plan",
            {"plan_id": plan_id, "scope": "step", "step_id": "restart-master"},
        )

        blocked = executor.run(
            "execute_ops_operation_step",
            {"plan_id": plan_id, "step_id": "restart-master"},
        )
        loaded_after_execute = executor._operation_plan_store().load_plan(plan_id)

    assert "ops_operation_execution" in blocked
    assert f"plan_id={plan_id}" in blocked
    assert "execute_step=restart-master" in blocked
    assert "result_status=blocked" in blocked
    assert "execution_result=no_recipe_attached; environment unchanged" in blocked
    assert [step.status for step in loaded_after_execute.steps] == [
        "pending",
        "blocked",
        "pending",
    ]


def test_store_executes_confirmed_allowlisted_recipe_and_persists_result():
    from klonet_agent.ops.operations import (
        OperationPlanStore,
        RecipeExecutionResult,
        render_plan,
    )
    from tests.helpers import local_temp_dir

    calls = []

    def recipe_runner(plan, step):
        calls.append((plan.plan_id, step.step_id, step.recipe_id))
        return RecipeExecutionResult("completed", "recipe_id=test-restart executed=true")

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir, recipe_runner=recipe_runner)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "restart-master")
        step.status = "approved"
        step.recipe_id = "test-restart"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)
        rendered = render_plan(loaded)

    assert calls == [(plan.plan_id, "restart-master", "test-restart")]
    assert "ops_operation_execution" in result
    assert "result_status=completed" in result
    assert "execution_result=recipe_id=test-restart executed=true" in result
    assert "recipe=test-restart" in rendered
    assert [step.status for step in loaded.steps] == [
        "pending",
        "completed",
        "pending",
    ]


def test_store_blocks_bound_recipe_when_runner_is_not_configured():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "restart-master")
        step.status = "approved"
        step.recipe_id = "test-restart"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=blocked" in result
    assert "execution_result=recipe_runner_unavailable; environment unchanged" in result
    assert [step.status for step in loaded.steps] == [
        "pending",
        "blocked",
        "pending",
    ]


def test_store_marks_recipe_exception_as_failed_and_fails_plan():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    def recipe_runner(plan, step):
        raise RuntimeError("boom")

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir, recipe_runner=recipe_runner)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "restart-master")
        step.status = "approved"
        step.recipe_id = "test-restart"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=failed" in result
    assert "execution_result=recipe_exception=boom" in result
    assert loaded.status == "failed"
    assert [step.status for step in loaded.steps] == [
        "pending",
        "failed",
        "pending",
    ]


def test_restart_screen_component_recipe_dry_run_generates_safe_preview():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102 master",
            recipe_bindings={
                "restart-master": {
                    "recipe_id": "restart_screen_component",
                    "args": {
                        "platform": "102",
                        "component": "master",
                        "screen_session": "102_m",
                        "project_root": "/home/adminis/lht/102_project",
                    },
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "restart-master")
        step.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=restart_screen_component" in result
    assert "command_preview=/usr/local/bin/klonet-agent-op restart-screen-component" in result
    assert "--platform 102" in result
    assert "--component master" in result
    assert "--screen 102_m" in result
    assert "--project-root /home/adminis/lht/102_project" in result
    assert [step.status for step in loaded.steps] == [
        "pending",
        "completed",
        "pending",
    ]


def test_restart_screen_component_recipe_blocks_unknown_component():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102 database",
            recipe_bindings={
                "restart-master": {
                    "recipe_id": "restart_screen_component",
                    "args": {
                        "platform": "102",
                        "component": "database",
                        "screen_session": "102_db",
                        "project_root": "/home/adminis/lht/102_project",
                    },
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "restart-master")
        step.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=blocked" in result
    assert "unsupported_component=database" in result
    assert [step.status for step in loaded.steps] == [
        "pending",
        "blocked",
        "pending",
    ]


def test_executor_create_plan_can_bind_restart_screen_recipe_for_dry_run():
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u1", project_id="p1", mode="ops")
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        executor = ToolExecutor(
            session=session,
            allowed_tools={
                "create_ops_operation_plan",
                "approve_ops_operation_plan",
                "execute_ops_operation_step",
            },
            memory_store=store,
        )
        created = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102 master",
                "recipe_bindings": {
                    "restart-master": {
                        "recipe_id": "restart_screen_component",
                        "args": {
                            "platform": "102",
                            "component": "master",
                            "screen_session": "102_m",
                            "project_root": "/home/adminis/lht/102_project",
                        },
                    }
                },
            },
        )
        plan_id = _extract_plan_id(created)
        executor.set_user_authorization_context(f"confirm {plan_id}")
        executor.run("approve_ops_operation_plan", {"plan_id": plan_id, "scope": "plan"})
        executor.set_user_authorization_context(f"confirm-step {plan_id} restart-master")
        executor.run(
            "approve_ops_operation_plan",
            {"plan_id": plan_id, "scope": "step", "step_id": "restart-master"},
        )
        result = executor.run(
            "execute_ops_operation_step",
            {"plan_id": plan_id, "step_id": "restart-master"},
        )

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=restart_screen_component" in result
    assert "command_preview=/usr/local/bin/klonet-agent-op restart-screen-component" in result


def _extract_plan_id(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("plan_id="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"plan_id not found in:\n{text}")
