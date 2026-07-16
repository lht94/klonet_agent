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


def test_memory_history_drops_unclosed_tool_calls():
    """中断的工具调用不能再次进入 OpenAI 请求历史。"""

    from klonet_agent.memory.store import MemoryStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = MemoryStore.for_session(temp_dir, "u1", "p1")
        store.append_history({"role": "user", "content": "start deploy"})
        store.append_history(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "execute_ops_operation_step",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        )

        history = store.load_unarchived_history(max_messages=20)

    assert history == [{"role": "user", "content": "start deploy"}]


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
        assert "102_m and lht_m" in second.memory_prompt(mode="ops")


def test_shared_ops_memory_is_not_injected_into_mentor_prompt():
    """Mentor should not see runtime Ops memory unless it explicitly switches mode."""

    from klonet_agent.memory.store import MemoryStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = MemoryStore.for_session(temp_dir, "u1", "p1")
        store.append_shared_episode(
            "tool_observation: inspect_klonet_runtime found 102_m and lht_m"
        )
        store.write_shared_ops_baseline(
            "Ubuntu 22.04; nginx active; redis ports 8873, 8872"
        )

        mentor_prompt = store.memory_prompt(mode="mentor")
        ops_prompt = store.memory_prompt(mode="ops")

    assert "102_m and lht_m" not in mentor_prompt
    assert "Ubuntu 22.04" not in mentor_prompt
    assert "102_m and lht_m" in ops_prompt
    assert "Ubuntu 22.04" in ops_prompt


def test_shared_ops_memory_only_injects_recent_days():
    """最近几天的共享 Ops 记忆自动注入，旧记录不自动注入。"""

    from klonet_agent.memory.store import MemoryStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = MemoryStore.for_session(temp_dir, "u1", "p1")
        shared_dir = store.shared_dir
        assert shared_dir is not None
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / "2026-06-29.md").write_text("recent diagnostic\n", encoding="utf-8")
        (shared_dir / "2026-06-01.md").write_text("old diagnostic\n", encoding="utf-8")

        injected = store.read_shared_memory(today="2026-06-29", recent_days=3)

    assert "recent diagnostic" in injected
    assert "old diagnostic" not in injected


def test_shared_ops_memory_can_search_archived_days_on_demand():
    """旧共享记忆不自动注入，但可以按需检索。"""

    from klonet_agent.memory.store import MemoryStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = MemoryStore.for_session(temp_dir, "u1", "p1")
        shared_dir = store.shared_dir
        assert shared_dir is not None
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / "2026-06-01.md").write_text(
            "question: 怎么启动新平台\nconclusion: Redis 是共享依赖\n",
            encoding="utf-8",
        )

        result = store.search_shared_memory("Redis 新平台")

    assert "2026-06-01.md" in result
    assert "Redis 是共享依赖" in result
