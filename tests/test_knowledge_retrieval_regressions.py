"""Regression tests for intent-routed knowledge retrieval."""

from klonet_agent.knowledge.collection_router import (
    collection_ids,
    collection_paths,
    route_collections,
)
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.models import SearchRequest
from klonet_agent.knowledge.query_builder import QueryBuilder
from klonet_agent.knowledge.retriever import KnowledgeRetriever


def test_concept_query_hits_foundation_overview_after_intent_routing():
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "concept",
            "operation": "unknown",
            "target": "klonet_platform",
            "confidence": 0.9,
        }
    )
    plan = QueryBuilder().build(
        "klonet是什么",
        intent=intent,
        top_k=3,
    )
    collections = route_collections(intent)

    outcome = KnowledgeRetriever().search_request(
        SearchRequest(
            query=plan.query,
            task_type=plan.task_type,
            intent=plan.intent_operation,
            collections=collection_ids(collections),
            allowed_paths=collection_paths(collections),
            top_k=3,
        )
    )

    assert outcome.status == "reliable"
    assert outcome.results[0].path == "knowledge/klonet/00_project_overview.md"
    assert "核心结论" in outcome.results[0].title
    assert any(
        result.path == "knowledge/klonet/00_project_overview.md"
        for result in outcome.results
    )
