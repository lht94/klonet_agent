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
        if tools is None:
            return _intent_response()
        message = SimpleNamespace(content="已给出当前建议。", tool_calls=None)
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(total_tokens=10)
        return SimpleNamespace(choices=[choice], usage=usage)


class RewrittenQueryLLM:
    """模拟模型用改写后的查询重新请求 Klonet 工具。"""

    def __init__(self):
        self.calls = []
        self.answer_calls = 0

    def complete(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": tools})
        if tools is None:
            return _intent_response()
        self.answer_calls += 1
        if self.answer_calls == 1:
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
        self.answer_calls = 0

    def complete(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": tools})
        if tools is None:
            return _intent_response()
        self.answer_calls += 1
        if self.answer_calls == 1:
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
        self.answer_calls = 0

    def complete(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": tools})
        if tools is None:
            return _intent_response()
        self.answer_calls += 1
        if self.answer_calls == 1:
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


def _stream_chunk(
    *,
    content=None,
    tool_calls=None,
    finish_reason=None,
    usage=None,
):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _intent_response(content="{}"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(total_tokens=0),
    )


def _answer_calls(llm):
    return [call for call in llm.calls if call.get("tools") is not None]


class StreamingAnswerLLM:
    """模拟模型用流式分片返回普通自然语言回答。"""

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        if tools is None:
            return _intent_response()
        if stream:
            return iter(
                [
                    _stream_chunk(content="第一段"),
                    _stream_chunk(content="，第二段"),
                    _stream_chunk(finish_reason="stop", usage=SimpleNamespace(total_tokens=12)),
                ]
            )
        message = SimpleNamespace(content="不应走非流式回答", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=12),
        )


class StreamingToolThenAnswerLLM:
    """模拟模型先用流式 tool call 请求工具，再流式返回最终回答。"""

    def __init__(self):
        self.calls = []
        self.answer_calls = 0

    def complete(self, messages, tools, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        if tools is None:
            return _intent_response()
        self.answer_calls += 1
        if self.answer_calls == 1:
            return iter(
                [
                    _stream_chunk(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="search-stream-1",
                                function=SimpleNamespace(
                                    name="search_knowledge",
                                    arguments='{"query": "Klonet ',
                                ),
                            )
                        ]
                    ),
                    _stream_chunk(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(
                                    name=None,
                                    arguments='启动", "intent": {"scope": "klonet", "task_type": "operation_guide", "operation": "platform_start", "confidence": 0.95}}',
                                ),
                            )
                        ],
                        finish_reason="tool_calls",
                        usage=SimpleNamespace(total_tokens=8),
                    ),
                ]
            )
        return iter(
            [
                _stream_chunk(content="启动命令如下"),
                _stream_chunk(finish_reason="stop", usage=SimpleNamespace(total_tokens=9)),
            ]
        )


class OpsPlanInjectionLLM:
    """First requests runtime inspection, then records the final-answer context."""

    def __init__(self):
        self.calls = []
        self.answer_calls = 0

    def complete(self, messages, tools, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        if tools is None:
            return _intent_response(
                json.dumps(
                    {
                        "intent": {
                            "scope": "klonet",
                            "task_type": "operation_guide",
                            "operation": "platform_start",
                            "confidence": 0.95,
                        }
                    },
                    ensure_ascii=False,
                )
            )
        self.answer_calls += 1
        if self.answer_calls == 1:
            return iter(
                [
                    _stream_chunk(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="runtime-1",
                                function=SimpleNamespace(
                                    name="inspect_klonet_runtime",
                                    arguments='{"checks":["redis","docker_containers","ports","screen"]}',
                                ),
                            )
                        ],
                        finish_reason="tool_calls",
                        usage=SimpleNamespace(total_tokens=4),
                    )
                ]
            )
        return iter(
            [
                _stream_chunk(content="按当前环境启动建议如下"),
                _stream_chunk(finish_reason="stop", usage=SimpleNamespace(total_tokens=6)),
            ]
        )


class StaticOpsExecutor:
    def run(self, tool_name, tool_args):
        return "\n".join(
            [
                "inspect_klonet_runtime",
                "- redis: detected - active",
                "- docker_containers: detected - redis_102 Up 6 days",
                "- ports: detected - LISTEN 0 4096 0.0.0.0:12000",
                "- screen: detected - 102_m lht_m",
            ]
        )


class PlatformStartIntentAnalyzer:
    """Return a deterministic platform-start intent for Ops planner tests."""

    def analyze(self, user_input, *, recent_history=None):
        from klonet_agent.knowledge.intent import QueryIntent
        from klonet_agent.knowledge.intent_analyzer import IntentAnalysis

        return IntentAnalysis(
            intent=QueryIntent.from_mapping(
                {
                    "scope": "klonet",
                    "task_type": "operation_guide",
                    "operation": "platform_start",
                    "confidence": 0.95,
                }
            ),
            token_usage=0,
        )


class MultiToolOpsPlanLLM:
    """Request multiple tools, then capture the follow-up model context."""

    def __init__(self):
        self.calls = []
        self.answer_calls = 0

    def complete(self, messages, tools, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        if tools is None:
            return _intent_response()
        self.answer_calls += 1
        if self.answer_calls == 1:
            return iter(
                [
                    _stream_chunk(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="knowledge-1",
                                function=SimpleNamespace(
                                    name="search_knowledge",
                                    arguments='{"query":"Klonet 平台启动"}',
                                ),
                            ),
                            SimpleNamespace(
                                index=1,
                                id="runtime-1",
                                function=SimpleNamespace(
                                    name="inspect_klonet_runtime",
                                    arguments='{"checks":["redis","ports","screen"]}',
                                ),
                            ),
                            SimpleNamespace(
                                index=2,
                                id="memory-1",
                                function=SimpleNamespace(
                                    name="search_shared_ops_memory",
                                    arguments='{"query":"启动新平台"}',
                                ),
                            ),
                        ],
                        finish_reason="tool_calls",
                        usage=SimpleNamespace(total_tokens=4),
                    )
                ]
            )
        return iter(
            [
                _stream_chunk(content="按当前环境启动建议如下。"),
                _stream_chunk(finish_reason="stop", usage=SimpleNamespace(total_tokens=6)),
            ]
        )


class MultiToolStaticOpsExecutor:
    def run(self, tool_name, tool_args):
        if tool_name == "search_knowledge":
            return "检索到以下可靠 Klonet 证据：平台启动应检查端口和 screen。"
        if tool_name == "search_shared_ops_memory":
            return "以下是历史共享 Ops 记忆检索结果，只能作为线索。"
        return StaticOpsExecutor().run(tool_name, tool_args)


class StreamTimeoutError(Exception):
    pass


class StreamingTimeoutThenNonStreamLLM:
    """Simulate stream handshake timeout, then successful non-stream retry."""

    def __init__(self):
        self.calls = []

    def complete(self, messages, tools, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        if tools is None:
            return _intent_response()
        if stream:
            raise StreamTimeoutError("Request timed out.")
        message = SimpleNamespace(content="fallback answer", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=11),
        )


class CapturingIntentAnalyzer:
    """在意图解析期间检查用户是否已经看到正在思考提示。"""

    def __init__(self, capsys):
        self.capsys = capsys
        self.output_during_analyze = ""

    def analyze(self, user_input, *, recent_history=None):
        from klonet_agent.knowledge.intent_analyzer import IntentAnalysis
        from klonet_agent.knowledge.intent import QueryIntent

        self.output_during_analyze = self.capsys.readouterr().out
        return IntentAnalysis(
            intent=QueryIntent.from_mapping(
                {
                    "scope": "klonet",
                    "task_type": "concept",
                    "operation": "unknown",
                    "confidence": 0.9,
                }
            ),
            token_usage=0,
        )


class ClarifyingContinueIntentAnalyzer:
    """模拟模型把“继续”误判成部署澄清。"""

    def analyze(self, user_input, *, recent_history=None):
        from klonet_agent.knowledge.intent_analyzer import IntentAnalysis
        from klonet_agent.knowledge.intent import QueryIntent

        if user_input.strip() == "继续":
            return IntentAnalysis(
                intent=QueryIntent.from_mapping(
                    {
                        "scope": "klonet",
                        "task_type": "deployment_guidance",
                        "operation": "unknown",
                        "clarification_required": True,
                        "clarification_question": "你是想首次安装 Klonet 环境，还是启动已经安装好的平台服务？",
                        "confidence": 0.95,
                    }
                ),
                token_usage=0,
            )
        return IntentAnalysis(
            intent=QueryIntent.from_mapping(
                {
                    "scope": "klonet",
                    "task_type": "concept",
                    "operation": "unknown",
                    "confidence": 0.9,
                }
            ),
            token_usage=0,
        )


class PauseThenAnswerLLM:
    """第一轮触发工具调用上限，第二轮恢复后给最终回答。"""

    def __init__(self):
        self.calls = []
        self.answer_calls = 0

    def complete(self, messages, tools, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        self.answer_calls += 1
        if self.answer_calls == 1:
            return iter(
                [
                    _stream_chunk(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="search-before-pause",
                                function=SimpleNamespace(
                                    name="search_knowledge",
                                    arguments=json.dumps(
                                        {
                                            "query": "卫星平台",
                                            "intent": {
                                                "scope": "klonet",
                                                "task_type": "concept",
                                                "operation": "unknown",
                                                "confidence": 0.9,
                                            },
                                        },
                                        ensure_ascii=False,
                                    ),
                                ),
                            )
                        ],
                        finish_reason="tool_calls",
                        usage=SimpleNamespace(total_tokens=5),
                    )
                ]
            )
        return iter(
            [
                _stream_chunk(content="继续上一轮卫星平台介绍。"),
                _stream_chunk(finish_reason="stop", usage=SimpleNamespace(total_tokens=6)),
            ]
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
        for tool in _answer_calls(llm)[0]["tools"]
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
    for call in _answer_calls(llm):
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

    first_messages = _answer_calls(llm)[0]["messages"]
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


def test_ops_klonet_search_budget_is_wider_than_mentor():
    """Ops mode should allow more Klonet retrieval attempts during diagnosis loops."""

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
            mode="ops",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops"),
            session=session,
            llm=llm,
            tool_executor=executor,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        _, history, _ = orchestrator.single_chat(
            "Klonet 鎷撴墤閮ㄧ讲鎬庝箞瀹炵幇",
            history,
            0,
        )

    search_calls = [
        call
        for call in executor.calls
        if call[0] == "search_knowledge"
    ]
    tool_results = [
        message["content"]
        for message in history
        if message["role"] == "tool"
    ]
    assert len(search_calls) == 3
    assert not any("retrieval budget" in result for result in tool_results)


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
        for message in _answer_calls(llm)[0]["messages"]
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
        for tool in _answer_calls(llm)[0]["tools"]
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
        for message in _answer_calls(llm)[0]["messages"]
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
        for message in _answer_calls(llm)[0]["messages"]
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
        for message in _answer_calls(llm)[1]["messages"]
        if message["role"] == "system" and "本轮回答策略" in message["content"]
    ]
    assert "启动前提、标准启动命令、验证方式" in policy_prompts[0]
    assert "不得包含环境安装步骤" in policy_prompts[0]


def test_single_chat_streams_plain_answer_without_duplicate_print(capsys):
    """最终自然语言回答应边到达边输出，不能等完整响应后再重复打印。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingAnswerLLM()
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
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        _, history, token = orchestrator.single_chat("解释 Klonet", history, 0)

    output = capsys.readouterr().out
    assert "Klonet Agent：第一段，第二段" in output
    assert output.count("第一段，第二段") == 1
    assert history[-1]["content"] == "第一段，第二段"
    assert "\u6b63\u5728\u601d\u8003" in output
    assert output.index("\u6b63\u5728\u601d\u8003") < output.index(history[-1]["content"])
    assert token == 12
    assert _answer_calls(llm)[0]["stream"] is True


def test_default_mode_prints_visible_reasoning_trace(capsys):
    """默认模式应输出用户可见的思考摘要，再输出最终回答。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingAnswerLLM()
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
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        _, history, _ = orchestrator.single_chat("解释 Klonet", history, 0)

    output = capsys.readouterr().out
    assert "思考摘要" in output
    assert "问题类型" in output
    assert output.index("思考摘要") < output.index(history[-1]["content"])


def test_thinking_prompt_is_printed_before_intent_analysis(capsys):
    """用户输入后应立刻显示正在思考，避免意图解析阶段卡顿。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingAnswerLLM()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        intent_analyzer = CapturingIntentAnalyzer(capsys)
        orchestrator = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=session,
            llm=llm,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
            intent_analyzer=intent_analyzer,
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("解释 Klonet", history, 0)

    assert "正在思考" in intent_analyzer.output_during_analyze


def test_default_mode_prints_progress_milestones(capsys):
    """Default mode should show safe progress milestones while long steps run."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingToolThenAnswerLLM()
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
        orchestrator.single_chat("我要启动 Klonet", history, 0)

    output = capsys.readouterr().out
    assert "正在理解你的问题" in output
    assert "已识别：" in output
    assert "正在组织回答" in output
    assert "正在检索知识库" not in output
    assert "正在调用工具" not in output
    assert "工具完成" not in output
    assert "观察：" not in output
    assert "思考摘要" in output
    assert "已调用 search_knowledge" in output
    assert "INTENT_ANALYSIS_PROMPT" not in output
    assert '"semantic_frame"' not in output


def test_ops_mode_prints_tool_loop_trace_without_reasoning_summary(capsys):
    """Ops mode should show audit-friendly tool-loop progress, not reasoning summary."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingToolThenAnswerLLM()
        executor = RecordingToolExecutor()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="ops",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops"),
            session=session,
            llm=llm,
            tool_executor=executor,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("帮我看看有哪些平台", history, 0)

    output = capsys.readouterr().out
    assert "目标：运行态盘点" in output
    assert "模式：只读诊断" in output
    assert "正在检索知识库：Klonet 启动" in output
    assert "观察：unexpected tool result" in output
    assert "正在调用工具" not in output
    assert "工具完成" not in output
    assert "工具结果摘要" not in output
    assert "下一步：" not in output
    assert "思考摘要" not in output


def test_ops_progress_uses_route_summary_not_generic_task_type(capsys):
    """Ops progress should show operational goal/slots, not Mentor task_type labels."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    user_input = (
        "我在 `/home/adminis/lht/102_project` 里再次启动 `web_terminal_main.py`，"
        "报 address already in use。请精确确认占用 5045 的 PID、命令和 cwd；"
        "不要仅凭 screen 存在下结论，也不要修改环境。"
    )
    with local_temp_dir() as temp_dir:
        llm = StreamingAnswerLLM()
        executor = RecordingToolExecutor()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="ops",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops"),
            session=session,
            llm=llm,
            tool_executor=executor,
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        orchestrator.single_chat(user_input, history, 0)

    output = capsys.readouterr().out
    assert "目标：端口占用诊断" in output
    assert "线索：port=5045" in output
    assert "component=web_terminal_main.py" in output
    assert "模式：只读诊断" in output
    assert "已识别：code_lookup" not in output


def test_ops_visible_tools_are_not_narrowed_by_generic_scope():
    """Ops should route by operational tools, not by Mentor-style general scope."""

    from klonet_agent.agents import get_profile
    from klonet_agent.knowledge import route_query
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator._query_route = route_query("hello")

    tool_names = {
        tool["function"]["name"]
        for tool in orchestrator._visible_tools()
    }

    assert "inspect_klonet_runtime" in tool_names
    assert "inspect_process_detail" in tool_names
    assert "read_project_journal" in tool_names


def test_ops_scope_message_injects_tool_routing_plan():
    """Ops scope message should expose deterministic tool routing hints."""

    from klonet_agent.agents import get_profile
    from klonet_agent.knowledge import route_query
    from klonet_agent.ops.routing import route_ops_request
    from klonet_agent.orchestrator import AgentOrchestrator

    user_input = (
        "我在 `/home/adminis/lht/102_project` 里再次启动 `web_terminal_main.py`，"
        "报 address already in use。请精确确认占用 5045 的 PID、命令和 cwd。"
    )
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator._query_route = route_query(user_input)
    orchestrator._turn_intent = None
    orchestrator._intent_decision = None
    orchestrator._ops_route = route_ops_request(user_input)

    content = orchestrator._build_turn_scope_message(user_input)["content"]

    assert "Ops tool routing" in content
    assert "goal:" in content
    assert "port_conflict" in content
    assert "recommended_tools: inspect_process_detail, inspect_klonet_runtime" in content
    assert 'tool_args_hint: inspect_process_detail {"ports":[5045]}' in content
    assert "confirm realtime listener PID/cmd/cwd" in content


def test_ops_tool_action_uses_safe_arguments_only():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"

    assert orchestrator._format_tool_action(
        "search_code",
        {"query": "vemu_frontend", "token": "secret"},
    ) == "正在搜索源码：vemu_frontend"
    assert orchestrator._format_tool_action(
        "inspect_screen_session",
        {"session": "102_m", "password": "secret"},
    ) == "正在检查 screen 会话：102_m"
    assert orchestrator._format_tool_action(
        "unknown_tool",
        {"password": "secret"},
    ) == "正在执行工具：unknown_tool"


def test_ops_operation_plan_actions_are_named_explicitly():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"

    assert orchestrator._format_tool_action(
        "create_ops_operation_plan",
        {
            "operation": "restart_platform",
            "target": "102",
            "secret": "hidden",
        },
    ) == "Ops plan: create restart_platform for 102"
    assert orchestrator._format_tool_action(
        "approve_ops_operation_plan",
        {"plan_id": "restart-abc", "scope": "plan", "token": "hidden"},
    ) == "Ops plan: approve plan restart-abc"
    assert orchestrator._format_tool_action(
        "execute_ops_next_step",
        {"plan_id": "restart-abc", "password": "hidden"},
    ) == "Ops plan: execute next step for restart-abc"
    assert orchestrator._format_tool_action(
        "execute_ops_operation_step",
        {"plan_id": "restart-abc", "step_id": "restart-master"},
    ) == "Ops plan: execute restart-master for restart-abc"


def test_ops_observation_shows_three_real_lines_and_omission():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"
    lines, omitted = orchestrator._tool_observation_lines(
        "inspect_klonet_runtime",
        "\n".join(
            [
                "inspect_klonet_runtime",
                "- redis: detected - active",
                "- docker: detected - redis_102 Up 6 days",
                "- ports: detected - 0.0.0.0:12000",
                "- screen: detected - 102_m",
            ]
        ),
    )

    assert lines == [
        "- redis: detected - active",
        "- docker: detected - redis_102 Up 6 days",
        "- ports: detected - 0.0.0.0:12000",
    ]
    assert omitted is True


def test_ops_knowledge_observation_prioritizes_source_and_evidence():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"
    lines, omitted = orchestrator._tool_observation_lines(
        "search_knowledge",
        "\n".join(
            [
                "检索到以下可靠 Klonet 证据：",
                "- retrieval_status: reliable",
                "- confidence: 1.0",
                "- route_scope: klonet",
                "",
                "[1] knowledge/klonet/architecture/frontend.md / Web 前端",
                "- layer: curated",
                "- path: knowledge/klonet/architecture/frontend.md",
                "- snippet:",
                "vemu_frontend 是 Klonet 平台的 Web 前端，负责展示平台页面。",
                "",
                "[2] knowledge/klonet/ops/startup.md / 启动",
                "- layer: curated",
                "- path: knowledge/klonet/ops/startup.md",
                "- snippet:",
                "后端核心进程通过 screen 会话启动。",
            ]
        ),
    )

    assert lines == [
        "- 来源：knowledge/klonet/architecture/frontend.md",
        "- 证据：vemu_frontend 是 Klonet 平台的 Web 前端，负责展示平台页面。",
        "- 另有 1 条证据已省略",
    ]
    assert omitted is False


def test_ops_observation_handles_empty_error_and_long_lines():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"

    assert orchestrator._tool_observation_lines("search_code", "") == (
        ["工具未返回可展示结果。"],
        False,
    )
    assert orchestrator._tool_observation_lines(
        "search_code",
        "Error: source index unavailable",
    ) == (["失败：source index unavailable"], False)
    lines, omitted = orchestrator._tool_observation_lines(
        "search_code",
        "x" * 200,
    )
    assert lines == ["x" * 157 + "..."]
    assert omitted is False


def test_ops_injects_deterministic_environment_plan_before_final_answer(capsys):
    """Ops final answers should be constrained by a deterministic environment plan."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = OpsPlanInjectionLLM()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="ops",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops"),
            session=session,
            llm=llm,
            tool_executor=StaticOpsExecutor(),
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("我怎么启动 Klonet", history, 0)

    final_messages = _answer_calls(llm)[1]["messages"]
    plan_messages = [
        message["content"]
        for message in final_messages
        if message["role"] == "system"
        and "Ops deterministic environment plan" in message["content"]
    ]

    assert plan_messages
    assert "step=redis action=skip" in plan_messages[0]
    assert "step=docker action=skip" in plan_messages[0]
    assert "step=gunicorn action=verify" in plan_messages[0]


def test_init_history_scopes_shared_ops_memory_by_mode():
    """Mentor history should not receive shared runtime environment memories."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        memory_store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        memory_store.append_shared_episode(
            "tool_observation: inspect_klonet_runtime found 102_m and lht_m"
        )
        memory_store.write_shared_ops_baseline("Ubuntu 22.04; nginx active")

        mentor = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=AgentSession(
                user_id="u1",
                project_id="p1",
                mode="mentor",
                workspace_path=temp_dir / "workspace",
                journal_path=temp_dir / "journal.md",
            ),
            llm=FakeLLM(),
            trace_logger=TraceLogger(temp_dir / "mentor_trace.jsonl"),
            memory_store=memory_store,
        )
        ops = AgentOrchestrator(
            profile=get_profile("ops"),
            session=AgentSession(
                user_id="u1",
                project_id="p1",
                mode="ops",
                workspace_path=temp_dir / "workspace",
                journal_path=temp_dir / "journal.md",
            ),
            llm=FakeLLM(),
            trace_logger=TraceLogger(temp_dir / "ops_trace.jsonl"),
            memory_store=memory_store,
        )

        mentor_context = "\n".join(
            message.get("content", "") for message in mentor.init_history()
        )
        ops_context = "\n".join(
            message.get("content", "") for message in ops.init_history()
        )

    assert "102_m and lht_m" not in mentor_context
    assert "Ubuntu 22.04" not in mentor_context
    assert "102_m and lht_m" in ops_context
    assert "Ubuntu 22.04" in ops_context


def test_ops_plan_does_not_split_assistant_tool_response_pair(capsys, monkeypatch):
    """OpenAI requires assistant tool_calls to be followed directly by tool messages."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    def planner_after_first_tool(*, user_input, operation, tool_events):
        if not tool_events:
            return ""
        return "【Ops deterministic environment plan】\nstep=ports action=verify"

    monkeypatch.setattr(
        "klonet_agent.orchestrator.build_ops_environment_plan",
        planner_after_first_tool,
    )

    with local_temp_dir() as temp_dir:
        llm = MultiToolOpsPlanLLM()
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="ops",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops"),
            session=session,
            llm=llm,
            tool_executor=MultiToolStaticOpsExecutor(),
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
            intent_analyzer=PlatformStartIntentAnalyzer(),
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("鎴戞€庝箞鍚姩 Klonet", history, 0)

    final_messages = _answer_calls(llm)[1]["messages"]
    assistant_index = next(
        index
        for index, message in enumerate(final_messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    tool_call_count = len(final_messages[assistant_index]["tool_calls"])
    following_roles = [
        message.get("role")
        for message in final_messages[
            assistant_index + 1 : assistant_index + 1 + tool_call_count
        ]
    ]

    assert following_roles == ["tool"] * tool_call_count


def test_ops_tool_observation_is_appended_to_shared_memory(capsys):
    """Ops tool observations should be reusable across users via shared memory."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    class RuntimeExecutor:
        def run(self, tool_name, tool_args):
            return "inspect_klonet_runtime\n- screen: detected - 102_m lht_m"

    class RuntimeThenAnswerLLM:
        def __init__(self):
            self.calls = []
            self.answer_calls = 0

        def complete(self, messages, tools, stream=False):
            self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
            self.answer_calls += 1
            if self.answer_calls == 1:
                return iter(
                    [
                        _stream_chunk(
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="runtime-1",
                                    function=SimpleNamespace(
                                        name="inspect_klonet_runtime",
                                        arguments='{"checks":["screen"]}',
                                    ),
                                )
                            ],
                            finish_reason="tool_calls",
                            usage=SimpleNamespace(total_tokens=4),
                        )
                    ]
                )
            return iter(
                [
                    _stream_chunk(content="当前有 102 和 lht。"),
                    _stream_chunk(finish_reason="stop", usage=SimpleNamespace(total_tokens=6)),
                ]
            )

    with local_temp_dir() as temp_dir:
        memory_store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="ops",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops"),
            session=session,
            llm=RuntimeThenAnswerLLM(),
            tool_executor=RuntimeExecutor(),
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=memory_store,
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("看看有哪些平台", history, 0)

        shared = memory_store.read_shared_memory()

        assert "## Ops 诊断记录" in shared
        assert "- question: 看看有哪些平台" in shared
        assert "inspect_klonet_runtime" in shared
        assert "102_m lht_m" in shared
        assert "- conclusion: 当前有 102 和 lht。" in shared


def test_brief_mode_prints_only_final_answer(capsys):
    """简短模式不输出思考摘要。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingAnswerLLM()
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
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
            answer_style="brief",
        )
        history = orchestrator.init_history()
        _, history, _ = orchestrator.single_chat("解释 Klonet", history, 0)

    output = capsys.readouterr().out
    assert "思考摘要" not in output
    assert "正在思考" not in output
    assert f"Klonet Agent：{history[-1]['content']}" in output


def test_single_chat_streams_tool_call_then_final_answer(capsys):
    """流式模式下仍能聚合 tool call 参数、执行工具，再流式输出最终回答。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingToolThenAnswerLLM()
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
        _, history, token = orchestrator.single_chat("我要启动 Klonet", history, 0)

    output = capsys.readouterr().out
    assert executor.calls[0][0] == "search_knowledge"
    assert executor.calls[0][1]["query"] == "Klonet 启动"
    assert executor.calls[0][1]["intent"]["operation"] == "platform_start"
    assert executor.calls[0][1]["conversation_state"]["deployment_phase"] == "platform_startup"
    assert executor.calls[0][1]["conversation_state"]["current_topic"] == "klonet_platform_start"
    assert "Klonet Agent：启动命令如下" in output
    assert history[-1]["content"] == "启动命令如下"
    assert token == 17
    assert [call["stream"] for call in _answer_calls(llm)] == [True, True]


def test_stream_timeout_falls_back_to_non_stream_answer(capsys):
    """If stream connection times out, retry once without stream instead of crashing."""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = StreamingTimeoutThenNonStreamLLM()
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
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
        )
        history = orchestrator.init_history()
        _, history, token = orchestrator.single_chat("hello", history, 0)

    output = capsys.readouterr().out
    assert "Klonet Agent：fallback answer" in output
    assert history[-1]["content"] == "fallback answer"
    assert token == 11
    assert [call["stream"] for call in _answer_calls(llm)] == [True, False]


def test_continue_after_tool_limit_resumes_paused_turn_before_clarification(
    capsys,
    monkeypatch,
):
    """工具上限暂停后，“继续”应恢复上一轮，不触发部署澄清。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    monkeypatch.setattr("klonet_agent.orchestrator.MAX_TOOL_ROUNDS", 1)

    with local_temp_dir() as temp_dir:
        llm = PauseThenAnswerLLM()
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
            intent_analyzer=ClarifyingContinueIntentAnalyzer(),
        )
        history = orchestrator.init_history()
        _, history, token = orchestrator.single_chat("介绍一下卫星平台", history, 0)
        paused_output = capsys.readouterr().out

        reply, history, token = orchestrator.single_chat("继续", history, token)

    output = capsys.readouterr().out
    assert "本轮工具调用已达到上限" in paused_output
    assert "首次安装 Klonet 环境" not in output
    assert reply == "继续上一轮卫星平台介绍。"
    assert history[-1]["content"] == "继续上一轮卫星平台介绍。"


def test_continue_after_normal_answer_resumes_last_turn_before_clarification(
    capsys,
):
    """即使没有工具上限暂停，“继续”也应优先延续上一轮主题。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        llm = PauseThenAnswerLLM()
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
            trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
            intent_analyzer=ClarifyingContinueIntentAnalyzer(),
        )
        history = orchestrator.init_history()
        _, history, token = orchestrator.single_chat("介绍一下卫星平台", history, 0)
        capsys.readouterr()

        reply, history, token = orchestrator.single_chat("继续", history, token)

    output = capsys.readouterr().out
    assert "首次安装 Klonet 环境" not in output
    assert reply == "继续上一轮卫星平台介绍。"
