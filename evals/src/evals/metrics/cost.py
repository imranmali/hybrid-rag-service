"""Cost estimate (design D7). Judge tokens are excluded: they are evaluation overhead."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding_per_1m: float = 0.0
    llm_prompt_per_1m: float = 0.0
    llm_completion_per_1m: float = 0.0
    rerank_per_1k: float = 0.0


def load_pricing(path: Path) -> Pricing:
    return Pricing.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def query_cost(usage: Mapping[str, int | float], pricing: Pricing) -> float:
    return (
        usage.get("embed_prompt_tokens", 0) * pricing.embedding_per_1m / 1e6
        + usage.get("llm_prompt_tokens", 0) * pricing.llm_prompt_per_1m / 1e6
        + usage.get("llm_completion_tokens", 0) * pricing.llm_completion_per_1m / 1e6
        + usage.get("rerank_pairs", 0) * pricing.rerank_per_1k / 1e3
    )


def tokens_per_query(usage: Mapping[str, int | float]) -> int:
    return int(
        usage.get("embed_prompt_tokens", 0)
        + usage.get("llm_prompt_tokens", 0)
        + usage.get("llm_completion_tokens", 0)
    )


def cost_summary(usages: Sequence[Mapping[str, int | float]], pricing: Pricing) -> dict:
    n = len(usages)
    if n == 0:
        return {"cost_per_query": 0.0, "tokens_per_query": 0.0, "rerank_pairs_per_query": 0.0}
    return {
        "cost_per_query": sum(query_cost(u, pricing) for u in usages) / n,
        "tokens_per_query": sum(tokens_per_query(u) for u in usages) / n,
        "rerank_pairs_per_query": sum(u.get("rerank_pairs", 0) for u in usages) / n,
    }
