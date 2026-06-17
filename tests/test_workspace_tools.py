"""workspace 与结构化工具测试。"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_workspace_file_tools(tmp_path):
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.file_ops import list_files, read_file, write_file

    session = AgentSession(
        user_id="u1",
        project_id="p1",
        workspace_path=tmp_path / "workspace",
        journal_path=tmp_path / "journal.md",
    )
    write_file(session, "src/demo.py", "print('hello')\n")

    assert "src/" in list_files(session, ".")
    assert "hello" in read_file(session, "src/demo.py")


def test_workspace_blocks_escape(tmp_path):
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.file_ops import read_file

    session = AgentSession(
        user_id="u1",
        project_id="p1",
        workspace_path=tmp_path / "workspace",
        journal_path=tmp_path / "journal.md",
    )

    try:
        read_file(session, "../outside.txt")
    except PermissionError as exc:
        assert "workspace" in str(exc)
    else:
        raise AssertionError("应该拒绝读取 workspace 外部路径")
