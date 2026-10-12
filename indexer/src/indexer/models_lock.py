"""Ollama digest lock enforcement for reproducible indexing."""

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from indexer.config import Settings


class ModelDriftError(RuntimeError):
    """A locked model changed; CLI maps this to the C3 exit code."""

    exit_code = 3


@dataclass(frozen=True)
class ModelDigests:
    embedding: str
    llm: str


def verify_model_lock(
    settings: Settings,
    *,
    allow_drift: bool = False,
    client: httpx.Client | None = None,
    lock_path: Path | None = None,
) -> ModelDigests:
    """Compare live Ollama digests with the committed model inventory."""
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.get(f"{settings.ollama_host_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        live = {
            str(model["name"]): str(model["digest"])
            for model in response.json().get("models", [])
            if "name" in model and "digest" in model
        }
    finally:
        if owns_client:
            client.close()
    lock_path = lock_path or Path(__file__).resolve().parents[3] / "infra" / "models.lock.json"
    locked = json.loads(lock_path.read_text(encoding="utf-8"))["ollama"]
    embedding = _lookup(live, settings.ollama_embed_model)
    llm = _lookup(live, settings.ollama_llm_model)
    expected_embedding = str(locked[settings.ollama_embed_model]["digest"])
    expected_llm = str(locked[settings.ollama_llm_model]["digest"])
    drift = []
    if embedding != expected_embedding:
        drift.append(
            f"{settings.ollama_embed_model}: locked {expected_embedding}, live {embedding}"
        )
    if llm != expected_llm:
        drift.append(f"{settings.ollama_llm_model}: locked {expected_llm}, live {llm}")
    if drift and not allow_drift:
        raise ModelDriftError("model digest drift: " + "; ".join(drift))
    return ModelDigests(embedding=embedding, llm=llm)


def _lookup(models: dict[str, str], configured_name: str) -> str:
    expected = configured_name if ":" in configured_name else f"{configured_name}:latest"
    try:
        return models[expected]
    except KeyError as error:
        message = f"configured model {expected!r} is not available from /api/tags"
        raise ModelDriftError(message) from error
