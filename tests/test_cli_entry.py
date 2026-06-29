"""CLI 启动方式测试。

这些测试不用手动修改 sys.path，尽量模拟用户在仓库根目录直接运行命令的场景。
"""

import importlib
import io
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeStream:
    """模拟 Windows GBK stdout/stderr。"""

    encoding = "cp936"

    def __init__(self, is_tty=False, content=""):
        self.config = {}
        self._is_tty = is_tty
        self._content = content

    def reconfigure(self, **kwargs):
        self.config.update(kwargs)

    def isatty(self):
        return self._is_tty

    def read(self):
        return self._content


def test_module_cli_can_run_from_project_root():
    """用户在仓库根目录下也应该可以用模块方式查看帮助。"""

    result = subprocess.run(
        [sys.executable, "-m", "klonet_agent.agent", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "--mode" in result.stdout
    assert "--user-id" in result.stdout
    assert "--project-id" in result.stdout


def test_script_cli_can_run_from_project_root():
    """用户在仓库根目录下直接运行 agent.py 也应该可以查看帮助。"""

    result = subprocess.run(
        [sys.executable, "agent.py", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "--mode" in result.stdout
    assert "--user-id" in result.stdout
    assert "--project-id" in result.stdout


def test_cli_configures_utf8_output_streams():
    """CLI 启动时应该把输出流配置成 UTF-8，避免 Windows GBK 崩溃。"""

    from klonet_agent.app.cli import configure_console_encoding

    stdout = FakeStream()
    stderr = FakeStream()

    configure_console_encoding(stdout=stdout, stderr=stderr)

    assert stdout.config["encoding"] == "utf-8"
    assert stdout.config["errors"] == "replace"
    assert stderr.config["encoding"] == "utf-8"
    assert stderr.config["errors"] == "replace"



def test_cli_configures_non_interactive_stdin_as_utf8():
    """管道 stdin 应使用 UTF-8 严格解码，不能静默替换中文。"""

    from klonet_agent.app.cli import configure_console_encoding

    stdin = FakeStream(is_tty=False)
    configure_console_encoding(
        stdin=stdin,
        stdout=FakeStream(),
        stderr=FakeStream(),
    )

    assert stdin.config["encoding"] == "utf-8"
    assert stdin.config["errors"] == "strict"


def test_cli_keeps_interactive_stdin_encoding():
    """交互式控制台继续使用系统输入机制，不强制修改编码。"""

    from klonet_agent.app.cli import configure_console_encoding

    stdin = FakeStream(is_tty=True)
    configure_console_encoding(
        stdin=stdin,
        stdout=FakeStream(),
        stderr=FakeStream(),
    )

    assert stdin.config == {}


def test_piped_prompt_preserves_multiline_chinese_as_one_turn():
    """UTF-8 管道中的多行中文应作为一个完整用户问题。"""

    from klonet_agent.app.cli import read_piped_prompt
    from klonet_agent.knowledge import route_query

    content = (
        "我想构建虚拟机，不需要 Klonet 环境。\n"
        "主要需求：Docker Compose、DinD 和 Rust 编译环境。\n"
    )
    stdin = io.StringIO(content)

    prompt = read_piped_prompt(stdin)

    assert prompt == content.strip()
    assert route_query(prompt).scope == "general"


def test_cli_does_not_clear_user_input_line():
    """用户提交的问题应保留在终端历史里，不能被 agent 清掉。"""

    import klonet_agent.app.cli as cli

    assert not hasattr(cli, "clear_interactive_input_line")


def test_cli_loads_readline_for_interactive_non_windows_terminal(monkeypatch):
    """Unix 交互终端应启用系统 readline 处理多字节字符退格。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)
    loaded = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", loaded.append)

    configure(
        stdin=FakeStream(is_tty=True),
        stdout=FakeStream(is_tty=True),
    )

    assert loaded == ["readline"]


def test_cli_keeps_native_windows_interactive_input(monkeypatch):
    """Windows 应保留已经正常工作的原生控制台输入。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)
    loaded = []
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(importlib, "import_module", loaded.append)

    configure(
        stdin=FakeStream(is_tty=True),
        stdout=FakeStream(is_tty=True),
    )

    assert loaded == []


@pytest.mark.parametrize(
    ("stdin_is_tty", "stdout_is_tty"),
    [(False, True), (True, False), (False, False)],
)
def test_cli_does_not_load_readline_for_redirected_streams(
    monkeypatch,
    stdin_is_tty,
    stdout_is_tty,
):
    """管道或重定向流不能切换成交互式 readline。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)
    loaded = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", loaded.append)

    configure(
        stdin=FakeStream(is_tty=stdin_is_tty),
        stdout=FakeStream(is_tty=stdout_is_tty),
    )

    assert loaded == []


def test_cli_allows_missing_optional_readline(monkeypatch):
    """精简 Unix Python 缺少 readline 时仍应正常启动。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)

    def missing_readline(name):
        raise ModuleNotFoundError("No module named 'readline'", name=name)

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", missing_readline)

    configure(
        stdin=FakeStream(is_tty=True),
        stdout=FakeStream(is_tty=True),
    )


def test_cli_does_not_hide_unrelated_readline_import_errors(monkeypatch):
    """readline 内部依赖故障不能被误当成可选模块缺失。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)

    def missing_dependency(name):
        raise ModuleNotFoundError("No module named '_readline'", name="_readline")

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", missing_dependency)

    with pytest.raises(ModuleNotFoundError, match="_readline"):
        configure(
            stdin=FakeStream(is_tty=True),
            stdout=FakeStream(is_tty=True),
        )
