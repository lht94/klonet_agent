"""DashScope OpenAI-compatible rerank client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Mapping

from openai import BaseModel

from klonet_agent.config import (
    RAG_RERANK_TIMEOUT_SECONDS,
    RERANK_BASE_URL,
    RERANK_MODEL,
)
if TYPE_CHECKING:
    from klonet_agent.knowledge.models import RetrievedChunk


@dataclass(frozen=True)
class RerankItem:
    index: int
    relevance_score: float


class _RerankResponseItem(BaseModel):
    index: int
    relevance_score: float


class _RerankResponse(BaseModel):
    results: List[_RerankResponseItem]


class RerankClient:
    """Small adapter around DashScope's ``POST /reranks`` endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = RERANK_BASE_URL,
        model: str = RERANK_MODEL,
        timeout: float = RAG_RERANK_TIMEOUT_SECONDS,
        client: Any | None = None,
    ):
        self.api_key = (
            api_key
            or os.getenv("RERANK_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("EMBEDDING_API_KEY")
        )
        self.base_url = base_url
        self.model = model
        if client is not None:
            self.client = client
        elif self.api_key:
            from openai import OpenAI

            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=timeout,
            )
        else:
            self.client = None

    @property
    def available(self) -> bool:
        return self.client is not None

    def rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        *,
        top_n: int,
    ) -> list[RerankItem]:
        if not self.client or not candidates:
            return []
        documents = [_document_text(item) for item in candidates]
        response = self.client.post(
            "/reranks",
            body={
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": min(max(1, top_n), len(documents)),
            },
            cast_to=_RerankResponse,
        )
        return _parse_response(response, candidate_count=len(candidates))


def _document_text(item: RetrievedChunk) -> str:
    content = item.rerank_text or item.snippet
    return (
        f"title: {item.title}\n"
        f"path: {item.path}\n"
        f"layer: {item.layer}\n"
        f"content:\n{content[:3000]}"
    )


def _parse_response(response: Any, *, candidate_count: int) -> list[RerankItem]:
    payload: Any
    if isinstance(response, Mapping):
        payload = response
    elif callable(getattr(response, "model_dump", None)):
        payload = response.model_dump()
    elif callable(getattr(response, "dict", None)):
        payload = response.dict()
    elif callable(getattr(response, "json", None)):
        payload = response.json()
    else:
        payload = response

    raw_results = (
        payload.get("results")
        if isinstance(payload, Mapping)
        else getattr(payload, "results", None)
    )
    if not isinstance(raw_results, list):
        raise ValueError("rerank response has no results")
    parsed: list[RerankItem] = []
    seen: set[int] = set()
    for raw in raw_results:
        if isinstance(raw, Mapping):
            index = raw.get("index")
            score = raw.get("relevance_score", raw.get("score"))
        else:
            index = getattr(raw, "index", None)
            score = getattr(raw, "relevance_score", getattr(raw, "score", None))
        try:
            normalized_index = int(index)
            normalized_score = float(score)
        except (TypeError, ValueError):
            continue
        if (
            normalized_index < 0
            or normalized_index >= candidate_count
            or normalized_index in seen
        ):
            continue
        seen.add(normalized_index)
        parsed.append(RerankItem(normalized_index, normalized_score))
    if not parsed:
        raise ValueError("rerank response contains no valid items")
    return parsed
