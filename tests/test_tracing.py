"""Trace 日志测试。"""

import json
import sys
from pathlib import Path

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_trace_logger_records_tool_event():
    """工具调用 trace 应该写成 JSONL，方便后续评估统计。"""

    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        logger = TraceLogger(temp_dir / "trace.jsonl")
        logger.record_tool_call(
            user_id="u1",
            project_id="p1",
            mode="coding",
            tool_name="run_tests",
            status="success",
            duration_ms=12,
            args={"command": "pytest -q"},
            result="2 passed",
        )

        rows = [
            json.loads(line)
            for line in (temp_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    assert len(rows) == 1
    assert rows[0]["event"] == "tool_call"
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["project_id"] == "p1"
    assert rows[0]["mode"] == "coding"
    assert rows[0]["tool_name"] == "run_tests"
    assert rows[0]["status"] == "success"
    assert rows[0]["duration_ms"] == 12


def test_tool_executor_writes_trace_for_denied_tool():
    """执行器拒绝工具时也要记录 trace，方便排查权限问题。"""

    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor
    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        logger = TraceLogger(temp_dir / "trace.jsonl")
        executor = ToolExecutor(
            session=session,
            allowed_tools={"search_knowledge"},
            trace_logger=logger,
        )

        result = executor.run("write_file", {"path": "demo.py", "content": "print(1)"})
        text = (temp_dir / "trace.jsonl").read_text(encoding="utf-8")

    row = json.loads(text)
    assert result.startswith("Error:")
    assert row["event"] == "tool_call"
    assert row["tool_name"] == "write_file"
    assert row["status"] == "denied"


def test_tool_executor_truncates_long_result():
    """工具结果过长时，执行器应该统一截断，避免塞爆上下文。"""

    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor

    class LongResultExecutor(ToolExecutor):
        def _run_allowed_tool(self, tool_name, tool_args):
            return "A" * 13000

    executor = LongResultExecutor(session=AgentSession(), allowed_tools={"demo"})
    result = executor.run("demo", {})

    assert len(result) < 13000
    assert "工具结果过长，已截断" in result


def test_trace_logger_records_llm_usage():
    """LLM token 和耗时也应该进入 trace。"""

    from klonet_agent.tracing.logger import TraceLogger

    with local_temp_dir() as temp_dir:
        logger = TraceLogger(temp_dir / "trace.jsonl")
        logger.record_llm_call(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            total_tokens=128,
            duration_ms=34,
        )

        row = json.loads((temp_dir / "trace.jsonl").read_text(encoding="utf-8"))

    assert row["event"] == "llm_call"
    assert row["total_tokens"] == 128
    assert row["duration_ms"] == 34


def test_orchestrator_records_llm_trace():
    """编排器调用模型时应该记录 LLM trace。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    class FakeUsage:
        total_tokens = 77

    class FakeResponse:
        usage = FakeUsage()

    class FakeLLM:
        def complete(self, messages, tools):
            return FakeResponse()

    with local_temp_dir() as temp_dir:
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            mode="mentor",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        logger = TraceLogger(temp_dir / "trace.jsonl")
        orchestrator = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=session,
            llm=FakeLLM(),
            trace_logger=logger,
        )
        response = orchestrator.chat_with_llm([{"role": "user", "content": "hi"}])
        row = json.loads((temp_dir / "trace.jsonl").read_text(encoding="utf-8"))

    assert response.usage.total_tokens == 77
    assert row["event"] == "llm_call"
    assert row["mode"] == "mentor"
    assert row["total_tokens"] == 77
