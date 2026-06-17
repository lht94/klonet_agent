"""应用入口模块。

这里放不同运行方式的入口，例如 CLI、本地调试服务、未来的 Web/API 服务。
入口层应该尽量薄，只负责组装配置并启动 agent。
"""

from klonet_agent.app.cli import run_chat


__all__ = ["run_chat"]
