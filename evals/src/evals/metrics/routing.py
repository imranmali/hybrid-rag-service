"""Routing metrics (design D7): route accuracy, 2x2 confusion matrix, SQL answer accuracy."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

ROUTES = ("sql", "vector")


def confusion_matrix(pairs: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """`pairs` = (expected_route, actual_route); result[expected][actual]."""
    matrix = {e: {a: 0 for a in ROUTES} for e in ROUTES}
    for expected, actual in pairs:
        matrix[expected][actual] += 1
    return matrix


def route_accuracy(pairs: Sequence[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    return sum(1 for e, a in pairs if e == a) / len(pairs)


_NUM = re.compile(r"^[+-]?\d+(\.\d+)?$")


def _norm(value: Any) -> str:
    text = " ".join(str(value).split()).lower()
    if _NUM.match(text):
        number = float(text)
        return str(int(number)) if number == int(number) else repr(number)
    return text


def _expected(expected: str) -> tuple[str, set[str] | str]:
    try:
        parsed = json.loads(expected)
    except (json.JSONDecodeError, TypeError):
        return "scalar", _norm(expected)
    if isinstance(parsed, list):
        return "list", {_norm(v) for v in parsed}
    return "scalar", _norm(parsed)


def sql_answer_correct(response: dict[str, Any], expected: str) -> bool:
    """Correct iff the response took the SQL route and its rows match `expected`:
    scalar -> some returned cell equals it; list -> first-column values form the same set."""
    if response.get("route") != "sql":
        return False
    sql = response.get("sql") or {}
    rows = sql.get("rows") or []
    if sql.get("error") or not rows:
        return False
    kind, target = _expected(expected)
    if kind == "list":
        return {_norm(r[0]) for r in rows if r} == target
    return any(_norm(cell) == target for row in rows for cell in row)


def sql_accuracy(results: Sequence[bool]) -> float | None:
    return (sum(results) / len(results)) if results else None
