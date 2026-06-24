"""Mentor 当前轮回答策略测试。"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_troubleshooting_policy_uses_diagnostic_structure():
    """故障排查应按原因、顺序和依据组织，并允许必要展开。"""

    from klonet_agent.answer_policy import build_answer_policy

    text = build_answer_policy("troubleshooting", "拓扑部署卡住怎么排查")

    assert "最可能原因、排查顺序、判断依据" in text
    assert "500 至 1000 字" in text


def test_code_lookup_policy_explains_call_chain():
    """源码定位应覆盖入口、调用链和关键状态。"""

    from klonet_agent.answer_policy import build_answer_policy

    text = build_answer_policy("code_lookup", "TopoDeployAPI 在哪里实现")

    assert "入口、调用链、关键状态、注意点" in text
    assert "按必要步骤展开" in text


def test_deployment_development_policy_uses_deployment_structure():
    """development 中的部署意图应使用部署指导结构。"""

    from klonet_agent.answer_policy import build_answer_policy

    text = build_answer_policy("development", "Klonet 应该怎么部署")

    assert "推荐方案、当前前提、执行步骤、验证方式" in text


def test_project_progress_policy_keeps_required_next_step():
    """项目进度中的下一步属于必要结构，不应被通用规则删除。"""

    from klonet_agent.answer_policy import build_answer_policy

    text = build_answer_policy("project_progress", "当前项目进度如何")

    assert "当前状态、已完成、阻塞项、下一步" in text
    assert "下一步仅在回答项目进度所必需时提供" in text


def test_simple_fact_policy_is_shorter_than_concept_explanation():
    """简单事实和概念解释应获得不同的长度建议。"""

    from klonet_agent.answer_policy import build_answer_policy

    fact = build_answer_policy("concept", "Klonet 是什么？")
    concept = build_answer_policy("concept", "解释 Klonet 的核心架构和工作原理")

    assert "100 至 300 字" in fact
    assert "300 至 600 字" in concept


def test_unknown_policy_falls_back_safely():
    """未知任务类型应安全降级，不阻断回答。"""

    from klonet_agent.answer_policy import build_answer_policy

    text = build_answer_policy("unknown", "解释一下")

    assert "【本轮回答策略】" in text
    assert "第一段直接给出结论" in text
    assert "没有可靠证据时说明不确定" in text



def test_concept_route_with_deployment_intent_uses_deployment_structure():
    """Router 默认归为 concept 的部署问法仍应获得部署指导结构。"""

    from klonet_agent.answer_policy import build_answer_policy

    text = build_answer_policy("concept", "Klonet 应该怎么部署")

    assert "推荐方案、当前前提、执行步骤、验证方式" in text
    assert "按必要步骤展开" in text


def test_platform_start_intent_uses_command_focused_structure():
    """结构化启动意图应覆盖关键词策略，并禁止环境安装内容。"""

    from klonet_agent.answer_policy import build_answer_policy
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "platform_start",
            "excluded_intents": ["environment_setup"],
            "confidence": 0.98,
        }
    )

    text = build_answer_policy("concept", "我怎么启动 Klonet", intent=intent)

    assert "启动前提、标准启动命令、验证方式" in text
    assert "不得包含环境安装步骤" in text
    assert "不得推测 start.sh" in text
