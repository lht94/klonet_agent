"""项目日志测试。"""

import sys
from pathlib import Path

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_project_journal():
    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("实现 Klonet 测试功能")
        journal.update_status("开发中")
        journal.append_event("执行记录", "完成需求分析")
        journal.record_test_result("pytest -q 通过")

        text = journal.read()

    assert "实现 Klonet 测试功能" in text
    assert "当前状态：开发中" in text
    assert "完成需求分析" in text
    assert "pytest -q 通过" in text


def test_project_journal_summary_is_shorter_than_full_log():
    """项目日志应该能生成摘要，避免每次全量注入上下文。"""

    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("实现 Klonet 测试功能")
        journal.update_status("开发中")
        for index in range(20):
            journal.append_event("执行记录", f"完成第 {index} 个开发步骤")

        full_text = journal.read()
        summary = journal.summary(max_chars=300)

    assert "项目日志摘要" in summary
    assert "当前状态：开发中" in summary
    assert "实现 Klonet 测试功能" in summary
    assert len(summary) <= 300
    assert len(summary) < len(full_text)


def test_read_project_journal_tool_can_return_summary():
    """读取项目日志工具应该支持摘要返回。"""

    from klonet_agent.journal.project_journal import ProjectJournal
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor

    with local_temp_dir() as temp_dir:
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        journal = ProjectJournal.from_session(session)
        journal.ensure("实现 Klonet 测试功能")
        for index in range(20):
            journal.append_event("执行记录", f"完成第 {index} 个开发步骤")

        executor = ToolExecutor(
            session=session,
            allowed_tools={"read_project_journal"},
        )
        result = executor.run("read_project_journal", {"max_chars": 260})

    assert "项目日志摘要" in result
    assert len(result) <= 260
