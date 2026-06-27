"""Build the semantic vector sidecar for the Klonet knowledge index."""

from __future__ import annotations

import argparse

from klonet_agent.config import KNOWLEDGE_VECTOR_INDEX_FILE
from klonet_agent.knowledge.retriever import KnowledgeRetriever


def build_vectors(retriever: KnowledgeRetriever | None = None) -> int:
    """Build vectors through the retriever so index loading stays consistent."""

    active_retriever = retriever or KnowledgeRetriever()
    return active_retriever.build_vector_index()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build knowledge chunk embeddings for semantic RAG retrieval.",
    )
    parser.parse_args()
    count = build_vectors()
    print(f"built {count} vectors -> {KNOWLEDGE_VECTOR_INDEX_FILE}")
    return 0 if count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
