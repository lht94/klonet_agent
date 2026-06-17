"""工作区 Git 操作。"""

import subprocess
from pathlib import Path


def show_diff(workspace_path: Path) -> str:
    """返回 workspace 内的 git diff。

    如果 workspace 不是 Git 仓库，返回友好提示。第一阶段只读 diff，不做 commit/push。
    """

    result = subprocess.run(
        ["git", "diff", "--"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = result.stdout or result.stderr
    if "not a git repository" in output.lower():
        return "当前 workspace 不是 Git 仓库，无法生成 git diff。"
    return output or "当前没有检测到 diff。"
