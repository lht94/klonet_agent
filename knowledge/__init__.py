"""知识库与检索模块。

这个包负责技能系统、Klonet 源码/文档索引、历史踩坑检索和 RAG 上下文注入。
"""

from klonet_agent.knowledge.rag import KNOWLEDGE_BASE, KnowledgeBase
from klonet_agent.knowledge.skill_loader import SKILL_LOADER, SkillLoader


__all__ = ["SKILL_LOADER", "SkillLoader", "KNOWLEDGE_BASE", "KnowledgeBase"]
