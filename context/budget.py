"""模型感知的上下文预算。

模型的总上下文窗口由输入和输出共享。只比较历史 token 与总窗口，
会遗漏输出空间、tool schema 和供应商计算方式的差异。这里为每个模型
建立能力配置，在调用前划分出 hard/soft input limit。

配置解析优先级：
1. 环境变量中的明确模型配置（KLONET_AGENT_CONTEXT_WINDOW 等）；
2. 代码中已知模型 profile；
3. 保守的默认窗口。
不允许未知模型回退到旧的 500,000 全局常量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from klonet_agent.context.tokens import estimate_tool_tokens


@dataclass(frozen=True)
class ModelContextProfile:
    """单个模型的上下文能力配置。"""

    context_window: int
    max_output_tokens: int
    safety_margin_tokens: int
    soft_limit_ratio: float = 0.70
    source: str = "builtin"


@dataclass(frozen=True)
class ContextBudget:
    """一次模型调用可用的输入预算。"""

    hard_input_limit: int
    soft_input_limit: int
    reserved_output_tokens: int
    reserved_tool_tokens: int
    reserved_safety_tokens: int
    model: str
    profile_source: str


# 已知模型 profile。窗口与输出上限按各供应商公开发布的规格保守填写；
# 供应商调整规格时可通过环境变量覆盖，不必改代码。
_KNOWN_PROFILES: dict[str, ModelContextProfile] = {
    "gemini-3.7-flash": ModelContextProfile(
        context_window=1_000_000,
        max_output_tokens=65_536,
        safety_margin_tokens=8_192,
    ),
    "glm-5.2": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
    ),
}

# 未知模型的保守默认：宁可提前压缩，也不把请求发向必然超限的供应商。
_FALLBACK_PROFILE = ModelContextProfile(
    context_window=65_536,
    max_output_tokens=8_192,
    safety_margin_tokens=8_192,
    source="fallback",
)

# 环境变量覆盖。
ENV_CONTEXT_WINDOW = "KLONET_AGENT_CONTEXT_WINDOW"
ENV_MAX_OUTPUT_TOKENS = "KLONET_AGENT_MAX_OUTPUT_TOKENS"
ENV_SAFETY_MARGIN_TOKENS = "KLONET_AGENT_SAFETY_MARGIN_TOKENS"
ENV_SOFT_LIMIT_RATIO = "KLONET_AGENT_SOFT_LIMIT_RATIO"


def _env_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_ratio(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if 0.1 <= value <= 0.95:
        return value
    return None


def get_model_profile(model: str) -> ModelContextProfile:
    """解析模型 profile：环境变量 > 已知 profile > 保守默认。"""

    base = _KNOWN_PROFILES.get((model or "").strip().lower())
    if base is None:
        base = _FALLBACK_PROFILE
    else:
        base = replace(base, source="builtin")

    window = _env_int(ENV_CONTEXT_WINDOW)
    output = _env_int(ENV_MAX_OUTPUT_TOKENS)
    margin = _env_int(ENV_SAFETY_MARGIN_TOKENS)
    ratio = _env_ratio(ENV_SOFT_LIMIT_RATIO)

    if window is None and output is None and margin is None and ratio is None:
        return base

    return ModelContextProfile(
        context_window=window or base.context_window,
        max_output_tokens=output or base.max_output_tokens,
        safety_margin_tokens=margin or base.safety_margin_tokens,
        soft_limit_ratio=ratio if ratio is not None else base.soft_limit_ratio,
        source="env",
    )


def build_context_budget(
    model: str,
    tool_definitions: list[dict] | None = None,
) -> ContextBudget:
    """为指定模型和工具集合构造一次调用的输入预算。"""

    profile = get_model_profile(model)
    tool_tokens = estimate_tool_tokens(tool_definitions)

    reserved_output = min(profile.max_output_tokens, max(4_096, profile.max_output_tokens // 2))
    reserved_safety = profile.safety_margin_tokens

    hard_input_limit = (
        profile.context_window
        - reserved_output
        - tool_tokens
        - reserved_safety
    )
    # 防御极端配置：tool schema 巨大或窗口极小时，仍要保证有限的可用输入。
    hard_input_limit = max(2_048, hard_input_limit)
    soft_input_limit = max(1_024, int(hard_input_limit * profile.soft_limit_ratio))

    return ContextBudget(
        hard_input_limit=hard_input_limit,
        soft_input_limit=soft_input_limit,
        reserved_output_tokens=reserved_output,
        reserved_tool_tokens=tool_tokens,
        reserved_safety_tokens=reserved_safety,
        model=model,
        profile_source=profile.source,
    )
