"""Klonet RAG 路由、结构化检索和证据格式化。"""

from __future__ import annotations

from klonet_agent.config import DEFAULT_RAG_TOP_K
from klonet_agent.knowledge.models import QueryRoute, QueryScope, SearchRequest
from klonet_agent.knowledge.retriever import KnowledgeRetriever
from klonet_agent.knowledge.router import DEFAULT_QUERY_ROUTER


def route_query(query: str) -> QueryRoute:
    """返回完整软路由结果。"""

    return DEFAULT_QUERY_ROUTER.route(query)


def classify_query_scope(query: str) -> QueryScope:
    """兼容旧接口，只返回范围字符串。"""

    return route_query(query).scope


class KnowledgeBase:
    """对外提供统一的 Klonet 知识检索入口。"""

    def __init__(self, retriever: KnowledgeRetriever | None = None):
        self.retriever = retriever or KnowledgeRetriever()

    def search_knowledge(
        self,
        query: str,
        top_k: int = DEFAULT_RAG_TOP_K,
        *,
        task_type: str | None = None,
        layers: tuple[str, ...] | None = None,
        domains: tuple[str, ...] | None = None,
        min_priority: str | None = None,
    ) -> str:
        """按软路由检索，并返回带来源和可靠度的证据。"""

        route = route_query(query)
        if route.hard_disable_rag:
            return (
                "该问题明确不属于 Klonet 知识库范围，未执行 Klonet RAG。"
                "请保留用户的否定条件，使用通用技术知识回答。"
            )
        if route.scope == "general" and route.confidence >= 0.8:
            return (
                "该问题高概率属于通用技术范围，未主动注入 Klonet 证据。"
                "如用户后续明确关联 Klonet，再执行专属知识检索。"
            )

        request = SearchRequest(
            query=query,
            task_type=task_type or route.task_type,
            layers=layers,
            domains=domains or route.domains or None,
            min_priority=min_priority,
            top_k=top_k,
        )
        outcome = self.retriever.search_request(request)
        if outcome.status == "none":
            return (
                "未检索到可靠 Klonet 证据。"
                "请明确说明证据不足，并建议读取源码、补充问题信息或完善知识文档。"
            )

        prefix = (
            "检索到以下可靠 Klonet 证据："
            if outcome.status == "reliable"
            else "只检索到相关度较弱的 Klonet 候选证据，请谨慎引用："
        )
        lines = [
            prefix,
            f"- retrieval_status: {outcome.status}",
            f"- confidence: {outcome.confidence}",
            f"- route_scope: {route.scope}",
            f"- task_type: {request.task_type}",
        ]
        for index, item in enumerate(outcome.results, start=1):
            lines.append(
                f"\n[{index}] {item.title}\n"
                f"- layer: {item.layer}\n"
                f"- domain: {item.domain}\n"
                f"- quality: {item.quality}\n"
                f"- path: {item.path}\n"
                f"- relevance: {item.relevance}\n"
                f"- snippet:\n{item.snippet}"
            )
        return "\n".join(lines)


KNOWLEDGE_BASE = KnowledgeBase()
