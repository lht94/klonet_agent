"""ContextCompiler 编译测试。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def _compiler():
    from klonet_agent.context.compiler import ContextCompiler

    return ContextCompiler()


def _small_window(monkeypatch):
    """把窗口缩到 4096，让预算相关断言小而确定。

    hard = 4096 - 512(输出预留) - 256(安全余量) = 3328
    soft = int(3328 * 0.70) = 2329
    """

    monkeypatch.setenv("KLONET_AGENT_CONTEXT_WINDOW", "4096")
    monkeypatch.setenv("KLONET_AGENT_MAX_OUTPUT_TOKENS", "512")
    monkeypatch.setenv("KLONET_AGENT_SAFETY_MARGIN_TOKENS", "256")


def _request(**overrides):
    from klonet_agent.context.compiler import ContextRequest

    defaults = dict(
        model="glm-5.2",
        system_messages=[
            {"role": "system", "content": "你是 Klonet 教学协作 Agent。"},
            {"role": "system", "content": "安全规则：不得越权操作。"},
        ],
        history_messages=[
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新回答"},
        ],
        current_user_message={"role": "user", "content": "当前问题"},
    )
    defaults.update(overrides)
    return ContextRequest(**defaults)


def test_required_zone_is_never_dropped():
    """系统规则、当前输入必须 100% 保留。"""

    compiled = _compiler().compile(_request())
    text = "".join(str(m.get("content")) for m in compiled.messages)
    assert "安全规则" in text
    assert "当前问题" in text
    assert compiled.messages[-1]["content"] == "当前问题"


def test_system_messages_come_first_and_keep_order():
    """系统消息置顶且保持相对顺序。"""

    compiled = _compiler().compile(_request())
    roles = [m["role"] for m in compiled.messages]
    assert roles[:2] == ["system", "system"]
    assert all(role != "system" for role in roles[2:])


def test_estimated_tokens_never_exceed_hard_limit():
    """编译结果估算 token 不得超过 hard input limit。"""

    long_history = [
        {"role": "user", "content": f"历史问题 {i} " + "x" * 200}
        for i in range(50)
    ]
    long_history = [
        msg
        for pair in zip(long_history[::2], [
            {"role": "assistant", "content": "y" * 100} for _ in range(25)
        ])
        for msg in pair
    ]
    compiled = _compiler().compile(_request(history_messages=long_history))
    assert compiled.estimated_input_tokens <= compiled.hard_input_limit


def test_older_history_evicted_before_newer(monkeypatch):
    """预算紧张时旧历史优先淘汰，最近回合保留。"""

    _small_window(monkeypatch)
    long_old = [
        {"role": "user", "content": "旧问题 " + "x" * 8000},
        {"role": "assistant", "content": "旧回答 " + "y" * 8000},
    ]
    recent = [
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]
    compiled = _compiler().compile(
        _request(history_messages=long_old + recent)
    )
    text = "".join(str(m.get("content")) for m in compiled.messages)
    assert "新问题" in text and "新回答" in text
    assert compiled.omitted_event_ids, "旧历史应被淘汰并记录"
    assert compiled.estimated_input_tokens <= compiled.hard_input_limit


def test_transient_messages_only_in_current_request():
    """临时控制消息进入必需区，且由调用方控制生命周期。"""

    compiled = _compiler().compile(
        _request(transient_messages=[{"role": "user", "content": "本轮临时指令"}])
    )
    text = "".join(str(m.get("content")) for m in compiled.messages)
    assert "本轮临时指令" in text
    # 编译器不持久化任何内容，history_messages 不受影响。
    assert all(m.get("content") != "本轮临时指令" for m in _request().history_messages)


def test_checkpoint_message_included_with_id():
    """checkpoint 消息进入必需区并回传 checkpoint_id。"""

    checkpoint = {
        "role": "system",
        "checkpoint_id": "cp-001",
        "content": "【任务检查点】目标：部署 Klonet；下一步：验证 nginx。",
    }
    compiled = _compiler().compile(_request(checkpoint_message=checkpoint))
    assert compiled.checkpoint_id == "cp-001"
    assert any(m is checkpoint for m in compiled.messages)


def test_evidence_zone_has_separate_budget(monkeypatch):
    """证据区有独立预算，超预算时从旧到新淘汰。"""

    _small_window(monkeypatch)
    evidence = [
        {"role": "user", "content": f"证据 {i} " + "e" * 1500}
        for i in range(10)
    ]
    compiled = _compiler().compile(_request(evidence_messages=evidence))
    evidence_in_message = [
        m for m in compiled.messages if str(m.get("content", "")).startswith("证据 ")
    ]
    assert 0 < len(evidence_in_message) < 10
    assert compiled.omitted_event_ids


def test_required_zone_over_hard_limit_raises_locally(monkeypatch):
    """必需区自身超硬预算时本地确定性拒绝，不请求供应商。"""

    _small_window(monkeypatch)

    import pytest

    from klonet_agent.context.compiler import ContextOverflowError

    with pytest.raises(ContextOverflowError) as exc_info:
        _compiler().compile(
            _request(
                current_user_message={
                    "role": "user",
                    "content": "超长输入 " + "z" * 20_000,
                },
            )
        )
    assert exc_info.value.areas


def test_tool_chain_in_history_stays_complete():
    """带工具链的历史经编译后不得出现残缺交换。"""

    from klonet_agent.memory.store import sanitize_openai_tool_history

    history = [
        {"role": "user", "content": "查 nginx"},
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
        {"role": "tool", "tool_call_id": "call_1", "content": "nginx active"},
        {"role": "assistant", "content": "nginx 正常"},
        {"role": "user", "content": "再查 redis"},
    ]
    compiled = _compiler().compile_history(history, model="glm-5.2")
    sanitized = sanitize_openai_tool_history(compiled.messages)
    assert len(sanitized) == len(compiled.messages), "编译结果不应需要修复"


def test_compile_history_splits_flat_history_correctly():
    """compile_history 便捷入口正确拆分扁平历史。"""

    history = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "当前问题"},
    ]
    compiled = _compiler().compile_history(history, model="glm-5.2")
    assert compiled.messages[0]["content"] == "系统规则"
    assert compiled.messages[-1]["content"] == "当前问题"
    assert compiled.estimated_input_tokens > 0
    assert compiled.hard_input_limit > 0


def test_tool_loop_tail_without_user_message_compiles():
    """工具循环中最后一条是 tool result 时也能编译（无当前输入）。"""

    history = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "查 redis"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "redis ok"},
    ]
    compiled = _compiler().compile_history(history, model="glm-5.2")
    text = "".join(str(m.get("content")) for m in compiled.messages)
    assert "redis ok" in text


# --------------------------------------------------------------------------- #
# Memory Pack（阶段 5）：记忆是证据，不是系统规则
# --------------------------------------------------------------------------- #


def _pack_message(
    text: str = "【按问题检索到的相关记忆】\n- 本项目运行时要求 Python 3.11",
    memory_ids: tuple[str, ...] = ("mem-1",),
) -> dict:
    return {
        "role": "user",
        "content": text,
        "_memory_pack": True,
        "_memory_pack_ids": list(memory_ids),
    }


def test_memory_pack_lands_in_evidence_zone_not_system_zone():
    compiled = _compiler().compile(_request(memory_pack_message=_pack_message()))

    all_text = "".join(str(m.get("content")) for m in compiled.messages)
    system_text = "".join(
        str(m.get("content")) for m in compiled.messages if m.get("role") == "system"
    )
    assert "Python 3.11" in all_text
    # 检索到的记忆不拥有系统规则的优先级。
    assert "Python 3.11" not in system_text
    assert compiled.areas["memory_pack"] > 0
    assert compiled.memory_pack_ids == ("mem-1",)


def test_memory_pack_sits_after_required_zone_and_before_current_input():
    compiled = _compiler().compile(_request(memory_pack_message=_pack_message()))
    contents = [str(m.get("content")) for m in compiled.messages]

    pack_index = next(
        index for index, text in enumerate(contents) if "按问题检索到的相关记忆" in text
    )
    assert pack_index >= 2  # 两条系统规则在前面
    # 当前用户输入必须仍是最后一条。
    assert contents[-1] == "当前问题"
    assert pack_index < len(contents) - 1


def test_memory_pack_has_a_budget_separate_from_other_evidence(monkeypatch):
    _small_window(monkeypatch)
    evidence = [{"role": "user", "content": "证据 " + "e" * 300} for _ in range(4)]
    compiled = _compiler().compile(
        _request(
            memory_pack_message=_pack_message(),
            evidence_messages=evidence,
        )
    )

    assert compiled.areas["memory_pack"] > 0
    assert compiled.areas["evidence"] > 0
    text = "".join(str(m.get("content")) for m in compiled.messages)
    # 两者互不挤占：记忆包与证据都在。
    assert "按问题检索到的相关记忆" in text
    assert "证据 " in text


def test_memory_pack_is_dropped_whole_when_it_cannot_fit(monkeypatch):
    """放不下就整条丢掉（不截断），并且必须被记为 omitted。"""

    _small_window(monkeypatch)
    huge = _pack_message(text="【按问题检索到的相关记忆】\n" + "记" * 20000)
    compiled = _compiler().compile(_request(memory_pack_message=huge))

    assert compiled.areas["memory_pack"] == 0
    assert compiled.memory_pack_ids == ()
    text = "".join(str(m.get("content")) for m in compiled.messages)
    assert "按问题检索到的相关记忆" not in text
    assert "memory_pack" in compiled.omitted_event_ids


def test_context_request_without_memory_pack_is_unchanged():
    compiled = _compiler().compile(_request())

    assert compiled.areas["memory_pack"] == 0
    assert compiled.memory_pack_ids == ()
    assert compiled.omitted_event_ids == ()
