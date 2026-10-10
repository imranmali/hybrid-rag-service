"""CI gate (eval-ci-gate spec): compare a grid's run records with the thresholds file.

Exit codes: 0 pass, 1 metric below threshold, 2 degraded response or failed request,
3 setup error (raised before this module is reached, or a config missing from the run)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evals.thresholds import metric_value

RERECORD_HINT = (
    "A degraded response or failed request in replay mode usually means a cassette miss: "
    "re-record the cassettes with `make eval-record-ci`."
)


@dataclass
class GateRow:
    config: str
    metric: str
    value: float | None
    threshold: float
    passed: bool


@dataclass
class GateResult:
    exit_code: int = 0
    rows: list[GateRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def evaluate(
    thresholds: dict[str, Any], records: dict[str, dict[str, Any]], *, replay: bool = False
) -> GateResult:
    result = GateResult()
    max_degraded = int(thresholds.get("max_degraded", 0))
    missing = degraded_or_failed = below = False
    for name, mins in thresholds["configs"].items():
        rec = records.get(name)
        if rec is None:
            result.problems.append(f"{name}: no run record in this grid")
            missing = True
            continue
        degraded, errors = rec["degraded"]["count"], rec["errors"]["count"]
        if degraded > max_degraded:
            result.problems.append(f"{name}: {degraded} degraded response(s) (max {max_degraded})")
            degraded_or_failed = True
        if errors:
            result.problems.append(f"{name}: {errors} failed request(s)")
            degraded_or_failed = True
        for metric, minimum in mins.items():
            value = metric_value(rec, metric)
            passed = value is not None and value >= minimum
            below = below or not passed
            result.rows.append(GateRow(name, metric, value, minimum, passed))
    result.exit_code = 3 if missing else 2 if degraded_or_failed else 1 if below else 0
    if result.exit_code == 2 and replay:
        result.problems.append(RERECORD_HINT)
    return result


def format_table(result: GateResult) -> str:
    lines = [f"{'config':<15} {'metric':<15} {'value':>8} {'threshold':>10}  status"]
    for r in result.rows:
        value = "n/a" if r.value is None else f"{r.value:.3f}"
        lines.append(
            f"{r.config:<15} {r.metric:<15} {value:>8} {r.threshold:>10.3f}  "
            + ("ok" if r.passed else "FAIL  <-- below threshold")
        )
    lines += [f"! {p}" for p in result.problems]
    lines.append(f"gate exit code: {result.exit_code}")
    return "\n".join(lines)
