"""Pydantic models of the eval config (contract C7). `extra="forbid"` at every level and every
`pipeline` key required, so a typo or a missing switch can never silently change an experiment."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

QUESTION_TYPES = ("factoid", "multi_chunk", "filter", "aggregate", "unanswerable")
QuestionType = Literal["factoid", "multi_chunk", "filter", "aggregate", "unanswerable"]


class ConfigError(ValueError):
    """Invalid or incomplete config; the CLI maps it to exit code 3."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fusion(_Strict):
    method: Literal["rrf"]
    k: int = Field(ge=1, le=1000)


class Retrieval(_Strict):
    vector: bool
    bm25: bool
    vector_k: int = Field(ge=1, le=200)
    bm25_k: int = Field(ge=1, le=200)
    fusion: Fusion
    return_k: int = Field(ge=1, le=200)

    @model_validator(mode="after")
    def _one_leg(self) -> Retrieval:
        if not (self.vector or self.bm25):
            raise ValueError("at least one of retrieval.vector / retrieval.bm25 must be true")
        return self


class ExplicitFilters(_Strict):
    categories: list[str] | None = None
    authors: list[str] | None = None
    published_from: str | None = None
    published_to: str | None = None


class Filters(_Strict):
    enabled: bool
    extract_from_question: bool
    strategy: Literal["auto", "pre", "post"]
    selectivity_threshold: float = Field(ge=0, le=1)
    overfetch_factor: int = Field(ge=1, le=20)
    explicit: ExplicitFilters | None  # required key, `null` allowed


class Rerank(_Strict):
    enabled: bool
    candidates: int = Field(ge=1, le=100)


class Routing(_Strict):
    enabled: bool


class Generation(_Strict):
    enabled: bool
    context_k: int = Field(ge=1, le=20)


class Pipeline(_Strict):
    retrieval: Retrieval
    contextual: bool
    filters: Filters
    rerank: Rerank
    routing: Routing
    generation: Generation

    @model_validator(mode="after")
    def _cross_checks(self) -> Pipeline:
        r = self.retrieval
        if r.vector_k * self.filters.overfetch_factor > 1000:
            raise ValueError("retrieval.vector_k x filters.overfetch_factor must be <= 1000")
        if self.rerank.candidates > r.return_k:
            raise ValueError("rerank.candidates must be <= retrieval.return_k")
        return self


class Judge(_Strict):
    enabled: bool
    provider: Literal["ollama", "anthropic"]
    model: str


class Run(_Strict):
    seed: int
    concurrency: int = Field(ge=1)
    warmup_queries: int = Field(ge=0)
    timeout_s: float = Field(gt=0)


class EvalConfig(_Strict):
    name: str
    description: str
    grid_row: int | None = Field(default=None, ge=1, le=6)
    question_types: list[QuestionType] = Field(default_factory=lambda: list(QUESTION_TYPES))
    pipeline: Pipeline
    judge: Judge
    run: Run

    def pipeline_payload(self) -> dict:
        """The `pipeline` object sent verbatim on every /v1/query request."""
        return self.pipeline.model_dump(mode="json")


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        kind = {"missing": "missing key", "extra_forbidden": "unknown key"}.get(
            err["type"], err["msg"]
        )
        lines.append(f"  {loc}: {kind}")
    return f"invalid config {path}:\n" + "\n".join(lines)


def load_config(path: str | Path) -> tuple[EvalConfig, str]:
    """Load and validate a config file; returns (config, sha256 of the file bytes)."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a YAML mapping")
    try:
        config = EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc
    if config.name != path.stem:
        raise ConfigError(f"config name {config.name!r} must equal the filename stem {path.stem!r}")
    return config, hashlib.sha256(raw).hexdigest()
