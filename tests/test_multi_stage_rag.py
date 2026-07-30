"""Unified LLM query planning and multi-stage retrieval."""

from types import SimpleNamespace


class _FakePlannerClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.payload


def test_query_planner_combines_store_routing_and_query_generation():
    from klonet_agent.knowledge.query_planner import QueryPlanner

    client = _FakePlannerClient(
        {
            "standalone_query": "mentor 如何调用 TopoDeployAPI 创建拓扑",
            "retrieval_tasks": [
                {
                    "store": "source_code",
                    "purpose": "定位实现",
                    "keyword_queries": ["TopoDeployAPI topology"],
                    "semantic_queries": ["创建拓扑的源码实现"],
                    "exact_terms": ["TopoDeployAPI", "InventedAPI"],
                },
                {
                    "store": "public_docs",
                    "purpose": "查询使用说明",
                    "keyword_queries": ["mentor 拓扑"],
                    "semantic_queries": ["mentor 的拓扑创建流程"],
                    "exact_terms": [],
                },
            ],
            "excluded_terms": [],
            "confidence": 0.93,
        }
    )

    plan = QueryPlanner(client).plan("mentor 怎么调用 TopoDeployAPI 创建拓扑？")

    assert len(client.calls) == 1
    assert [task.store for task in plan.retrieval_tasks] == [
        "source_code",
        "public_docs",
    ]
    assert "TopoDeployAPI" in plan.retrieval_tasks[0].exact_terms
    assert "InventedAPI" not in plan.retrieval_tasks[0].exact_terms
    assert client.calls[0][1]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert client.calls[0][1]["response_format"] == {"type": "json_object"}


def test_query_planner_failure_falls_back_to_original_query_and_both_stores():
    from klonet_agent.knowledge.query_planner import QueryPlanner

    class BrokenClient:
        def complete(self, messages, **kwargs):
            raise TimeoutError("slow")

    plan = QueryPlanner(BrokenClient()).plan("TopoDeployAPI 在哪里")

    assert plan.status == "fallback"
    assert plan.standalone_query == "TopoDeployAPI 在哪里"
    assert {task.store for task in plan.retrieval_tasks} == {
        "public_docs",
        "source_code",
    }
    assert all("TopoDeployAPI" in task.exact_terms for task in plan.retrieval_tasks)


def test_query_planner_never_drops_explicit_negative_constraints():
    from klonet_agent.knowledge.query_planner import QueryPlanner

    client = _FakePlannerClient(
        {
            "standalone_query": "查询 Klonet 源码实现",
            "retrieval_tasks": [
                {
                    "store": "public_docs",
                    "purpose": "查询文档",
                    "keyword_queries": ["公共文档"],
                    "semantic_queries": ["Klonet 文档说明"],
                    "exact_terms": [],
                }
            ],
            "excluded_terms": ["源码", "模型虚构的排除项"],
            "confidence": 0.8,
        }
    )
    original = "只看公共文档，不要查询源码"

    plan = QueryPlanner(client).plan(original)

    assert plan.standalone_query == original
    assert plan.excluded_terms == ("源码",)


def test_front_loaded_intent_call_also_returns_the_reusable_retrieval_plan():
    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer

    payload = {
        "scope": "klonet",
        "task_type": "development",
        "operation": "topology_deploy",
        "target": "TopoDeployAPI",
        "requires_retrieval": True,
        "confidence": 0.92,
        "standalone_query": "TopoDeployAPI 如何创建拓扑",
        "retrieval_tasks": [
            {
                "store": "source_code",
                "purpose": "定位实现",
                "keyword_queries": ["TopoDeployAPI"],
                "semantic_queries": ["拓扑创建源码实现"],
                "exact_terms": ["TopoDeployAPI"],
            },
            {
                "store": "public_docs",
                "purpose": "查询流程",
                "keyword_queries": ["拓扑部署"],
                "semantic_queries": ["Klonet 拓扑部署流程"],
                "exact_terms": [],
            },
        ],
        "excluded_terms": [],
    }

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            import json

            self.calls.append((messages, kwargs))
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(payload, ensure_ascii=False)
                        )
                    )
                ],
                usage=SimpleNamespace(total_tokens=20),
            )

    class NoCases:
        def search_for_prompt(self, *args, **kwargs):
            return ()

    llm = FakeLLM()
    analysis = IntentAnalyzer(llm, intent_case_retriever=NoCases()).analyze(
        "TopoDeployAPI 如何创建拓扑"
    )

    assert len(llm.calls) == 1
    assert analysis.retrieval_plan is not None
    assert [task.store for task in analysis.retrieval_plan.retrieval_tasks] == [
        "source_code",
        "public_docs",
    ]
    assert llm.calls[0][1]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_retrieval_plan_round_trips_through_internal_tool_arguments():
    from klonet_agent.knowledge.query_planner import (
        QueryPlanner,
        plan_as_dict,
        plan_from_mapping,
    )

    analysis_client = _FakePlannerClient(
        {
            "standalone_query": "TopoDeployAPI 实现",
            "retrieval_tasks": [
                {
                    "store": "source_code",
                    "purpose": "定位实现",
                    "keyword_queries": ["TopoDeployAPI"],
                    "semantic_queries": ["拓扑部署实现"],
                    "exact_terms": ["TopoDeployAPI"],
                }
            ],
            "excluded_terms": [],
            "confidence": 0.9,
        }
    )
    original = "TopoDeployAPI 在哪里实现"
    plan = QueryPlanner(analysis_client).plan(original)

    restored = plan_from_mapping(
        original,
        plan_as_dict(plan),
        support_text=original,
    )

    assert restored is not None
    assert restored.retrieval_tasks == plan.retrieval_tasks


def _chunk(chunk_id, *, layer, path, score, exact=0.0, symbol=""):
    from klonet_agent.knowledge.models import RetrievedChunk

    return RetrievedChunk(
        chunk_id=chunk_id,
        layer=layer,
        source=layer,
        path=path,
        title=chunk_id,
        snippet=f"{chunk_id} evidence",
        domain="topology",
        priority="P1",
        status="current",
        quality="reviewed",
        sensitivity="public",
        last_verified="",
        score=score,
        exact_score=exact,
        symbol=symbol,
    )


class _FakeRetriever:
    vector_status = "ready"
    vector_status_detail = "loaded_vectors=2"

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def recall_channel(self, request, channel, *, top_k):
        self.calls.append((request.query, channel, top_k))
        return list(self.rows.get((request.query, channel), ()))


class _StaticPlanner:
    def __init__(self, plan):
        self.value = plan
        self.calls = 0

    def plan(self, query, **kwargs):
        self.calls += 1
        return self.value


class _StaticReranker:
    available = True

    def __init__(self):
        self.calls = []

    def rerank(self, query, candidates, *, top_n):
        from klonet_agent.llm.reranker import RerankItem

        self.calls.append((query, [item.chunk_id for item in candidates], top_n))
        indexes = {item.chunk_id: index for index, item in enumerate(candidates)}
        return [
            RerankItem(indexes["code"], 0.95),
            RerankItem(indexes["doc"], 0.55),
        ]


def test_multi_stage_retriever_fuses_channels_then_reranks():
    from klonet_agent.knowledge.models import RetrievalPlan, RetrievalTask
    from klonet_agent.knowledge.multi_stage import MultiStageRetriever

    plan = RetrievalPlan(
        original_query="怎么创建拓扑",
        standalone_query="Klonet 如何创建拓扑",
        retrieval_tasks=(
            RetrievalTask(
                store="public_docs",
                purpose="查询流程",
                keyword_queries=("拓扑 创建",),
                semantic_queries=("Klonet 拓扑创建流程",),
            ),
            RetrievalTask(
                store="source_code",
                purpose="定位实现",
                exact_terms=("TopoDeployAPI",),
            ),
        ),
        confidence=0.9,
    )
    doc = _chunk("doc", layer="curated", path="guide.md", score=3)
    public = _FakeRetriever(
        {
            ("怎么创建拓扑", "bm25"): [doc],
            ("怎么创建拓扑", "dense"): [doc],
            ("Klonet 如何创建拓扑", "dense"): [doc],
        }
    )
    code = _chunk(
        "code",
        layer="source_code",
        path="topo.py",
        score=100,
        exact=100,
        symbol="TopoDeployAPI",
    )
    source = _FakeRetriever({("TopoDeployAPI", "exact"): [code]})
    reranker = _StaticReranker()
    planner = _StaticPlanner(plan)

    outcome = MultiStageRetriever(
        public_retriever=public,
        source_retriever=source,
        planner=planner,
        reranker=reranker,
    ).search(
        "怎么创建拓扑",
        top_k=2,
        task_type="development",
    )

    assert planner.calls == 1
    assert outcome.status == "reliable"
    assert [item.chunk_id for item in outcome.results] == ["code", "doc"]
    assert len(outcome.results[1].recall_channels) == 3
    assert outcome.results[0].rerank_score == 0.95
    assert outcome.fusion_status.startswith("weighted_rrf")
    assert outcome.rerank_status.startswith("qwen3_rerank")


def test_supplied_retrieval_plan_skips_a_second_planner_call():
    from klonet_agent.knowledge.models import RetrievalPlan, RetrievalTask
    from klonet_agent.knowledge.multi_stage import MultiStageRetriever

    plan = RetrievalPlan(
        original_query="Klonet 是什么",
        standalone_query="Klonet 是什么",
        retrieval_tasks=(
            RetrievalTask(store="public_docs", purpose="查询概念"),
        ),
    )
    retriever = _FakeRetriever({})
    planner = _StaticPlanner(plan)

    outcome = MultiStageRetriever(
        public_retriever=retriever,
        source_retriever=_FakeRetriever({}),
        planner=planner,
        reranker=SimpleNamespace(available=False),
    ).search(
        "Klonet 是什么",
        top_k=3,
        task_type="concept",
        retrieval_plan=plan,
    )

    assert planner.calls == 0
    assert outcome.planner_status == "planned"


def test_rerank_client_uses_dashscope_compatible_endpoint():
    from klonet_agent.llm.reranker import RerankClient

    class FakeHTTPClient:
        def __init__(self):
            self.requests = []

        def post(self, path, **kwargs):
            self.requests.append((path, kwargs))
            return {"results": [{"index": 0, "relevance_score": 0.8}]}

    http = FakeHTTPClient()
    client = RerankClient(client=http, api_key="test", model="qwen3-rerank")
    results = client.rerank(
        "query",
        [_chunk("doc", layer="curated", path="guide.md", score=1)],
        top_n=10,
    )

    assert results[0].index == 0
    assert http.requests[0][0] == "/reranks"
    body = http.requests[0][1]["body"]
    assert body["model"] == "qwen3-rerank"
    assert body["top_n"] == 1
    assert body["documents"][0].startswith("title: doc")


def test_dense_query_variants_are_primed_in_one_embedding_batch():
    import json

    from klonet_agent.knowledge.retriever import KnowledgeRetriever
    from tests.helpers import local_temp_dir

    class Provider:
        def __init__(self):
            self.batches = []
            self.singles = []

        def embed_text(self, text):
            self.singles.append(text)
            return (1.0, 0.0)

        def embed_texts(self, texts):
            self.batches.append(tuple(texts))
            return tuple((1.0, 0.0) for _ in texts)

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        vector_file = temp_dir / "vectors.jsonl"
        index_file.write_text(
            json.dumps(
                {
                    "chunk_id": "one",
                    "layer": "curated",
                    "path": "guide.md",
                    "title": "Guide",
                    "content": "Klonet topology",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        vector_file.write_text(
            json.dumps({"chunk_id": "one", "embedding": [1.0, 0.0]}) + "\n",
            encoding="utf-8",
        )
        provider = Provider()
        retriever = KnowledgeRetriever(
            index_file=index_file,
            vector_index_file=vector_file,
            embedding_provider=provider.embed_text,
            auto_build_vectors=False,
        )
        retriever.prime_query_embeddings(["query one", "query two", "query one"])

        assert provider.batches == [("query one", "query two")]
        assert retriever._embed_query("query one") == (1.0, 0.0)
        assert provider.singles == []
