from types import SimpleNamespace

from tests.helpers import local_temp_dir


class ShouldNotCallLLM:
    def __init__(self):
        self.calls = []

    def complete(self, messages, tools, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        raise AssertionError("LLM should not be called when clarification is required")


class RecordingToolExecutor:
    def __init__(self):
        self.calls = []

    def run(self, tool_name, tool_args):
        self.calls.append((tool_name, tool_args))
        return "unexpected"


class SequentialLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools=None, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        return self.responses.pop(0)


def _response(content, tokens=3):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(total_tokens=tokens),
    )


def _mentor_orchestrator(temp_dir, llm=None, executor=None):
    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    session = AgentSession(
        user_id="u1",
        project_id="p1",
        mode="mentor",
        workspace_path=temp_dir / "workspace",
        journal_path=temp_dir / "journal.md",
    )
    return AgentOrchestrator(
        profile=get_profile("mentor"),
        session=session,
        llm=llm or ShouldNotCallLLM(),
        tool_executor=executor,
        trace_logger=TraceLogger(temp_dir / "trace.jsonl"),
        memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
    )


def test_query_intent_keeps_clarification_fields():
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "deployment_guidance",
            "operation": "unknown",
            "target": "klonet_platform",
            "symptom": "address_already_in_use",
            "requires_retrieval": False,
            "clarification_required": True,
            "clarification_question": "安装环境还是启动平台？",
            "confidence": 0.41,
        }
    )

    assert intent.task_type == "deployment_guidance"
    assert intent.symptom == "address_already_in_use"
    assert intent.requires_retrieval is False
    assert intent.clarification_required is True
    assert intent.clarification_question == "安装环境还是启动平台？"


def test_ambiguous_klonet_deploy_asks_clarification_without_llm_or_tools(capsys):
    with local_temp_dir() as temp_dir:
        llm = ShouldNotCallLLM()
        executor = RecordingToolExecutor()
        orchestrator = _mentor_orchestrator(temp_dir, llm=llm, executor=executor)
        history = orchestrator.init_history()
        reply, history, token = orchestrator.single_chat("怎么部署 Klonet？", history, 0)

    output = capsys.readouterr().out
    assert "安装基础环境" in reply
    assert "启动已经安装好的 Klonet 平台服务" in reply
    assert reply in output
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == reply
    assert token == 0
    assert llm.calls == []
    assert executor.calls == []


def test_mentor_runs_structured_intent_analysis_before_tool_enabled_answer():
    with local_temp_dir() as temp_dir:
        llm = SequentialLLM(
            [
                _response(
                    '{"scope":"klonet","task_type":"troubleshooting",'
                    '"operation":"platform_start","target":"web_terminal",'
                    '"symptom":"address_already_in_use",'
                    '"requires_retrieval":true,'
                    '"clarification_required":false,'
                    '"confidence":0.92}',
                    tokens=5,
                ),
                _response("端口已被占用，先用 ss -lntp 找占用进程。", tokens=7),
            ]
        )
        orchestrator = _mentor_orchestrator(
            temp_dir,
            llm=llm,
            executor=RecordingToolExecutor(),
        )
        history = orchestrator.init_history()
        reply, history, token = orchestrator.single_chat(
            "启动 web-terminal 的时候报 address already in use，为什么？",
            history,
            0,
        )

    assert reply.startswith("端口已被占用")
    assert token == 12
    assert len(llm.calls) == 2
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["stream"] is False
    assert "只输出 JSON" in llm.calls[0]["messages"][0]["content"]
    assert llm.calls[1]["tools"] is not None
    assert llm.calls[1]["stream"] is True




def test_search_tool_inherits_front_loaded_intent_instead_of_reinterpreting():
    """search_knowledge should use the front-loaded intent as the single source of truth."""

    with local_temp_dir() as temp_dir:
        llm = SequentialLLM(
            [
                _response(
                    '{"scope":"klonet","task_type":"deployment_guidance",'
                    '"operation":"platform_start","target":"klonet_platform",'
                    '"excluded_intents":["environment_setup"],'
                    '"requires_retrieval":true,"confidence":0.95}',
                    tokens=5,
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                tool_calls=[
                                    SimpleNamespace(
                                        id="search-1",
                                        function=SimpleNamespace(
                                            name="search_knowledge",
                                            arguments=(
                                                '{"query":"Klonet 环境安装",'
                                                '"intent":{"scope":"klonet",'
                                                '"task_type":"deployment_preparation",'
                                                '"operation":"environment_setup",'
                                                '"confidence":0.99}}'
                                            ),
                                        ),
                                    )
                                ],
                            )
                        )
                    ],
                    usage=SimpleNamespace(total_tokens=7),
                ),
                _response("按启动路径回答。", tokens=3),
            ]
        )
        executor = RecordingToolExecutor()
        orchestrator = _mentor_orchestrator(temp_dir, llm=llm, executor=executor)
        history = orchestrator.init_history()
        orchestrator.single_chat("不是配置环境，我是想启动 Klonet", history, 0)

    assert executor.calls[0][0] == "search_knowledge"
    assert executor.calls[0][1]["intent"]["operation"] == "platform_start"
    assert executor.calls[0][1]["intent"]["excluded_intents"] == ["environment_setup"]


def test_context_reference_does_not_trigger_memoryless_clarification(capsys):
    """用户引用上一轮选项时，前置澄清不能失忆式追问。"""

    with local_temp_dir() as temp_dir:
        llm = SequentialLLM(
            [
                _response(
                    '{"scope":"klonet","task_type":"general",'
                    '"operation":"unknown","target":"",'
                    '"requires_retrieval":false,'
                    '"clarification_required":true,'
                    '"clarification_question":"你提到的第一种是指什么？",'
                    '"confidence":0.42}',
                    tokens=5,
                ),
                _response("普通用户直接在浏览器打开 Klonet 地址，登录后按实验流程使用。", tokens=7),
            ]
        )
        orchestrator = _mentor_orchestrator(
            temp_dir,
            llm=llm,
            executor=RecordingToolExecutor(),
        )
        history = orchestrator.init_history()
        history.append(
            {
                "role": "assistant",
                "content": (
                    "场景一：你在浏览器里用 Klonet（普通用户）。\n"
                    "场景二：你要在自己的电脑上部署运行 Klonet（管理员/开发者）。"
                ),
            }
        )
        reply, history, token = orchestrator.single_chat(
            "是第一种，我怎么使用",
            history,
            0,
        )

    assert "第一种是指什么" not in reply
    assert "浏览器" in reply
    assert len(llm.calls) == 2
    assert "场景一" in llm.calls[0]["messages"][1]["content"]
    assert token == 12


def test_credential_question_uses_safety_boundary_without_llm_or_retrieval(capsys):
    with local_temp_dir() as temp_dir:
        llm = ShouldNotCallLLM()
        executor = RecordingToolExecutor()
        orchestrator = _mentor_orchestrator(temp_dir, llm=llm, executor=executor)
        history = orchestrator.init_history()
        reply, history, token = orchestrator.single_chat("虚拟机用户名和密码是什么？", history, 0)

    output = capsys.readouterr().out
    assert "不能" in reply
    assert "明文" in reply
    assert "账号" in reply or "密码" in reply
    assert reply in output
    assert history[-1]["content"] == reply
    assert token == 0
    assert llm.calls == []
    assert executor.calls == []
