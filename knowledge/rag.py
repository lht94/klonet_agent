"""Klonet RAG 知识库。"""

from __future__ import annotations

from klonet_agent.knowledge.retriever import KnowledgeRetriever


class KnowledgeBase:
    """对外提供统一的 Klonet 知识检索入口。"""

    def __init__(self, retriever: KnowledgeRetriever | None = None):
        self.retriever = retriever or KnowledgeRetriever()

    def search_knowledge(self, query: str, top_k: int = 5) -> str:
        """检索知识库，并返回适合塞回模型上下文的文本。"""

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
