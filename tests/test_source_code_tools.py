"""Klonet 源码只读检索工具测试。"""

import sys
from pathlib import Path

import pytest

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_search_code_finds_literal_matches_inside_source_root(monkeypatch):
    """search_code 应该只在 Klonet 源码根目录内做高效文本检索。"""

    from klonet_agent.tools import source_code

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "klonet_knowledge" / "02_vemu_uestc_code"
        target = source_root / "mains" / "web_terminal_main.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            "PORT = 8081\n"
            "def start_web_terminal():\n"
            "    return 'web-terminal address already in use'\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(source_code, "SOURCE_ROOT", source_root)

        result = source_code.search_code("address already in use")

    assert "mains/web_terminal_main.py:3" in result
    assert "address already in use" in result


def test_read_source_file_reads_line_window_and_blocks_escape(monkeypatch):
    """read_source_file 只能读取源码根目录内文件，并支持行范围。"""

    from klonet_agent.tools import source_code

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "klonet_knowledge" / "02_vemu_uestc_code"
        source_file = source_root / "mains" / "gun.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text(
            "line1\nline2 target\nline3\nline4\n",
            encoding="utf-8",
        )
        outside = temp_dir / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        monkeypatch.setattr(source_code, "SOURCE_ROOT", source_root)

        result = source_code.read_source_file("mains/gun.py", start_line=2, end_line=3)

        assert "mains/gun.py:2" in result
        assert "line2 target" in result
        assert "line1" not in result
        with pytest.raises(PermissionError):
            source_code.read_source_file("../secret.txt")


def test_list_source_files_lists_relative_paths_and_blocks_escape(monkeypatch):
    """list_source_files 返回源码根内相对路径，拒绝越界路径。"""

    from klonet_agent.tools import source_code

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "klonet_knowledge" / "02_vemu_uestc_code"
        (source_root / "mains").mkdir(parents=True)
        (source_root / "mains" / "gun.py").write_text("x", encoding="utf-8")
        (source_root / "README.md").write_text("readme", encoding="utf-8")
        monkeypatch.setattr(source_code, "SOURCE_ROOT", source_root)

        result = source_code.list_source_files("mains")

        assert "mains/gun.py" in result
        assert "README.md" not in result
        with pytest.raises(PermissionError):
            source_code.list_source_files("../")


def test_tool_executor_dispatches_source_code_tools(monkeypatch):
    """ToolExecutor 应能分发源码工具，供 Mentor 调用。"""

    from klonet_agent.tools import source_code
    from klonet_agent.tools.executor import ToolExecutor

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "klonet_knowledge" / "02_vemu_uestc_code"
        target = source_root / "mains" / "web_terminal_main.py"
        target.parent.mkdir(parents=True)
        target.write_text("def main():\n    return 'web_terminal'\n", encoding="utf-8")
        monkeypatch.setattr(source_code, "SOURCE_ROOT", source_root)

        executor = ToolExecutor(
            allowed_tools={"search_code", "read_source_file", "list_source_files"}
        )

        assert "web_terminal" in executor.run("search_code", {"query": "web_terminal"})
        assert "def main" in executor.run(
            "read_source_file", {"path": "mains/web_terminal_main.py"}
        )
        assert "mains/web_terminal_main.py" in executor.run(
            "list_source_files", {"path": "mains"}
        )

