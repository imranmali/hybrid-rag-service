"""Ollama chat + model inventory (contract C3) and the optional Anthropic provider.

Everything defaults to temperature 0 and seed 42. Nothing here ever requires an API key unless
`provider="anthropic"` is explicitly selected."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from evals.settings import Settings, get_settings

SEED = 42
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    """The model server was unreachable or answered with an error."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    prompt_tokens: int
    completion_tokens: int

    def json(self) -> dict:
        """Parse the content as a JSON object; raises ValueError when malformed."""
        data = json.loads(self.content)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data


class LLM(Protocol):
    provider: str
    model: str

    def chat(
        self,
        system: str,
        user: str,
        *,
        num_predict: int,
        json_format: bool = True,
        seed: int = SEED,
    ) -> ChatResult: ...


class OllamaLLM:
    provider = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str,
        *,
        keep_alive: str = "30m",
        timeout_s: float = 300.0,
        client: httpx.Client | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        self._client = client or httpx.Client(timeout=timeout_s)

    def chat(
        self,
        system: str,
        user: str,
        *,
        num_predict: int,
        json_format: bool = True,
        seed: int = SEED,
    ) -> ChatResult:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0,
                "seed": seed,
                "num_ctx": 8192,
                "num_predict": num_predict,
            },
        }
        if json_format:
            body["format"] = "json"
        try:
            resp = self._client.post(f"{self.base_url}/api/chat", json=body)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"ollama chat failed: {exc}") from exc
        return ChatResult(
            content=data["message"]["content"],
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            completion_tokens=int(data.get("eval_count", 0)),
        )


class AnthropicLLM:
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        timeout_s: float = 120.0,
        client: httpx.Client | None = None,
    ):
        if not api_key or not model:
            raise LLMError("anthropic provider needs ANTHROPIC_API_KEY and a model id")
        self.model = model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout_s)

    def chat(
        self,
        system: str,
        user: str,
        *,
        num_predict: int,
        json_format: bool = True,
        seed: int = SEED,  # the Messages API has no seed; temperature 0 is the best we can do
    ) -> ChatResult:
        if json_format:
            system += "\nRespond with a single JSON object and nothing else."
        body = {
            "model": self.model,
            "max_tokens": num_predict,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}
        try:
            resp = self._client.post(ANTHROPIC_URL, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"anthropic request failed: {exc}") from exc
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        usage = data.get("usage", {})
        return ChatResult(
            text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        )


def make_llm(provider: str, model: str | None = None, settings: Settings | None = None) -> LLM:
    settings = settings or get_settings()
    if provider == "anthropic":
        return AnthropicLLM(model or settings.anthropic_model, settings.anthropic_api_key)
    return OllamaLLM(
        model or settings.ollama_llm_model,
        settings.ollama_host_url,
        keep_alive=settings.ollama_keep_alive,
    )


def default_generator_llm(settings: Settings | None = None) -> LLM:
    """LLM for dataset generation / precheck: `LLM_PROVIDER` decides (design D3)."""
    settings = settings or get_settings()
    return make_llm(settings.llm_provider, None, settings)


def fetch_digests(base_url: str, *, client: httpx.Client | None = None) -> dict[str, str]:
    """`GET /api/tags` -> {model name: digest}. A tagless alias is added for `<name>:latest`."""
    http = client or httpx.Client(timeout=10.0)
    try:
        resp = http.get(f"{base_url.rstrip('/')}/api/tags")
        resp.raise_for_status()
        models = resp.json().get("models", [])
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMError(f"cannot read {base_url}/api/tags: {exc}") from exc
    digests: dict[str, str] = {}
    for m in models:
        name, digest = m["name"], m["digest"]
        digests[name] = digest
        if name.endswith(":latest"):
            digests[name.removesuffix(":latest")] = digest
    return digests
