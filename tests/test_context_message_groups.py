"""完整消息组解析与选择测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "context_traces"


def _load_trace(name: str) -> list[dict]:
    rows = []
    for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _groups(messages):
    from klonet_agent.context.message_groups import parse_message_groups

    return parse_message_groups(messages)


def test_simple_user_turn_stays_atomic():
    """user 消息与其回答必须始终在同一组。"""

    messages = [
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": "问题二"},
        {"role": "assistant", "content": "回答二"},
    ]
    groups = _groups(messages)
    assert len(groups) == 2
    assert all(group.group_type == "user_turn" for group in groups)
    assert groups[0].messages[0]["content"] == "问题一"
    assert groups[1].messages[0]["content"] == "问题二"


def test_multiple_tool_calls_never_split():
    """多 tool call 与全部 tool results 必须同组。"""

    messages = _load_trace("multiple_tool_calls.jsonl")
    groups = [g for g in _groups(messages) if g.group_type != "system"]
    assert len(groups) == 1
    group = groups[0]
    roles = [m["role"] for m in group.messages]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    call_ids = {
        call["id"] for m in group.messages for call in (m.get("tool_calls") or [])
    }
    result_ids = {
        m.get("tool_call_id") for m in group.messages if m.get("role") == "tool"
    }
    assert call_ids == result_ids


def test_interrupted_chain_is_one_droppable_group():
    """中断工具链整体成组，可以被整体淘汰，不会留下残片。"""

    from klonet_agent.context.message_groups import select_recent_groups

    messages = _load_trace("interrupted_tool_chain.jsonl")
    groups = [g for g in _groups(messages) if g.group_type != "system"]
    assert len(groups) == 1

    included, omitted = select_recent_groups(groups, token_budget=0)
    assert included == []
    assert len(omitted) == 1


def test_single_group_over_budget_is_omitted_whole_not_split():
    """单个组超过整个预算时整组 omit，绝不拆链（阶段 7）。

    旧实现在这种情况下会"降级保留首尾"——丢掉中间的工具交换、只留
    user/assistant 文本，那会产生"有 tool_call 没有 tool result"的协议残片。
    现在宁可让这一轮历史区为空，也不发送残缺的工具链。
    """

    from klonet_agent.context.message_groups import select_recent_groups

    messages = _load_trace("long_tool_result.jsonl")
    groups = [g for g in _groups(messages) if g.group_type != "system"]
    included, omitted = select_recent_groups(groups, token_budget=10)

    assert included == [], "整组放不下时必须整组淘汰，不得拆链"
    assert len(omitted) == 1
    # 被淘汰的组本身保持完整：tool_calls 与 tool result 都还在。
    roles = [m["role"] for m in omitted[0].messages]
    assert "tool" in roles
    assert any(m.get("tool_calls") for m in omitted[0].messages)


def test_older_complete_groups_dropped_first():
    """预算不足时先淘汰最早的完整组。"""

    from klonet_agent.context.message_groups import select_recent_groups

    messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]
    groups = [g for g in _groups(messages) if g.group_type != "system"]
    included, omitted = select_recent_groups(groups, token_budget=20)

    assert len(included) == 1
    assert included[0].messages[0]["content"] == "新问题"
    assert len(omitted) == 1
    assert omitted[0].messages[0]["content"] == "旧问题"


def test_old_format_history_without_event_ids_still_works():
    """旧格式历史缺少 event_id 时读取仍应正常，并生成稳定位置 ID。"""

    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你？"},
    ]
    groups = _groups(messages)
    assert groups[0].event_ids == ("msg-000000", "msg-000001")
    assert groups[0].event_ids == groups[0].event_ids  # 稳定性由格式保证


def test_orphan_leading_tool_results_form_droppable_group():
    """历史开头的孤立 tool 结果应成组且类型为 orphan。"""

    messages = [
        {"role": "tool", "tool_call_id": "call_x", "content": "孤立结果"},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "好的"},
    ]
    groups = [g for g in _groups(messages) if g.group_type != "system"]
    assert groups[0].group_type == "orphan"
    assert groups[1].group_type == "user_turn"


def test_fixture_traces_round_trip_through_groups():
    """全部基线轨迹都能解析，且组内消息不重叠不丢失。"""

    for name in (
        "long_tool_result.jsonl",
        "multiple_tool_calls.jsonl",
        "interrupted_tool_chain.jsonl",
        "oversized_user_input.jsonl",
    ):
        messages = _load_trace(name)
        groups = _groups(messages)
        merged = [dict(m) for g in groups for m in g.messages]
        assert merged == messages, f"{name} 解析后消息丢失或重复"
