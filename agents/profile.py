"""Agent Profile。

Profile 只描述行为差异，不承载复杂业务逻辑。真正的工具执行、记忆、日志和检索仍在各自模块中。
"""

from dataclasses import dataclass, field
from typing import Set

from klonet_agent.prompts import CODING_PROMPT, MENTOR_PROMPT, OPS_PROMPT


@dataclass(frozen=True)
class AgentProfile:
    """描述某一种 Agent 模式。"""

    name: str                                             # 模式名
    mode_prompt: str                                      # 该模式专用提示词
    allowed_tools: Set[str] = field(default_factory=set)  # 允许调用哪些工具
    default_workflow: str = ""                            # 默认工作流
    requires_rag: bool = False                            # 是否需要知识检索
    requires_review: bool = False                         # 是否需要 review


MENTOR_TOOLS = {
    "load_skill",
    "search_knowledge",
    "search_code",
    "read_source_file",
    "list_source_files",
    "read_project_journal",
    "list_files",
    "read_file",
    "append_episode",
    "write_memory",
    "write_user",
    "web_fetch",
}

ENVIRONMENT_TOOLS = {
    "inspect_ops_context",
    "inspect_platform_instances",
    "inspect_system_environment",
    "inspect_klonet_runtime",
    "inspect_process_detail",
    "read_klonet_logs",
    "read_ops_file",
    "inspect_screen_session",
    "search_shared_ops_memory",
}

OPS_OPERATION_TOOLS = {
    "create_ops_operation_plan",
    "list_ops_operation_plans",
    "describe_ops_operation_plan",
    "approve_ops_operation_plan",
    "execute_ops_operation_step",
    "execute_ops_next_step",
    "resolve_ops_blocked_step",
}

OPS_TOOLS = {
    "load_skill",
    "search_knowledge",
    "search_code",
    "read_source_file",
    "list_source_files",
    "read_project_journal",
    "list_files",
    "read_file",
    "append_episode",
    "web_fetch",
} | ENVIRONMENT_TOOLS | OPS_OPERATION_TOOLS

CODING_TOOLS = MENTOR_TOOLS | {
    "update_todos",
    "list_files",
    "read_file",
    "write_file",
    "run_tests",
    "show_diff",
    "create_project_journal",
    "append_journal_event",
    "update_project_status",
    "record_test_result",
    "record_acceptance_gap",
}


def get_profile(name: str) -> AgentProfile:
    """按名称返回 profile，未知名称默认 Mentor。"""

    normalized = (name or "mentor").strip().lower()
    if normalized == "coding":
        return AgentProfile(
            name="coding",
            mode_prompt=CODING_PROMPT,
            allowed_tools=CODING_TOOLS,
            default_workflow="plan -> retrieve -> edit -> test -> diff -> journal -> review",
            requires_rag=True,
            requires_review=True,
        )
    if normalized == "ops":
        return AgentProfile(
            name="ops",
            mode_prompt=OPS_PROMPT,
            allowed_tools=OPS_TOOLS,
            default_workflow="route -> retrieve runbook -> inspect read-only environment -> plan -> confirm -> execute controlled recipe",
            requires_rag=True,
            requires_review=False,
        )
    return AgentProfile(
        name="mentor",
        mode_prompt=MENTOR_PROMPT,
        allowed_tools=MENTOR_TOOLS,
        default_workflow="route -> retrieve if needed -> answer directly",
        requires_rag=True,
        requires_review=False,
    )
