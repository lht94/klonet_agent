"""上下文管理基线测试。

在修改上下文算法之前，先用固定轨迹回答"一轮请求的 token 主要消耗在哪里"。
这些 fixture 同时是后续 message_groups / compiler / compactor 的回归输入。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "context_traces"

EXPECTED_TRACES = {
    # trace 文件名 -> (期望消息数下限, 轨迹特征)
    "long_tool_result.jsonl": (4, "tool"),
    "multiple_tool_calls.jsonl": (5, "tool"),
    "interrupted_tool_chain.jsonl": (2, "interrupted"),
    "oversized_user_input.jsonl": (1, "user"),
}


def _load_trace(name: str) -> list[dict]:
    rows = []
    for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def test_fixtures_are_complete_and_repeatable():
    """四条合成轨迹都必须存在、可解析且可重复加载。"""

    for name in EXPECTED_TRACES:
        first = _load_trace(name)
        second = _load_trace(name)
        assert first == second, f"{name} 两次加载结果不一致"
        min_count, _ = EXPECTED_TRACES[name]
        assert len(first) >= min_count, f"{name} 消息数不足"
        assert all(isinstance(row.get("role"), str) for row in first)


def test_fixture_has_no_sensitive_fields():
    """fixture 必须脱敏：不允许出现密钥、令牌、密码字段。"""

    banned_keys = {"api_key", "apikey", "token", "password", "secret"}
    for name in EXPECTED_TRACES:
        for row in _load_trace(name):
            for key in row:
                assert key.lower() not in banned_keys, f"{name} 含敏感字段 {key}"
            text = json.dumps(row, ensure_ascii=False).lower()
            for word in ("sk-", "password=", "bearer "):
                assert word not in text, f"{name} 含敏感内容 {word}"


def test_long_tool_result_fixture_is_dominated_by_tool_output():
    """长工具结果轨迹中，token 消耗应当集中在 tool 消息上。"""

    from klonet_agent.context.tokens import estimate_message_tokens

    messages = _load_trace("long_tool_result.jsonl")
    tool_tokens = sum(
        estimate_message_tokens(m) for m in messages if m.get("role") == "tool"
    )
    other_tokens = sum(
        estimate_message_tokens(m) for m in messages if m.get("role") != "tool"
    )
    assert tool_tokens > other_tokens, "tool 输出应当是该轨迹的主要 token 消耗"


def test_interrupted_tool_chain_fixture_is_really_incomplete():
    """中断工具链轨迹必须真的缺少 tool result，用于验证分组器的容错。"""

    messages = _load_trace("interrupted_tool_chain.jsonl")
    assistant = [m for m in messages if m.get("role") == "assistant"]
    tools = [m for m in messages if m.get("role") == "tool"]
    assert len(assistant) == 1 and assistant[0].get("tool_calls")
    assert len(tools) == 0, "中断轨迹不应包含任何 tool result"


def test_oversized_user_input_fixture_is_single_long_turn():
    """超长用户输入轨迹：单条 user 消息远超普通消息长度。"""

    messages = _load_trace("oversized_user_input.jsonl")
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert len(messages[0]["content"]) > 800


def test_baseline_stats_answer_where_tokens_go():
    """基线统计：能够回答每条轨迹的消息数、最大消息字符数与估算 token。"""

    from klonet_agent.context.tokens import estimate_messages_tokens

    stats = {}
    for name in EXPECTED_TRACES:
        messages = _load_trace(name)
        stats[name] = {
            "message_count": len(messages),
            "max_message_chars": max(len(json.dumps(m, ensure_ascii=False)) for m in messages),
            "estimated_tokens": estimate_messages_tokens(messages),
        }
    # 至少能区分出"哪条轨迹最贵"。
    most_expensive = max(stats, key=lambda name: stats[name]["estimated_tokens"])
    assert most_expensive in {"long_tool_result.jsonl", "oversized_user_input.jsonl"}
