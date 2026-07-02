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
    assert "operation_args" in properties
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
    assert set(_status_by_step(loaded).values()) == {"pending"}


def test_restart_operation_plan_has_component_restart_steps():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
        )

    assert [step.step_id for step in plan.steps] == [
        "precheck-runtime",
        "restart-master",
        "restart-worker",
        "restart-celery",
        "restart-web-terminal",
        "verify-health",
    ]
    assert {
        step.step_id
        for step in plan.steps
        if step.requires_step_confirmation
    } == {
        "restart-master",
        "restart-worker",
        "restart-celery",
        "restart-web-terminal",
    }


def test_destroy_operation_plan_default_binds_stop_platform_recipe_without_execution():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="destroy_platform",
            target="102",
            objective="destroy platform 102",
        )
        loaded = store.load_plan(plan.plan_id)
        rendered = render_plan(loaded)

    step = next(item for item in loaded.steps if item.step_id == "stop-services")
    assert step.status == "pending"
    assert step.recipe_id == "stop_platform_screens"
    assert step.recipe_args == {"platform": "102"}
    assert "stop-services" in rendered
    assert "recipe=stop_platform_screens" in rendered
    assert "recipe_args.platform=102" in rendered


def test_deploy_operation_plan_default_binds_start_platform_recipe_when_project_root_is_explicit():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="deploy platform 103",
            operation_args={
                "project_root": "/home/adminis/lht/103_project/vemu_uestc",
            },
        )
        loaded = store.load_plan(plan.plan_id)
        rendered = render_plan(loaded)

    step = next(item for item in loaded.steps if item.step_id == "start-services")
    assert step.status == "pending"
    assert step.recipe_id == "start_platform_screens"
    assert step.recipe_args == {
        "platform": "103",
        "project_root": "/home/adminis/lht/103_project/vemu_uestc",
    }
    assert "recipe=start_platform_screens" in rendered
    assert "recipe_args.platform=103" in rendered
    assert "recipe_args.project_root=/home/adminis/lht/103_project/vemu_uestc" in rendered


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
    assert "total_steps=6" in rendered
    assert "pending=6" in rendered
    assert "next_step=precheck-runtime" in rendered
    assert (
        "execution_order=precheck-runtime -> restart-master -> restart-worker -> "
        "restart-celery -> restart-web-terminal -> verify-health"
    ) in rendered


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
    assert set(_status_by_step(loaded_after_execute).values()) == {"pending"}


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
    statuses = _status_by_step(loaded_after_execute)
    assert statuses["restart-master"] == "blocked"
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


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
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "completed"
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


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
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "blocked"
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


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
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "failed"
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


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
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "completed"
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


def test_start_platform_screens_recipe_dry_run_generates_safe_preview():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="start platform 103 screens",
            recipe_bindings={
                "start-services": {
                    "recipe_id": "start_platform_screens",
                    "args": {
                        "platform": "103",
                        "project_root": "/home/adminis/lht/103_project/vemu_uestc",
                    },
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "start-services")
        step.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "start-services")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=start_platform_screens" in result
    assert "command_preview=/usr/local/bin/klonet-agent-op start-platform-screens" in result
    assert "--platform 103" in result
    assert "--project-root /home/adminis/lht/103_project/vemu_uestc" in result
    statuses = _status_by_step(loaded)
    assert statuses["start-services"] == "completed"
    assert statuses["precheck"] == "pending"
    assert statuses["prepare-files"] == "pending"


def test_stop_screen_component_recipe_dry_run_generates_safe_preview():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="destroy_platform",
            target="102",
            objective="stop platform 102 master",
            recipe_bindings={
                "stop-services": {
                    "recipe_id": "stop_screen_component",
                    "args": {
                        "platform": "102",
                        "component": "master",
                        "screen_session": "102_m",
                    },
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "stop-services")
        step.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "stop-services")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=stop_screen_component" in result
    assert "command_preview=/usr/local/bin/klonet-agent-op stop-screen-component" in result
    assert "--platform 102" in result
    assert "--component master" in result
    assert "--screen 102_m" in result
    statuses = _status_by_step(loaded)
    assert statuses["stop-services"] == "completed"
    assert statuses["identify-owned-resources"] == "pending"
    assert statuses["cleanup-owned-resources"] == "pending"


def test_stop_platform_screens_recipe_dry_run_generates_safe_preview():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="destroy_platform",
            target="102",
            objective="stop all 102 platform screens",
            recipe_bindings={
                "stop-services": {
                    "recipe_id": "stop_platform_screens",
                    "args": {
                        "platform": "102",
                    },
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "stop-services")
        step.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "stop-services")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=stop_platform_screens" in result
    assert "command_preview=/usr/local/bin/klonet-agent-op stop-platform-screens" in result
    assert "--platform 102" in result
    statuses = _status_by_step(loaded)
    assert statuses["stop-services"] == "completed"
    assert statuses["identify-owned-resources"] == "pending"
    assert statuses["cleanup-owned-resources"] == "pending"


def test_restart_screen_component_recipe_execute_calls_fixed_helper_command():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    calls = []

    def command_runner(command):
        calls.append(command)
        return "helper stdout ok"

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(
                dry_run=False,
                command_runner=command_runner,
            ),
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

    assert calls == [
        [
            "/usr/local/bin/klonet-agent-op",
            "restart-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
            "--project-root",
            "/home/adminis/lht/102_project",
        ]
    ]
    assert "result_status=completed" in result
    assert "dry_run=false" in result
    assert "helper stdout ok" in result
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "completed"
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


def test_restart_screen_component_recipe_execute_reports_helper_failure():
    import subprocess

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    def command_runner(command):
        raise subprocess.CalledProcessError(7, command, output="bad", stderr="screen failed")

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(
                dry_run=False,
                command_runner=command_runner,
            ),
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

    assert "result_status=failed" in result
    assert "helper_failed returncode=7" in result
    assert "screen failed" in result
    assert loaded.status == "failed"


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
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "blocked"
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


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


def test_executor_create_deploy_plan_passes_operation_args_to_default_recipe():
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u1", project_id="p1", mode="ops")
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        executor = ToolExecutor(
            session=session,
            allowed_tools={"create_ops_operation_plan"},
            memory_store=store,
        )
        result = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "deploy_platform",
                "target": "103",
                "objective": "deploy platform 103",
                "operation_args": {
                    "project_root": "/home/adminis/lht/103_project/vemu_uestc",
                },
            },
        )

    assert "recipe=start_platform_screens" in result
    assert "recipe_args.platform=103" in result
    assert "recipe_args.project_root=/home/adminis/lht/103_project/vemu_uestc" in result


def test_executor_operation_plan_store_defaults_to_dry_run_recipe_runner():
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u1", project_id="p1", mode="ops")
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        executor = ToolExecutor(session=session, memory_store=store)
        operation_store = executor._operation_plan_store()

    assert operation_store.recipe_runner.dry_run is True


def test_executor_operation_plan_store_can_enable_real_execution_by_env(monkeypatch):
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    monkeypatch.setenv("KLONET_AGENT_OPS_REAL_EXECUTION", "1")
    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u1", project_id="p1", mode="ops")
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        executor = ToolExecutor(session=session, memory_store=store)
        operation_store = executor._operation_plan_store()

    assert operation_store.recipe_runner.dry_run is False


def _extract_plan_id(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("plan_id="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"plan_id not found in:\n{text}")


def _status_by_step(plan):
    return {step.step_id: step.status for step in plan.steps}
