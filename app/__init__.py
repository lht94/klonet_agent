"""应用入口模块。

这里放不同运行方式的入口，例如 CLI、本地调试服务、未来的 Web/API 服务。
入口层应该尽量薄，只负责组装配置并启动 agent。
"""


def __getattr__(name: str):
    """按需加载聊天入口，避免 admin 命令触发记忆初始化副作用。"""

    if name == "run_chat":
        from klonet_agent.app.cli import run_chat

        return run_chat
    raise AttributeError(name)


__all__ = ["run_chat"]
