"""Configuration sourced exclusively from the reranker environment."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for one single-process reranker instance."""

    model_config = SettingsConfigDict(env_prefix="RERANKER_", extra="ignore")

    model: str = "BAAI/bge-reranker-v2-m3"
    model_revision: str = "main"
    max_length: PositiveInt = 512
    batch_size: PositiveInt = 16
    max_passages: PositiveInt = 100
    device: Literal["auto", "mps", "cuda", "cpu"] = "auto"
    torch_threads: int = Field(default=0, ge=0)
    max_concurrency: PositiveInt = 1
    port: PositiveInt = Field(default=8080, validation_alias="PORT")


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide immutable settings object."""

    return Settings()
