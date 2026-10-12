"""Batched 1024-dimensional bge-m3 embeddings from host Ollama."""

from collections.abc import Sequence

import httpx

from indexer.config import Settings


class EmbeddingDimensionError(ValueError):
    """Raised before a malformed model response can enter Postgres."""


class OllamaEmbedder:
    batch_size = 32
    dimensions = 1024

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=120.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            response = self._client.post(
                f"{self.settings.ollama_host_url.rstrip('/')}/api/embed",
                json={
                    "model": self.settings.ollama_embed_model,
                    "input": batch,
                    "truncate": True,
                    "keep_alive": self.settings.ollama_keep_alive,
                },
            )
            response.raise_for_status()
            batch_vectors = response.json()["embeddings"]
            if len(batch_vectors) != len(batch):
                raise ValueError(
                    f"{self.settings.ollama_embed_model} returned {len(batch_vectors)} embeddings "
                    f"for {len(batch)} inputs"
                )
            for vector in batch_vectors:
                if len(vector) != self.dimensions:
                    raise EmbeddingDimensionError(
                        f"{self.settings.ollama_embed_model} returned dimension {len(vector)}; "
                        f"expected {self.dimensions}"
                    )
                vectors.append([float(value) for value in vector])
        return vectors
