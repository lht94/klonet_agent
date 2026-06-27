"""Conversation state and retrieval query planning."""


def test_conversation_state_resolves_recent_b_option():
    from klonet_agent.knowledge.conversation_state import ConversationStateManager

    state = ConversationStateManager().from_turn(
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

    assert state.deployment_phase == "platform_startup"
    assert state.machine_role == "target_server"
    assert state.confirmed_slots["selected_option"] == "B"
    assert state.last_option_map["B"] == "platform_startup"


def test_conversation_state_treats_short_platform_name_as_late_usage_supplement():
    from klonet_agent.knowledge.conversation_state import ConversationStateManager

    state = ConversationStateManager().from_turn(
        "klonet平台",
        recent_history=[
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
        ],
    )

    assert state.deployment_phase == "platform_usage"
    assert state.current_topic == "klonet_platform_usage"
    assert state.confirmed_slots["platform_name"] == "klonet"
    assert "platform_startup" in state.excluded_meanings
    assert "environment_setup" in state.excluded_meanings


def test_query_builder_keeps_original_query_and_structured_signals():
    from klonet_agent.knowledge.conversation_state import ConversationState
    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.query_builder import QueryBuilder
    from klonet_agent.knowledge.semantic_understanding import SemanticFrame

    intent = QueryIntent.from_mapping(
        {
            "scope": "mixed",
            "task_type": "deployment_preparation",
            "operation": "unknown",
            "target": "operator_local_pc",
            "excluded_intents": ["environment_setup", "dependency_install"],
            "confidence": 0.91,
        }
    )
    frame = SemanticFrame.from_mapping(
        {
            "machine_role": "operator_local_pc",
            "deployment_phase": "local_tool_preparation",
            "action_goal": "prepare_tools",
            "target_component": "operator_computer",
        }
    )
    state = ConversationState(
        machine_role="operator_local_pc",
        deployment_phase="local_tool_preparation",
    )

    plan = QueryBuilder().build(
        "部署平台之前，电脑里需要下载什么软件吗？",
        intent=intent,
        semantic_frame=frame,
        conversation_state=state,
        top_k=3,
    )

    assert "部署平台之前，电脑里需要下载什么软件吗？" in plan.query
    assert "operator_local_pc" in plan.query
    assert "SSH" in plan.query
    assert "VS Code" in plan.query
    assert "Docker Redis MySQL" not in plan.query
    assert plan.excluded_intents == ("environment_setup", "dependency_install")


def test_knowledge_base_uses_query_builder_plan():
    from klonet_agent.knowledge.conversation_state import ConversationState
    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.rag import KnowledgeBase

    class RecordingRetriever:
        def __init__(self):
            self.request = None

        def search_request(self, request):
            from klonet_agent.knowledge.models import SearchOutcome

            self.request = request
            return SearchOutcome(status="none")

    recorder = RecordingRetriever()
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "deployment_guidance",
            "operation": "platform_start",
            "target": "klonet_platform",
            "confidence": 0.95,
        }
    )

    KnowledgeBase(retriever=recorder).search_knowledge(
        "B",
        top_k=3,
        intent=intent,
        conversation_state=ConversationState(
            current_topic="klonet_platform_start",
            deployment_phase="platform_startup",
            machine_role="target_server",
            confirmed_slots={"selected_option": "B"},
        ),
    )

    assert recorder.request is not None
    assert "B" in recorder.request.query
    assert "selected_option:B" in recorder.request.query
    assert "Redis" in recorder.request.query
    assert recorder.request.intent == "platform_start"
    assert recorder.request.top_k >= 8
