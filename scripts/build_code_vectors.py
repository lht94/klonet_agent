"""Build the structure-aware Klonet source index and vector sidecar."""

from __future__ import annotations

import argparse

from klonet_agent.config import (
    CODE_INDEX_FILE,
    CODE_VECTOR_INDEX_FILE,
    KLONET_SOURCE_ROOT,
)
from klonet_agent.knowledge.code_indexer import CodeIndexer
from klonet_agent.knowledge.retriever import KnowledgeRetriever


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build structure-aware Klonet source embeddings.",
    )
    parser.add_argument("--force-index", action="store_true")
    parser.add_argument("--force-vectors", action="store_true")
    args = parser.parse_args()

    indexer = CodeIndexer()
    if args.force_index or indexer.is_stale():
        chunks = indexer.build()
        print(f"built {chunks} source chunks -> {CODE_INDEX_FILE}")
    else:
        print(f"source index is current -> {CODE_INDEX_FILE}")

    retriever = KnowledgeRetriever(
        index_file=CODE_INDEX_FILE,
        vector_index_file=CODE_VECTOR_INDEX_FILE,
        auto_build_vectors=False,
    )
    vectors = retriever.build_vector_index(append=not args.force_vectors)
    print(f"built {vectors} source vectors -> {CODE_VECTOR_INDEX_FILE}")
    if not KLONET_SOURCE_ROOT.is_dir():
        print(f"missing source root: {KLONET_SOURCE_ROOT}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
