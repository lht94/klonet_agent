"""CLI 启动方式测试。

这些测试不用手动修改 sys.path，尽量模拟用户在仓库根目录直接运行命令的场景。
"""

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeStream:
    """模拟 Windows GBK stdout/stderr。"""

    encoding = "cp936"

    def __init__(self):
        self.config = {}

    def reconfigure(self, **kwargs):
        self.config.update(kwargs)


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
