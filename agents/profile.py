"""Agent Profile。

Profile 只描述行为差异，不承载复杂业务逻辑。真正的工具执行、记忆、日志和检索仍在各自模块中。
"""

from dataclasses import dataclass, field

from klonet_agent.prompts import CODING_PROMPT, MENTOR_PROMPT


@dataclass(frozen=True)
class AgentProfile:
    """描述某一种 Agent 模式。"""

    name: str
    mode_prompt: str
    allowed_tools: set[str] = field(default_factory=set)
    default_workflow: str = ""
    requires_rag: bool = False
    requires_review: bool = False


MENTOR_TOOLS = {
    "load_skill",
    "search_knowledge",
    "read_project_journal",
    "append_episode",
    "write_memory",
    "write_user",
    "web_fetch",
    "update_todos",
}

CODING_TOOLS = MENTOR_TOOLS | {
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
    return AgentProfile(
        name="mentor",
        mode_prompt=MENTOR_PROMPT,
        allowed_tools=MENTOR_TOOLS,
        default_workflow="retrieve -> explain -> suggest next step",
        requires_rag=True,
        requires_review=False,
    )
