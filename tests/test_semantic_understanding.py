"""Semantic understanding and decision planning for Klonet mentor turns."""

from klonet_agent.knowledge.semantic_understanding import (
    SemanticDecisionPlanner,
    SemanticFrame,
    SemanticState,
)


def test_local_computer_question_routes_to_operator_tool_preparation():
    frame = SemanticFrame.from_mapping(
        {
            "scope": "mixed",
            "user_role": "learner",
            "perspective": "asking_about_own_pc",
            "machine_role": "operator_local_pc",
            "deployment_phase": "local_tool_preparation",
            "action_goal": "prepare_tools",
            "target_component": "operator_computer",
            "ambiguity": {"level": "low"},
            "confidence": 0.91,
        }
    )

    decision = SemanticDecisionPlanner().plan(
        "部署平台之前，电脑里需要下载什么软件吗？",
        frame,
        SemanticState(),
    )

    assert decision.intent.task_type == "deployment_preparation"
    assert decision.intent.operation == "unknown"
    assert decision.intent.target == "operator_local_pc"
    assert decision.clarification_action == "none"
    assert decision.answer_mode == "local_tool_preparation"


def test_bare_deploy_platform_defaults_to_startup_with_soft_note():
    frame = SemanticFrame.from_mapping(
        {
            "scope": "klonet",
            "user_role": "operator",
            "perspective": "operating_target_machine",
            "machine_role": "unspecified",
            "deployment_phase": "unknown",
            "action_goal": "start_services",
            "target_component": "klonet_platform",
            "ambiguity": {
                "level": "medium",
                "candidates": ["platform_startup", "environment_setup"],
                "defaultable": True,
            },
            "confidence": 0.74,
        }
    )

    decision = SemanticDecisionPlanner().plan(
        "我怎么部署平台",
        frame,
        SemanticState(),
    )

    assert decision.intent.operation == "platform_start"
    assert decision.intent.excluded_intents == ()
    assert decision.clarification_action == "soft_note"
    assert "首次安装环境" in decision.soft_note
    assert decision.intent.clarification_required is False


def test_topology_deploy_is_not_confused_with_platform_startup():
    frame = SemanticFrame.from_mapping(
        {
            "scope": "klonet",
            "user_role": "learner",
            "perspective": "using_platform",
            "machine_role": "unspecified",
            "deployment_phase": "topology_deploy",
            "action_goal": "deploy_topology",
            "target_component": "topology",
            "symptom": "progress_stuck",
            "ambiguity": {"level": "low"},
            "confidence": 0.93,
        }
    )

    decision = SemanticDecisionPlanner().plan(
        "前端怎么部署拓扑出错了？",
        frame,
        SemanticState(),
    )

    assert decision.intent.task_type == "troubleshooting"
    assert decision.intent.operation == "topology_deploy"
    assert decision.intent.target == "topology"
    assert decision.clarification_action == "none"


def test_context_state_resolves_first_option_to_platform_usage():
    state = SemanticState.from_history(
        [
            {
                "role": "assistant",
                "content": (
                    "场景一：你在浏览器里用 Klonet（普通用户）。\n"
                    "场景二：你要在自己的电脑上部署运行 Klonet（管理员/开发者）。"
                ),
            }
        ]
    )
    frame = SemanticFrame.from_mapping(
        {
            "scope": "klonet",
            "context_refs": ["第一种"],
            "deployment_phase": "unknown",
            "action_goal": "use_feature",
            "target_component": "klonet_platform",
            "ambiguity": {"level": "medium"},
            "confidence": 0.6,
        }
    )

    decision = SemanticDecisionPlanner().plan("第一种怎么使用", frame, state)

    assert decision.intent.task_type == "operation_guide"
    assert decision.intent.operation == "unknown"
    assert decision.intent.target == "klonet_platform"
    assert decision.answer_mode == "platform_usage"
    assert decision.clarification_action == "none"
    assert "platform_start" in decision.intent.excluded_intents
    assert "environment_setup" in decision.intent.excluded_intents


def test_context_state_resolves_b_option_to_platform_startup():
    state = SemanticState.from_history(
        [
            {
                "role": "assistant",
                "content": (
                    "A：服务器是全新的，从来没装过 Klonet，需要首次环境部署。\n"
                    "B：服务器上已经有 Klonet，只是没启动，需要重新拉起 Redis、Master、Celery、Worker、Nginx。"
                ),
            }
        ]
    )
    frame = SemanticFrame.from_mapping(
        {
            "scope": "klonet",
            "context_refs": ["B"],
            "deployment_phase": "unknown",
            "action_goal": "unknown",
            "target_component": "klonet_platform",
            "ambiguity": {"level": "medium"},
            "confidence": 0.55,
        }
    )

    decision = SemanticDecisionPlanner().plan("B", frame, state)

    assert decision.intent.task_type == "deployment_guidance"
    assert decision.intent.operation == "platform_start"
    assert decision.intent.target == "klonet_platform"
    assert decision.clarification_action == "none"


def test_intent_analyzer_accepts_semantic_frame_output_from_model():
    from types import SimpleNamespace

    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer

    class FakeLLM:
        def complete(self, messages, tools=None, stream=False):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"scope":"mixed",'
                                '"perspective":"asking_about_own_pc",'
                                '"machine_role":"operator_local_pc",'
                                '"deployment_phase":"local_tool_preparation",'
                                '"action_goal":"prepare_tools",'
                                '"target_component":"operator_computer",'
                                '"ambiguity":{"level":"low"},'
                                '"confidence":0.9}'
                            )
                        )
                    )
                ],
                usage=SimpleNamespace(total_tokens=9),
            )

    analysis = IntentAnalyzer(FakeLLM()).analyze(
        "部署平台之前，电脑里需要下载什么软件吗？"
    )

    assert analysis.intent.task_type == "deployment_preparation"
    assert analysis.intent.target == "operator_local_pc"
    assert analysis.intent.operation == "unknown"
    assert analysis.intent.excluded_intents == ("environment_setup", "dependency_install")
    assert analysis.token_usage == 9


def test_intent_analyzer_injects_retrieved_intent_cases():
    from types import SimpleNamespace

    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer

    class RecordingLLM:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=None, stream=False):
            self.calls.append({"messages": messages, "tools": tools, "stream": stream})
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"scope":"klonet",'
                                '"context_refs":["B"],'
                                '"deployment_phase":"unknown",'
                                '"target_component":"klonet_platform",'
                                '"confidence":0.6}'
                            )
                        )
                    )
                ],
                usage=SimpleNamespace(total_tokens=11),
            )

    llm = RecordingLLM()
    IntentAnalyzer(llm).analyze(
        "B",
        recent_history=[
            {
                "role": "assistant",
                "content": (
                    "A：服务器是全新的，需要首次环境部署。\n"
                    "B：服务器上已经有 Klonet，只是没启动，需要平台启动。"
                ),
            }
        ],
    )

    user_message = llm.calls[0]["messages"][1]["content"]
    assert "platform_start_after_b_option" in user_message
    assert "继承上文 B 选项" in user_message
    assert "semantic_frame" in user_message


def test_intent_analyzer_does_not_inject_conflicting_intent_cases(tmp_path):
    import json
    from types import SimpleNamespace

    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
    from klonet_agent.knowledge.intent_cases import IntentCaseRetriever

    case_file = tmp_path / "cases.jsonl"
    case_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "case_id": "deploy_means_startup",
                        "tag": "intent_parse",
                        "latest_query": "deploy platform",
                        "semantic_frame": {"deployment_phase": "platform_startup"},
                        "intent": {"operation": "platform_start"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "case_id": "deploy_means_environment_setup",
                        "tag": "intent_parse",
                        "latest_query": "deploy platform",
                        "semantic_frame": {"deployment_phase": "environment_setup"},
                        "intent": {"operation": "environment_setup"},
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    class RecordingLLM:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=None, stream=False):
            self.calls.append({"messages": messages, "tools": tools, "stream": stream})
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"scope":"klonet",'
                                '"deployment_phase":"platform_startup",'
                                '"target_component":"klonet_platform",'
                                '"confidence":0.7}'
                            )
                        )
                    )
                ],
                usage=SimpleNamespace(total_tokens=11),
            )

    llm = RecordingLLM()
    IntentAnalyzer(
        llm,
        intent_case_retriever=IntentCaseRetriever(root=tmp_path),
    ).analyze("deploy platform")

    user_message = llm.calls[0]["messages"][1]["content"]
    assert "Intent Case" not in user_message
    assert "deploy_means_startup" not in user_message
    assert "deploy_means_environment_setup" not in user_message
