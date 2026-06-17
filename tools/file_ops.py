"""文件读写工具。"""

from pathlib import Path

from klonet_agent.session import AgentSession
from klonet_agent.workspace.manager import WORKSPACE_MANAGER


def list_files(session: AgentSession, path: str = ".") -> str:
    """列出 workspace 内文件。"""

    sandbox = WORKSPACE_MANAGER.sandbox_for(session)
    target = sandbox.resolve_path(path)
    if not target.exists():
        return f"路径不存在：{path}"
    if target.is_file():
        return str(target.relative_to(session.workspace_path))
    lines = []
    for item in sorted(target.iterdir()):
        suffix = "/" if item.is_dir() else ""
        lines.append(str(item.relative_to(session.workspace_path)) + suffix)
    return "\n".join(lines) or "(empty)"


def read_file(session: AgentSession, path: str, max_chars: int = 12000) -> str:
    """读取 workspace 内文本文件。"""

    sandbox = WORKSPACE_MANAGER.sandbox_for(session)
    target = sandbox.resolve_path(path)
    if not target.exists():
        return f"文件不存在：{path}"
    if not target.is_file():
        return f"目标不是文件：{path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[内容过长，已截断]"
    return text


def write_file(session: AgentSession, path: str, content: str) -> str:
    """写入 workspace 内文本文件。"""

    sandbox = WORKSPACE_MANAGER.sandbox_for(session)
    target = sandbox.resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"文件已写入：{target.relative_to(session.workspace_path)}"
