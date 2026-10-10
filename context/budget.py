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

from klonet_agent.context import tokens as _tokens_module


@dataclass(frozen=True)
class ModelContextProfile:
    """单个模型的上下文能力配置。

    ``tokenizer_id`` 为 HuggingFace 模型仓库 ID（如
    ``"deepseek-ai/DeepSeek-V3"``）。公开 tokenizer 的模型填这里，
    编译器会按它懒加载真路径 tokenizer；闭源模型（如 Google Gemini API 系）
    保持 ``None``，永远走启发式估算。详见 ``context/tokens.py``。
    """

    context_window: int
    max_output_tokens: int
    safety_margin_tokens: int
    soft_limit_ratio: float = 0.70
    tokenizer_id: str | None = None
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
    # 与 ModelContextProfile.tokenizer_id 相同含义：从 profile 透传给编译器，
    # 让编译器决定是否懒加载真路径 tokenizer。
    tokenizer_id: str | None = None


# 已知模型 profile。窗口与输出上限按各供应商公开发布的规格保守填写；
# 供应商调整规格时可通过环境变量覆盖，不必改代码。
# 国产模型统一按 128K 窗口保守取值（实际规格不低于该值），避免掉进 fallback
# 的 65536 兜底而过早触发压缩。
_KNOWN_PROFILES: dict[str, ModelContextProfile] = {
    "gemini-3.7-flash": ModelContextProfile(
        context_window=1_000_000,
        max_output_tokens=65_536,
        safety_margin_tokens=8_192,
        tokenizer_id=None,  # Google Gemini API 闭源，无公开 tokenizer
    ),
    "glm-5.2": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
        tokenizer_id="THUDM/glm-4-9b-chat",
    ),
    "glm-5.3": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
        tokenizer_id="THUDM/glm-4-9b-chat",
    ),
    "glm-5.3-flash": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
        tokenizer_id="THUDM/glm-4-9b-chat",
    ),
    "deepseek-v4-flash": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
        tokenizer_id="deepseek-ai/DeepSeek-V3",
    ),
    "deepseek-v4.1-flash": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
        tokenizer_id="deepseek-ai/DeepSeek-V3",
    ),
    "deepseek-v4-pro": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
        tokenizer_id="deepseek-ai/DeepSeek-V3",
    ),
    "qwen3.8-flash": ModelContextProfile(
        context_window=128_000,
        max_output_tokens=16_384,
        safety_margin_tokens=8_192,
        tokenizer_id="Qwen/Qwen2.5-7B-Instruct",
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
ENV_TOKENIZER_ID = "KLONET_AGENT_TOKENIZER_ID"


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


def _env_tokenizer_id() -> str | None:
    raw = os.getenv(ENV_TOKENIZER_ID, "").strip()
    return raw or None


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
    tokenizer_id = _env_tokenizer_id()

    if (
        window is None
        and output is None
        and margin is None
        and ratio is None
        and tokenizer_id is None
    ):
        return base

    return ModelContextProfile(
        context_window=window or base.context_window,
        max_output_tokens=output or base.max_output_tokens,
        safety_margin_tokens=margin or base.safety_margin_tokens,
        soft_limit_ratio=ratio if ratio is not None else base.soft_limit_ratio,
        tokenizer_id=tokenizer_id if tokenizer_id is not None else base.tokenizer_id,
        source="env",
    )


def build_context_budget(
    model: str,
    tool_definitions: list[dict] | None = None,
) -> ContextBudget:
    """为指定模型和工具集合构造一次调用的输入预算。"""

    profile = get_model_profile(model)
    # 工具 schema 的预留同样按真路径算，避免预留偏小导致 hard_input_limit 偏大。
    # 用模块属性查找而非 ``from ... import get_tokenizer``，是为了让
    # 测试里的 ``monkeypatch.setattr(tokens_module, "get_tokenizer", ...)``
    # 在本模块也能生效——import 绑定会冻结引用，模块属性查找则每次都查。
    tokenizer = _tokens_module.get_tokenizer(profile.tokenizer_id)
    tool_tokens = _tokens_module.estimate_tool_tokens(tool_definitions, tokenizer=tokenizer)

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
        tokenizer_id=profile.tokenizer_id,
    )
