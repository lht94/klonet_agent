"""运行时编排边界测试。"""

from types import SimpleNamespace

from tests.helpers import local_temp_dir


class FakeLLM:
    """记录调用参数并返回固定自然语言回答。"""

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        message = SimpleNamespace(content="已给出当前建议。", tool_calls=None)
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(total_tokens=10)
        return SimpleNamespace(choices=[choice], usage=usage)


def _orchestrator(temp_dir, mode="mentor"):
    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    profile = get_profile(mode)
    session = AgentSession(
        user_id="u1",
        project_id="p1",
        mode=mode,
        workspace_path=temp_dir / "workspace",
        journal_path=temp_dir / "journal.md",
    )
    llm = FakeLLM()
    memory_store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
    orchestrator = AgentOrchestrator(
        profile=profile,
        session=session,
        llm=llm,
        trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
        memory_store=memory_store,
    )
    return orchestrator, session, llm


def test_general_query_hides_klonet_search_tool():
    """明确域外的问题不应该向模型暴露 Klonet 检索工具。"""

    with local_temp_dir() as temp_dir:
        orchestrator, _, llm = _orchestrator(temp_dir)
        history = orchestrator.init_history()
        orchestrator.single_chat(
            "不需要 Klonet，只做 Linux VM Docker Compose DinD Rust 实验",
            history,
            0,
        )

    visible_names = {
        tool["function"]["name"]
        for tool in llm.calls[0]["tools"]
    }
    assert "search_knowledge" not in visible_names


def test_coding_todo_auto_continue_has_limit():
    """Coding todo 最多自动续跑一次，之后应等待用户确认。"""

    with local_temp_dir() as temp_dir:
        orchestrator, session, llm = _orchestrator(temp_dir, mode="coding")
        session.update_todos(
            [{"id": 1, "content": "继续开发", "status": "in_progress"}],
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("完成当前任务", history, 0)

    assert len(llm.calls) == 2
    assert session.todos[0]["status"] == "waiting_user"

def test_soft_general_query_keeps_search_tool_visible():
    """非明确否定的通用分类只能软路由，不能控制工具硬权限。"""

    with local_temp_dir() as temp_dir:
        orchestrator, _, llm = _orchestrator(temp_dir)
        history = orchestrator.init_history()
        orchestrator.single_chat(
            "如何配置 Docker Compose 自定义网络",
            history,
            0,
        )

    visible_names = {
        tool["function"]["name"]
        for tool in llm.calls[0]["tools"]
    }
    assert "search_knowledge" in visible_names
