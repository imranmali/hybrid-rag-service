"""Retrieval-only calls to the live service used while building the dataset: the top chunks for
an unanswerable candidate, and the filters the service extracts from a filter question."""

from __future__ import annotations

from typing import Any

from evals.client import ServiceClient


def probe_pipeline(*, filters: bool) -> dict[str, Any]:
    """Hybrid retrieval with generation, rerank and routing off (complete C6 pipeline object)."""
    return {
        "retrieval": {
            "vector": True,
            "bm25": True,
            "vector_k": 50,
            "bm25_k": 50,
            "fusion": {"method": "rrf", "k": 60},
            "return_k": 50,
        },
        "contextual": True,
        "filters": {
            "enabled": filters,
            "extract_from_question": True,
            "strategy": "auto",
            "selectivity_threshold": 0.25,
            "overfetch_factor": 5,
            "explicit": None,
        },
        "rerank": {"enabled": False, "candidates": 20},
        "routing": {"enabled": False},
        "generation": {"enabled": False, "context_k": 6},
    }


def top_chunks(client: ServiceClient, question: str, k: int = 5) -> list[dict[str, Any]]:
    res = client.query(question, probe_pipeline(filters=False))
    if not res.ok:
        raise RuntimeError(f"service query failed: {res.error}")
    return res.body["chunks"][:k]


def extracted_filters(client: ServiceClient, question: str) -> dict[str, Any]:
    res = client.query(question, probe_pipeline(filters=True))
    if not res.ok:
        raise RuntimeError(f"service query failed: {res.error}")
    return (res.body.get("filters") or {}).get("extracted") or {}
