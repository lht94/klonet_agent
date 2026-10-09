"""``python -m klonet_agent.memory.maintenance`` 入口（04 计划阶段 2）。"""

from __future__ import annotations

import sys

from klonet_agent.memory.maintenance.cli import main

if __name__ == "__main__":
    sys.exit(main())
