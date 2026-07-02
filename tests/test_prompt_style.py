"""Prompt 与运行文案风格测试。"""

import sys
from pathlib import Path

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


FORBIDDEN_LEGACY_WORDS = ("小鸡毛", "小白")


def test_system_prompts_use_klonet_agent_identity():
    """系统提示词应该保持 Klonet 教学协作 Agent 定位。"""

    from klonet_agent.prompts import MENTOR_PROMPT, build_system_prompts

    text = "\n".join(build_system_prompts(MENTOR_PROMPT))

    assert "Klonet 专用教学协作 Agent" in text
    assert "Klonet Mentor Agent" in text
    for word in FORBIDDEN_LEGACY_WORDS:
        assert word not in text


def test_system_prompts_describe_all_agent_modes():
    """能力自述问题应知道 Mentor、Ops 和 Coding 三种模式。"""

    from klonet_agent.prompts import MENTOR_PROMPT, build_system_prompts

    text = "\n".join(build_system_prompts(MENTOR_PROMPT))

    assert "Mentor 模式" in text
    assert "Ops 模式" in text
    assert "Coding 模式" in text
    assert "只读环境感知" in text
    assert "代码修改" in text


def test_mentor_prompt_keeps_generic_rag_secondary():
    """Mentor Prompt 应与 generic 的执行层策略保持一致。"""

    from klonet_agent.prompts import MENTOR_PROMPT

    assert "通用知识作为主要依据" in MENTOR_PROMPT
    assert "Klonet RAG 只能作为辅助证据" in MENTOR_PROMPT
    assert "最多检索 1 次" in MENTOR_PROMPT
    assert "明确排除 Klonet 的问题禁止检索" not in MENTOR_PROMPT

def test_mentor_prompt_requires_direct_concise_evidence_based_answers():
    """Mentor 应直接、简洁地回答，并避免无证据推测和机械收尾。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.prompts import MENTOR_PROMPT

    assert "第一段直接给出结论" in MENTOR_PROMPT
    assert "只解释理解结论所必需的原因" in MENTOR_PROMPT
    assert "不重复用户问题，不汇报内部检索过程" in MENTOR_PROMPT
    assert "不机械追加学习建议、源码路径或下一步" in MENTOR_PROMPT
    assert "不生成 Klonet 架构推测" in MENTOR_PROMPT
    assert "suggest next step" not in get_profile("mentor").default_workflow


def test_mentor_prompt_requires_structured_intent_before_retrieval():
    """Mentor 检索前必须保存否定、前提和多轮纠正信息。"""

    from klonet_agent.prompts import MENTOR_PROMPT

    assert "search_knowledge" in MENTOR_PROMPT
    assert "intent 参数" in MENTOR_PROMPT
    assert "不得把被否定的方向作为主要检索目标" in MENTOR_PROMPT
    assert "先向用户澄清" in MENTOR_PROMPT


def test_mentor_prompt_forbids_operation_plan_generation():
    """Mentor may recommend Ops but must not list executable environment plans."""

    from klonet_agent.prompts import MENTOR_PROMPT

    assert "Mentor 模式不得生成 OperationPlan" in MENTOR_PROMPT
    assert "不得输出 confirm <plan_id>" in MENTOR_PROMPT
    assert "建议切换到 Ops 模式" in MENTOR_PROMPT



def test_ops_prompt_prefers_next_step_tool_for_approved_operation_plans():
    """Ops should drive approved OperationPlans with the next-step tool."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "execute_ops_next_step" in OPS_PROMPT
    assert "execute_ops_operation_step" in OPS_PROMPT
    assert "批准后的 OperationPlan 默认调用 execute_ops_next_step" in OPS_PROMPT
    assert "只有用户明确指定 step_id" in OPS_PROMPT


def test_memory_prompt_uses_teaching_agent_language():
    """记忆提示词不应该残留旧个人 Agent 称呼。"""

    from klonet_agent.memory.store import MemoryStore

    with local_temp_dir() as temp_dir:
        store = MemoryStore(temp_dir / "memory", temp_dir / "memory" / "USER.md")
        text = store.memory_prompt()

    assert "长期记忆" in text
    assert "用户画像与偏好" in text
    for word in FORBIDDEN_LEGACY_WORDS:
        assert word not in text


def test_runtime_text_does_not_use_legacy_persona_words():
    """运行时输出文案不再使用旧个人 Agent 称呼。"""

    runtime_files = [
        "app/cli.py",
        "memory/store.py",
        "orchestrator.py",
        "session.py",
        "tools/executor.py",
    ]

    for relative_path in runtime_files:
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for word in FORBIDDEN_LEGACY_WORDS:
            assert word not in text, f"{relative_path} 仍包含旧称呼：{word}"
