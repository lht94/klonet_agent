"""最小导入测试。

这个测试用来保证 agent_v7 -> klonet_agent 迁移后，核心模块至少可以被导入。
"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_core_imports():
    import klonet_agent
    from klonet_agent.agents import get_profile
    from klonet_agent.session import AgentSession

    assert klonet_agent.__version__
    assert get_profile("mentor").name == "mentor"
    assert get_profile("coding").name == "coding"
    assert AgentSession(user_id="u1", project_id="p1").user_id == "u1"


def test_profile_tool_permissions():
    """确认 Mentor 和 Coding 的工具权限边界。"""

    from klonet_agent.agents import get_profile

    mentor = get_profile("mentor")
    coding = get_profile("coding")

    assert "search_knowledge" in mentor.allowed_tools
    assert "read_project_journal" in mentor.allowed_tools
    assert "write_file" not in mentor.allowed_tools
    assert "run_tests" not in mentor.allowed_tools
    assert "show_diff" not in mentor.allowed_tools

    assert "write_file" in coding.allowed_tools
    assert "run_tests" in coding.allowed_tools
    assert "show_diff" in coding.allowed_tools
    assert coding.requires_review is True


def test_session_can_update_todos():
    """确认 session 自身可以维护任务列表。"""

    from klonet_agent.session import AgentSession

    session = AgentSession(user_id="u1", project_id="p1")
    result = session.update_todos(
        [
            {"id": 1, "content": "检索知识库", "status": "completed"},
            {"id": 2, "content": "整理回答", "status": "in_progress"},
        ]
    )

    assert "total=2" in result
    assert session.todos[0]["status"] == "completed"
    assert session.todos[1]["status"] == "in_progress"
