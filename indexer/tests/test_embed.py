import json

import httpx
import pytest
import respx

from indexer.config import Settings
from indexer.embed.ollama import EmbeddingDimensionError, OllamaEmbedder


@respx.mock
def test_embedder_batches_at_32() -> None:
    route = respx.post("http://localhost:11434/api/embed").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={"embeddings": [[0.0] * 1024 for _ in json.loads(request.content)["input"]]},
        )
    )
    embedder = OllamaEmbedder(
        Settings(indexer_database_url="postgresql://example", _env_file=None), client=httpx.Client()
    )

    vectors = embedder.embed([f"text-{index}" for index in range(70)])

    assert len(vectors) == 70
    assert route.call_count == 3


@respx.mock
def test_embedder_rejects_wrong_dimension() -> None:
    respx.post("http://localhost:11434/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.0] * 3]})
    )
    embedder = OllamaEmbedder(
        Settings(indexer_database_url="postgresql://example", _env_file=None), client=httpx.Client()
    )

    with pytest.raises(EmbeddingDimensionError, match="dimension 3"):
        embedder.embed(["one"])
