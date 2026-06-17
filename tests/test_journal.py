"""项目日志测试。"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_project_journal(tmp_path):
    from klonet_agent.journal.project_journal import ProjectJournal

    journal = ProjectJournal(tmp_path / "u1" / "p1.md", "u1", "p1")
    journal.ensure("实现 Klonet 测试功能")
    journal.update_status("开发中")
    journal.append_event("执行记录", "完成需求分析")
    journal.record_test_result("pytest -q 通过")

    text = journal.read()
    assert "实现 Klonet 测试功能" in text
    assert "当前状态：开发中" in text
    assert "完成需求分析" in text
    assert "pytest -q 通过" in text
