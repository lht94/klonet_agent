"""Agent Profile 定义。

Mentor 和 Coding 共用底层 runtime，通过 profile 控制提示词、工具集合和默认工作流。
"""

from klonet_agent.agents.coding import CODING_PROFILE
from klonet_agent.agents.mentor import MENTOR_PROFILE
from klonet_agent.agents.profile import AgentProfile, get_profile


__all__ = ["AgentProfile", "MENTOR_PROFILE", "CODING_PROFILE", "get_profile"]
