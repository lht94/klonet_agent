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


def test_executor_accepts_exact_user_confirm_text_and_refuses_unknown_step_execution():
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

    assert "status=approved" in approved
    assert blocked.startswith("Error:")
    assert "尚未接入真实执行 recipe" in blocked


def _extract_plan_id(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("plan_id="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"plan_id not found in:\n{text}")
