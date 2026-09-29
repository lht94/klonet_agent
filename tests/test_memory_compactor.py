"""MemoryCompactor 结构化压缩测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

_MESSAGES = [
    {"role": "user", "content": "帮我部署 Klonet，但不能改 nginx 主配置。"},
    {"role": "assistant", "content": "好的，我先探测环境。"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "inspect_ops_context", "arguments": "{}"},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "docker 24.0 可用"},
    {"role": "assistant", "content": "环境探测完成，建议使用 Docker Compose。"},
]


def _valid_json_reply():
    return json.dumps(
        {
            "goal": "部署 Klonet 平台",
            "status": "in_progress",
            "constraints": ["不能修改 nginx 主配置"],
            "decisions": ["使用 Docker Compose 部署"],
            "completed_steps": ["环境探测完成"],
            "pending_steps": ["编写 compose 文件"],
            "failed_attempts": [],
            "evidence_refs": ["inspect_ops_context: docker 24.0 可用"],
            "touched_files": [],
            "verification": [],
            "unresolved_questions": [],
            "next_action": "编写 docker-compose.yml",
        },
        ensure_ascii=False,
    )


def _compactor(reply):
    from klonet_agent.memory.compactor import MemoryCompactor

    calls: list[list[dict]] = []

    def complete_fn(messages):
        calls.append(messages)
        return reply

    return MemoryCompactor(complete_fn), calls


def test_compact_produces_valid_checkpoint():
    compactor, calls = _compactor(_valid_json_reply())
    checkpoint = compactor.compact(
        _MESSAGES,
        user_id="u1",
        project_id="p1",
        mode="ops",
        source_event_start="msg-000000",
        source_event_end="msg-000004",
    )
    assert checkpoint.goal == "部署 Klonet 平台"
    assert checkpoint.next_action == "编写 docker-compose.yml"
    assert checkpoint.user_id == "u1"
    assert checkpoint.source_event_end == "msg-000004"
    assert len(calls) == 1
    # 压缩请求包含对话历史文本。
    assert "inspect_ops_context" in calls[0][-1]["content"]


def test_compact_tolerates_code_block_wrapping():
    wrapped = "```json\n" + _valid_json_reply() + "\n```"
    compactor, _ = _compactor(wrapped)
    checkpoint = compactor.compact(
        _MESSAGES,
        user_id="u1",
        project_id="p1",
        mode="mentor",
        source_event_end="msg-000004",
    )
    assert checkpoint.goal == "部署 Klonet 平台"


def test_invalid_reply_gets_one_repair_attempt():
    """第一次响应无效时只做一次修复重试，仍失败则确定性报错。"""

    import pytest

    from klonet_agent.memory.compactor import CompactionError

    invalid = "这不是 JSON"
    compactor, calls = _compactor(invalid)
    with pytest.raises(CompactionError):
        compactor.compact(
            _MESSAGES,
            user_id="u1",
            project_id="p1",
            mode="mentor",
            source_event_end="msg-000004",
        )
    assert len(calls) == 2, "应当恰好重试一次"
    # 修复请求里带了上一轮的无效回复作为上下文。
    assert any(
        m.get("role") == "assistant" and "这不是 JSON" in str(m.get("content"))
        for m in calls[1]
    )


def test_double_failure_with_previous_checkpoint_keeps_old():
    """两次都失败且有旧 checkpoint 时返回旧 checkpoint。"""

    from klonet_agent.memory.models import TaskCheckpoint

    previous = TaskCheckpoint(
        checkpoint_id="cp-old",
        version=3,
        user_id="u1",
        project_id="p1",
        mode="mentor",
        goal="旧目标",
        status="in_progress",
        constraints=(),
        decisions=(),
        completed_steps=("旧步骤",),
        pending_steps=(),
        failed_attempts=(),
        evidence_refs=(),
        touched_files=(),
        verification=(),
        unresolved_questions=(),
        next_action="旧下一步",
        source_event_start="msg-000000",
        source_event_end="msg-000010",
        created_at="2026-09-28T10:00:00+08:00",
    )
    compactor, calls = _compactor("垃圾输出")
    result = compactor.compact(
        _MESSAGES,
        previous_checkpoint=previous,
        user_id="u1",
        project_id="p1",
        mode="mentor",
    )
    assert result.checkpoint_id == "cp-old"
    assert len(calls) == 2


def test_double_failure_without_previous_raises():
    import pytest

    from klonet_agent.memory.compactor import CompactionError

    compactor, _ = _compactor("垃圾输出")
    with pytest.raises(CompactionError):
        compactor.compact(
            _MESSAGES, user_id="u1", project_id="p1", mode="mentor"
        )


def test_repair_succeeds_on_second_reply():
    """第一次输出缺 next_action，第二次修复成功。"""

    bad = json.dumps({"goal": "部署", "status": "in_progress"}, ensure_ascii=False)
    compactor, calls = _compactor_bad_then_good(bad)
    checkpoint = compactor.compact(
        _MESSAGES,
        user_id="u1",
        project_id="p1",
        mode="mentor",
        source_event_end="msg-000004",
    )
    assert checkpoint.next_action == "编写 docker-compose.yml"
    assert len(calls) == 2


def _compactor_bad_then_good(bad_reply):
    from klonet_agent.memory.compactor import MemoryCompactor

    replies = [bad_reply, _valid_json_reply()]
    calls: list[list[dict]] = []

    def complete_fn(messages):
        calls.append(messages)
        return replies.pop(0)

    return MemoryCompactor(complete_fn), calls
