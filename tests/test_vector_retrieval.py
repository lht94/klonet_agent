"""Vector retrieval coverage for intent cases and RAG chunks."""

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tests.helpers import local_temp_dir


def _write_rows(path, rows):
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_knowledge_vector_index_builds_embeddings_for_chunks():
    from klonet_agent.knowledge.vector_index import KnowledgeVectorIndex

    calls = []

    def embed(text: str):
        calls.append(text)
        return [1.0, 0.0] if "first" in text else [0.0, 1.0]

    rows = [
        {"chunk_id": "one", "title": "first title", "content": "first body"},
        {"chunk_id": "two", "title": "second title", "content": "second body"},
    ]
    with local_temp_dir() as temp_dir:
        vector_file = temp_dir / "vectors.jsonl"
        count = KnowledgeVectorIndex(
            vector_file=vector_file,
            embedding_provider=embed,
        ).build(rows)
        loaded = KnowledgeVectorIndex(vector_file=vector_file).load()

    assert count == 2
    assert calls == ["first title\nfirst body", "second title\nsecond body"]
    assert loaded == {"one": (1.0, 0.0), "two": (0.0, 1.0)}


def test_knowledge_vector_index_can_append_limited_path_subset():
    from klonet_agent.knowledge.vector_index import KnowledgeVectorIndex

    calls = []

    def embed(text: str):
        calls.append(text)
        return [float(len(calls)), 0.0]

    rows = [
        {
            "chunk_id": "existing",
            "path": "knowledge/klonet/00_project_overview.md",
            "title": "existing title",
            "content": "existing body",
        },
        {
            "chunk_id": "target",
            "path": "knowledge/klonet/00_project_overview.md",
            "title": "target title",
            "content": "target body",
        },
        {
            "chunk_id": "ignored",
            "path": "doc/00_project_overview.md",
            "title": "ignored title",
            "content": "ignored body",
        },
    ]

    with local_temp_dir() as temp_dir:
        vector_file = temp_dir / "vectors.jsonl"
        vector_file.write_text(
            '{"chunk_id": "existing", "embedding": [9.0, 0.0]}\n',
            encoding="utf-8",
        )
        count = KnowledgeVectorIndex(
            vector_file=vector_file,
            embedding_provider=embed,
        ).build(
            rows,
            append=True,
            limit=1,
            include_paths=("knowledge/klonet/00_project_overview.md",),
        )
        loaded = KnowledgeVectorIndex(vector_file=vector_file).load()

    assert count == 1
    assert calls == ["target title\ntarget body"]
    assert loaded == {
        "existing": (9.0, 0.0),
        "target": (1.0, 0.0),
    }


def test_retriever_uses_semantic_vector_match_when_keywords_differ():
    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        {
            "chunk_id": "startup",
            "layer": "curated",
            "source": "curated",
            "path": "startup.md",
            "title": "start services",
            "content": "gunicorn celery redis screen",
            "domain": "deployment",
            "priority": "P1",
            "status": "current",
            "quality": "verified",
            "sensitivity": "public",
        },
        {
            "chunk_id": "topology",
            "layer": "curated",
            "source": "curated",
            "path": "topology.md",
            "title": "topology progress",
            "content": "worker celery progress bar stuck",
            "domain": "topology",
            "priority": "P1",
            "status": "current",
            "quality": "verified",
            "sensitivity": "public",
        },
    ]

    def embed(text: str):
        lowered = text.lower()
        if "topology progress" in lowered or "graph creation hangs" in lowered:
            return [1.0, 0.0]
        if "start services" in lowered:
            return [0.0, 1.0]
        return [0.0, 0.0]

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        vector_file = temp_dir / "vectors.jsonl"
        _write_rows(index_file, rows)
        retriever = KnowledgeRetriever(
            index_file=index_file,
            vector_index_file=vector_file,
            embedding_provider=embed,
        )
        retriever.build_vector_index()
        outcome = retriever.search_request(
            SearchRequest(query="graph creation hangs", top_k=1),
        )

    assert [item.chunk_id for item in outcome.results] == ["topology"]
    assert outcome.results[0].semantic_score > 0.9


def test_retriever_automatically_builds_missing_vector_sidecar():
    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        {
            "chunk_id": "startup",
            "title": "start services",
            "content": "gunicorn celery redis",
        },
        {
            "chunk_id": "topology",
            "title": "topology progress",
            "content": "worker progress bar",
        },
    ]
    calls = []

    def embed(text: str):
        calls.append(text)
        if "topology" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        vector_file = temp_dir / "vectors.jsonl"
        _write_rows(index_file, rows)
        retriever = KnowledgeRetriever(
            index_file=index_file,
            vector_index_file=vector_file,
            embedding_provider=embed,
            auto_build_vectors=True,
        )
        outcome = retriever.search_request(
            SearchRequest(query="topology", top_k=1),
        )
        loaded = retriever._vectors
        vector_file_created = vector_file.exists()

    assert vector_file_created
    assert set(loaded) == {"startup", "topology"}
    assert calls[:2] == [
        "start services\ngunicorn celery redis",
        "topology progress\nworker progress bar",
    ]
    assert outcome.retrieval_mode == "hybrid"
    assert outcome.vector_status == "ready"


def test_vector_index_rebuilds_changed_chunk_content_incrementally():
    from klonet_agent.knowledge.vector_index import KnowledgeVectorIndex

    calls = []

    def embed(text: str):
        calls.append(text)
        return [float(len(calls)), 1.0]

    with local_temp_dir() as temp_dir:
        vector_file = temp_dir / "vectors.jsonl"
        index = KnowledgeVectorIndex(
            vector_file=vector_file,
            embedding_provider=embed,
        )
        index.build(
            [{"chunk_id": "same", "title": "title", "content": "version one"}]
        )
        index.build(
            [{"chunk_id": "same", "title": "title", "content": "version two"}],
            append=True,
        )
        loaded = index.load()

    assert calls == [
        "title\nversion one",
        "title\nversion two",
    ]
    assert loaded["same"] == (2.0, 1.0)


def test_vector_index_append_removes_chunks_no_longer_in_public_scope():
    """增量同步也必须删除已从当前知识索引移除的旧向量。"""

    from klonet_agent.knowledge.vector_index import KnowledgeVectorIndex

    def embed(text: str):
        return [1.0, 1.0]

    current_rows = [
        {"chunk_id": "current", "title": "current title", "content": "current body"},
    ]
    with local_temp_dir() as temp_dir:
        vector_file = temp_dir / "vectors.jsonl"
        vector_file.write_text(
            '{"chunk_id": "current", "embedding": [1.0, 1.0]}\n'
            '{"chunk_id": "removed", "embedding": [2.0, 2.0]}\n',
            encoding="utf-8",
        )
        index = KnowledgeVectorIndex(
            vector_file=vector_file,
            embedding_provider=embed,
        )

        assert index.missing_count(current_rows) == 1
        count = index.build(current_rows, append=True)
        loaded = index.load()

    assert count == 0
    assert loaded == {"current": (1.0, 1.0)}


def test_vector_index_persists_completed_batches_before_failure():
    from klonet_agent.knowledge.vector_index import KnowledgeVectorIndex

    calls = []

    def embed(text: str):
        calls.append(text)
        if "second" in text:
            raise RuntimeError("temporary failure")
        return [1.0, 0.0]

    rows = [
        {"chunk_id": "one", "title": "first", "content": "body"},
        {"chunk_id": "two", "title": "second", "content": "body"},
    ]
    with local_temp_dir() as temp_dir:
        vector_file = temp_dir / "vectors.jsonl"
        index = KnowledgeVectorIndex(
            vector_file=vector_file,
            embedding_provider=embed,
        )
        try:
            index.build(rows, batch_size=1)
        except RuntimeError:
            pass
        loaded = index.load()

    assert calls == ["first\nbody", "second\nbody"]
    assert loaded == {"one": (1.0, 0.0)}


def test_vector_index_rebuilds_when_embedding_model_changes():
    from klonet_agent.knowledge.vector_index import KnowledgeVectorIndex

    class Provider:
        def __init__(self):
            self.model = "model-a"
            self.calls = []

        def embed(self, text: str):
            self.calls.append((self.model, text))
            return [1.0, float(len(self.calls))]

    provider = Provider()
    rows = [{"chunk_id": "one", "title": "title", "content": "body"}]
    with local_temp_dir() as temp_dir:
        index = KnowledgeVectorIndex(
            vector_file=temp_dir / "vectors.jsonl",
            embedding_provider=provider.embed,
        )
        index.build(rows)
        provider.model = "model-b"
        index.build(rows, append=True)

    assert [model for model, _ in provider.calls] == ["model-a", "model-b"]


def test_retriever_keeps_bm25_available_when_auto_build_fails():
    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        {
            "chunk_id": "one",
            "title": "Klonet topology",
            "content": "Klonet topology deployment",
        }
    ]

    def broken_embed(text: str):
        raise RuntimeError("provider unavailable")

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        vector_file = temp_dir / "vectors.jsonl"
        _write_rows(index_file, rows)
        outcome = KnowledgeRetriever(
            index_file=index_file,
            vector_index_file=vector_file,
            embedding_provider=broken_embed,
            auto_build_vectors=True,
        ).search_request(SearchRequest(query="Klonet topology"))

    assert outcome.results
    assert outcome.retrieval_mode == "bm25"
    assert outcome.vector_status == "error"
    assert outcome.vector_status_detail == "auto_build_failed:RuntimeError"


def test_default_intent_case_retriever_uses_embedding_client_when_configured(monkeypatch):
    from klonet_agent.knowledge import intent_cases

    class FakeEmbeddingClient:
        def __init__(self):
            self.calls = []

        def embed_text(self, text):
            self.calls.append(text)
            return (1.0, 0.0)

    fake_client = FakeEmbeddingClient()
    monkeypatch.setattr(
        intent_cases,
        "build_default_embedding_provider",
        lambda: fake_client.embed_text,
    )

    retriever = intent_cases.build_default_intent_case_retriever(root="missing")

    assert retriever.embedding_provider is not None
    assert retriever.embedding_provider("hello") == (1.0, 0.0)
    assert fake_client.calls == ["hello"]


def test_build_knowledge_vectors_script_delegates_to_retriever():
    from scripts.build_knowledge_vectors import build_vectors

    class FakeRetriever:
        def __init__(self):
            self.kwargs = None

        def build_vector_index(self, **kwargs):
            self.kwargs = kwargs
            return 42

    retriever = FakeRetriever()

    assert build_vectors(retriever=retriever) == 42
    assert retriever.kwargs == {
        "append": False,
        "limit": None,
        "include_paths": (),
    }


def test_build_knowledge_vectors_script_passes_incremental_options():
    from scripts.build_knowledge_vectors import build_vectors

    class FakeRetriever:
        def __init__(self):
            self.kwargs = None

        def build_vector_index(self, **kwargs):
            self.kwargs = kwargs
            return 3

    retriever = FakeRetriever()

    assert build_vectors(
        retriever=retriever,
        append=True,
        limit=3,
        include_paths=("knowledge/klonet/00_project_overview.md",),
    ) == 3
    assert retriever.kwargs == {
        "append": True,
        "limit": 3,
        "include_paths": ("knowledge/klonet/00_project_overview.md",),
    }
