"""Indexer configuration loaded from the repository .env file and environment."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime settings for host-side indexing commands."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    indexer_database_url: str
    ollama_host_url: str = "http://localhost:11434"
    ollama_embed_model: str = "bge-m3"
    ollama_llm_model: str = "llama3.1:8b"
    ollama_keep_alive: str = "30m"
    llm_seed: int = 42
    llm_provider: Literal["ollama", "anthropic"] = "ollama"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    arxiv_contact: str | None = None

    @model_validator(mode="after")
    def validate_anthropic_settings(self) -> "Settings":
        if self.llm_provider == "anthropic" and (
            self.anthropic_api_key is None or not self.anthropic_api_key.get_secret_value()
        ):
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.llm_provider == "anthropic" and not self.anthropic_model:
            raise ValueError("ANTHROPIC_MODEL is required when LLM_PROVIDER=anthropic")
        return self
