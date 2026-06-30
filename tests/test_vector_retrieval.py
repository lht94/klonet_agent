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
