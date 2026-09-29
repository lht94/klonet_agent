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


def test_restart_recovery_loads_events_after_checkpoint(tmp_path):
    """重启恢复：只加载 checkpoint 覆盖之后的事件。"""

    store = _memory_store(tmp_path)
    for i in range(3):
        store.append_history({"role": "user", "content": f"旧消息{i}"})
        store.append_history({"role": "assistant", "content": f"旧回答{i}"})

    fake_llm = FakeLLM(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, pytest.MonkeyPatch())
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


def test_corrupted_checkpoint_falls_back_to_marker_recovery(tmp_path):
    """checkpoint 损坏时回退到旧的恢复方式。"""

    from klonet_agent.memory.checkpoint_store import CheckpointStore

    store = _memory_store(tmp_path)
    store.append_history({"role": "user", "content": "未压缩消息"})

    fake_llm = FakeLLM(_valid_checkpoint_json())
    orchestrator = _orchestrator(tmp_path, fake_llm, store, pytest.MonkeyPatch())
    checkpoint_dir = CheckpointStore(store.memory_dir).checkpoint_dir
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "checkpoint-v1.json").write_text(
        "{损坏的 json", encoding="utf-8"
    )

    recovered = orchestrator._load_recovered_history()
    assert [m.get("content") for m in recovered] == ["未压缩消息"]
