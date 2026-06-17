"""Shell 命令工具。"""

import subprocess

from klonet_agent.session import AgentSession
from klonet_agent.workspace.manager import WORKSPACE_MANAGER

# 定义完工具 schema，还要实现配套的工具函数。
# 也就是说，大模型本质上还是在“说话”，真正执行命令的是这里的 Python 代码。
def run_command_win(command: str) -> str:
    """在 Windows 上执行命令并返回文本输出。"""

    # Windows 系统强制调用 PowerShell，捕获 stdout/stderr 并以文本返回。
    wrapped = ["powershell", "-NoProfile", "-Command", command]
    try:
        result = subprocess.run(
            wrapped,
            capture_output=True,
            text=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        # 让 LLM 能看到命令超时的信息。
        return f"{command}命令超时30s"
    except Exception as exc:
        return f"{command}命令执行出错: {str(exc)}"

    # result.stdout 是标准输出，result.stderr 是标准错误。
    # 这里统一做解码回退，减少 Windows 中文乱码。
    output_bytes = result.stdout or result.stderr or b""
    for encoding in ("utf-8", "gb18030", "cp936"):
        try:
            return output_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return output_bytes.decode("utf-8", errors="replace")


def run_command_linux(command: str) -> str:
    """在 Linux/macOS 上执行 Shell 命令并返回文本输出。"""

    # command: str 和 -> str 都是类型注解，主要帮助阅读和编辑器提示，不会自动做运行时校验。
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # stdout 是正常输出，stderr 是错误信息；这里返回其中有内容的那个。
    return result.stdout or result.stderr


def run_tests(session: AgentSession, command: str = "pytest -q") -> str:
    """在当前 workspace 内运行测试命令。

    第一阶段只允许 pytest/python/python3，避免模型直接执行任意 shell。
    """

    sandbox = WORKSPACE_MANAGER.sandbox_for(session)
    try:
        parts = sandbox.validate_test_command(command)
    except PermissionError as exc:
        return f"Error: {exc}"
    try:
        result = subprocess.run(
            parts,
            cwd=session.workspace_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return f"{command} 命令超时 60s"
    return result.stdout or result.stderr or f"命令执行完成，退出码：{result.returncode}"
