"""Klonet RAG 知识库。"""

from __future__ import annotations

import re
from typing import Literal

from klonet_agent.config import DEFAULT_RAG_TOP_K
from klonet_agent.knowledge.retriever import KnowledgeRetriever


QueryScope = Literal["klonet", "general", "mixed"]

_KLONET_TERMS = {
    "klonet",
    "vemu",
    "拓扑部署",
    "拓扑删除",
    "worker注册",
    "worker 注册",
    "进度条卡住",
    "项目验收",
    "klonet源码",
    "klonet 源码",
    "代码风格指南",
}
_GENERAL_TERMS = {
    "docker compose",
    "docker-compose",
    "dind",
    "rust",
    "ubuntu",
    "linux vm",
    "通用技术",
}
_KLONET_NEGATION = re.compile(
    r"(?:不需要|无需|不使用|不要|独立于|排除)\s*(?:klonet|vemu)",
    re.IGNORECASE,
)


def classify_query_scope(query: str) -> QueryScope:
    """判断问题是否应该使用 Klonet 专属知识。"""

    normalized = " ".join((query or "").lower().split())
    if _KLONET_NEGATION.search(normalized):
        return "general"

    has_klonet = any(term in normalized for term in _KLONET_TERMS)
    has_general = any(term in normalized for term in _GENERAL_TERMS)
    if has_klonet and has_general:
        return "mixed"
    if has_klonet:
        return "klonet"
    if has_general:
        return "general"
    # 专用 Agent 中的模糊项目问题默认视为 Klonet，避免阶段、模块等问题漏检索。
    return "klonet"


class KnowledgeBase:
    """对外提供统一的 Klonet 知识检索入口。"""

    def __init__(self, retriever: KnowledgeRetriever | None = None):
        self.retriever = retriever or KnowledgeRetriever()

    def search_knowledge(self, query: str, top_k: int = DEFAULT_RAG_TOP_K) -> str:
        """检索知识库，并返回适合塞回模型上下文的文本。"""

        scope = classify_query_scope(query)
        if scope == "general":
            return (
                "该问题不属于 Klonet 知识库范围，未执行 Klonet RAG。"
                "请保留用户的否定条件，使用通用技术知识回答；"
                "需要实时资料时再使用联网工具。"
            )

        results = self.retriever.search(query, top_k=top_k)
        if not results:
            return "未检索到相关 Klonet 知识。请说明证据不足，并建议下一步读取源码或补充文档。"

        lines = ["检索到以下 Klonet 相关证据："]
        for index, item in enumerate(results, start=1):
            lines.append(
                f"\n[{index}] {item.title}\n"
                f"- source: {item.source}\n"
                f"- path: {item.path}\n"
                f"- score: {item.score}\n"
                f"- snippet:\n{item.snippet}"
            )
        return "\n".join(lines)


KNOWLEDGE_BASE = KnowledgeBase()
