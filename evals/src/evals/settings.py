"""Environment-driven settings; loads ../.env when present."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

EVALS_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = EVALS_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), extra="ignore", case_sensitive=False
    )

    rag_service_url: str = "http://localhost:8080"
    evals_database_url: str = "postgresql://rag_reader:rag_reader_dev@localhost:5433/rag"
    ollama_host_url: str = "http://localhost:11434"
    ollama_llm_model: str = "llama3.1:8b"
    ollama_keep_alive: str = "30m"
    llm_seed: int = 42
    llm_provider: str = "ollama"
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    wiremock_admin_url: str = "http://localhost:8089"


def get_settings() -> Settings:
    return Settings()
