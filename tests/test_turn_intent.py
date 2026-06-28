"""Unified turn intent and decision planning."""

from klonet_agent.knowledge.conversation_state import ConversationState
from klonet_agent.knowledge.conversation_state import ConversationStateManager
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.semantic_understanding import IntentDecision, SemanticFrame
from klonet_agent.knowledge.turn_intent import TurnIntentBuilder, TurnDecisionPlanner


def test_late_platform_supplement_keeps_platform_usage_intent():
    frame = SemanticFrame.from_mapping(
        {
            "scope": "klonet",
            "user_role": "learner",
            "deployment_phase": "unknown",
            "target_component": "klonet_platform",
            "confidence": 0.52,
        }
    )
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "deployment_guidance",
            "operation": "platform_start",
            "target": "klonet_platform",
            "clarification_required": True,
            "confidence": 0.55,
        }
    )
    state = ConversationState(
        current_topic="klonet_platform_usage",
        user_role="learner",
        machine_role="unspecified",
        deployment_phase="platform_usage",
        confirmed_slots={"platform_name": "klonet"},
        excluded_meanings=(
            "deployment_preparation",
            "environment_setup",
            "platform_startup",
        ),
    )

    turn_intent = TurnIntentBuilder().build(
        "klonet平台",
        recent_history=[
            {"role": "user", "content": "我在使用平台之前，电脑里需要下载什么软件吗？"},
            {
                "role": "assistant",
                "content": "场景一：普通用户用浏览器访问平台；场景二：管理员部署运行平台。",
            },
        ],
        intent=intent,
        semantic_frame=frame,
        decision=IntentDecision(intent=intent, clarification_action="ask_before_answer"),
        conversation_state=state,
    )
    turn_decision = TurnDecisionPlanner().plan(turn_intent)

    assert turn_intent.context_ref == "late_entity_fill"
    assert turn_intent.phase == "platform_usage"
    assert turn_intent.task_type == "operation_guide"
    assert turn_intent.operation == "unknown"
    assert "platform_startup" in turn_intent.excluded_meanings
    assert turn_decision.should_clarify is False
    assert turn_intent.to_query_intent().task_type == "operation_guide"


def test_continue_turn_restores_previous_intent_as_context_reference():
    previous = TurnIntentBuilder().build(
        "介绍一下卫星平台",
        intent=QueryIntent.from_mapping(
            {
                "scope": "klonet",
                "task_type": "concept",
                "target": "satellite_platform",
                "confidence": 0.8,
            }
        ),
    )
    misleading = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "deployment_guidance",
            "operation": "environment_setup",
            "clarification_required": True,
            "clarification_question": "你是安装环境还是启动平台？",
            "confidence": 0.7,
        }
    )

    turn_intent = TurnIntentBuilder().build(
        "继续",
        intent=misleading,
        resume_state={"turn_intent": previous},
    )
    turn_decision = TurnDecisionPlanner().plan(turn_intent)

    assert turn_intent.context_ref == "continue"
    assert turn_intent.task_type == "concept"
    assert turn_intent.target == "satellite_platform"
    assert turn_decision.should_resume is True
    assert turn_decision.should_clarify is False


def test_short_unknown_token_uses_low_information_clarification():
    turn_intent = TurnIntentBuilder().build(
        "NH",
        intent=QueryIntent.from_mapping(
            {
                "scope": "klonet",
                "task_type": "deployment_guidance",
                "operation": "environment_setup",
                "clarification_required": True,
                "confidence": 0.62,
            }
        ),
    )
    turn_decision = TurnDecisionPlanner().plan(turn_intent)

    assert turn_intent.context_ref == "none"
    assert turn_intent.clarification_type == "low_information"
    assert turn_decision.should_clarify is True
    assert turn_decision.clarification_reason == "low_information_input"
    assert "安装环境还是启动平台" not in turn_decision.clarification_reply


def test_accept_any_reply_to_deploy_choice_does_not_clarify_again():
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "concept",
            "operation": "unknown",
            "target": "klonet_platform",
            "clarification_required": True,
            "clarification_question": "你是想首次安装 Klonet 环境，还是启动已经安装好的平台服务？",
            "confidence": 0.66,
        }
    )

    turn_intent = TurnIntentBuilder().build(
        "我说了都行",
        recent_history=[
            {
                "role": "assistant",
                "content": (
                    "你是想首次安装 Klonet 环境，还是启动已经安装好的平台服务？"
                    "A：首次环境部署。B：平台启动。"
                ),
            },
            {"role": "user", "content": "都行"},
        ],
        intent=intent,
    )
    turn_decision = TurnDecisionPlanner().plan(turn_intent)

    assert turn_intent.context_ref == "accept_any"
    assert turn_intent.task_type == "deployment_guidance"
    assert turn_intent.operation == "unknown"
    assert turn_intent.target == "klonet_platform"
    assert turn_decision.should_clarify is False
    assert turn_intent.to_query_intent().clarification_required is False


def test_accept_any_reply_to_generic_options_does_not_require_deploy_context():
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "topology_deploy",
            "target": "topology",
            "clarification_required": True,
            "clarification_question": "你想创建拓扑、导入拓扑，还是查看拓扑节点？",
            "confidence": 0.68,
        }
    )

    turn_intent = TurnIntentBuilder().build(
        "都可以",
        recent_history=[
            {
                "role": "assistant",
                "content": "A：创建拓扑。B：导入拓扑。C：查看拓扑节点。",
            },
        ],
        intent=intent,
    )
    turn_decision = TurnDecisionPlanner().plan(turn_intent)

    assert turn_intent.context_ref == "accept_any"
    assert turn_intent.task_type == "operation_guide"
    assert turn_intent.operation == "topology_deploy"
    assert turn_decision.should_clarify is False


def test_third_or_c_option_reply_is_treated_as_context_option():
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "concept",
            "operation": "unknown",
            "target": "topology",
            "clarification_required": True,
            "clarification_question": "你选的是哪一种？",
            "confidence": 0.63,
        }
    )

    for user_input in ("选C", "第三种", "方案三", "第3个", "C", "选D", "第四种", "路线四"):
        turn_intent = TurnIntentBuilder().build(
            user_input,
            recent_history=[
                {
                    "role": "assistant",
                    "content": "A：虚拟机。B：OVS 交换机。C：路由器。D：控制器。",
                },
            ],
            intent=intent,
        )
        turn_decision = TurnDecisionPlanner().plan(turn_intent)

        assert turn_intent.context_ref == "option_select"
        assert turn_decision.should_clarify is False


def test_conversation_state_records_general_c_option_selection():
    state = ConversationStateManager().from_turn(
        "选C",
        recent_history=[
            {
                "role": "assistant",
                "content": "A：虚拟机。B：OVS 交换机。C：路由器。D：控制器。",
            }
        ],
    )

    assert state.confirmed_slots["selected_option"] == "C"
    assert state.last_option_map["C"] == "option_c"


def test_source_question_marks_source_need_without_changing_business_intent():
    turn_intent = TurnIntentBuilder().build(
        "你这边没有源码吗？TopoManager.py 里有哪些节点？",
        intent=QueryIntent.from_mapping(
            {
                "scope": "klonet",
                "task_type": "operation_guide",
                "operation": "topology_deploy",
                "target": "topology",
                "confidence": 0.78,
            }
        ),
    )
    turn_decision = TurnDecisionPlanner().plan(turn_intent)

    assert turn_intent.source_need == "source_index"
    assert turn_intent.task_type == "operation_guide"
    assert turn_intent.operation == "topology_deploy"
    assert turn_decision.source_required is True
