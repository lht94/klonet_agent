"""Persistent vector index for knowledge chunks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
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

    def build(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        append: bool = False,
        limit: int | None = None,
        include_paths: Sequence[str] | None = None,
        batch_size: int = 10,
    ) -> int:
        """Embed rows and write chunk_id -> vector records."""

        if self.embedding_provider is None:
            return 0

        self.vector_file.parent.mkdir(parents=True, exist_ok=True)
        records = self.load_records() if append else {}
        if append:
            current_chunk_ids = {
                str(row.get("chunk_id") or f"legacy-{index}")
                for index, row in enumerate(rows)
            }
            records = {
                chunk_id: record
                for chunk_id, record in records.items()
                if chunk_id in current_chunk_ids
            }
        selected_paths = {path for path in include_paths or () if path}
        if limit is not None and limit <= 0:
            _write_records(self.vector_file, records)
            return 0

        pending: list[tuple[str, str, str]] = []
        embedding_model = _embedding_model(self.embedding_provider)
        for index, row in enumerate(rows):
            chunk_id = str(row.get("chunk_id") or f"legacy-{index}")
            if selected_paths and str(row.get("path") or "") not in selected_paths:
                continue
            text = _row_text(row)
            content_hash = _content_hash(text)
            existing = records.get(chunk_id)
            if (
                existing is not None
                and _vector(existing.get("embedding")) is not None
                and existing.get("content_hash") in {None, "", content_hash}
                and existing.get("embedding_model") in {
                    None,
                    "",
                    embedding_model,
                }
            ):
                continue
            pending.append((chunk_id, text, content_hash))
            if limit is not None and len(pending) >= limit:
                break

        count = 0
        safe_batch_size = max(1, int(batch_size))
        try:
            for start in range(0, len(pending), safe_batch_size):
                batch = pending[start:start + safe_batch_size]
                vectors = _embed_batch(
                    self.embedding_provider,
                    [text for _, text, _ in batch],
                )
                if len(vectors) != len(batch):
                    raise ValueError(
                        "embedding provider returned a different number of vectors"
                    )
                for (chunk_id, _, content_hash), values in zip(batch, vectors):
                    vector = _vector(values)
                    if vector is None:
                        continue
                    records[chunk_id] = {
                        "chunk_id": chunk_id,
                        "embedding": vector,
                        "content_hash": content_hash,
                        "embedding_model": embedding_model,
                    }
                    count += 1
        except Exception:
            if count:
                _write_records(self.vector_file, records)
            raise
        else:
            _write_records(self.vector_file, records)
        return count

    def load(self) -> dict[str, tuple[float, ...]]:
        """Load all persisted vectors keyed by chunk id."""

        return {
            chunk_id: vector
            for chunk_id, record in self.load_records().items()
            if (vector := _vector(record.get("embedding"))) is not None
        }

    def load_records(self) -> dict[str, dict[str, Any]]:
        """Load vector records, including fingerprints used for incremental rebuilds."""

        if not self.vector_file.exists():
            return {}

        records: dict[str, dict[str, Any]] = {}
        with self.vector_file.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk_id = str(row.get("chunk_id") or "").strip()
                vector = _vector(row.get("embedding"))
                if chunk_id and vector is not None:
                    records[chunk_id] = {
                        "chunk_id": chunk_id,
                        "embedding": vector,
                        "content_hash": str(row.get("content_hash") or ""),
                        "embedding_model": str(row.get("embedding_model") or ""),
                    }
        return records

    def missing_count(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Return how many vector records are missing, stale, or out of scope."""

        records = self.load_records()
        embedding_model = _embedding_model(self.embedding_provider)
        current_chunk_ids = {
            str(row.get("chunk_id") or f"legacy-{index}")
            for index, row in enumerate(rows)
        }
        missing = len(set(records) - current_chunk_ids)
        for index, row in enumerate(rows):
            chunk_id = str(row.get("chunk_id") or f"legacy-{index}")
            text = _row_text(row)
            content_hash = _content_hash(text)
            record = records.get(chunk_id)
            if record is None or _vector(record.get("embedding")) is None:
                missing += 1
                continue
            stored_hash = str(record.get("content_hash") or "")
            if stored_hash and stored_hash != content_hash:
                missing += 1
                continue
            stored_model = str(record.get("embedding_model") or "")
            if stored_model and stored_model != embedding_model:
                missing += 1
        return missing


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


def _embed_batch(
    provider: EmbeddingProvider,
    texts: Sequence[str],
) -> tuple[Sequence[float], ...]:
    owner = getattr(provider, "__self__", None)
    batch_method = getattr(owner, "embed_texts", None)
    if callable(batch_method):
        return tuple(batch_method(texts))
    return tuple(provider(text) for text in texts)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embedding_model(provider: EmbeddingProvider | None) -> str:
    owner = getattr(provider, "__self__", None)
    return str(getattr(owner, "model", "") or "")


def _write_records(path: Path, records: Mapping[str, Mapping[str, Any]]):
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_name = file.name
            for chunk_id, record in records.items():
                vector = _vector(record.get("embedding"))
                if vector is None:
                    continue
                payload = {
                    "chunk_id": chunk_id,
                    "embedding": vector,
                }
                content_hash = str(record.get("content_hash") or "")
                if content_hash:
                    payload["content_hash"] = content_hash
                embedding_model = str(record.get("embedding_model") or "")
                if embedding_model:
                    payload["embedding_model"] = embedding_model
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _vector(values: Sequence[float] | None) -> tuple[float, ...] | None:
    if values is None:
        return None
    vector = tuple(float(value) for value in values)
    return vector if any(value != 0.0 for value in vector) else None
