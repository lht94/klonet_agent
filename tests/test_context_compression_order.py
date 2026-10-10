"""阶段 7 回归：soft 压缩与 hard 淘汰的**执行顺序**。

这三条不变量最容易在后续改动里悄悄回归：

1. **压缩看完整候选，不看裁剪结果。** 「原始候选已超过 soft、但因为有一条
   巨大消息被跳过导致裁剪后低于 soft」时，仍必须触发压缩。旧实现（先淘汰、
   再拿裁剪后的 estimated 判断）会漏掉这种情形——被跳过的巨大内容既没进
   上下文、也没进 checkpoint，等于静默丢失。
2. **淘汰只在候选超过 hard 时发生。** 候选装得下时 omitted 必须为空，
   不能因为"分区配额"就提前丢内容。
3. **工具交换不被拆开。** 整组放不下就整组 omit，绝不只保留首尾文本。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

# 未知模型 → 走 65536 保守 fallback，无 tokenizer_id → 纯字符启发式估算。
# 这样断言只依赖窗口配置，不受 HF tokenizer 是否可下载影响。
_MODEL = "phase7-unknown-model"


def _window(monkeypatch, hard_target: int) -> None:
    """把 hard_input_limit 精确设定为 ``hard_target``。

    hard = context_window − reserved_output − tool_tokens − safety_margin。
    这里取 output=4096、safety=1024、无工具（tool_tokens=0），
    于是 window = hard_target + 5120。
    """

    monkeypatch.setenv("KLONET_AGENT_CONTEXT_WINDOW", str(hard_target + 5_120))
    monkeypatch.setenv("KLONET_AGENT_MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.setenv("KLONET_AGENT_SAFETY_MARGIN_TOKENS", "1024")


def _budget(monkeypatch, hard_target: int):
    from klonet_agent.context.budget import build_context_budget

    _window(monkeypatch, hard_target)
    return build_context_budget(_MODEL)


def _tool_exchange(payload_chars: int) -> list[dict]:
    """一段完整的工具交换：user → assistant(tool_calls) → tool → assistant。"""

    return [
        {"role": "user", "content": "查一下日志"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "x" * payload_chars},
        {"role": "assistant", "content": "日志结论：服务正常"},
    ]


# --------------------------------------------------------------------------- #
# 不变量 1：压缩看完整候选
# --------------------------------------------------------------------------- #


def test_compression_triggers_on_full_candidate_not_on_trimmed_result(monkeypatch):
    """完整候选超 soft、裁剪后低于 soft —— 仍必须触发压缩。

    这是阶段 7 的核心回归。旧实现的 compression_required 用的是**裁剪后**的
    estimated，所以这个场景会判定为"不需要压缩"，让那条超大的工具输出
    既没进上下文也没进 checkpoint。
    """

    from klonet_agent.context.compiler import ContextCompiler

    budget = _budget(monkeypatch, hard_target=100_000)
    soft = budget.soft_input_limit
    assert soft < 100_000  # soft = 70k

    history = [
        {"role": "user", "content": "旧问题一 " + "a" * 4_000},
        {"role": "assistant", "content": "旧回答一"},
        # 一条自身就远超历史区预算的工具输出：必然被跳过。
        *_tool_exchange(600_000),
        {"role": "user", "content": "现在怎么办"},
    ]

    compiled = ContextCompiler().compile_history(history, model=_MODEL)

    # 完整候选（含那条巨大工具输出）远超 soft。
    assert compiled.candidate_tokens > soft, (
        f"完整候选应超过 soft：candidate={compiled.candidate_tokens} soft={soft}"
    )
    # 实际发送的量远低于 soft（那条巨大消息被跳过了）。
    assert compiled.final_tokens < soft, (
        f"裁剪后应低于 soft：final={compiled.final_tokens} soft={soft}"
    )
    # 关键断言：尽管裁剪后很低，压缩仍必须被请求。
    assert compiled.compression_required is True, (
        "压缩必须基于完整候选判断；旧口径会漏掉这个场景"
    )


def test_compression_not_required_when_candidate_fits_below_soft(monkeypatch):
    """候选本身就低于 soft 时，不应请求压缩。"""

    from klonet_agent.context.compiler import ContextCompiler

    _budget(monkeypatch, hard_target=100_000)

    history = [
        {"role": "user", "content": "短问题"},
        {"role": "assistant", "content": "短回答"},
        {"role": "user", "content": "再问一句"},
    ]
    compiled = ContextCompiler().compile_history(history, model=_MODEL)

    assert compiled.compression_required is False


# --------------------------------------------------------------------------- #
# 不变量 2：淘汰只在超 hard 时发生
# --------------------------------------------------------------------------- #


def test_no_eviction_when_candidate_fits(monkeypatch):
    """候选装得下时 omitted 必须为空——不允许"提前丢"。"""

    from klonet_agent.context.compiler import ContextCompiler

    _budget(monkeypatch, hard_target=100_000)

    history = [
        {"role": "user", "content": "问题 " + "q" * 500},
        {"role": "assistant", "content": "回答 " + "r" * 500},
        {"role": "user", "content": "追问 " + "s" * 500},
    ]
    compiled = ContextCompiler().compile_history(history, model=_MODEL)

    assert compiled.omitted_event_ids == ()
    assert compiled.eviction_reasons == ()
    assert compiled.final_tokens <= compiled.hard_input_limit


def test_eviction_attributes_reasons_when_candidate_exceeds_hard(monkeypatch):
    """候选确实超过 hard 时，淘汰必须带区域与原因码，而不只是 id 列表。"""

    from klonet_agent.context.compiler import ContextCompiler

    # 20 个完整回合（每回合约 3k 字符 ≈ 750 token）总量约 15k，
    # 明显超过 10k 的 hard，必然触发淘汰。
    _budget(monkeypatch, hard_target=10_000)

    # 多个完整回合，总量明显超过 20k。
    history: list[dict] = []
    for index in range(20):
        history.append({"role": "user", "content": f"第{index}问 " + "u" * 1_500})
        history.append({"role": "assistant", "content": f"第{index}答 " + "a" * 1_500})
    history.append({"role": "user", "content": "最后的问题"})

    compiled = ContextCompiler().compile_history(history, model=_MODEL)

    assert compiled.omitted_event_ids, "超过 hard 时必须发生淘汰"
    assert compiled.eviction_reasons, "淘汰必须记录原因码"
    assert all(
        reason.startswith("history:") for reason in compiled.eviction_reasons
    ), f"历史淘汰应带 history: 前缀，实际 {compiled.eviction_reasons[:3]}"
    assert compiled.final_tokens <= compiled.hard_input_limit


# --------------------------------------------------------------------------- #
# 不变量 3：工具交换不被拆开
# --------------------------------------------------------------------------- #


def test_huge_tool_exchange_is_omitted_whole_never_split():
    """单个工具交换超过整个预算时整组 omit，绝不拆链。"""

    from klonet_agent.context.message_groups import (
        parse_message_groups,
        select_recent_groups,
    )

    messages = _tool_exchange(200_000)
    groups = [g for g in parse_message_groups(messages) if g.group_type != "system"]

    included, omitted = select_recent_groups(groups, token_budget=100)

    assert included == [], "整组放不下时必须整组淘汰"
    assert len(omitted) == 1
    roles = [message["role"] for message in omitted[0].messages]
    # 关键：tool_calls 与 tool result 都还在同一个组里，没有残片。
    assert "tool" in roles
    assert any(message.get("tool_calls") for message in omitted[0].messages)


def test_tool_chain_stays_intact_in_compiled_history(monkeypatch):
    """经完整编译后，历史里的工具交换仍保持完整（不需要 sanitize 修复）。"""

    from klonet_agent.context.compiler import ContextCompiler
    from klonet_agent.memory.store import sanitize_openai_tool_history

    _budget(monkeypatch, hard_target=100_000)

    history = [
        {"role": "user", "content": "先看看 nginx"},
        *_tool_exchange(2_000),
        {"role": "user", "content": "再确认一下"},
    ]
    compiled = ContextCompiler().compile_history(history, model=_MODEL)
    sanitized = sanitize_openai_tool_history(compiled.messages)

    assert len(sanitized) == len(compiled.messages), "编译结果不应需要修复"


# --------------------------------------------------------------------------- #
# 三段式 token 计量
# --------------------------------------------------------------------------- #


def test_three_stage_token_metering_is_monotonic(monkeypatch):
    """candidate >= post_compaction >= final，三个阶段都可审计。"""

    from klonet_agent.context.compiler import ContextCompiler

    _budget(monkeypatch, hard_target=100_000)

    history = [
        {"role": "user", "content": "旧问题 " + "a" * 8_000},
        {"role": "assistant", "content": "旧回答 " + "b" * 8_000},
        {"role": "user", "content": "当前问题"},
    ]
    compiled = ContextCompiler().compile_history(history, model=_MODEL)

    assert compiled.candidate_tokens >= compiled.post_compaction_tokens
    assert compiled.post_compaction_tokens >= compiled.final_tokens
    assert compiled.final_tokens == compiled.estimated_input_tokens


def test_inventory_never_trims(monkeypatch):
    """inventory 是只读计量：巨大候选也要如实报出，不做任何省略。"""

    from klonet_agent.context.budget import build_context_budget
    from klonet_agent.context.compiler import ContextCompiler, ContextRequest

    _budget(monkeypatch, hard_target=100_000)

    history = _tool_exchange(600_000)
    request = ContextRequest(
        model=_MODEL,
        system_messages=[{"role": "system", "content": "规则"}],
        history_messages=history[:-1],
        current_user_message=history[-1],
    )
    inventory = ContextCompiler().inventory(request)

    assert inventory.candidate_tokens > inventory.hard_input_limit
    assert inventory.exceeds_soft_limit is True
    assert inventory.exceeds_hard_limit is True
    # 三个分项之和 = 候选总量（不丢项）。
    assert inventory.candidate_tokens == (
        inventory.required_tokens
        + inventory.memory_pack_tokens
        + inventory.evidence_tokens
        + inventory.history_tokens
    )
