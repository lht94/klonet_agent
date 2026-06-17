"""命令行启动脚本。"""

import argparse
import sys
from pathlib import Path


# 当用户在仓库根目录直接运行 `python agent.py` 时，当前目录就是包目录本身。
# 这时需要把父目录加入 sys.path，绝对导入 `klonet_agent.app` 才能成立。
PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

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
