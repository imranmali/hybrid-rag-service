"""Request and response models for the public reranker contract."""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, model_validator

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class Passage(BaseModel):
    id: NonBlankString
    text: NonBlankString


class RerankRequest(BaseModel):
    query: Query
    passages: Annotated[list[Passage], Field(min_length=1, max_length=100)]
    top_k: Annotated[int | None, Field(default=None, ge=1)] = None

    @model_validator(mode="after")
    def ids_must_be_unique(self) -> "RerankRequest":
        ids = [passage.id for passage in self.passages]
        if len(ids) != len(set(ids)):
            raise ValueError("passage ids must be unique within a request")
        return self

    def resolved_top_k(self) -> int:
        """Return the requested count clamped to the submitted passage count."""

        return min(self.top_k if self.top_k is not None else len(self.passages), len(self.passages))


class RerankResult(BaseModel):
    id: str
    index: int
    score: Annotated[float, Field(ge=0.0, le=1.0)]


class RerankResponse(BaseModel):
    model: str
    results: list[RerankResult]
    latency_ms: Annotated[float, Field(ge=0.0)]


class HealthResponse(BaseModel):
    status: str
    model: str
    revision: str
    device: str
    max_length: int
    ready: bool
