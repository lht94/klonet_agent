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



def test_ops_prompt_says_confirm_auto_advances_operation_plans():
    """Ops should rely on plan confirmation auto-advancing safe steps."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "execute_ops_operation_step" in OPS_PROMPT
    assert "list_ops_operation_plans" in OPS_PROMPT
    assert "describe_ops_operation_plan" in OPS_PROMPT
    assert "resolve_ops_blocked_step" in OPS_PROMPT
    assert "blocked" in OPS_PROMPT
    assert "running" in OPS_PROMPT
    assert "不得直接 confirm-step" in OPS_PROMPT
    assert "approve_ops_operation_plan 会自动按状态机连续执行" in OPS_PROMPT
    assert "不要在刚 confirm 后再要求用户确认普通步骤" in OPS_PROMPT
    assert "不要自行重建计划绕过当前计划" in OPS_PROMPT


def test_ops_prompt_allows_controlled_startup_file_edits_but_not_business_development():
    """Ops can modify deployment startup files through OperationPlan only."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "不得做普通业务源码开发修改" in OPS_PROMPT
    assert "允许通过 OperationPlan + write_ops_file 修改平台启动必需文件" in OPS_PROMPT
    assert "vemu_config/config.py" in OPS_PROMPT
    assert "mains/web_terminal_main.py" in OPS_PROMPT


def test_ops_prompt_routes_nginx_install_and_reload_through_actions():
    """Ops should not ask the user to sudo-copy nginx config by hand."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "install_nginx_config" in OPS_PROMPT
    assert "reload_nginx" in OPS_PROMPT
    assert "不得通过 `write_ops_file` 直接修改 `/etc/nginx" in OPS_PROMPT
    assert "不得要求用户手工 `sudo cp`" in OPS_PROMPT


def test_ops_prompt_uses_dedicated_account_platform_path():
    """Ops should not reuse historical adminis paths as deployment defaults."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "/home/klonet-agent/platforms/<platform>_project" in OPS_PROMPT
    assert "不要把历史服务器用户名" in OPS_PROMPT
    assert "后端仓库通常应 clone 到其下的 `vemu_uestc/`" in OPS_PROMPT


def test_ops_prompt_respects_user_pause_before_plan_execution():
    """Explicit pause requests must not be converted into execution tools."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "暂停" in OPS_PROMPT
    assert "本轮不得调用 approve_ops_operation_plan" in OPS_PROMPT
    assert "execute_ops_next_step" in OPS_PROMPT
    assert "resolve_ops_blocked_step" in OPS_PROMPT


def test_ops_prompt_routes_python_environment_recovery_through_controlled_plan():
    """Environment recovery should prefer controlled plans but preserve fallback agency."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "python -m pip install" in OPS_PROMPT
    assert "pip install" in OPS_PROMPT
    assert "pip uninstall" in OPS_PROMPT
    assert "--force-reinstall" in OPS_PROMPT
    assert "remove_python_package_entries" in OPS_PROMPT
    assert "python -c" in OPS_PROMPT
    assert "覆盖 `__init__.py`" in OPS_PROMPT
    assert "用受控 apt 安装系统包" in OPS_PROMPT
    assert "用受控 `python3.8 -m pip install`" in OPS_PROMPT
    assert "最后备选" in OPS_PROMPT
    assert "安全风险" in OPS_PROMPT
    assert "需要管理员显式选择" in OPS_PROMPT


def test_ops_prompt_blocks_helper_policy_mismatch_workarounds():
    """Helper contract drift should not be silently bypassed as a controlled step."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "helper_policy_mismatch" in OPS_PROMPT
    assert "升级 installed helper" in OPS_PROMPT
    assert "apt-get" in OPS_PROMPT
    assert "dpkg" in OPS_PROMPT
    assert "外部管理员救援选项" in OPS_PROMPT
    assert "需要用户明确改约束或管理员确认" in OPS_PROMPT


def test_ops_prompt_requires_action_bindings_for_mutating_deploy_steps():
    """Deploy plans should not defer bindings for later."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "必须在创建计划时就绑定具体 action 和 args" in OPS_PROMPT
    assert "不得创建“checkpoint 占位步骤”" in OPS_PROMPT
    assert "未绑定 action 的修改步骤会被状态机阻塞" in OPS_PROMPT


def test_ops_prompt_forbids_plaintext_secrets_in_plans():
    """Plans and summaries should not leak config secrets."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "不得写入明文 password" in OPS_PROMPT
    assert "[REDACTED]" in OPS_PROMPT
    assert "敏感字段继承父类" in OPS_PROMPT


def test_ops_prompt_prioritizes_process_detail_for_port_owner_evidence():
    """Port/PID/cwd questions should prefer precise process evidence first."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "inspect_process_detail" in OPS_PROMPT
    assert "PID、命令或 cwd" in OPS_PROMPT
    assert "不得先用 screen 存在、历史日志" in OPS_PROMPT


def test_tool_descriptions_do_not_suggest_adminis_paths():
    """Model-visible tool examples should use neutral or klonet-agent paths."""

    from klonet_agent.tools.registry import TOOLS

    descriptions = []
    for tool in TOOLS:
        function = tool.get("function", {})
        descriptions.append(function.get("description", ""))
        properties = function.get("parameters", {}).get("properties", {})
        for value in properties.values():
            descriptions.append(value.get("description", ""))

    joined = "\n".join(descriptions)

    assert "/home/adminis" not in joined


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
