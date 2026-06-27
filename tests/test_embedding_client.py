"""Embedding client adapter used by intent-case hybrid retrieval."""

from types import SimpleNamespace


def test_embedding_client_returns_vector_from_openai_compatible_response():
    from klonet_agent.llm.embeddings import EmbeddingClient

    class FakeEmbeddings:
        def __init__(self):
            self.requests = []

        def create(self, **request):
            self.requests.append(request)
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        embedding=[0.1, 0.2, 0.3],
                    )
                ]
            )

    fake_embeddings = FakeEmbeddings()
    fake_client = SimpleNamespace(embeddings=fake_embeddings)

    client = EmbeddingClient(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-embedding",
        client=fake_client,
    )

    vector = client.embed_text("用户问自己的笔记本要装什么工具")

    assert vector == (0.1, 0.2, 0.3)
    assert fake_embeddings.requests == [
        {
            "model": "test-embedding",
            "input": "用户问自己的笔记本要装什么工具",
        }
    ]
