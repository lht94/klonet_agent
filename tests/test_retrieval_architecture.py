"""BM25、metadata、软路由和检索评估测试。"""

import json

from tests.helpers import local_temp_dir


def _write_rows(path, rows):
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_mixed_tokenizer_handles_chinese_and_code_identifiers():
    from klonet_agent.knowledge.tokenizer import MixedTokenizer

    tokens = MixedTokenizer().tokenize(
        "拓扑部署时 TopoDeployAPI 调用 /master/topo/ 后进度条卡住",
    )

    assert "拓扑部署" in tokens
    assert "topodeployapi" in tokens
    assert "/master/topo/" in tokens
    assert "进度条" in tokens
    assert "卡住" in tokens


def test_indexer_propagates_frontmatter_and_heading_metadata():
    from klonet_agent.knowledge.indexer import KnowledgeIndexer

    with local_temp_dir() as temp_dir:
        root = temp_dir / "repo"
        doc = root / "knowledge" / "klonet" / "flows" / "deploy.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(
            "---\n"
            "domain: topology\n"
            "priority: P0\n"
            "status: current\n"
            "quality: verified\n"
            "sensitivity: public\n"
            "last_verified: 2026-06-23\n"
            "---\n"
            "# 拓扑部署\n\n部署入口。\n"
            "## 进度排查\n\n检查 process_bar。\n",
            encoding="utf-8",
        )
        index_file = temp_dir / "index.jsonl"
        KnowledgeIndexer(root=root, index_file=index_file).build()
        rows = [
            json.loads(line)
            for line in index_file.read_text(encoding="utf-8").splitlines()
        ]

    assert len(rows) == 2
    assert {row["domain"] for row in rows} == {"topology"}
    assert {row["priority"] for row in rows} == {"P0"}
    assert {row["quality"] for row in rows} == {"verified"}
    assert all(row["chunk_id"] for row in rows)


def test_indexer_normalizes_existing_knowledge_frontmatter():
    """兼容知识库现有的 domains 和扩展状态字段。"""

    from klonet_agent.knowledge.indexer import KnowledgeIndexer

    with local_temp_dir() as temp_dir:
        root = temp_dir / "repo"
        doc = root / "knowledge" / "klonet_experience" / "case.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(
            "---\n"
            "domains: topology, celery, worker\n"
            "priority: P0\n"
            "status: diagnostic_playbook\n"
            "last_verified: 2026-06-23\n"
            "---\n"
            "# 拓扑部署进度卡住\n\n检查任务状态。\n",
            encoding="utf-8",
        )
        index_file = temp_dir / "index.jsonl"
        KnowledgeIndexer(root=root, index_file=index_file).build()
        row = json.loads(index_file.read_text(encoding="utf-8").splitlines()[0])

    assert row["domain"] == "topology"
    assert row["status"] == "current"
    assert row["quality"] == "reviewed"


def test_indexer_excludes_non_public_chunks():
    from klonet_agent.knowledge.indexer import KnowledgeIndexer

    with local_temp_dir() as temp_dir:
        root = temp_dir / "repo"
        doc = root / "knowledge" / "klonet" / "ops" / "secret.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(
            "---\nsensitivity: restricted\n---\n# 私有资料\n\n不应进入索引。",
            encoding="utf-8",
        )
        index_file = temp_dir / "index.jsonl"
        count = KnowledgeIndexer(root=root, index_file=index_file).build()

    assert count == 0


def test_soft_router_only_hard_disables_explicit_negation():
    from klonet_agent.knowledge.router import QueryRouter

    router = QueryRouter()
    explicit = router.route("这个问题与 Klonet 无关，只讲 Docker 网络")
    generic = router.route("如何配置 Docker Compose 自定义网络")
    ambiguous = router.route("这个项目怎么部署？")
    mixed = router.route("Klonet 里的 Docker Compose 应该怎么写？")

    assert explicit.scope == "general"
    assert explicit.hard_disable_rag is True
    assert generic.scope == "general"
    assert generic.hard_disable_rag is False
    assert ambiguous.scope == "klonet"
    assert ambiguous.confidence < 0.7
    assert mixed.scope == "mixed"


def test_bm25_and_layer_weights_follow_task_type():
    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        {
            "chunk_id": "curated-1",
            "layer": "curated",
            "source": "curated",
            "path": "knowledge/klonet/flows/topology.md",
            "title": "拓扑部署",
            "content": "拓扑部署进度条卡住，检查任务状态。",
            "domain": "topology",
            "priority": "P1",
            "status": "current",
            "quality": "verified",
            "sensitivity": "public",
            "last_verified": "2026-06-23",
        },
        {
            "chunk_id": "experience-1",
            "layer": "experience",
            "source": "experience",
            "path": "knowledge/klonet_experience/cases/progress.md",
            "title": "进度条卡住案例",
            "content": "拓扑部署进度条卡住，历史根因是 Worker 任务异常。",
            "domain": "topology",
            "priority": "P1",
            "status": "current",
            "quality": "reviewed",
            "sensitivity": "public",
            "last_verified": "2026-06-23",
        },
    ]
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        _write_rows(index_file, rows)
        retriever = KnowledgeRetriever(index_file=index_file)
        outcome = retriever.search_request(
            SearchRequest(
                query="拓扑部署进度条卡住怎么排查",
                task_type="troubleshooting",
                top_k=2,
            ),
        )

    assert outcome.status == "reliable"
    assert outcome.results[0].layer == "experience"


def test_exact_route_query_still_prefers_machine_index():
    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        {
            "chunk_id": "curated",
            "layer": "curated",
            "source": "curated",
            "path": "knowledge/klonet/flows/topology.md",
            "title": "拓扑",
            "content": "/master/topo/ TopoDeployAPI " * 5,
            "domain": "topology",
            "priority": "P0",
            "status": "current",
            "quality": "verified",
            "sensitivity": "public",
            "last_verified": "",
        },
        {
            "chunk_id": "route",
            "layer": "machine_index",
            "source": "machine_index",
            "path": "knowledge/klonet_index/routes.jsonl",
            "title": "/master/topo/",
            "content": '{"route": "/master/topo/", "view_class": "TopoDeployAPI"}',
            "domain": "topology",
            "priority": "P1",
            "status": "current",
            "quality": "generated",
            "sensitivity": "public",
            "last_verified": "",
        },
    ]
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        _write_rows(index_file, rows)
        outcome = KnowledgeRetriever(index_file=index_file).search_request(
            SearchRequest(query="/master/topo/ TopoDeployAPI", task_type="code_lookup"),
        )

    assert outcome.results[0].layer == "machine_index"
    assert outcome.results[0].exact_score > 0


def test_metadata_filters_deprecated_and_restricted_rows():
    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    base = {
        "layer": "curated",
        "source": "curated",
        "title": "部署",
        "content": "Klonet 拓扑部署流程",
        "domain": "topology",
        "priority": "P1",
        "quality": "reviewed",
        "last_verified": "",
    }
    rows = [
        dict(base, chunk_id="ok", path="ok.md", status="current", sensitivity="public"),
        dict(base, chunk_id="old", path="old.md", status="deprecated", sensitivity="public"),
        dict(base, chunk_id="secret", path="secret.md", status="current", sensitivity="restricted"),
    ]
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        _write_rows(index_file, rows)
        outcome = KnowledgeRetriever(index_file=index_file).search_request(
            SearchRequest(query="Klonet 拓扑部署", top_k=10),
        )

    assert [item.chunk_id for item in outcome.results] == ["ok"]


def test_domain_filter_accepts_router_metadata_aliases():
    """路由领域和知识 frontmatter 使用不同词表时仍可匹配。"""

    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    row = {
        "chunk_id": "environment",
        "layer": "curated",
        "source": "curated",
        "path": "knowledge/klonet/ops/environment_setup.md",
        "title": "Klonet 环境部署",
        "content": "Klonet 项目部署依赖和环境准备。",
        "domain": "deployment",
        "priority": "P0",
        "status": "current",
        "quality": "verified",
        "sensitivity": "public",
        "last_verified": "2026-06-23",
    }
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        _write_rows(index_file, [row])
        outcome = KnowledgeRetriever(index_file=index_file).search_request(
            SearchRequest(query="这个项目怎么部署", domains=("runtime",)),
        )

    assert [item.chunk_id for item in outcome.results] == ["environment"]


def test_knowledge_base_formats_relevance_and_metadata():
    from klonet_agent.knowledge.rag import KnowledgeBase
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    row = {
        "chunk_id": "one",
        "layer": "curated",
        "source": "curated",
        "path": "knowledge/klonet/flows/topology.md",
        "title": "拓扑部署",
        "content": "Klonet 拓扑部署由 TopoDeployAPI 处理。",
        "domain": "topology",
        "priority": "P0",
        "status": "current",
        "quality": "verified",
        "sensitivity": "public",
        "last_verified": "2026-06-23",
    }
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        _write_rows(index_file, [row])
        result = KnowledgeBase(
            KnowledgeRetriever(index_file=index_file),
        ).search_knowledge("Klonet 拓扑部署入口")

    assert "layer: curated" in result
    assert "domain: topology" in result
    assert "relevance:" in result
