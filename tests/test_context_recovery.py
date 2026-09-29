"""软压缩与恢复测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def _valid_checkpoint_json():
    return json.dumps(
        {
            "goal": "部署 Klonet 平台",
            "status": "in_progress",
            "constraints": ["不能修改 nginx 主配置"],
            "decisions": [],
            "completed_steps": ["环境探测"],
            "pending_steps": ["启动后端"],
            "failed_attempts": [],
            "evidence_refs": ["inspect_ops_context"],
            "touched_files": [],
            "verification": [],
            "unresolved_questions": [],
            "next_action": "运行 docker compose up -d",
        },
        ensure_ascii=False,
    )


class FakeResponse:
    def __init__(self, content: str):
        self.usage = type("Usage", (), {"total_tokens": 42})()
        message = type("Message", (), {"content": content})()
        self.choices = [type("Choice", (), {"message": message})()]


class FakeLLM:
    """返回固定文本的假客户端；记录每次收到的消息。"""

    model = "glm-5.2"

    def __init__(self, content: str):
        self.content = content
        self.received: list[list[dict]] = []

    def complete(self, messages, tools=None, stream=False):
        self.received.append(list(messages))
        return FakeResponse(self.content)


def _memory_store(tmp_path):
    from klonet_agent.memory.store import MemoryStore

    return MemoryStore(tmp_path / "memory", tmp_path / "USER.md")


def _orchestrator(tmp_path, fake_llm, memory_store, monkeypatch):
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    monkeypatch.setenv("KLONET_AGENT_CONTEXT_WINDOW", "2048")
    monkeypatch.setenv("KLONET_AGENT_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("KLONET_AGENT_SAFETY_MARGIN_TOKENS", "128")

    session = AgentSession(
        user_id="u1",
        project_id="p1",
        mode="mentor",
        workspace_path=tmp_path / "workspace",
        journal_path=tmp_path / "journal.md",
    )
    return AgentOrchestrator(
        profile=get_profile("mentor"),
        session=session,
        llm=fake_llm,
        trace_logger=TraceLogger(tmp_path / "trace.jsonl"),
        memory_store=memory_store,
    )


def test_count_and_load_history_after(tmp_path):
    """count_history_rows 与 load_history_after 配套工作。"""

    store = _memory_store(tmp_path)
    for i in range(5):
        store.append_history({"role": "user", "content": f"消息{i}"})
        store.append_history({"role": "assistant", "content": f"回答{i}"})

    assert store.count_history_rows() == 10
    after = store.load_history_after(6)
    assert [m["content"] for m in after] == ["消息3", "回答3", "消息4", "回答4"]


def test_soft_threshold_triggers_checkpoint_and_recompile(tmp_path, monkeypatch):
    """超过软阈值：先压缩生成 checkpoint，再携带 checkpoint 重新编译。"""

    store = _memory_store(tmp_path)
    fake_llm = FakeLLM(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)

    long_history = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "背景 " + "x" * 4000},
        {"role": "assistant", "content": "背景回答 " + "y" * 2000},
        {"role": "user", "content": "当前问题"},
    ]
    orchestrator.chat_with_llm(long_history)

    # checkpoint 已落盘。
    saved = orchestrator.checkpoint_store.load_latest()
    assert saved is not None
    assert saved.goal == "部署 Klonet 平台"
    assert saved.source_event_end.startswith("rows-")

    # 最终发给模型的消息里带有检查点 system 消息。
    sent = fake_llm.received[-1]
    assert any("【任务检查点】" in str(m.get("content")) for m in sent)


def test_compaction_failure_keeps_original_history(tmp_path, monkeypatch):
    """压缩两次失败时不产生 checkpoint，主流程继续。"""

    store = _memory_store(tmp_path)
    fake_llm = FakeLLM("这不是 JSON")
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)

    long_history = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "背景 " + "x" * 4000},
        {"role": "assistant", "content": "背景回答"},
        {"role": "user", "content": "当前问题"},
    ]
    orchestrator.chat_with_llm(long_history)

    assert orchestrator.checkpoint_store.load_latest() is None
    # 主请求仍然发出（压缩失败不阻断）。
    assert fake_llm.received


def test_restart_recovery_loads_events_after_checkpoint(tmp_path, monkeypatch):
    """重启恢复：只加载 checkpoint 覆盖之后的事件。"""

    store = _memory_store(tmp_path)
    for i in range(3):
        store.append_history({"role": "user", "content": f"旧消息{i}"})
        store.append_history({"role": "assistant", "content": f"旧回答{i}"})

    fake_llm = FakeLLM(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)
    orchestrator.checkpoint_store.save(
        orchestrator.memory_compactor.compact(
            [{"role": "user", "content": "旧对话"}],
            user_id="u1",
            project_id="p1",
            mode="mentor",
            source_event_start="rows-0",
            source_event_end=f"rows-{store.count_history_rows()}",
        )
    )

    # 重启后追加新事件。
    store.append_history({"role": "user", "content": "重启后的新消息"})
    store.append_history({"role": "assistant", "content": "新回答"})

    recovered = orchestrator._load_recovered_history()
    contents = [m.get("content") for m in recovered]
    assert "旧消息0" not in contents
    assert "重启后的新消息" in contents and "新回答" in contents


def test_corrupted_checkpoint_falls_back_to_marker_recovery(tmp_path, monkeypatch):
    """checkpoint 损坏时回退到旧的恢复方式。"""

    from klonet_agent.memory.checkpoint_store import CheckpointStore

    store = _memory_store(tmp_path)
    store.append_history({"role": "user", "content": "未压缩消息"})

    fake_llm = FakeLLM(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)
    checkpoint_dir = CheckpointStore(store.memory_dir).checkpoint_dir
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "checkpoint-v1.json").write_text(
        "{损坏的 json", encoding="utf-8"
    )

    recovered = orchestrator._load_recovered_history()
    assert [m.get("content") for m in recovered] == ["未压缩消息"]


# --------------------------------------------------------------------------
# 阶段 0 前置门（02 计划 P0）的验收测试
# --------------------------------------------------------------------------


class FakeLLMRequiringTools:
    """tools 是必填位置的假客户端，复现压缩路径的接口不兼容。"""

    model = "glm-5.2"

    def __init__(self, content: str):
        self.content = content
        self.received: list[tuple[list[dict], object]] = []

    def complete(self, messages, tools, stream=False):
        self.received.append((list(messages), tools))
        return FakeResponse(self.content)


def test_hard_overflow_refuses_instead_of_sending_full_history(tmp_path, monkeypatch):
    """必需区超过硬预算：抛确定性本地错误，绝不把完整历史发给供应商。"""

    from klonet_agent.context.compiler import ContextOverflowError

    store = _memory_store(tmp_path)
    fake_llm = FakeLLM("不应该被调用")
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)

    history = [
        {"role": "system", "content": "系统规则 " + "x" * 20000},
        {"role": "user", "content": "当前问题"},
    ]
    with pytest.raises(ContextOverflowError):
        orchestrator.chat_with_llm(history)

    assert fake_llm.received == []


def test_non_compiler_path_still_enforces_hard_limit(tmp_path, monkeypatch):
    """显式关闭编译器时，发送前硬预算断言仍然拦住超限请求。"""

    from klonet_agent import orchestrator as orchestrator_module
    from klonet_agent.context.compiler import ContextOverflowError

    store = _memory_store(tmp_path)
    fake_llm = FakeLLM("不应该被调用")
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)
    monkeypatch.setattr(orchestrator_module, "CONTEXT_COMPILER_ENABLED", False)

    history = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "x" * 20000},
    ]
    with pytest.raises(ContextOverflowError):
        orchestrator.chat_with_llm(history)

    assert fake_llm.received == []


def test_compaction_call_is_compatible_with_required_tools_argument(
    tmp_path, monkeypatch
):
    """压缩调用显式传 tools=None，兼容 tools 必填的 LLM 实现。"""

    store = _memory_store(tmp_path)
    fake_llm = FakeLLMRequiringTools(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)

    history = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "背景 " + "x" * 4000},
        {"role": "assistant", "content": "背景回答 " + "y" * 2000},
        {"role": "user", "content": "当前问题"},
    ]
    orchestrator.chat_with_llm(history)

    assert orchestrator.checkpoint_store.load_latest() is not None


def test_compaction_drops_covered_history_and_strips_event_id(tmp_path, monkeypatch):
    """checkpoint 生成后，同一请求不再携带被覆盖的原始历史。"""

    store = _memory_store(tmp_path)
    fake_llm = FakeLLM(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)

    # 显式带上事件行号，等价于 init_history 从 history.jsonl 恢复出来的消息。
    history = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "旧背景 " + "x" * 4000, "event_id": "rows-0"},
        {"role": "assistant", "content": "旧回答 " + "y" * 2000, "event_id": "rows-1"},
        {"role": "user", "content": "当前问题", "event_id": "rows-2"},
    ]
    orchestrator.chat_with_llm(history)

    sent = fake_llm.received[-1]
    contents = [str(message.get("content")) for message in sent]
    assert any("【任务检查点】" in content for content in contents)
    assert not any("旧背景" in content for content in contents)
    assert not any("旧回答" in content for content in contents)
    assert any("当前问题" in content for content in contents)

    # 事件身份只在本地使用，不能进入请求体。
    assert all("event_id" not in message for message in sent)

    saved = orchestrator.checkpoint_store.load_latest()
    assert saved is not None
    assert saved.source_event_end == "rows-2"


def test_recovery_is_not_capped_by_message_count(tmp_path, monkeypatch):
    """恢复不再固定 20 条：checkpoint 之后的事件全部作为候选。"""

    store = _memory_store(tmp_path)
    fake_llm = FakeLLM(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, monkeypatch)
    orchestrator.checkpoint_store.save(
        orchestrator.memory_compactor.compact(
            [{"role": "user", "content": "旧对话"}],
            user_id="u1",
            project_id="p1",
            mode="mentor",
            source_event_start="rows-0",
            source_event_end="rows-0",
        )
    )

    for index in range(40):
        store.append_history({"role": "user", "content": f"新消息{index}"})
        store.append_history({"role": "assistant", "content": f"新回答{index}"})

    recovered = orchestrator._load_recovered_history()
    assert len(recovered) == 80
    assert recovered[0]["content"] == "新消息0"
    assert recovered[-1]["content"] == "新回答39"


def test_legacy_compression_gate_is_closed_by_default(tmp_path, monkeypatch):
    """旧压缩路径默认关闭，只有显式打开才恢复 20 条后缀与 MAX_TOKEN 判断。"""

    from klonet_agent import orchestrator as orchestrator_module

    store = _memory_store(tmp_path)
    orchestrator = _orchestrator(tmp_path, FakeLLM("x"), store, monkeypatch)

    assert orchestrator_module.LEGACY_MEMORY_COMPRESSION_ENABLED is False
    assert orchestrator._legacy_history_cap() == 0

    monkeypatch.setattr(orchestrator_module, "LEGACY_MEMORY_COMPRESSION_ENABLED", True)
    assert orchestrator._legacy_history_cap() == orchestrator_module.HISTORY_MAX_MESSAGES
