"""Multi-task, multi-channel recall, RRF fusion and reranking."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Callable

from klonet_agent.config import (
    CODE_INDEX_FILE,
    CODE_VECTOR_INDEX_FILE,
    RAG_FUSION_TOP_K,
    RAG_RECALL_TOP_K,
    RAG_RERANK_TOP_N,
)
from klonet_agent.knowledge.code_indexer import CodeIndexer
from klonet_agent.knowledge.models import (
    RetrievalPlan,
    RetrievalTask,
    RetrievedChunk,
    SearchOutcome,
    SearchRequest,
)
from klonet_agent.knowledge.query_planner import QueryPlanner
from klonet_agent.knowledge.retriever import KnowledgeRetriever
from klonet_agent.llm.reranker import RerankClient


_RRF_K = 60
_CHANNEL_WEIGHTS = {"bm25": 1.0, "dense": 1.0, "exact": 2.0}


class MultiStageRetriever:
    """Coordinate planning, independent recall channels and final ranking."""

    def __init__(
        self,
        *,
        public_retriever: KnowledgeRetriever,
        planner: QueryPlanner | None = None,
        source_retriever: KnowledgeRetriever | None = None,
        reranker: RerankClient | None = None,
        code_indexer: CodeIndexer | None = None,
    ):
        self.public_retriever = public_retriever
        self.planner = planner or QueryPlanner()
        self._source = source_retriever
        self.reranker = reranker or RerankClient()
        self.code_indexer = code_indexer or CodeIndexer()

    def search(
        self,
        query: str,
        *,
        top_k: int,
        task_type: str,
        layers: tuple[str, ...] | None = None,
        domains: tuple[str, ...] | None = None,
        min_priority: str | None = None,
        collections: tuple[str, ...] = (),
        allowed_paths: tuple[str, ...] = (),
        excluded_intents: tuple[str, ...] = (),
        conversation_state=None,
        context_hints=None,
        retrieval_plan: RetrievalPlan | None = None,
    ) -> SearchOutcome:
        timings: dict[str, float] = {}
        started = time.perf_counter()
        plan = retrieval_plan or self.planner.plan(
            query,
            conversation_state=conversation_state,
            context_hints=context_hints,
        )
        timings["planner"] = _elapsed_ms(started)

        recall_started = time.perf_counter()
        fused, recall_counts, vector_statuses = self._recall_and_fuse(
            plan,
            task_type=task_type,
            layers=layers,
            domains=domains,
            min_priority=min_priority,
            collections=collections,
            allowed_paths=allowed_paths,
            excluded_intents=excluded_intents,
        )
        timings["recall_and_fusion"] = _elapsed_ms(recall_started)
        candidates = fused[:RAG_FUSION_TOP_K]

        rerank_started = time.perf_counter()
        ranked, rerank_status = self._rerank(plan, candidates)
        timings["rerank"] = _elapsed_ms(rerank_started)
        selected = _select_diverse(ranked, max(1, top_k))
        outcome = _classify(selected)
        outcome.retrieval_plan = plan
        outcome.planner_status = plan.status
        outcome.recall_counts = recall_counts
        outcome.fusion_status = (
            f"weighted_rrf:k={_RRF_K};candidates={len(fused)}"
        )
        outcome.rerank_status = rerank_status
        outcome.stage_timings_ms = timings
        outcome.retrieval_mode = "multi_stage"
        outcome.vector_status = _combined_vector_status(vector_statuses)
        outcome.vector_status_detail = ";".join(vector_statuses)
        return outcome

    def _recall_and_fuse(
        self,
        plan: RetrievalPlan,
        *,
        task_type: str,
        layers: tuple[str, ...] | None,
        domains: tuple[str, ...] | None,
        min_priority: str | None,
        collections: tuple[str, ...],
        allowed_paths: tuple[str, ...],
        excluded_intents: tuple[str, ...],
    ) -> tuple[list[RetrievedChunk], dict[str, int], list[str]]:
        aggregate: dict[str, RetrievedChunk] = {}
        rrf_scores: dict[str, float] = {}
        channels: dict[str, list[str]] = {}
        purposes: dict[str, list[str]] = {}
        recall_counts: dict[str, int] = {}
        vector_statuses: list[str] = []
        executed: set[tuple[str, str, str]] = set()
        prepared_retrievers = {
            task.store: (
                self.public_retriever
                if task.store == "public_docs"
                else self._source_retriever()
            )
            for task in plan.retrieval_tasks
        }
        prime_jobs = []
        for task in plan.retrieval_tasks:
            retriever = prepared_retrievers[task.store]
            prime_embeddings = getattr(retriever, "prime_query_embeddings", None)
            if not callable(prime_embeddings):
                continue
            dense_queries = list(
                dict.fromkeys(
                    channel_query.strip()
                    for channel, channel_query in _recall_calls(plan, task)
                    if channel == "dense" and channel_query.strip()
                )
            )[:3]
            prime_jobs.append((prime_embeddings, dense_queries))
        if prime_jobs:
            seeders = [
                getattr(retriever, "seed_query_embeddings", None)
                for retriever in prepared_retrievers.values()
            ]
            if len(prime_jobs) > 1 and all(callable(seeder) for seeder in seeders):
                # Both indexes use DEFAULT_EMBEDDING_MODEL. Embed the union
                # once, then reuse those query vectors in each index.
                all_queries = list(
                    dict.fromkeys(
                        query
                        for _, queries in prime_jobs
                        for query in queries
                    )
                )
                try:
                    shared = prime_jobs[0][0](all_queries)
                except Exception:
                    shared = {}
                if shared:
                    for seeder in seeders:
                        seeder(shared)
            else:
                with ThreadPoolExecutor(max_workers=min(2, len(prime_jobs))) as pool:
                    futures = [
                        pool.submit(prime_embeddings, queries)
                        for prime_embeddings, queries in prime_jobs
                    ]
                    for future in futures:
                        try:
                            future.result()
                        except Exception:
                            # recall_channel retains its single-query fallback.
                            pass

        for task_index, task in enumerate(plan.retrieval_tasks):
            retriever = prepared_retrievers[task.store]
            request_factory = _request_factory(
                store=task.store,
                task_type=task_type,
                layers=layers,
                domains=domains,
                min_priority=min_priority,
                collections=collections,
                allowed_paths=allowed_paths,
                excluded_intents=excluded_intents,
            )
            calls = _recall_calls(plan, task)
            task_channel_counts = {"bm25": 0, "dense": 0, "exact": 0}
            task_channel_limits = {"bm25": 3, "dense": 3, "exact": 12}
            for channel, channel_query in calls:
                normalized_query = channel_query.strip()
                call_key = (task.store, channel, normalized_query.casefold())
                if (
                    not normalized_query
                    or call_key in executed
                    or task_channel_counts[channel] >= task_channel_limits[channel]
                ):
                    continue
                executed.add(call_key)
                task_channel_counts[channel] += 1
                request = request_factory(normalized_query)
                results = retriever.recall_channel(
                    request,
                    channel,
                    top_k=RAG_RECALL_TOP_K,
                )
                channel_id = (
                    f"{task.store}:{channel}:task{task_index + 1}:"
                    f"{len(recall_counts) + 1}"
                )
                recall_counts[channel_id] = len(results)
                for rank, item in enumerate(results, start=1):
                    key = item.chunk_id
                    if key not in aggregate:
                        aggregate[key] = replace(item)
                    else:
                        _merge_channel_evidence(aggregate[key], item)
                    rrf_scores[key] = rrf_scores.get(key, 0.0) + (
                        _CHANNEL_WEIGHTS[channel] * 1.3 / (_RRF_K + rank)
                    )
                    if channel_id not in channels.setdefault(key, []):
                        channels[key].append(channel_id)
                    if task.purpose not in purposes.setdefault(key, []):
                        purposes[key].append(task.purpose)

            vector_statuses.append(
                f"{task.store}:{retriever.vector_status}:"
                f"{retriever.vector_status_detail}"
            )

        fused = []
        for key, item in aggregate.items():
            item.rrf_score = round(rrf_scores[key], 8)
            item.score = item.rrf_score
            item.final_score = item.rrf_score
            item.recall_channels = tuple(channels.get(key, ()))
            item.task_purposes = tuple(purposes.get(key, ()))
            fused.append(item)
        fused.sort(key=lambda item: item.rrf_score, reverse=True)
        return fused, recall_counts, vector_statuses

    def _rerank(
        self,
        plan: RetrievalPlan,
        candidates: list[RetrievedChunk],
    ) -> tuple[list[RetrievedChunk], str]:
        if not candidates:
            return [], "skipped:no_candidates"
        if not self.reranker.available:
            return _rrf_fallback(candidates), "fallback:missing_credentials"
        try:
            items = self.reranker.rerank(
                plan.standalone_query or plan.original_query,
                candidates,
                top_n=min(RAG_RERANK_TOP_N, len(candidates)),
            )
        except Exception as exc:
            return _rrf_fallback(candidates), f"fallback:{type(exc).__name__}"

        rerank_values = _normalize([item.relevance_score for item in items])
        rrf_values = _normalize([item.rrf_score for item in candidates])
        ranked = []
        for rerank_item, normalized_rerank in zip(items, rerank_values):
            candidate = candidates[rerank_item.index]
            candidate.rerank_score = round(rerank_item.relevance_score, 8)
            exact_boost = 0.1 if candidate.exact_score > 0 else 0.0
            candidate.final_score = round(
                min(
                    1.0,
                    0.75 * normalized_rerank
                    + 0.25 * rrf_values[rerank_item.index]
                    + exact_boost,
                ),
                8,
            )
            candidate.score = candidate.final_score
            ranked.append(candidate)
        ranked.sort(key=lambda item: item.final_score, reverse=True)
        return ranked, f"qwen3_rerank:top_n={len(ranked)}"

    def _source_retriever(self) -> KnowledgeRetriever:
        if self._source is not None:
            return self._source
        if self.code_indexer.is_stale():
            self.code_indexer.build()
        self._source = KnowledgeRetriever(
            index_file=CODE_INDEX_FILE,
            vector_index_file=CODE_VECTOR_INDEX_FILE,
        )
        return self._source


def _request_factory(
    *,
    store: str,
    task_type: str,
    layers: tuple[str, ...] | None,
    domains: tuple[str, ...] | None,
    min_priority: str | None,
    collections: tuple[str, ...],
    allowed_paths: tuple[str, ...],
    excluded_intents: tuple[str, ...],
) -> Callable[[str], SearchRequest]:
    def build(query: str) -> SearchRequest:
        if store == "source_code":
            return SearchRequest(
                query=query,
                task_type=(
                    task_type
                    if task_type in {"development", "code_lookup", "troubleshooting"}
                    else "code_lookup"
                ),
                layers=("source_code",),
                min_priority=min_priority,
                top_k=RAG_RECALL_TOP_K,
            )
        return SearchRequest(
            query=query,
            task_type=task_type,
            layers=layers,
            domains=domains,
            excluded_intents=excluded_intents,
            min_priority=min_priority,
            collections=collections,
            allowed_paths=allowed_paths,
            top_k=RAG_RECALL_TOP_K,
        )

    return build


def _recall_calls(
    plan: RetrievalPlan,
    task: RetrievalTask,
) -> list[tuple[str, str]]:
    return [
        ("bm25", plan.original_query),
        ("dense", plan.original_query),
        *[("bm25", item) for item in task.keyword_queries],
        ("dense", plan.standalone_query),
        *[("dense", item) for item in task.semantic_queries],
        *[("exact", item) for item in task.exact_terms],
    ]


def _rrf_fallback(candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    values = _normalize([item.rrf_score for item in candidates])
    for item, score in zip(candidates, values):
        item.final_score = round(score, 8)
        item.score = item.final_score
    return sorted(candidates, key=lambda item: item.final_score, reverse=True)


def _merge_channel_evidence(
    target: RetrievedChunk,
    incoming: RetrievedChunk,
) -> None:
    """Keep the strongest explainable score from every recall channel."""

    target.bm25_score = max(target.bm25_score, incoming.bm25_score)
    target.exact_score = max(target.exact_score, incoming.exact_score)
    target.semantic_score = max(target.semantic_score, incoming.semantic_score)
    target.metadata_score = max(target.metadata_score, incoming.metadata_score)
    target.matched_terms = tuple(
        dict.fromkeys((*target.matched_terms, *incoming.matched_terms))
    )
    if len(incoming.rerank_text) > len(target.rerank_text):
        target.rerank_text = incoming.rerank_text


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lower, upper = min(values), max(values)
    if upper == lower:
        return [1.0 for _ in values]
    return [(value - lower) / (upper - lower) for value in values]


def _select_diverse(
    candidates: list[RetrievedChunk],
    limit: int,
) -> list[RetrievedChunk]:
    selected: list[RetrievedChunk] = []
    public_counts: dict[str, int] = {}
    source_symbols: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.layer == "source_code":
            symbol_key = (candidate.path, candidate.symbol or candidate.title)
            if symbol_key in source_symbols:
                continue
            source_symbols.add(symbol_key)
        else:
            count = public_counts.get(candidate.path, 0)
            if count >= 2:
                continue
            public_counts[candidate.path] = count + 1
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _classify(results: list[RetrievedChunk]) -> SearchOutcome:
    if not results:
        return SearchOutcome(status="none", reason="no_relevant_evidence")
    top = results[0]
    reliable = (
        top.exact_score > 0
        or len(top.recall_channels) >= 2
        or (top.rerank_score is not None and top.final_score >= 0.5)
    )
    status = "reliable" if reliable else "weak"
    for index, result in enumerate(results):
        result.relevance = status if index == 0 else "candidate"
    return SearchOutcome(
        status=status,
        results=results,
        confidence=round(min(1.0, max(0.0, top.final_score)), 4),
        reason="multi_stage_match" if reliable else "weak_multi_stage_match",
    )


def _combined_vector_status(statuses: list[str]) -> str:
    if not statuses:
        return "not_used"
    if all(":ready:" in item for item in statuses):
        return "ready"
    if any(":ready:" in item for item in statuses):
        return "partial"
    return "unavailable"


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
