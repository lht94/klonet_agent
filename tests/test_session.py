"""会话隔离测试。"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_session_paths_are_isolated_by_user_and_project():
    """不同用户和项目应该对应不同 workspace 与 journal。"""

    from klonet_agent.session import AgentSession

    first = AgentSession(user_id="student_a", project_id="lab_1", mode="mentor")
    second = AgentSession(user_id="student_b", project_id="lab_2", mode="coding")

    assert first.workspace_path != second.workspace_path
    assert first.journal_path != second.journal_path
    assert first.workspace_path.parts[-2:] == ("student_a", "lab_1")
    assert second.workspace_path.parts[-2:] == ("student_b", "lab_2")
    assert first.journal_path.parts[-2:] == ("student_a", "lab_1.md")
    assert second.journal_path.parts[-2:] == ("student_b", "lab_2.md")
    assert first.mode == "mentor"
    assert second.mode == "coding"


def test_session_todos_do_not_leak_between_sessions():
    """不同 session 的 todo 列表应该互不影响。"""

    from klonet_agent.session import AgentSession

    first = AgentSession(user_id="student_a", project_id="lab_1")
    second = AgentSession(user_id="student_a", project_id="lab_2")

    first.update_todos(
        [
            {"id": 1, "content": "阅读源码", "status": "completed"},
            {"id": 2, "content": "整理问题", "status": "in_progress"},
        ]
    )

    assert len(first.todos) == 2
    assert second.todos == []


def test_update_todos_normalizes_invalid_status():
    """模型给出非法状态时，应该回退成 pending。"""

    from klonet_agent.session import AgentSession

    session = AgentSession(user_id="student_a", project_id="lab_1")
    session.update_todos(
        [
            {"id": 1, "content": "检查任务", "status": "unknown"},
        ]
    )

    assert session.todos == [{"id": 1, "content": "检查任务", "status": "pending"}]


def test_update_todos_rejects_multiple_in_progress_without_mutating_existing_list():
    """同一时间不能有多个 in_progress，失败时不覆盖原任务列表。"""

    from klonet_agent.session import AgentSession

    session = AgentSession(user_id="student_a", project_id="lab_1")
    session.update_todos(
        [
            {"id": 1, "content": "已有任务", "status": "pending"},
        ]
    )
    result = session.update_todos(
        [
            {"id": 1, "content": "任务一", "status": "in_progress"},
            {"id": 2, "content": "任务二", "status": "in_progress"},
        ]
    )

    assert result.startswith("Error:")
    assert session.todos == [{"id": 1, "content": "已有任务", "status": "pending"}]

def test_memory_history_is_isolated_by_user_and_project():
    """工作历史应该按 user_id 和 project_id 隔离。"""

    from klonet_agent.memory.store import MemoryStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        first = MemoryStore.for_session(temp_dir, "u1", "p1")
        second = MemoryStore.for_session(temp_dir, "u1", "p2")
        first.append_history({"role": "user", "content": "first project"})

        assert first.history_file != second.history_file
        assert second.load_unarchived_history() == []


def test_memory_history_only_loads_recent_messages():
    """初始化上下文时只加载最近的工作历史。"""

    from klonet_agent.memory.store import MemoryStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = MemoryStore.for_session(temp_dir, "u1", "p1")
        for index in range(30):
            store.append_history({"role": "user", "content": f"message-{index}"})

        history = store.load_unarchived_history(max_messages=20)

    assert len(history) == 20
    assert history[0]["content"] == "message-10"


def test_shared_ops_memory_is_visible_across_users():
    """Ops 工具证据应能沉淀到多用户共享记忆，但不混入用户画像。"""

    from klonet_agent.memory.store import MemoryStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        first = MemoryStore.for_session(temp_dir, "u1", "p1")
        second = MemoryStore.for_session(temp_dir, "u2", "p2")

        first.append_shared_episode(
            "tool_observation: inspect_klonet_runtime found 102_m and lht_m"
        )

        assert "102_m and lht_m" in second.read_shared_memory()
        assert "102_m and lht_m" in second.memory_prompt()
