"""workspace 与结构化工具测试。"""

import sys
from pathlib import Path

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_workspace_file_tools():
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.file_ops import list_files, read_file, write_file

    with local_temp_dir() as temp_dir:
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        write_file(session, "src/demo.py", "print('hello')\n")

        assert "src/" in list_files(session, ".")
        assert "hello" in read_file(session, "src/demo.py")


def test_workspace_blocks_escape():
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.file_ops import read_file

    with local_temp_dir() as temp_dir:
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )

        try:
            read_file(session, "../outside.txt")
        except PermissionError as exc:
            assert "workspace" in str(exc)
        else:
            raise AssertionError("应该拒绝读取 workspace 外部路径")


def test_show_diff_reports_files_when_workspace_has_no_git():
    """无 Git 仓库时，show_diff 应该降级返回文件变更摘要。"""

    from klonet_agent.workspace.git_ops import show_diff

    with local_temp_dir() as temp_dir:
        workspace = temp_dir / "workspace"
        workspace.mkdir()
        (workspace / "demo.py").write_text("print('hello')\n", encoding="utf-8")

        result = show_diff(workspace)

    assert "当前 workspace 不是 Git 仓库" in result
    assert "demo.py" in result


def test_show_diff_reports_untracked_files_in_git_workspace():
    """Git 仓库中新增但未暂存的文件，也应该在 show_diff 中可见。"""

    import subprocess

    from klonet_agent.workspace.git_ops import show_diff

    with local_temp_dir() as temp_dir:
        workspace = temp_dir / "workspace"
        workspace.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        (workspace / "new_file.py").write_text("print('new')\n", encoding="utf-8")

        result = show_diff(workspace)

    assert "未跟踪文件" in result
    assert "new_file.py" in result
