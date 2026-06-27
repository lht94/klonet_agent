"""Klonet RAG 路由、结构化检索和证据格式化。"""

from __future__ import annotations

from klonet_agent.config import DEFAULT_RAG_TOP_K
from klonet_agent.knowledge.collection_router import collection_ids, collection_paths, route_collections
from klonet_agent.knowledge.conversation_state import ConversationState
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.models import QueryRoute, QueryScope, SearchRequest
from klonet_agent.knowledge.query_builder import QueryBuilder
from klonet_agent.knowledge.retriever import KnowledgeRetriever
from klonet_agent.knowledge.router import DEFAULT_QUERY_ROUTER
from klonet_agent.knowledge.semantic_understanding import SemanticFrame


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
        intent: QueryIntent | None = None,
        semantic_frame: SemanticFrame | None = None,
        conversation_state: ConversationState | None = None,
    ) -> str:
        """按软路由检索，并返回带来源和可靠度的证据。"""

        route = route_query(query)
        if intent is not None and intent.confidence < 0.6:
            intent = None
        if route.hard_disable_rag:
            return (
                "该问题明确不属于 Klonet 知识库范围，未执行 Klonet RAG。"
                "请保留用户的否定条件，使用通用技术知识回答。"
            )
        if intent is not None and intent.scope == "general":
            return (
                "结构化意图表明该问题属于通用技术范围，未执行 Klonet RAG。"
                "请使用通用技术知识回答，并保留用户的原始约束。"
            )
        if intent is None and route.scope == "general" and route.confidence >= 0.8:
            return (
                "该问题高概率属于通用技术范围，未主动注入 Klonet 证据。"
                "如用户后续明确关联 Klonet，再执行专属知识检索。"
            )

        plan = QueryBuilder().build(
            query,
            intent=intent,
            semantic_frame=semantic_frame,
            conversation_state=conversation_state,
            top_k=top_k,
            task_type=task_type,
            domains=domains,
            layers=layers,
        )
        collections = route_collections(intent)
        request = SearchRequest(
            query=plan.query,
            task_type=(plan.task_type if plan.task_type != "auto" else route.task_type),
            layers=plan.layers,
            domains=plan.domains or route.domains or None,
            intent=plan.intent_operation,
            excluded_intents=plan.excluded_intents,
            min_priority=min_priority,
            collections=collection_ids(collections),
            allowed_paths=collection_paths(collections),
            top_k=plan.top_k,
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
            f"- operation: {request.intent}",
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
