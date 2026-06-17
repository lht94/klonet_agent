"""工作区沙箱与路径限制。"""

from __future__ import annotations

import shlex
from pathlib import Path


DANGEROUS_COMMANDS = {
    "rm",
    "sudo",
    "chmod",
    "chown",
    "mkfs",
    "dd",
    "shutdown",
    "reboot",
    "curl",
    "wget",
    "git",
    "pip",
    "npm",
}

ALLOWED_TEST_COMMANDS = {
    "pytest",
    "python",
    "python3",
}


class WorkspaceSandbox:
    """限制文件和命令只能在当前 workspace 内执行。"""

    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path.resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, path: str | Path) -> Path:
        """把用户输入路径解析到 workspace 内。"""

        raw = Path(path)
        target = raw if raw.is_absolute() else self.workspace_path / raw
        resolved = target.resolve()
        if not self.is_path_allowed(resolved):
            raise PermissionError(f"路径不在当前 workspace 内：{resolved}")
        return resolved

    def is_path_allowed(self, path: Path) -> bool:
        """判断路径是否位于 workspace 内。"""

        try:
            path.resolve().relative_to(self.workspace_path)
            return True
        except ValueError:
            return False

    def validate_test_command(self, command: str) -> list[str]:
        """校验测试命令，只允许白名单内的基础命令。"""

        parts = shlex.split(command)
        if not parts:
            raise PermissionError("测试命令不能为空。")
        program = Path(parts[0]).name
        if program not in ALLOWED_TEST_COMMANDS:
            raise PermissionError(f"不允许执行该命令：{program}")
        if any(Path(part).is_absolute() and not self.is_path_allowed(Path(part)) for part in parts[1:]):
            raise PermissionError("命令参数包含 workspace 外部路径。")
        return parts

    def reject_dangerous_command(self, command: str) -> str | None:
        """给旧 run_command 兼容层使用，发现危险命令时返回拒绝原因。"""

        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return f"命令解析失败：{exc}"
        if not parts:
            return "命令不能为空。"
        program = Path(parts[0]).name
        if program in DANGEROUS_COMMANDS:
            return f"出于安全考虑，当前阶段不允许直接执行 {program}。请使用结构化工具或人工确认。"
        return None
