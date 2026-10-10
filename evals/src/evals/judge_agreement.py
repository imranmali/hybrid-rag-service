"""Judge vs human agreement on the labelled subset (BUILD_SPEC judge-agreement check)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from evals.dataset import work
from evals.report import load_eligible_runs

LABELS = work.DATASET_DIR / "human_labels.jsonl"


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(ca[k] * cb[k] for k in ca) / (n * n)
    if expected == 1:
        return 1.0 if observed == 1 else 0.0
    return (observed - expected) / (1 - expected)


def agreement(labels: list[dict[str, Any]], evals_dir: Path) -> dict[str, Any]:
    runs = {rec["config"]["name"]: rec for _, rec in load_eligible_runs(evals_dir)}
    out: dict[str, Any] = {}
    for key, judge_key in (("faithfulness", "faithfulness"), ("answer_relevance", "relevance")):
        pairs, skipped = [], 0
        for row in labels:
            human = row.get(key)
            rec = runs.get(row.get("config", ""))
            if human is None or rec is None:
                skipped += 1
                continue
            q = next((q for q in rec["questions"] if q["id"] == row["id"]), None)
            verdict = ((q or {}).get("judge") or {}).get(judge_key)
            if not verdict or verdict.get("label") is None:
                skipped += 1
                continue
            pairs.append((str(human), verdict["label"]))
        out[key] = {
            "n": len(pairs),
            "skipped": skipped,
            "agreement": sum(1 for a, b in pairs if a == b) / len(pairs) if pairs else None,
            "kappa": cohens_kappa(pairs),
        }
    return out


def report(labels_path: Path = LABELS, evals_dir: Path = work.DATASET_DIR.parent) -> str:
    if not labels_path.exists():
        return f"pending: no {labels_path.parent.name}/{labels_path.name}"
    labels = work.read_jsonl(labels_path)
    result = agreement(labels, evals_dir)
    lines = [f"judge agreement vs {len(labels)} human labels"]
    for key, r in result.items():
        a = "n/a" if r["agreement"] is None else f"{r['agreement']:.3f}"
        k = "n/a" if r["kappa"] is None else f"{r['kappa']:.3f}"
        lines.append(f"  {key}: n={r['n']} (skipped {r['skipped']}) agreement={a} kappa={k}")
    return "\n".join(lines)
