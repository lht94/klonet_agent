"""上下文管理包。

在 AgentOrchestrator 与 LLMClient 之间提供统一的上下文编译层：
- budget: 模型感知的上下文预算；
- message_groups: 把扁平消息解析成完整语义组；
- compiler: 调用前按预算组装最终消息列表。
"""

from klonet_agent.context.budget import (
    ContextBudget,
    ModelContextProfile,
    build_context_budget,
)
from klonet_agent.context.compiler import (
    CompiledContext,
    ContextCompiler,
    ContextOverflowError,
    ContextRequest,
)

__all__ = [
    "ContextBudget",
    "ModelContextProfile",
    "build_context_budget",
    "CompiledContext",
    "ContextCompiler",
    "ContextOverflowError",
    "ContextRequest",
]
