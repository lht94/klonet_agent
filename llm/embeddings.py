"""OpenAI-compatible embedding client for semantic retrieval adapters."""

from __future__ import annotations

import os
from typing import Any

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv():
        return None

from klonet_agent.config import (
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
)


load_dotenv()


def get_embedding_api_key() -> str | None:
    """Return the first configured embedding API key."""

    return (
        os.environ.get("EMBEDDING_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def build_default_embedding_provider():
    """Build the default embedding callable when credentials are configured."""

    if not get_embedding_api_key():
        return None
    return EmbeddingClient().embed_text


class EmbeddingClient:
    """Small adapter around an OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_EMBEDDING_BASE_URL,
        model: str = DEFAULT_EMBEDDING_MODEL,
        client: Any | None = None,
    ):
        self.api_key = api_key or get_embedding_api_key()
        self.base_url = base_url
        self.model = model
        if client is not None:
            self.client = client
        else:
            from openai import OpenAI

            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def embed_text(self, text: str) -> tuple[float, ...]:
        """Return a dense vector for one text input."""

        response = self.client.embeddings.create(
            model=self.model,
            input=text,
        )
        if not getattr(response, "data", None):
            return ()
        embedding = getattr(response.data[0], "embedding", None) or ()
        return tuple(float(value) for value in embedding)
