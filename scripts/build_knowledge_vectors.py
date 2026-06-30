"""Build the semantic vector sidecar for the Klonet knowledge index."""

from __future__ import annotations

import argparse

from klonet_agent.config import KNOWLEDGE_VECTOR_INDEX_FILE
from klonet_agent.knowledge.retriever import KnowledgeRetriever


def build_vectors(
    retriever: KnowledgeRetriever | None = None,
    *,
    append: bool = False,
    limit: int | None = None,
    include_paths: tuple[str, ...] = (),
) -> int:
    """Build vectors through the retriever so index loading stays consistent."""

    active_retriever = retriever or KnowledgeRetriever()
    return active_retriever.build_vector_index(
        append=append,
        limit=limit,
        include_paths=include_paths,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build knowledge chunk embeddings for semantic RAG retrieval.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Keep existing chunk vectors and only embed missing chunks.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of new chunk vectors to build in this run.",
    )
    parser.add_argument(
        "--include-path",
        action="append",
        default=[],
        help="Only build vectors for this indexed path. Can be repeated.",
    )
    args = parser.parse_args()
    count = build_vectors(
        append=args.append,
        limit=args.limit,
        include_paths=tuple(args.include_path),
    )
    print(f"built {count} vectors -> {KNOWLEDGE_VECTOR_INDEX_FILE}")
    return 0 if count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
