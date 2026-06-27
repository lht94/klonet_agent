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


def test_bare_klonet_deploy_no_longer_stops_before_semantic_analysis(capsys):
    with local_temp_dir() as temp_dir:
        llm = SequentialLLM(
            [
                _response(
                    '{"scope":"klonet","deployment_phase":"unknown",'
                    '"action_goal":"start_services",'
                    '"target_component":"klonet_platform",'
                    '"ambiguity":{"level":"medium","defaultable":true},'
                    '"confidence":0.74}',
                    tokens=5,
                ),
                _response(
                    "先按启动已安装好的 Klonet 平台服务理解；如果你指首次安装环境，流程不同。",
                    tokens=7,
                ),
            ]
        )
        executor = RecordingToolExecutor()
        orchestrator = _mentor_orchestrator(temp_dir, llm=llm, executor=executor)
        history = orchestrator.init_history()
        reply, history, token = orchestrator.single_chat("怎么部署 Klonet？", history, 0)

    output = capsys.readouterr().out
    assert "先按启动" in reply
    assert "首次安装环境" in reply
    assert reply in output
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == reply
    assert token == 12
    assert len(llm.calls) == 2
    assert "这里先按启动已安装好的 Klonet 平台服务理解" in "\n".join(
        message["content"] for message in llm.calls[1]["messages"]
    )
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


def test_late_platform_name_supplement_continues_previous_usage_question(capsys):
    """短补充说明应补全上一轮问题，而不是开启新的部署澄清分支。"""

    with local_temp_dir() as temp_dir:
        llm = SequentialLLM(
            [
                _response(
                    '{"scope":"mixed","user_role":"learner",'
                    '"perspective":"asking_about_own_pc",'
                    '"machine_role":"operator_local_pc",'
                    '"deployment_phase":"local_tool_preparation",'
                    '"action_goal":"prepare_tools",'
                    '"target_component":"operator_computer",'
                    '"excluded_meanings":["environment_setup","platform_startup"],'
                    '"ambiguity":{"level":"low"},'
                    '"confidence":0.9}',
                    tokens=5,
                ),
                _response(
                    "你如果只是作为普通用户使用 Klonet 平台，电脑上有浏览器即可。\n"
                    "场景一：你在浏览器里用 Klonet（普通用户）。\n"
                    "场景二：你要部署运行 Klonet（管理员/开发者）。",
                    tokens=7,
                ),
                _response(
                    '{"scope":"klonet","user_role":"learner",'
                    '"perspective":"using_platform",'
                    '"machine_role":"unspecified",'
                    '"deployment_phase":"platform_usage",'
                    '"action_goal":"use_feature",'
                    '"target_component":"klonet_platform",'
                    '"excluded_meanings":["environment_setup","platform_startup"],'
                    '"ambiguity":{"level":"low","resolved_by_context":true},'
                    '"confidence":0.86}',
                    tokens=5,
                ),
                _response("继续按普通用户浏览器使用 Klonet 平台回答。", tokens=7),
            ]
        )
        orchestrator = _mentor_orchestrator(
            temp_dir,
            llm=llm,
            executor=RecordingToolExecutor(),
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("我在使用平台之前，电脑里需要下载什么软件吗？", history, 0)
        reply, history, token = orchestrator.single_chat("klonet平台", history, 12)

    assert "首次安装" not in reply
    assert "启动已经安装" not in reply
    assert "普通用户" in reply or "浏览器" in reply
    assert len(llm.calls) == 4
    second_intent_prompt = llm.calls[2]["messages"][1]["content"]
    assert "电脑里需要下载什么软件" in second_intent_prompt


def test_late_platform_name_supplement_ignores_model_deploy_clarification(capsys):
    """模型把短补充误判为部署歧义时，策略层应按上下文继续。"""

    with local_temp_dir() as temp_dir:
        llm = SequentialLLM(
            [
                _response(
                    '{"scope":"klonet","task_type":"deployment_guidance",'
                    '"operation":"unknown","target":"klonet_platform",'
                    '"clarification_required":true,'
                    '"clarification_question":"你是想首次安装 Klonet 环境，还是启动已经安装好的平台服务？",'
                    '"confidence":0.7}',
                    tokens=5,
                ),
                _response("继续回答普通用户浏览器使用 Klonet 平台。", tokens=7),
            ]
        )
        orchestrator = _mentor_orchestrator(
            temp_dir,
            llm=llm,
            executor=RecordingToolExecutor(),
        )
        history = orchestrator.init_history()
        history.extend(
            [
                {
                    "role": "user",
                    "content": "我在使用平台之前，电脑里需要下载什么软件吗？",
                },
                {
                    "role": "assistant",
                    "content": (
                        "你如果只是作为普通用户使用，电脑上有浏览器即可。\n"
                        "场景一：你在浏览器里用平台（普通用户）。\n"
                        "场景二：你要部署运行平台（管理员/开发者）。"
                    ),
                },
            ]
        )

        reply, history, token = orchestrator.single_chat("klonet平台", history, 0)

    assert "首次安装" not in reply
    assert "启动已经安装" not in reply
    assert "浏览器使用" in reply
    assert len(llm.calls) == 2
    assert token == 12


def test_b_option_inherits_platform_start_and_runtime_path_guard(capsys):
    """用户只回答 B 时，应继承上文平台启动路线，并保留运行路径验证约束。"""

    with local_temp_dir() as temp_dir:
        llm = SequentialLLM(
            [
                _response(
                    '{"scope":"klonet","context_refs":["B"],'
                    '"deployment_phase":"unknown",'
                    '"action_goal":"unknown",'
                    '"target_component":"klonet_platform",'
                    '"ambiguity":{"level":"medium"},'
                    '"confidence":0.55}',
                    tokens=5,
                ),
                _response("按平台启动路线回答，并先验证 gunicorn 路径。", tokens=7),
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
                    "A：服务器是全新的，从来没装过 Klonet，需要首次环境部署。\n"
                    "B：服务器上已经有 Klonet，只是没启动，需要重新拉起 Redis、Master、Celery、Worker、Nginx。"
                ),
            }
        )

        reply, history, token = orchestrator.single_chat("B", history, 0)

    assert "平台启动路线" in reply
    assert token == 12
    second_call_messages = "\n".join(
        message["content"] for message in llm.calls[1]["messages"]
    )
    assert "command -v gunicorn" in second_call_messages
    assert "只执行当前机器实际存在的一套命令" in second_call_messages


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
