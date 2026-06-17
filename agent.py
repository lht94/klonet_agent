"""命令行启动脚本。"""

import argparse

from klonet_agent.app import run_chat


def main():
    """解析最小启动参数，然后进入 CLI 对话。"""

    parser = argparse.ArgumentParser(description="Klonet 专用教学协作 Agent")
    parser.add_argument("--mode", choices=["mentor", "coding"], default="mentor")
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    run_chat(mode=args.mode, user_id=args.user_id, project_id=args.project_id)


if __name__ == "__main__":
    main()
