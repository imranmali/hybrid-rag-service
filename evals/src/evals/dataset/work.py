"""Paths and JSONL helpers for the committed `evals/dataset/work/` provenance files."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evals.settings import EVALS_DIR

DATASET_DIR = EVALS_DIR / "dataset"
WORK_DIR = DATASET_DIR / "work"
SAMPLES = WORK_DIR / "samples.jsonl"
CANDIDATES = WORK_DIR / "candidates.jsonl"
PRECHECK = WORK_DIR / "precheck.jsonl"
FILTER_CHECKS = WORK_DIR / "filter_checks.jsonl"
VERIFICATION = WORK_DIR / "verification.jsonl"
FROZEN = DATASET_DIR / "questions.v1.jsonl"
MANIFEST = DATASET_DIR / "MANIFEST.json"
CARD = DATASET_DIR / "DATASET_CARD.md"
HUMAN_LABELS_TEMPLATE = DATASET_DIR / "human_labels.template.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
