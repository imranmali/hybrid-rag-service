"""Confirms a filter candidate's expected filters against what the service extracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

FIELDS = ("categories", "authors", "published_from", "published_to")
Extractor = Callable[[str], dict[str, Any]]  # question -> `filters.extracted`


def _norm(filters: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in FIELDS:
        v = (filters or {}).get(f)
        if v in (None, [], ""):
            continue
        out[f] = sorted(v) if isinstance(v, list) else v
    return out


def compare_filters(
    expected: dict[str, Any] | None, extracted: dict[str, Any] | None
) -> dict[str, Any]:
    exp, got = _norm(expected), _norm(extracted)
    return {
        "match": exp == got,
        "expected": exp,
        "extracted": got,
        "differences": sorted(f for f in FIELDS if exp.get(f) != got.get(f)),
    }


def check_candidate(
    cand: dict[str, Any], extractor: Extractor, question: str | None = None
) -> dict[str, Any]:
    """`question` overrides the candidate's text (used when verifying an edit)."""
    q = question or cand["question"]
    result = compare_filters(cand.get("expected_filters"), extractor(q))
    return {"candidate_id": cand["candidate_id"], "question": q, **result}


def run_check_filters(
    candidates: list[dict[str, Any]], extractor: Extractor
) -> list[dict[str, Any]]:
    return [check_candidate(c, extractor) for c in candidates if c["type"] == "filter"]
