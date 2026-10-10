#!/usr/bin/env python3
"""Print the model digest lock (design.md contract C3) to stdout.

Resolves the pinned Ollama models from the live ``/api/tags`` endpoint and the reranker
revision from the Hugging Face model API. Usage (from the repo root):

    make models-lock          # python3 infra/scripts/models_lock.py > infra/models.lock.json

Environment (the repo-root ``.env`` is read as a fallback; real environment variables win):
    OLLAMA_HOST_URL   default http://localhost:11434
    OLLAMA_EMBED_MODEL, OLLAMA_LLM_MODEL   default bge-m3 / llama3.1:8b
    RERANKER_MODEL    default BAAI/bge-reranker-v2-m3
    HF_ENDPOINT       default https://huggingface.co

Exit codes: 0 ok, 1 a model or revision could not be resolved (message on stderr).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def fail(message: str) -> None:
    print(f"models_lock: {message}", file=sys.stderr)
    sys.exit(1)


def get_json(url: str, what: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "hybrid-rag-models-lock"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        fail(f"cannot fetch {what} from {url}: {exc}")
        raise  # unreachable, keeps type checkers happy


def resolve_ollama(tags: dict, configured: str, base_url: str) -> str:
    wanted = configured if ":" in configured else f"{configured}:latest"
    for model in tags.get("models", []):
        if model.get("name") == wanted:
            digest = str(model.get("digest", ""))
            if not DIGEST_RE.match(digest):
                fail(f"Ollama reported a malformed digest for {wanted}: {digest!r}")
            return digest
    available = ", ".join(sorted(m.get("name", "?") for m in tags.get("models", []))) or "none"
    fail(
        f"model {configured!r} (looked for {wanted!r}) is not pulled on {base_url}. "
        f"Run: ollama pull {configured}  (available: {available})"
    )
    raise AssertionError("unreachable")


def main() -> None:
    env = {**load_dotenv(ROOT / ".env"), **os.environ}
    base_url = env.get("OLLAMA_HOST_URL") or "http://localhost:11434"
    embed_model = env.get("OLLAMA_EMBED_MODEL") or "bge-m3"
    llm_model = env.get("OLLAMA_LLM_MODEL") or "llama3.1:8b"
    reranker_model = env.get("RERANKER_MODEL") or "BAAI/bge-reranker-v2-m3"
    hf_endpoint = (env.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")

    tags = get_json(f"{base_url.rstrip('/')}/api/tags", "the Ollama model inventory")
    ollama = {
        name: {"digest": resolve_ollama(tags, name, base_url)} for name in (embed_model, llm_model)
    }

    if reranker_model == "fake":
        revision = "fake"
    else:
        info = get_json(f"{hf_endpoint}/api/models/{reranker_model}", "the reranker model info")
        revision = str(info.get("sha", ""))
        if not re.match(r"^[0-9a-f]{40}$", revision):
            fail(f"Hugging Face returned no commit sha for {reranker_model!r}: {revision!r}")

    lock = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: UP017
        "ollama": ollama,
        "reranker": {"model": reranker_model, "revision": revision},
    }
    print(json.dumps(lock, indent=2))


if __name__ == "__main__":
    main()
