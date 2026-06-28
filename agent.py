"""命令行启动脚本。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# 当用户在仓库根目录直接运行 `python agent.py` 时，当前目录就是包目录本身。
# 这时需要把父目录加入 sys.path，绝对导入 `klonet_agent.app` 才能成立。
PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from klonet_agent.app.admin import AdminDataStore, DeleteResult
from klonet_agent.config import JOURNAL_DIR, MEMORY_DIR, WORKSPACE_DIR


def main():
    """解析最小启动参数，然后进入 CLI 对话。"""

    parser = argparse.ArgumentParser(description="Klonet 专用教学协作 Agent")
    subparsers = parser.add_subparsers(dest="command")
    admin_parser = subparsers.add_parser("admin", help="管理本地用户、对话和项目数据")
    _add_admin_arguments(admin_parser)

    parser.add_argument("--mode", choices=["mentor", "coding", "ops"], default="mentor")
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()

    if args.command == "admin":
        _run_admin(args)
        return

    from klonet_agent.app import run_chat

    run_chat(mode=args.mode, user_id=args.user_id, project_id=args.project_id)


def _add_admin_arguments(parser: argparse.ArgumentParser) -> None:
    """注册本地数据管理子命令。"""

    parser.add_argument("--memory-root", type=Path, default=MEMORY_DIR)
    parser.add_argument("--journal-root", type=Path, default=JOURNAL_DIR)
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_DIR)
    subparsers = parser.add_subparsers(dest="admin_command", required=True)

    subparsers.add_parser("list-users", help="列出用户")

    list_projects = subparsers.add_parser("list-projects", help="列出用户项目")
    list_projects.add_argument("--user-id", required=True)

    show_user = subparsers.add_parser("show-user", help="读取用户画像")
    show_user.add_argument("--user-id", required=True)

    show_conversation = subparsers.add_parser("show-conversation", help="读取对话记录")
    show_conversation.add_argument("--user-id", required=True)
    show_conversation.add_argument("--project-id", required=True)

    show_project = subparsers.add_parser("show-project", help="读取项目资料")
    show_project.add_argument("--user-id", required=True)
    show_project.add_argument("--project-id", required=True)

    delete_user = subparsers.add_parser("delete-user", help="删除用户全部数据")
    delete_user.add_argument("--user-id", required=True)
    delete_user.add_argument("--yes", action="store_true")

    delete_conversation = subparsers.add_parser("delete-conversation", help="删除对话和项目记忆")
    delete_conversation.add_argument("--user-id", required=True)
    delete_conversation.add_argument("--project-id", required=True)
    delete_conversation.add_argument("--yes", action="store_true")

    delete_project = subparsers.add_parser("delete-project", help="删除项目全部数据")
    delete_project.add_argument("--user-id", required=True)
    delete_project.add_argument("--project-id", required=True)
    delete_project.add_argument("--yes", action="store_true")


def _run_admin(args: argparse.Namespace) -> None:
    """执行本地数据管理子命令。"""

    store = AdminDataStore(
        memory_root=args.memory_root,
        journal_root=args.journal_root,
        workspace_root=args.workspace_root,
    )
    command = args.admin_command
    if command == "list-users":
        _print_lines(store.list_users())
    elif command == "list-projects":
        _print_lines(store.list_projects(args.user_id))
    elif command == "show-user":
        print(store.read_user(args.user_id), end="")
    elif command == "show-conversation":
        print(store.read_conversation(args.user_id, args.project_id), end="")
    elif command == "show-project":
        print(store.read_project(args.user_id, args.project_id), end="")
    elif command == "delete-user":
        _print_delete_result(store.delete_user(args.user_id, confirm=args.yes))
    elif command == "delete-conversation":
        _print_delete_result(
            store.delete_conversation(args.user_id, args.project_id, confirm=args.yes)
        )
    elif command == "delete-project":
        _print_delete_result(store.delete_project(args.user_id, args.project_id, confirm=args.yes))


def _print_lines(lines: list[str]) -> None:
    for line in lines:
        print(line)


def _print_delete_result(result: DeleteResult) -> None:
    prefix = "DELETED" if result.deleted else "DRY RUN"
    print(f"{prefix}: {len(result.paths)} path(s)")
    for path in result.paths:
        print(path)


if __name__ == "__main__":
    main()
