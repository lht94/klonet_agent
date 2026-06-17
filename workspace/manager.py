"""用户/任务工作区管理器。"""

from pathlib import Path

from klonet_agent.config import WORKSPACE_DIR
from klonet_agent.session import AgentSession
from klonet_agent.workspace.sandbox import WorkspaceSandbox


class WorkspaceManager:
    """负责创建和定位当前用户项目的 workspace。"""

    def __init__(self, base_dir: Path = WORKSPACE_DIR):
        self.base_dir = base_dir

    def ensure_workspace(self, session: AgentSession) -> Path:
        """确保当前会话的 workspace 存在。"""

        session.workspace_path.mkdir(parents=True, exist_ok=True)
        readme = session.workspace_path / "README.md"
        if not readme.exists():
            readme.write_text(
                f"# Klonet Workspace\n\nuser_id: {session.user_id}\nproject_id: {session.project_id}\n",
                encoding="utf-8",
            )
        return session.workspace_path

    def sandbox_for(self, session: AgentSession) -> WorkspaceSandbox:
        """返回当前会话的沙箱。"""

        self.ensure_workspace(session)
        return WorkspaceSandbox(session.workspace_path)


WORKSPACE_MANAGER = WorkspaceManager()
