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
    assert "list_ops_operation_plans" in tool_names
    assert "describe_ops_operation_plan" in tool_names
    assert "approve_ops_operation_plan" in tool_names
    assert "execute_ops_operation_step" in tool_names
    assert "execute_ops_next_step" in tool_names
    assert "resolve_ops_blocked_step" in tool_names
    assert "create_ops_operation_plan" in profile.allowed_tools
    assert "list_ops_operation_plans" in profile.allowed_tools
    assert "describe_ops_operation_plan" in profile.allowed_tools
    assert "approve_ops_operation_plan" in profile.allowed_tools
    assert "execute_ops_operation_step" in profile.allowed_tools
    assert "execute_ops_next_step" in profile.allowed_tools
    assert "resolve_ops_blocked_step" in profile.allowed_tools
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
    assert "list_ops_operation_plans" not in profile.allowed_tools
    assert "describe_ops_operation_plan" not in profile.allowed_tools
    assert "approve_ops_operation_plan" not in profile.allowed_tools
    assert "execute_ops_operation_step" not in profile.allowed_tools
    assert "resolve_ops_blocked_step" not in profile.allowed_tools


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


def test_restart_operation_plan_default_binds_component_recipes_when_project_root_is_explicit():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
            operation_args={
                "project_root": "/home/adminis/lht/102_project",
            },
        )
        loaded = store.load_plan(plan.plan_id)
        rendered = render_plan(loaded)

    expected = {
        "restart-master": ("master", "102_m"),
        "restart-worker": ("worker", "102_w"),
        "restart-celery": ("celery", "102_c"),
        "restart-web-terminal": ("web_terminal", "102_web"),
    }
    for step_id, (component, screen_session) in expected.items():
        step = next(item for item in loaded.steps if item.step_id == step_id)
        assert step.status == "pending"
        assert step.recipe_id == "restart_screen_component"
        assert step.recipe_args == {
            "platform": "102",
            "component": component,
            "screen_session": screen_session,
            "project_root": "/home/adminis/lht/102_project",
        }
    assert "recipe=restart_screen_component" in rendered
    assert "recipe_args.project_root=/home/adminis/lht/102_project" in rendered
    assert "recipe_args.screen_session=102_m" in rendered
    assert "recipe_args.screen_session=102_web" in rendered


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


def test_deploy_operation_plan_default_binds_prepare_project_files_recipe():
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

    step = next(item for item in loaded.steps if item.step_id == "prepare-files")
    assert step.status == "pending"
    assert step.requires_step_confirmation is False
    assert step.recipe_id == "prepare_project_files"
    assert step.recipe_args == {
        "project_root": "/home/adminis/lht/103_project/vemu_uestc",
    }
    assert "prepare-files" in rendered
    assert "recipe=prepare_project_files" in rendered
    assert "recipe_args.project_root=/home/adminis/lht/103_project/vemu_uestc" in rendered


def test_deploy_operation_plan_non_destructive_steps_do_not_require_step_confirmation():
    from klonet_agent.ops.operations import OperationPlanStore
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

    steps = {step.step_id: step for step in plan.steps}
    assert steps["precheck"].requires_step_confirmation is False
    assert steps["prepare-files"].requires_step_confirmation is False
    assert steps["start-services"].requires_step_confirmation is False


def test_recipe_bindings_accept_recipe_args_and_clear_default_args():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="write startup file",
            operation_args={
                "project_root": "/home/adminis/lht/103_project/vemu_uestc",
            },
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "write_ops_file",
                    "recipe_args": {
                        "path": "/home/klonet-agent/vemu_uestc/mains/web_terminal_main.py",
                        "content": "# patched\n",
                    },
                }
            },
        )

    step = next(item for item in plan.steps if item.step_id == "prepare-files")
    assert step.recipe_id == "write_ops_file"
    assert step.recipe_args == {
        "path": "/home/klonet-agent/vemu_uestc/mains/web_terminal_main.py",
        "content": "# patched\n",
    }


def test_recipe_binding_without_args_preserves_same_default_recipe_args():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="prepare project files",
            operation_args={
                "project_root": "/home/adminis/lht/103_project/vemu_uestc",
            },
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "prepare_project_files",
                }
            },
        )

    step = next(item for item in plan.steps if item.step_id == "prepare-files")
    assert step.recipe_id == "prepare_project_files"
    assert step.recipe_args == {
        "project_root": "/home/adminis/lht/103_project/vemu_uestc",
    }


def test_confirmed_deploy_plan_executes_prepare_without_confirm_step():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    required_files = [
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ]
    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project" / "vemu_uestc"
        mains = project_root / "mains"
        mains.mkdir(parents=True)
        for filename in required_files:
            (mains / filename).write_text(f"# {filename}\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="deploy platform 103",
            operation_args={"project_root": str(project_root)},
        )
        store.approve_plan(plan.plan_id)

        precheck = store.execute_next_step(plan.plan_id)
        prepare = store.execute_next_step(plan.plan_id)

    assert "execute_step=precheck" in precheck
    assert "result_status=completed" in precheck
    assert "execute_step=prepare-files" in prepare
    assert "step requires explicit confirm-step" not in prepare
    assert "result_status=completed" in prepare
    assert "recipe_id=prepare_project_files" in prepare


def test_deploy_plan_execute_until_blocked_runs_all_controlled_steps():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    required_files = [
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ]
    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project" / "vemu_uestc"
        mains = project_root / "mains"
        mains.mkdir(parents=True)
        for filename in required_files:
            (mains / filename).write_text(f"# {filename}\n", encoding="utf-8")
            (project_root / filename).write_text(f"# root {filename}\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="deploy platform 103",
            operation_args={"project_root": str(project_root)},
        )
        store.approve_plan(plan.plan_id)

        result = store.execute_until_blocked(plan.plan_id)
        loaded = store.load_plan(plan.plan_id)

    assert "execute_step=precheck" in result
    assert "execute_step=prepare-files" in result
    assert "execute_step=start-services" in result
    assert "step requires explicit confirm-step" not in result
    assert loaded.status == "completed"
    assert set(_status_by_step(loaded).values()) == {"completed"}


def test_deploy_operation_plan_default_binds_extract_archive_recipe_for_prepare_files():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="env-setup",
            objective="extract Klonet install bundle",
            operation_args={
                "archive_path": "/home/adminis/vemu_install_2024_12_5.tar",
                "destination_dir": "/root",
            },
        )
        loaded = store.load_plan(plan.plan_id)
        rendered = render_plan(loaded)

    step = next(item for item in loaded.steps if item.step_id == "prepare-files")
    assert step.recipe_id == "extract_archive"
    assert step.recipe_args == {
        "archive_path": "/home/adminis/vemu_install_2024_12_5.tar",
        "destination_dir": "/root",
    }
    assert "recipe=extract_archive" in rendered
    assert "recipe_args.archive_path=/home/adminis/vemu_install_2024_12_5.tar" in rendered
    assert "recipe_args.destination_dir=/root" in rendered


def test_deploy_operation_plan_default_binds_run_install_script_recipe_for_prepare_files():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="env-setup",
            objective="run base requirements setup",
            operation_args={
                "script_dir": "/root/vemu_install_new_gen",
                "script_name": "base_requ_setup.sh",
                "script_args": "NORMAL",
            },
        )
        loaded = store.load_plan(plan.plan_id)
        rendered = render_plan(loaded)

    step = next(item for item in loaded.steps if item.step_id == "prepare-files")
    assert step.recipe_id == "run_install_script"
    assert step.recipe_args == {
        "script_dir": "/root/vemu_install_new_gen",
        "script_name": "base_requ_setup.sh",
        "script_args": "NORMAL",
    }
    assert "recipe=run_install_script" in rendered
    assert "recipe_args.script_dir=/root/vemu_install_new_gen" in rendered
    assert "recipe_args.script_name=base_requ_setup.sh" in rendered
    assert "recipe_args.script_args=NORMAL" in rendered


def test_manual_checkpoint_recipe_completes_confirmed_step_without_environment_change():
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
            objective="deploy platform 103",
            operation_args={
                "project_root": "/home/adminis/lht/103_project/vemu_uestc",
            },
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "manual_checkpoint",
                    "args": {
                        "reason": "project files and config prepared externally",
                        "project_root": "/home/adminis/lht/103_project/vemu_uestc",
                    },
                }
            },
        )
        plan.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        step = next(item for item in plan.steps if item.step_id == "prepare-files")
        step.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")
        loaded = store.load_plan(plan.plan_id)

    assert "execute_step=prepare-files" in result
    assert "result_status=completed" in result
    assert "recipe_id=manual_checkpoint" in result
    assert "environment unchanged" in result
    assert _status_by_step(loaded)["prepare-files"] == "completed"


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
    assert "execute_step=precheck-runtime" in approved
    assert "result_status=waiting_for_confirmation" in approved
    assert blocked.startswith("Error:")
    assert "step requires explicit confirm-step" in blocked
    statuses = _status_by_step(loaded_after_execute)
    assert statuses["precheck-runtime"] == "completed"
    assert statuses["restart-master"] == "pending"
    assert statuses["restart-worker"] == "pending"
    assert statuses["restart-celery"] == "pending"
    assert statuses["restart-web-terminal"] == "pending"
    assert statuses["verify-health"] == "pending"


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
        executor.run(
            "execute_ops_operation_step",
            {"plan_id": plan_id, "step_id": "precheck-runtime"},
        )
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
    assert statuses["precheck-runtime"] == "completed"
    assert statuses["restart-worker"] == "pending"
    assert statuses["verify-health"] == "pending"


def test_store_completes_normal_unbound_step_as_readonly_checkpoint():
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
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "precheck-runtime")
        loaded = store.load_plan(plan.plan_id)

    assert "execute_step=precheck-runtime" in result
    assert "result_status=completed" in result
    assert "execution_result=readonly_or_manual_checkpoint_completed; environment unchanged" in result
    assert _status_by_step(loaded)["precheck-runtime"] == "completed"


def test_store_execute_next_step_uses_current_execution_order():
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
        store.save_plan(plan)

        result = store.execute_next_step(plan.plan_id)
        loaded = store.load_plan(plan.plan_id)

    assert "execute_step=precheck-runtime" in result
    assert "result_status=completed" in result
    assert _status_by_step(loaded)["precheck-runtime"] == "completed"


def test_deploy_precheck_without_recipe_blocks_instead_of_fake_completion():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="deploy platform 103",
        )
        plan.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "precheck")
        loaded = store.load_plan(plan.plan_id)

    assert "execute_step=precheck" in result
    assert "result_status=blocked" in result
    assert "execution_result=deploy_precheck_requires_project_root_or_recipe; environment unchanged" in result
    assert "next_required_action=provide operation_args.project_root or bind a readonly precheck recipe" in result
    statuses = _status_by_step(loaded)
    assert statuses["precheck"] == "blocked"
    assert statuses["prepare-files"] == "pending"


def test_store_execute_next_step_reports_required_confirm_step_command():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
            operation_args={"project_root": "/home/adminis/lht/102_project"},
        )
        plan.status = "approved"
        store.save_plan(plan)

        store.execute_next_step(plan.plan_id)
        result = store.execute_next_step(plan.plan_id)

    assert result.startswith("Error:")
    assert "step requires explicit confirm-step" in result
    assert f"confirm-step {plan.plan_id} restart-master" in result
    assert "next_required_action=confirm-step" in result


def test_store_next_step_stops_at_blocked_step_until_resolved():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
            operation_args={"project_root": "/home/adminis/lht/102_project"},
        )
        plan.status = "approved"
        _complete_steps_before(plan, "restart-master")
        blocked_step = next(item for item in plan.steps if item.step_id == "restart-master")
        blocked_step.status = "blocked"
        store.save_plan(plan)

        rendered = render_plan(plan)
        result = store.execute_next_step(plan.plan_id)
        loaded = store.load_plan(plan.plan_id)

    assert "next_step=restart-master" in rendered
    assert "result_status=blocked" in result
    assert "execute_step=restart-master" in result
    assert "execution_result=step is blocked; resolve required action before continuing" in result
    assert _status_by_step(loaded)["restart-master"] == "blocked"


def test_store_does_not_approve_blocked_step_without_resolution_evidence():
    import pytest

    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
            operation_args={"project_root": "/home/adminis/lht/102_project"},
        )
        plan.status = "approved"
        _complete_steps_before(plan, "restart-master")
        blocked_step = next(item for item in plan.steps if item.step_id == "restart-master")
        blocked_step.status = "blocked"
        store.save_plan(plan)

        with pytest.raises(ValueError, match="resolve_ops_blocked_step"):
            store.approve_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert _status_by_step(loaded)["restart-master"] == "blocked"


def test_store_does_not_approve_running_step_without_reinspection():
    import pytest

    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
            operation_args={"project_root": "/home/adminis/lht/102_project"},
        )
        plan.status = "approved"
        _complete_steps_before(plan, "restart-master")
        running_step = next(item for item in plan.steps if item.step_id == "restart-master")
        running_step.status = "running"
        store.save_plan(plan)

        with pytest.raises(ValueError, match="running step"):
            store.approve_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert _status_by_step(loaded)["restart-master"] == "running"


def test_store_marks_interrupted_running_step_blocked_on_execute():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
            operation_args={"project_root": "/home/adminis/lht/102_project"},
        )
        plan.status = "approved"
        _complete_steps_before(plan, "restart-master")
        running_step = next(item for item in plan.steps if item.step_id == "restart-master")
        running_step.status = "running"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "previous_step_status=running" in result
    assert "result_status=blocked" in result
    assert "previous execution left this step running" in result
    assert "next_required_action=inspect_runtime" in result
    assert _status_by_step(loaded)["restart-master"] == "blocked"


def test_store_resolves_blocked_step_after_runtime_reinspection():
    from klonet_agent.ops.operations import OperationPlanStore, render_plan
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="restart_platform",
            target="102",
            objective="restart platform 102",
            operation_args={"project_root": "/home/adminis/lht/102_project"},
        )
        plan.status = "approved"
        _complete_steps_before(plan, "restart-master")
        blocked_step = next(item for item in plan.steps if item.step_id == "restart-master")
        blocked_step.status = "blocked"
        store.save_plan(plan)

        resolved = store.resolve_blocked_step(
            plan.plan_id,
            "restart-master",
            "inspect_runtime confirmed 102_m screen absent and port 5000 free",
        )
        rendered = render_plan(resolved)
        loaded = store.load_plan(plan.plan_id)

    statuses = _status_by_step(loaded)
    assert loaded.status == "approved"
    assert statuses["restart-master"] == "pending"
    assert "next_step=restart-master" in rendered
    assert "inspect_runtime confirmed 102_m screen absent and port 5000 free" in loaded.evidence


def test_store_blocks_step_execution_when_previous_step_is_incomplete():
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
            objective="restart platform 102",
            operation_args={"project_root": "/home/adminis/lht/102_project"},
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "restart-master")
        step.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert result.startswith("Error:")
    assert "previous step must be completed first: precheck-runtime" in result
    statuses = _status_by_step(loaded)
    assert statuses["precheck-runtime"] == "pending"
    assert statuses["restart-master"] == "approved"


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
        _complete_steps_before(plan, "restart-master")
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
    assert statuses["precheck-runtime"] == "completed"
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
        _complete_steps_before(plan, "restart-master")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=blocked" in result
    assert "execution_result=recipe_runner_unavailable; environment unchanged" in result
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "blocked"
    assert statuses["precheck-runtime"] == "completed"
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
        _complete_steps_before(plan, "restart-master")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=failed" in result
    assert "execution_result=recipe_exception=boom" in result
    assert loaded.status == "failed"
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "failed"
    assert statuses["precheck-runtime"] == "completed"
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
        _complete_steps_before(plan, "restart-master")
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
    assert statuses["precheck-runtime"] == "completed"
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
        _complete_steps_before(plan, "start-services")
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
    assert statuses["precheck"] == "completed"
    assert statuses["prepare-files"] == "completed"


def test_deploy_precheck_validates_required_project_entry_files():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    required_files = [
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ]
    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project" / "vemu_uestc"
        project_root.mkdir(parents=True)
        for filename in required_files:
            (project_root / filename).write_text("# entry\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="precheck deploy 103",
            operation_args={"project_root": str(project_root)},
        )
        plan.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "precheck")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=completed" in result
    assert "recipe_id=validate_project_files" in result
    assert "found_files=gun.py,master_main.py,celery_worker.py,web_terminal_main.py,worker_gun.py,worker_main.py" in result
    assert "environment unchanged" in result
    statuses = _status_by_step(loaded)
    assert statuses["precheck"] == "completed"
    assert statuses["prepare-files"] == "pending"


def test_deploy_precheck_blocks_when_required_project_entry_files_are_missing():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project" / "vemu_uestc"
        project_root.mkdir(parents=True)
        (project_root / "gun.py").write_text("# entry\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="precheck deploy 103",
            operation_args={"project_root": str(project_root)},
        )
        plan.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "precheck")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=blocked" in result
    assert "recipe_id=validate_project_files" in result
    assert "missing_files=master_main.py,celery_worker.py,web_terminal_main.py,worker_gun.py,worker_main.py" in result
    assert "environment unchanged" in result
    statuses = _status_by_step(loaded)
    assert statuses["precheck"] == "blocked"
    assert statuses["prepare-files"] == "pending"


def test_deploy_precheck_accepts_entry_files_in_mains_before_prepare_step():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    required_files = [
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ]
    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project" / "vemu_uestc"
        mains = project_root / "mains"
        mains.mkdir(parents=True)
        for filename in required_files:
            (mains / filename).write_text("# entry\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="precheck deploy 103",
            operation_args={"project_root": str(project_root)},
        )
        plan.status = "approved"
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "precheck")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=completed" in result
    assert "recipe_id=validate_project_files" in result
    assert "found_files=mains/gun.py,mains/master_main.py,mains/celery_worker.py,mains/web_terminal_main.py,mains/worker_gun.py,mains/worker_main.py" in result
    statuses = _status_by_step(loaded)
    assert statuses["precheck"] == "completed"
    assert statuses["prepare-files"] == "pending"


def test_prepare_project_files_recipe_dry_run_previews_mains_copy_without_writing():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project" / "vemu_uestc"
        mains = project_root / "mains"
        mains.mkdir(parents=True)
        (mains / "gun.py").write_text("# gun\n", encoding="utf-8")
        (mains / "master_main.py").write_text("# master\n", encoding="utf-8")
        (mains / "celery_worker.py").write_text("# celery\n", encoding="utf-8")
        (mains / "web_terminal_main.py").write_text("# web\n", encoding="utf-8")
        (mains / "worker_gun.py").write_text("# worker gun\n", encoding="utf-8")
        (mains / "worker_main.py").write_text("# worker\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="prepare deploy 103",
            operation_args={"project_root": str(project_root)},
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=prepare_project_files" in result
    assert "copy_preview=mains/gun.py->gun.py" in result
    assert not (project_root / "gun.py").exists()


def test_prepare_project_files_recipe_execute_copies_mains_entries():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    required_files = [
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ]
    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project" / "vemu_uestc"
        mains = project_root / "mains"
        mains.mkdir(parents=True)
        for filename in required_files:
            (mains / filename).write_text(f"# {filename}\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="prepare deploy 103",
            operation_args={"project_root": str(project_root)},
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

        copied = {filename: (project_root / filename).read_text(encoding="utf-8") for filename in required_files}

    assert "result_status=completed" in result
    assert "dry_run=false" in result
    assert "recipe_id=prepare_project_files" in result
    assert copied["master_main.py"] == "# master_main.py\n"


def test_extract_archive_recipe_dry_run_lists_members_without_writing():
    import tarfile

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        source_dir = temp_dir / "source"
        source_dir.mkdir()
        (source_dir / "base_requ_setup.sh").write_text("# setup\n", encoding="utf-8")
        archive = temp_dir / "vemu_install_new_gen.tar"
        with tarfile.open(archive, "w") as handle:
            handle.add(source_dir / "base_requ_setup.sh", arcname="vemu_install_new_gen/base_requ_setup.sh")
        destination = temp_dir / "extract"
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="extract install bundle",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "extract_archive",
                    "args": {
                        "archive_path": str(archive),
                        "destination_dir": str(destination),
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=extract_archive" in result
    assert "archive_members=vemu_install_new_gen/base_requ_setup.sh" in result
    assert not (destination / "vemu_install_new_gen" / "base_requ_setup.sh").exists()


def test_extract_archive_recipe_execute_extracts_tar_members():
    import tarfile

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        source_dir = temp_dir / "source"
        source_dir.mkdir()
        (source_dir / "docker_service.sh").write_text("# docker\n", encoding="utf-8")
        archive = temp_dir / "vemu_install_new_gen.tar"
        with tarfile.open(archive, "w") as handle:
            handle.add(source_dir / "docker_service.sh", arcname="vemu_install_new_gen/docker_service.sh")
        destination = temp_dir / "extract"
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="extract install bundle",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "extract_archive",
                    "args": {
                        "archive_path": str(archive),
                        "destination_dir": str(destination),
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

        extracted = (destination / "vemu_install_new_gen" / "docker_service.sh").read_text(encoding="utf-8")

    assert "result_status=completed" in result
    assert "dry_run=false" in result
    assert "recipe_id=extract_archive" in result
    assert extracted == "# docker\n"


def test_extract_archive_recipe_execute_uses_sudo_helper_for_root_destination():
    import tarfile

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    calls = []

    def command_runner(command):
        calls.append(command)
        return "helper extracted"

    with local_temp_dir() as temp_dir:
        payload = temp_dir / "docker_service.sh"
        payload.write_text("# docker\n", encoding="utf-8")
        archive = temp_dir / "vemu_install_2024_12_5.tar"
        with tarfile.open(archive, "w") as handle:
            handle.add(payload, arcname="vemu_install_new_gen/docker_service.sh")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False, command_runner=command_runner),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="env-setup",
            objective="extract install bundle",
            operation_args={
                "archive_path": str(archive),
                "destination_dir": "/root",
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert calls == [
        [
            "sudo",
            "-n",
            "/usr/local/bin/klonet-agent-op",
            "extract-archive",
            "--execute",
            "--archive-path",
            str(archive),
            "--destination-dir",
            "/root",
        ]
    ]
    assert "dry_run=false" in result
    assert "recipe_id=extract_archive" in result
    assert "helper extracted" in result


def test_extract_archive_recipe_blocks_path_traversal_members():
    import tarfile

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        payload = temp_dir / "evil.py"
        payload.write_text("bad\n", encoding="utf-8")
        archive = temp_dir / "bad.tar"
        with tarfile.open(archive, "w") as handle:
            handle.add(payload, arcname="../evil.py")
        destination = temp_dir / "extract"
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="extract install bundle",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "extract_archive",
                    "args": {
                        "archive_path": str(archive),
                        "destination_dir": str(destination),
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=blocked" in result
    assert "unsafe_archive_member=../evil.py" in result
    assert not (temp_dir / "evil.py.extracted").exists()


def test_run_install_script_recipe_dry_run_previews_allowlisted_script():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        script_dir = temp_dir / "vemu_install_new_gen"
        script_dir.mkdir()
        (script_dir / "base_requ_setup.sh").write_text("# setup\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="run base setup",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "run_install_script",
                    "args": {
                        "script_dir": str(script_dir),
                        "script_name": "base_requ_setup.sh",
                        "script_args": "NORMAL",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=run_install_script" in result
    assert "command_preview=" in result
    assert "base_requ_setup.sh NORMAL" in result
    assert "environment unchanged" in result


def test_run_install_script_recipe_execute_calls_fixed_command():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    calls = []

    def fake_runner(command):
        calls.append(command)
        return "script ok"

    with local_temp_dir() as temp_dir:
        script_dir = temp_dir / "vemu_install_new_gen"
        script_dir.mkdir()
        (script_dir / "docker_service.sh").write_text("# docker\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False, command_runner=fake_runner),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="run docker service",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "run_install_script",
                    "args": {
                        "script_dir": str(script_dir),
                        "script_name": "docker_service.sh",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=completed" in result
    assert "dry_run=false" in result
    assert "recipe_id=run_install_script" in result
    assert "script ok" in result
    assert len(calls) == 1
    assert calls[0][0:2] == ["bash", "-lc"]
    assert "docker_service.sh" in calls[0][2]


def test_run_install_script_recipe_default_runner_streams_output(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    calls = []

    def fake_run(command, check, text, encoding, errors):
        calls.append(
            {
                "command": command,
                "check": check,
                "text": text,
                "encoding": encoding,
                "errors": errors,
            }
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("klonet_agent.ops.recipes.subprocess.run", fake_run)

    with local_temp_dir() as temp_dir:
        script_dir = temp_dir / "vemu_install_new_gen"
        script_dir.mkdir()
        (script_dir / "docker_service.sh").write_text("# docker\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="run docker service",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "run_install_script",
                    "args": {
                        "script_dir": str(script_dir),
                        "script_name": "docker_service.sh",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert len(calls) == 1
    assert "capture_output" not in calls[0]
    assert calls[0]["command"][0:2] == ["bash", "-lc"]
    assert "streamed_to_console=true" in result
    assert "result_status=completed" in result


def test_run_install_script_recipe_execute_uses_sudo_helper_for_root_script_dir():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    calls = []

    def command_runner(command):
        calls.append(command)
        return "setup done"

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False, command_runner=command_runner),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="env-setup",
            objective="run base setup",
            operation_args={
                "script_dir": "/root/vemu_install_new_gen",
                "script_name": "base_requ_setup.sh",
                "script_args": "NORMAL",
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert calls == [
        [
            "sudo",
            "-n",
            "/usr/local/bin/klonet-agent-op",
            "run-install-script",
            "--execute",
            "--script-dir",
            "/root/vemu_install_new_gen",
            "--script-name",
            "base_requ_setup.sh",
            "--script-args",
            "NORMAL",
        ]
    ]
    assert "dry_run=false" in result
    assert "recipe_id=run_install_script" in result
    assert "setup done" in result


def test_run_install_script_recipe_blocks_unsupported_script():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        script_dir = temp_dir / "vemu_install_new_gen"
        script_dir.mkdir()
        (script_dir / "rm_everything.sh").write_text("# nope\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="run unsafe script",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "run_install_script",
                    "args": {
                        "script_dir": str(script_dir),
                        "script_name": "rm_everything.sh",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=blocked" in result
    assert "unsupported_script=rm_everything.sh" in result
    assert "environment unchanged" in result


def test_write_ops_file_recipe_dry_run_redacts_preview_without_writing():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        target = temp_dir / "nginx.conf"
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=True),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="write nginx draft",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "write_ops_file",
                    "args": {
                        "path": str(target),
                        "content": "proxy_pass http://127.0.0.1:5000;\napi_key=secret-token\n",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=write_ops_file" in result
    assert "proxy_pass http://127.0.0.1:5000;" in result
    assert "secret-token" not in result
    assert "[REDACTED]" in result
    assert not target.exists()


def test_write_ops_file_recipe_execute_writes_file_and_backs_up_existing_content():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        target = temp_dir / "config.py"
        target.write_text("master_port = 5000\n", encoding="utf-8")
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="write config",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "write_ops_file",
                    "args": {
                        "path": str(target),
                        "content": "master_port = 5100\n",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

        backups = list(temp_dir.glob("config.py.bak.*"))
        new_content = target.read_text(encoding="utf-8")
        old_content = backups[0].read_text(encoding="utf-8") if backups else ""

    assert "result_status=completed" in result
    assert "dry_run=false" in result
    assert "recipe_id=write_ops_file" in result
    assert "backup_path=" in result
    assert new_content == "master_port = 5100\n"
    assert old_content == "master_port = 5000\n"


def test_write_ops_file_recipe_allows_klonet_startup_source_files():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        targets = [
            temp_dir / "103_project" / "vemu_uestc" / "vemu_config" / "config.py",
            temp_dir / "103_project" / "vemu_uestc" / "mains" / "web_terminal_main.py",
            temp_dir / "103_project" / "vemu_uestc" / "web_terminal_main.py",
        ]
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        results = []
        for index, target in enumerate(targets):
            plan = store.create_plan(
                operation="deploy_platform",
                target=f"103-{index}",
                objective="write startup file",
                recipe_bindings={
                    "prepare-files": {
                        "recipe_id": "write_ops_file",
                        "args": {
                            "path": str(target),
                            "content": "# startup config\n",
                        },
                    }
                },
            )
            plan.status = "approved"
            prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
            prepare_step.status = "approved"
            _complete_steps_before(plan, "prepare-files")
            store.save_plan(plan)
            results.append(store.execute_step(plan.plan_id, "prepare-files"))

        assert all("result_status=completed" in result for result in results)
        assert all(target.read_text(encoding="utf-8") == "# startup config\n" for target in targets)


def test_write_ops_file_recipe_blocks_non_startup_python_source_files():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        target = temp_dir / "103_project" / "vemu_uestc" / "webserver" / "api" / "topo.py"
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="write business source",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "write_ops_file",
                    "args": {
                        "path": str(target),
                        "content": "# business logic\n",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=blocked" in result
    assert "unsupported_file_type=topo.py" in result
    assert not target.exists()


def test_write_ops_file_recipe_blocks_sensitive_paths():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        target = temp_dir / ".env"
        store = OperationPlanStore(
            temp_dir / "plans",
            recipe_runner=ControlledRecipeRunner(dry_run=False),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="write env",
            recipe_bindings={
                "prepare-files": {
                    "recipe_id": "write_ops_file",
                    "args": {
                        "path": str(target),
                        "content": "OPENAI_API_KEY=secret\n",
                    },
                }
            },
        )
        plan.status = "approved"
        prepare_step = next(item for item in plan.steps if item.step_id == "prepare-files")
        prepare_step.status = "approved"
        _complete_steps_before(plan, "prepare-files")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "prepare-files")

    assert "result_status=blocked" in result
    assert "refused_sensitive_path=.env" in result
    assert not target.exists()


def test_reload_nginx_recipe_dry_run_generates_fixed_preview():
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
            objective="validate and reload nginx",
            recipe_bindings={
                "start-services": {
                    "recipe_id": "reload_nginx",
                    "args": {},
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "start-services")
        step.status = "approved"
        _complete_steps_before(plan, "start-services")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "start-services")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=completed" in result
    assert "dry_run=true" in result
    assert "recipe_id=reload_nginx" in result
    assert "command_preview=/usr/local/bin/klonet-agent-op reload-nginx --dry-run" in result
    assert "environment unchanged" in result
    statuses = _status_by_step(loaded)
    assert statuses["start-services"] == "completed"


def test_reload_nginx_recipe_execute_tests_config_before_reload():
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    calls = []

    def command_runner(command):
        calls.append(command)
        return "nginx_test=ok nginx_reload=ok environment_changed=true"

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(
                dry_run=False,
                command_runner=command_runner,
            ),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="reload nginx",
            recipe_bindings={
                "start-services": {
                    "recipe_id": "reload_nginx",
                    "args": {},
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "start-services")
        step.status = "approved"
        _complete_steps_before(plan, "start-services")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "start-services")

    assert calls == [
        ["sudo", "-n", "/usr/local/bin/klonet-agent-op", "reload-nginx", "--execute"]
    ]
    assert "result_status=completed" in result
    assert "dry_run=false" in result
    assert "recipe_id=reload_nginx" in result
    assert "nginx_test=ok" in result
    assert "nginx_reload=ok" in result
    assert "environment_changed=true" in result


def test_reload_nginx_recipe_blocks_when_config_test_fails_without_reload():
    import subprocess

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    calls = []

    def command_runner(command):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1,
            command,
            output="",
            stderr="nginx_test_failed returncode=1 stderr=nginx: configuration file /etc/nginx/nginx.conf test failed environment_changed=false",
        )

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(
            temp_dir,
            recipe_runner=ControlledRecipeRunner(
                dry_run=False,
                command_runner=command_runner,
            ),
        )
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            objective="reload nginx",
            recipe_bindings={
                "start-services": {
                    "recipe_id": "reload_nginx",
                    "args": {},
                }
            },
        )
        plan.status = "approved"
        step = next(item for item in plan.steps if item.step_id == "start-services")
        step.status = "approved"
        _complete_steps_before(plan, "start-services")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "start-services")
        loaded = store.load_plan(plan.plan_id)

    assert calls == [
        ["sudo", "-n", "/usr/local/bin/klonet-agent-op", "reload-nginx", "--execute"]
    ]
    assert "result_status=blocked" in result
    assert "nginx_test_failed returncode=1" in result
    assert "test failed" in result
    assert "environment_changed=false" in result
    assert loaded.status == "approved"


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
        _complete_steps_before(plan, "stop-services")
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
    assert statuses["identify-owned-resources"] == "completed"
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
        _complete_steps_before(plan, "stop-services")
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
    assert statuses["identify-owned-resources"] == "completed"
    assert statuses["cleanup-owned-resources"] == "pending"


def test_helper_backed_recipes_use_sudo_only_for_real_execution():
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledRecipeRunner

    plan = OperationPlan(
        plan_id="plan-sudo-boundary",
        operation="restart_platform",
        target="102",
        objective="verify helper privilege boundary",
    )
    cases = [
        (
            "restart_screen_component",
            {
                "platform": "102",
                "component": "master",
                "screen_session": "102_m",
                "project_root": "/home/adminis/lht/102_project",
            },
            "restart-screen-component",
        ),
        (
            "stop_screen_component",
            {
                "platform": "102",
                "component": "master",
                "screen_session": "102_m",
            },
            "stop-screen-component",
        ),
        ("stop_platform_screens", {"platform": "102"}, "stop-platform-screens"),
        (
            "start_platform_screens",
            {
                "platform": "102",
                "project_root": "/home/adminis/lht/102_project",
            },
            "start-platform-screens",
        ),
        ("reload_nginx", {}, "reload-nginx"),
    ]

    for recipe_id, recipe_args, action in cases:
        calls = []
        step = OperationStep(
            step_id=f"test-{recipe_id}",
            title="test helper command",
            purpose="verify sudo boundary",
            recipe_id=recipe_id,
            recipe_args=recipe_args,
        )
        real_result = ControlledRecipeRunner(
            dry_run=False,
            command_runner=lambda command: calls.append(command) or "helper ok",
        )(plan, step)

        assert real_result.status == "completed"
        assert calls[0][:4] == [
            "sudo",
            "-n",
            "/usr/local/bin/klonet-agent-op",
            action,
        ]
        assert "--execute" in calls[0]

        dry_result = ControlledRecipeRunner(dry_run=True)(plan, step)
        assert dry_result.status == "completed"
        assert "command_preview=/usr/local/bin/klonet-agent-op" in dry_result.output
        assert "command_preview=sudo " not in dry_result.output
        assert "--dry-run" in dry_result.output


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
        _complete_steps_before(plan, "restart-master")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert calls == [
        [
            "sudo",
            "-n",
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
    assert statuses["precheck-runtime"] == "completed"
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
        _complete_steps_before(plan, "restart-master")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=failed" in result
    assert "helper_failed returncode=7" in result
    assert "screen failed" in result
    assert loaded.status == "failed"


def test_restart_recipe_unknown_environment_blocks_plan_until_reinspection():
    import subprocess

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledRecipeRunner
    from tests.helpers import local_temp_dir

    helper_stderr = "\n".join(
        [
            "klonet_agent_op",
            "error=command_failed",
            "failed_command=screen -dmS 102_m",
            "returncode=1",
            "environment_changed=unknown",
        ]
    )

    def command_runner(command):
        raise subprocess.CalledProcessError(1, command, output="", stderr=helper_stderr)

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
        _complete_steps_before(plan, "restart-master")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    statuses = _status_by_step(loaded)
    assert "result_status=blocked" in result
    assert "helper_environment_unknown" in result
    assert "next_required_action=inspect_runtime" in result
    assert loaded.status == "approved"
    assert statuses["restart-master"] == "blocked"
    assert statuses["precheck-runtime"] == "completed"


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
        _complete_steps_before(plan, "restart-master")
        store.save_plan(plan)

        result = store.execute_step(plan.plan_id, "restart-master")
        loaded = store.load_plan(plan.plan_id)

    assert "result_status=blocked" in result
    assert "unsupported_component=database" in result
    statuses = _status_by_step(loaded)
    assert statuses["restart-master"] == "blocked"
    assert statuses["precheck-runtime"] == "completed"
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
        executor.run(
            "execute_ops_operation_step",
            {"plan_id": plan_id, "step_id": "precheck-runtime"},
        )
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


def test_executor_execute_ops_next_step_stops_at_step_confirmation_after_auto_precheck():
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
                "execute_ops_next_step",
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

        result = executor.run("execute_ops_next_step", {"plan_id": plan_id})

    assert result.startswith("Error:")
    assert "execute_step=precheck-runtime" not in result
    assert "step requires explicit confirm-step" in result
    assert "restart-master" in result


def test_executor_resolve_ops_blocked_step_resets_step_to_pending():
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
                "resolve_ops_blocked_step",
            },
            memory_store=store,
        )
        created = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
                "operation_args": {"project_root": "/home/adminis/lht/102_project"},
            },
        )
        plan_id = _extract_plan_id(created)
        plan_store = executor._operation_plan_store()
        plan = plan_store.load_plan(plan_id)
        plan.status = "approved"
        _complete_steps_before(plan, "restart-master")
        step = next(item for item in plan.steps if item.step_id == "restart-master")
        step.status = "blocked"
        plan_store.save_plan(plan)

        result = executor.run(
            "resolve_ops_blocked_step",
            {
                "plan_id": plan_id,
                "step_id": "restart-master",
                "resolution_evidence": "inspect_runtime confirmed screen and port state refreshed",
            },
        )
        loaded = plan_store.load_plan(plan_id)

    assert "ops_operation_resolution" in result
    assert "result_status=resolved" in result
    assert "step_status=pending" in result
    assert "next_required_action=confirm-step" in result
    assert "restart-master" in result
    assert _status_by_step(loaded)["restart-master"] == "pending"
    assert "inspect_runtime confirmed screen and port state refreshed" in loaded.evidence


def test_executor_describe_ops_operation_plan_returns_current_state():
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
                "describe_ops_operation_plan",
            },
            memory_store=store,
        )
        created = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
                "operation_args": {"project_root": "/home/adminis/lht/102_project"},
            },
        )
        plan_id = _extract_plan_id(created)

        result = executor.run("describe_ops_operation_plan", {"plan_id": plan_id})

    assert result.startswith("ops_operation_plan")
    assert f"plan_id={plan_id}" in result
    assert "operation=restart_platform" in result
    assert "next_step=precheck-runtime" in result


def test_executor_list_ops_operation_plans_returns_recent_plan_summaries():
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
                "list_ops_operation_plans",
            },
            memory_store=store,
        )
        first = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
            },
        )
        second = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "deploy_platform",
                "target": "103",
                "objective": "deploy platform 103",
            },
        )
        first_id = _extract_plan_id(first)
        second_id = _extract_plan_id(second)

        result = executor.run("list_ops_operation_plans", {"limit": 5})

    assert result.startswith("ops_operation_plan_list")
    assert f"plan_id={first_id}" in result
    assert f"plan_id={second_id}" in result
    assert "operation=restart_platform" in result
    assert "operation=deploy_platform" in result
    assert "target=102" in result
    assert "target=103" in result
    assert "next_step=precheck-runtime" in result
    assert "next_step=precheck" in result


def test_executor_list_ops_operation_plans_filters_by_status():
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
                "list_ops_operation_plans",
            },
            memory_store=store,
        )
        pending = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
            },
        )
        approved = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "deploy_platform",
                "target": "103",
                "objective": "deploy platform 103",
            },
        )
        pending_id = _extract_plan_id(pending)
        approved_id = _extract_plan_id(approved)
        plan_store = executor._operation_plan_store()
        approved_plan = plan_store.load_plan(approved_id)
        approved_plan.status = "approved"
        plan_store.save_plan(approved_plan)

        result = executor.run("list_ops_operation_plans", {"status": "approved", "limit": 5})

    assert f"plan_id={approved_id}" in result
    assert "status=approved" in result
    assert f"plan_id={pending_id}" not in result
    assert "status=pending" not in result


def test_executor_list_ops_operation_plans_filters_by_target_and_operation():
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
                "list_ops_operation_plans",
            },
            memory_store=store,
        )
        restart_102 = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "102",
                "objective": "restart platform 102",
            },
        )
        deploy_102 = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "deploy_platform",
                "target": "102",
                "objective": "deploy platform 102",
            },
        )
        restart_103 = executor.run(
            "create_ops_operation_plan",
            {
                "operation": "restart_platform",
                "target": "103",
                "objective": "restart platform 103",
            },
        )
        restart_102_id = _extract_plan_id(restart_102)
        deploy_102_id = _extract_plan_id(deploy_102)
        restart_103_id = _extract_plan_id(restart_103)

        result = executor.run(
            "list_ops_operation_plans",
            {"target": "102", "operation": "restart_platform", "limit": 10},
        )

    assert f"plan_id={restart_102_id}" in result
    assert "target=102" in result
    assert "operation=restart_platform" in result
    assert f"plan_id={deploy_102_id}" not in result
    assert f"plan_id={restart_103_id}" not in result


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


def _complete_steps_before(plan, step_id):
    for step in plan.steps:
        if step.step_id == step_id:
            return
        step.status = "completed"
