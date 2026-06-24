"""运行时编排边界测试。"""

import json
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


class RewrittenQueryLLM:
    """模拟模型用改写后的查询重新请求 Klonet 工具。"""

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": tools})
        if len(self.calls) == 1:
            tool_calls = [
                SimpleNamespace(
                    id="search-1",
                    function=SimpleNamespace(
                        name="search_knowledge",
                        arguments=json.dumps(
                            {"query": "ContainerManager Docker API"},
                            ensure_ascii=False,
                        ),
                    ),
                ),
                SimpleNamespace(
                    id="search-2",
                    function=SimpleNamespace(
                        name="search_knowledge",
                        arguments=json.dumps(
                            {"query": "Worker Docker 容器创建"},
                            ensure_ascii=False,
                        ),
                    ),
                ),
                SimpleNamespace(
                    id="journal-1",
                    function=SimpleNamespace(
                        name="read_project_journal",
                        arguments="{}",
                    ),
                ),
            ]
            message = SimpleNamespace(content="", tool_calls=tool_calls)
        else:
            message = SimpleNamespace(content="通用虚拟机配置建议。", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=10),
        )


class RecordingToolExecutor:
    """记录真正进入执行层的工具调用。"""

    def __init__(self):
        self.calls = []

    def run(self, tool_name, tool_args):
        self.calls.append((tool_name, tool_args))
        return "unexpected tool result"


class BatchSearchLLM:
    """模拟模型在同一轮批量发起多次知识检索。"""

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": tools})
        if len(self.calls) == 1:
            tool_calls = [
                SimpleNamespace(
                    id=f"search-{index}",
                    function=SimpleNamespace(
                        name="search_knowledge",
                        arguments=json.dumps(
                            {"query": f"Klonet 拓扑部署 {index}"},
                            ensure_ascii=False,
                        ),
                    ),
                )
                for index in range(3)
            ]
            message = SimpleNamespace(content="", tool_calls=tool_calls)
        else:
            message = SimpleNamespace(content="已完成回答。", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=10),
        )


class StructuredIntentSearchLLM:
    """模拟模型在首次检索调用中提交结构化启动意图。"""

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": tools})
        if len(self.calls) == 1:
            tool_calls = [
                SimpleNamespace(
                    id="intent-search-1",
                    function=SimpleNamespace(
                        name="search_knowledge",
                        arguments=json.dumps(
                            {
                                "query": "Klonet 平台启动标准命令",
                                "intent": {
                                    "scope": "klonet",
                                    "task_type": "operation_guide",
                                    "operation": "platform_start",
                                    "target": "klonet_platform",
                                    "excluded_intents": ["environment_setup"],
                                    "prerequisites": ["environment_ready"],
                                    "is_correction": True,
                                    "confidence": 0.98,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            ]
            message = SimpleNamespace(content="", tool_calls=tool_calls)
        else:
            message = SimpleNamespace(content="标准启动命令回答。", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=10),
        )


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


def test_general_query_allows_secondary_search_but_hides_project_journal():
    """generic 可辅助检索一次，但不读取项目日志。"""

    with local_temp_dir() as temp_dir:
        orchestrator, _, llm = _orchestrator(temp_dir)
        history = orchestrator.init_history()
        orchestrator.single_chat(
            "Rust 多阶段构建如何只复制二进制文件",
            history,
            0,
        )

    visible_names = {
        tool["function"]["name"]
        for tool in llm.calls[0]["tools"]
    }
    assert "search_knowledge" in visible_names
    assert "read_project_journal" not in visible_names


def test_explicit_klonet_negation_blocks_rewritten_search():
    """明确排除 Klonet 后，模型改写查询也不能执行专属检索。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = RewrittenQueryLLM()
        executor = RecordingToolExecutor()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=session,
            llm=llm,
            tool_executor=executor,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        _, history, _ = orchestrator.single_chat(
            "不需要 Klonet，只做 Linux VM Docker Compose DinD Rust 实验",
            history,
            0,
        )

    assert executor.calls == []
    for call in llm.calls:
        visible_names = {
            tool["function"]["name"]
            for tool in call["tools"]
        }
        assert "search_knowledge" not in visible_names
        assert "read_project_journal" not in visible_names

    tool_results = [
        message["content"]
        for message in history
        if message["role"] == "tool"
    ]
    assert any("明确排除 Klonet" in result for result in tool_results)
    assert any("禁止读取" in result for result in tool_results)

    first_messages = llm.calls[0]["messages"]
    scope_prompts = [
        message["content"]
        for message in first_messages
        if message["role"] == "system" and "本轮问题范围" in message["content"]
    ]
    assert scope_prompts
    assert "后续查询改写不得改变" in scope_prompts[0]
    assert "禁止执行 Klonet RAG" in scope_prompts[0]
    assert "不能覆盖该否定条件" in scope_prompts[0]
    assert not any(
        message["role"] == "system" and "本轮问题范围" in message["content"]
        for message in history
    )


def test_klonet_search_budget_is_two():
    """Klonet 问题单轮最多执行两次知识检索。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = BatchSearchLLM()
        executor = RecordingToolExecutor()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=session,
            llm=llm,
            tool_executor=executor,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        _, history, _ = orchestrator.single_chat(
            "Klonet 拓扑部署怎么实现",
            history,
            0,
        )

    search_calls = [
        call
        for call in executor.calls
        if call[0] == "search_knowledge"
    ]
    assert len(search_calls) == 2
    tool_results = [
        message["content"]
        for message in history
        if message["role"] == "tool"
    ]
    assert any("检索预算" in result for result in tool_results)


def test_mixed_search_budget_is_two_and_requires_split_answer():
    """mixed 问题检索两次，并要求分区组织两类依据。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = BatchSearchLLM()
        executor = RecordingToolExecutor()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=session,
            llm=llm,
            tool_executor=executor,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        orchestrator.single_chat(
            "Klonet 里的 Docker Compose 应该怎么写",
            history,
            0,
        )

    search_calls = [
        call
        for call in executor.calls
        if call[0] == "search_knowledge"
    ]
    assert len(search_calls) == 2
    scope_prompts = [
        message["content"]
        for message in llm.calls[0]["messages"]
        if message["role"] == "system" and "本轮问题范围" in message["content"]
    ]
    assert "分区回答" in scope_prompts[0]


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


def test_turn_answer_policy_is_injected_and_removed_after_reply():
    """当前任务的回答策略应进入模型上下文，但不能污染后续历史。"""

    with local_temp_dir() as temp_dir:
        orchestrator, _, llm = _orchestrator(temp_dir)
        history = orchestrator.init_history()
        _, history, _ = orchestrator.single_chat(
            "Klonet 拓扑部署卡住怎么排查",
            history,
            0,
        )

    policy_prompts = [
        message["content"]
        for message in llm.calls[0]["messages"]
        if message["role"] == "system" and "本轮回答策略" in message["content"]
    ]
    assert policy_prompts
    assert "最可能原因、排查顺序、判断依据" in policy_prompts[0]
    assert "500 至 1000 字" in policy_prompts[0]
    assert not any(
        "本轮回答策略" in message.get("content", "")
        for message in history
    )


def test_coding_mode_does_not_receive_mentor_answer_policy():
    """Mentor 回答策略不能改变 Coding 模式的开发闭环。"""

    with local_temp_dir() as temp_dir:
        orchestrator, _, llm = _orchestrator(temp_dir, mode="coding")
        history = orchestrator.init_history()
        orchestrator.single_chat("解释当前实现", history, 0)

    assert not any(
        message["role"] == "system"
        and "本轮回答策略" in message.get("content", "")
        for message in llm.calls[0]["messages"]
    )


def test_model_intent_refreshes_mentor_policy_before_final_answer():
    """模型提交的可靠意图应覆盖关键词路由生成的初始策略。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StructuredIntentSearchLLM()
        executor = RecordingToolExecutor()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=session,
            llm=llm,
            tool_executor=executor,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        orchestrator.single_chat(
            "你这是配置环境吧，我怎么启动一个 Klonet 呢？",
            history,
            0,
        )

    assert executor.calls[0][1]["intent"]["operation"] == "platform_start"
    policy_prompts = [
        message["content"]
        for message in llm.calls[1]["messages"]
        if message["role"] == "system" and "本轮回答策略" in message["content"]
    ]
    assert "启动前提、标准启动命令、验证方式" in policy_prompts[0]
    assert "不得包含环境安装步骤" in policy_prompts[0]
