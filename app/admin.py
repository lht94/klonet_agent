"""本地运行时数据管理命令的业务层。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from klonet_agent.config import JOURNAL_DIR, MEMORY_DIR, WORKSPACE_DIR


@dataclass(frozen=True)
class DeleteResult:
    """删除操作的结果。"""

    deleted: bool
    paths: list[Path]


class AdminDataStore:
    """读取和删除本地用户、对话与项目数据。"""

    def __init__(
        self,
        memory_root: Path = MEMORY_DIR,
        journal_root: Path = JOURNAL_DIR,
        workspace_root: Path = WORKSPACE_DIR,
    ):
        self.memory_root = Path(memory_root)
        self.journal_root = Path(journal_root)
        self.workspace_root = Path(workspace_root)

    def list_users(self) -> list[str]:
        """列出所有有运行时数据的用户。"""

        names = set()
        for root in [
            self.memory_root / "users",
            self.memory_root / "sessions",
            self.journal_root,
            self.workspace_root,
        ]:
            if not root.exists():
                continue
            names.update(path.name for path in root.iterdir() if path.is_dir())
        return sorted(names)

    def list_projects(self, user_id: str) -> list[str]:
        """列出某个用户下已有数据的项目。"""

        user_name = _safe_path_component(user_id)
        names = set()
        session_root = self.memory_root / "sessions" / user_name
        if session_root.exists():
            names.update(path.name for path in session_root.iterdir() if path.is_dir())

        journal_root = self.journal_root / user_name
        if journal_root.exists():
            names.update(path.stem for path in journal_root.glob("*.md") if path.is_file())

        workspace_root = self.workspace_root / user_name
        if workspace_root.exists():
            names.update(path.name for path in workspace_root.iterdir() if path.is_dir())

        return sorted(names)

    def read_user(self, user_id: str) -> str:
        """读取用户画像。"""

        path = self.memory_root / "users" / _safe_path_component(user_id) / "USER.md"
        return _read_text(path)

    def read_conversation(self, user_id: str, project_id: str) -> str:
        """读取某个用户项目下的原始对话记录。"""

        path = self._session_dir(user_id, project_id) / "history.jsonl"
        return _read_text(path)

    def read_project(self, user_id: str, project_id: str) -> str:
        """读取某个用户项目下的项目记忆、日志和 workspace 文件清单。"""

        session_dir = self._session_dir(user_id, project_id)
        journal_path = self._journal_path(user_id, project_id)
        workspace_dir = self._workspace_dir(user_id, project_id)
        parts = [
            f"# project {user_id}/{project_id}",
            "## memory",
            _read_text(session_dir / "MEMORY.md"),
            "## episodes",
            _read_episode_files(session_dir),
            "## journal",
            _read_text(journal_path),
            "## workspace",
            _list_relative_files(workspace_dir),
        ]
        return "\n\n".join(part.strip() for part in parts if part.strip()) + "\n"

    def delete_user(self, user_id: str, confirm: bool = False) -> DeleteResult:
        """删除一个用户的全部本地运行时数据。"""

        user_name = _safe_path_component(user_id)
        paths = [
            self.memory_root / "users" / user_name,
            self.memory_root / "sessions" / user_name,
            self.journal_root / user_name,
            self.workspace_root / user_name,
        ]
        return self._delete(paths, confirm)

    def delete_conversation(
        self,
        user_id: str,
        project_id: str,
        confirm: bool = False,
    ) -> DeleteResult:
        """删除某个项目下的对话与项目记忆，不删除 journal 和 workspace。"""

        return self._delete([self._session_dir(user_id, project_id)], confirm)

    def delete_project(
        self,
        user_id: str,
        project_id: str,
        confirm: bool = False,
    ) -> DeleteResult:
        """删除某个项目的记忆、项目日志和 workspace。"""

        paths = [
            self._session_dir(user_id, project_id),
            self._journal_path(user_id, project_id),
            self._workspace_dir(user_id, project_id),
        ]
        return self._delete(paths, confirm)

    def _delete(self, paths: list[Path], confirm: bool) -> DeleteResult:
        existing = [self._safe_existing_path(path) for path in paths if path.exists()]
        if confirm:
            for path in existing:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        return DeleteResult(deleted=confirm and bool(existing), paths=existing)

    def _safe_existing_path(self, path: Path) -> Path:
        resolved = path.resolve()
        allowed_roots = [
            self.memory_root.resolve(),
            self.journal_root.resolve(),
            self.workspace_root.resolve(),
        ]
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ValueError(f"path is outside runtime roots: {resolved}")
        return resolved

    def _session_dir(self, user_id: str, project_id: str) -> Path:
        return (
            self.memory_root
            / "sessions"
            / _safe_path_component(user_id)
            / _safe_path_component(project_id)
        )

    def _journal_path(self, user_id: str, project_id: str) -> Path:
        return (
            self.journal_root
            / _safe_path_component(user_id)
            / f"{_safe_path_component(project_id)}.md"
        )

    def _workspace_dir(self, user_id: str, project_id: str) -> Path:
        return (
            self.workspace_root
            / _safe_path_component(user_id)
            / _safe_path_component(project_id)
        )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _safe_path_component(value: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in (value or "default").strip()
    )
    return cleaned or "default"


def _read_episode_files(session_dir: Path) -> str:
    if not session_dir.exists():
        return ""
    blocks = []
    for path in sorted(session_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        blocks.append(f"### {path.name}\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(blocks)


def _list_relative_files(root: Path) -> str:
    if not root.exists():
        return ""
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    return "\n".join(f"- {name}" for name in files)
