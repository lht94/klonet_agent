"""记忆系统模块。

这个包负责长期记忆、用户画像、会话历史、情景日志和压缩摘要。
项目开发过程日志不要混在这里，应该放到 journal 包中。
"""

from klonet_agent.memory.store import MEMORY_STORE, MemoryStore


__all__ = ["MEMORY_STORE", "MemoryStore"]
