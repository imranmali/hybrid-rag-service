"""Provider abstraction for document-level context summaries."""

from collections.abc import Sequence

import httpx

from indexer.config import Settings


class LlmClient:
    """Send the C3 chat request to Ollama or an explicitly configured Anthropic client."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=120.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(self, system: str, user: str, *, num_predict: int) -> str:
        if self.settings.llm_provider == "anthropic":
            return self._complete_anthropic(system, user, num_predict=num_predict)
        response = self._client.post(
            f"{self.settings.ollama_host_url.rstrip('/')}/api/chat",
            json={
                "model": self.settings.ollama_llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "keep_alive": self.settings.ollama_keep_alive,
                "options": {
                    "temperature": 0,
                    "seed": self.settings.llm_seed,
                    "num_ctx": 8192,
                    "num_predict": num_predict,
                },
            },
        )
        response.raise_for_status()
        return str(response.json()["message"]["content"])

    def _complete_anthropic(self, system: str, user: str, *, num_predict: int) -> str:
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - configuration-specific dependency
            message = "Install indexer[anthropic] to use LLM_PROVIDER=anthropic"
            raise RuntimeError(message) from error
        key = self.settings.anthropic_api_key
        if key is None:  # Settings validates this; retain a local guard for type narrowing.
            raise RuntimeError("ANTHROPIC_API_KEY is required")
        response = anthropic.Anthropic(api_key=key.get_secret_value()).messages.create(
            model=self.settings.anthropic_model,
            max_tokens=num_predict,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return _anthropic_text(response.content)


def _anthropic_text(content: Sequence[object]) -> str:
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return str(text)
    raise RuntimeError("Anthropic returned no text content")
