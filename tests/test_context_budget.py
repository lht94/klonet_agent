"""模型感知上下文预算测试。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def _budget(model, tools=None):
    from klonet_agent.context.budget import build_context_budget

    return build_context_budget(model, tools)


def test_known_models_get_deterministic_limits():
    """已知模型在调用前就能得到明确的 hard/soft limit。"""

    gemini = _budget("gemini-3.7-flash")
    glm = _budget("GLM-5.2")

    assert gemini.hard_input_limit > gemini.soft_input_limit > 0
    assert glm.hard_input_limit > glm.soft_input_limit > 0
    # gemini 窗口更大，输入预算也应该更大。
    assert gemini.hard_input_limit > glm.hard_input_limit


def test_domestic_models_are_not_shadowed_by_fallback():
    """生产在用的国产模型必须是已知 profile，而不是 65536 保守兜底。"""

    for model in (
        "deepseek-v4-flash",
        "glm-5.3-flash",
        "qwen3.8-flash",
    ):
        budget = _budget(model)
        assert budget.profile_source == "builtin", f"{model} 掉进了 fallback"
        # 128K 窗口扣除预留后应明显高于 fallback 的约 47k。
        assert budget.hard_input_limit > 90_000, f"{model} 的输入预算过小"


def test_unknown_model_uses_conservative_fallback():
    """未知模型必须回退到保守默认，而不是旧的全局 500000。"""

    budget = _budget("totally-unknown-model")
    assert budget.profile_source == "fallback"
    assert budget.hard_input_limit < 100_000
    assert budget.hard_input_limit > 0


def test_output_and_safety_are_reserved_from_window():
    """输出预留与安全余量必须从窗口中扣除，而不是与输入共享。"""

    from klonet_agent.context.budget import get_model_profile

    model = "glm-5.2"
    profile = get_model_profile(model)
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": "x" * 200,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for i in range(10)
    ]
    budget = _budget(model, tools)

    expected = (
        profile.context_window
        - budget.reserved_output_tokens
        - budget.reserved_tool_tokens
        - budget.reserved_safety_tokens
    )
    assert budget.reserved_output_tokens == min(
        profile.max_output_tokens, max(4_096, profile.max_output_tokens // 2)
    )
    assert budget.reserved_tool_tokens > 0
    assert budget.hard_input_limit == max(2_048, expected)


def test_larger_tool_set_shrinks_input_budget():
    """工具 schema 增长应当压缩可用输入预算。"""

    tools_small = [
        {
            "type": "function",
            "function": {"name": "a", "description": "hi", "parameters": {}},
        }
    ]
    tools_big = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": "x" * 2000,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for i in range(20)
    ]
    assert _budget("glm-5.2", tools_big).hard_input_limit < _budget(
        "glm-5.2", tools_small
    ).hard_input_limit


def test_env_overrides_profile(monkeypatch):
    """环境变量应能覆盖窗口、输出与软阈值比例。"""

    from klonet_agent.context.budget import get_model_profile

    monkeypatch.setenv("KLONET_AGENT_CONTEXT_WINDOW", "32000")
    monkeypatch.setenv("KLONET_AGENT_MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setenv("KLONET_AGENT_SAFETY_MARGIN_TOKENS", "1024")
    monkeypatch.setenv("KLONET_AGENT_SOFT_LIMIT_RATIO", "0.5")

    profile = get_model_profile("glm-5.2")
    assert profile.source == "env"
    assert profile.context_window == 32000
    assert profile.max_output_tokens == 2048
    assert profile.safety_margin_tokens == 1024
    assert profile.soft_limit_ratio == 0.5

    budget = _budget("glm-5.2")
    assert budget.hard_input_limit == 32000 - budget.reserved_output_tokens - 1024
    assert budget.soft_input_limit == int(budget.hard_input_limit * 0.5)


def test_invalid_env_values_are_ignored(monkeypatch):
    """非法环境配置不应让预算崩溃，也不应产生非法预算。"""

    monkeypatch.setenv("KLONET_AGENT_CONTEXT_WINDOW", "not-a-number")
    monkeypatch.setenv("KLONET_AGENT_SOFT_LIMIT_RATIO", "7")

    budget = _budget("glm-5.2")
    assert budget.hard_input_limit > budget.soft_input_limit > 0


def test_soft_limit_ratio_defaults_to_seventy_percent():
    """软阈值默认为 hard limit 的 70%。"""

    budget = _budget("glm-5.2")
    assert budget.soft_input_limit == int(budget.hard_input_limit * 0.70)
