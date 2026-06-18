"""工作区 Git 操作。"""

import subprocess
from pathlib import Path


SKIP_DIFF_PARTS = {".git", "__pycache__", ".pytest_cache"}


def show_diff(workspace_path: Path) -> str:
    """返回 workspace 内的 git diff。

    如果 workspace 不是独立 Git 仓库，降级返回文件摘要。第一阶段只读 diff，不做 commit/push。
    """

    workspace_path = workspace_path.resolve()
    git_root = _git_root(workspace_path)
    if git_root != workspace_path:
        return "当前 workspace 不是 Git 仓库，返回文件摘要：\n" + _file_summary(workspace_path)

    diff = subprocess.run(
        ["git", "diff", "--"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    output = diff.stdout or diff.stderr
    status_text = status.stdout.strip()
    if status_text:
        untracked = [
            line[3:].strip()
            for line in status_text.splitlines()
            if line.startswith("?? ")
        ]
        changed = [
            line
            for line in status_text.splitlines()
            if not line.startswith("?? ")
        ]
        parts = []
        if output:
            parts.append(output)
        if changed:
            parts.append("已跟踪文件变更：\n" + "\n".join(changed))
        if untracked:
            parts.append("未跟踪文件：\n" + "\n".join(untracked))
        return "\n\n".join(parts)
    return output or "当前没有检测到 diff。"


def _git_root(workspace_path: Path) -> Path | None:
    """返回 workspace 所属 Git 根目录。"""

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _file_summary(workspace_path: Path) -> str:
    """无 Git 时列出 workspace 内文件，作为 diff 降级信息。"""

    if not workspace_path.exists():
        return "workspace 不存在。"
    files = []
    for path in workspace_path.rglob("*"):
        if any(part in SKIP_DIFF_PARTS for part in path.parts):
            continue
        if path.is_file():
            files.append(path.relative_to(workspace_path).as_posix())
    if not files:
        return "当前 workspace 没有文件。"
    return "\n".join(files)
