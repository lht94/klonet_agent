"""CLI 启动方式测试。

这些测试不用手动修改 sys.path，尽量模拟用户在仓库根目录直接运行命令的场景。
"""

import io
import subprocess
import sys
from pathlib import Path


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


def test_cli_can_clear_interactive_input_line():
    """Ubuntu 交互终端提交输入后，应能清掉上一行避免残留。"""

    from klonet_agent.app.cli import clear_interactive_input_line

    class TtyWriter:
        def __init__(self):
            self.output = ""
            self.flushed = False

        def isatty(self):
            return True

        def write(self, value):
            self.output += value

        def flush(self):
            self.flushed = True

    stdout = TtyWriter()
    clear_interactive_input_line(stdout=stdout)

    assert stdout.output == "\033[A\033[2K"
    assert stdout.flushed is True
