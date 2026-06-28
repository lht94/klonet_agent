"""CLI 管理本地用户、对话和项目数据的测试。"""

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_admin_store_lists_users_and_projects():
    """管理层应该能从运行时目录推断已有用户和项目。"""

    from klonet_agent.app.admin import AdminDataStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = AdminDataStore(
            memory_root=temp_dir / "memory",
            journal_root=temp_dir / "journals",
            workspace_root=temp_dir / "workspaces",
        )
        _write_runtime_data(temp_dir, "u1", "p1")
        _write_runtime_data(temp_dir, "u1", "p2")
        _write_runtime_data(temp_dir, "u2", "alpha")

        assert store.list_users() == ["u1", "u2"]
        assert store.list_projects("u1") == ["p1", "p2"]


def test_admin_store_reads_user_conversation_and_project():
    """管理层应该按 user_id/project_id 读取用户画像、对话和项目资料。"""

    from klonet_agent.app.admin import AdminDataStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = AdminDataStore(
            memory_root=temp_dir / "memory",
            journal_root=temp_dir / "journals",
            workspace_root=temp_dir / "workspaces",
        )
        _write_runtime_data(temp_dir, "u1", "p1")

        assert "喜欢简洁回答" in store.read_user("u1")
        assert "hello" in store.read_conversation("u1", "p1")
        project_text = store.read_project("u1", "p1")
        assert "长期目标" in project_text
        assert "项目日志" in project_text
        assert "README.md" in project_text


def test_admin_store_deletes_only_after_confirmation():
    """删除命令默认 dry-run，只有 confirm=True 时才真正删除。"""

    from klonet_agent.app.admin import AdminDataStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = AdminDataStore(
            memory_root=temp_dir / "memory",
            journal_root=temp_dir / "journals",
            workspace_root=temp_dir / "workspaces",
        )
        _write_runtime_data(temp_dir, "u1", "p1")

        preview = store.delete_project("u1", "p1", confirm=False)
        assert preview.deleted is False
        assert (temp_dir / "memory" / "sessions" / "u1" / "p1").exists()

        result = store.delete_project("u1", "p1", confirm=True)
        assert result.deleted is True
        assert not (temp_dir / "memory" / "sessions" / "u1" / "p1").exists()
        assert not (temp_dir / "journals" / "u1" / "p1.md").exists()
        assert not (temp_dir / "workspaces" / "u1" / "p1").exists()


def test_admin_cli_lists_users_from_custom_roots():
    """CLI 子命令应该能列出自定义运行时根目录下的用户。"""

    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        _write_runtime_data(temp_dir, "u1", "p1")

        result = subprocess.run(
            [
                sys.executable,
                "agent.py",
                "admin",
                "--memory-root",
                str(temp_dir / "memory"),
                "--journal-root",
                str(temp_dir / "journals"),
                "--workspace-root",
                str(temp_dir / "workspaces"),
                "list-users",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode == 0, result.stderr
    assert "u1" in result.stdout


def test_admin_cli_delete_project_dry_run_keeps_files():
    """CLI 删除项目不带 --yes 时应该只预览，不删除文件。"""

    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        _write_runtime_data(temp_dir, "u1", "p1")

        result = subprocess.run(
            [
                sys.executable,
                "agent.py",
                "admin",
                "--memory-root",
                str(temp_dir / "memory"),
                "--journal-root",
                str(temp_dir / "journals"),
                "--workspace-root",
                str(temp_dir / "workspaces"),
                "delete-project",
                "--user-id",
                "u1",
                "--project-id",
                "p1",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr
        assert "DRY RUN" in result.stdout
        assert (temp_dir / "memory" / "sessions" / "u1" / "p1").exists()


def _write_runtime_data(root: Path, user_id: str, project_id: str) -> None:
    user_dir = root / "memory" / "users" / user_id
    session_dir = root / "memory" / "sessions" / user_id / project_id
    journal_dir = root / "journals" / user_id
    workspace_dir = root / "workspaces" / user_id / project_id
    user_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    journal_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    (user_dir / "USER.md").write_text("# 用户画像\n喜欢简洁回答\n", encoding="utf-8")
    (session_dir / "history.jsonl").write_text(
        '{"role":"user","content":"hello"}\n',
        encoding="utf-8",
    )
    (session_dir / "MEMORY.md").write_text("# 长期记忆\n长期目标\n", encoding="utf-8")
    (session_dir / "2026-06-28.md").write_text("# 情景记忆\n完成实验\n", encoding="utf-8")
    (journal_dir / f"{project_id}.md").write_text("# 项目日志\n开发中\n", encoding="utf-8")
    (workspace_dir / "README.md").write_text("# workspace\n", encoding="utf-8")
