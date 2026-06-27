"""Persistent vector index for knowledge chunks."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from klonet_agent.config import KNOWLEDGE_VECTOR_INDEX_FILE


EmbeddingProvider = Callable[[str], Sequence[float]]


class KnowledgeVectorIndex:
    """Store chunk embeddings in a small JSONL sidecar file."""

    def __init__(
        self,
        vector_file: Path | str = KNOWLEDGE_VECTOR_INDEX_FILE,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.vector_file = Path(vector_file)
        self.embedding_provider = embedding_provider

    def build(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Embed rows and write chunk_id -> vector records."""

        if self.embedding_provider is None:
            return 0

        self.vector_file.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with self.vector_file.open("w", encoding="utf-8") as file:
            for index, row in enumerate(rows):
                chunk_id = str(row.get("chunk_id") or f"legacy-{index}")
                text = _row_text(row)
                vector = _vector(self.embedding_provider(text))
                if vector is None:
                    continue
                file.write(
                    json.dumps(
                        {
                            "chunk_id": chunk_id,
                            "embedding": vector,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                count += 1
        return count

    def load(self) -> dict[str, tuple[float, ...]]:
        """Load all persisted vectors keyed by chunk id."""

        if not self.vector_file.exists():
            return {}

        vectors: dict[str, tuple[float, ...]] = {}
        with self.vector_file.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk_id = str(row.get("chunk_id") or "").strip()
                vector = _vector(row.get("embedding"))
                if chunk_id and vector is not None:
                    vectors[chunk_id] = vector
        return vectors


def cosine_similarity(
    left: Sequence[float] | None,
    right: Sequence[float] | None,
) -> float:
    """Return cosine similarity for equal-length non-zero vectors."""

    left_vector = _vector(left)
    right_vector = _vector(right)
    if left_vector is None or right_vector is None:
        return 0.0
    if len(left_vector) != len(right_vector):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left_vector))
    right_norm = math.sqrt(sum(value * value for value in right_vector))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(
        left_value * right_value
        for left_value, right_value in zip(left_vector, right_vector)
    )
    return dot / (left_norm * right_norm)


def _row_text(row: Mapping[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    content = str(row.get("content") or "").strip()
    return "\n".join(part for part in (title, content) if part)


def _vector(values: Sequence[float] | None) -> tuple[float, ...] | None:
    if values is None:
        return None
    vector = tuple(float(value) for value in values)
    return vector if any(value != 0.0 for value in vector) else None
