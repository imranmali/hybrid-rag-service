"""Latency percentiles (design D7): linear-interpolation p50/p95."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import numpy as np


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q, method="linear"))


def summarize(values: Sequence[float]) -> dict[str, float | int | None]:
    return {"p50": percentile(values, 50), "p95": percentile(values, 95), "n": len(values)}


def latency_summary(
    e2e_ms: Sequence[float], timings: Iterable[Mapping[str, float]]
) -> dict[str, dict]:
    """p50/p95 of client e2e, server `total` and every stage key present in `timings_ms`
    (over the responses that contain it)."""
    per_stage: dict[str, list[float]] = {}
    for t in timings:
        for key, value in t.items():
            per_stage.setdefault(key, []).append(float(value))
    stages = {k: summarize(v) for k, v in sorted(per_stage.items()) if k != "total"}
    return {
        "e2e": summarize(list(e2e_ms)),
        "server_total": summarize(per_stage.get("total", [])),
        "stages": stages,
    }
