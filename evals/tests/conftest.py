from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.dataset.schema import CI_QUOTA, QUOTA

EVALS_DIR = Path(__file__).resolve().parents[1]
CONFIGS_DIR = EVALS_DIR / "configs"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_record(
    qid: str,
    qtype: str = "factoid",
    *,
    ci: bool = False,
    gold: list[tuple[str, int]] | None = None,
    **overrides,
) -> dict:
    """A valid C8 record for `qtype` (gold chunks default to a rule-satisfying set)."""
    default_gold = {
        "factoid": [("2401.00001", 0)],
        "multi_chunk": [("2401.00001", 0), ("2401.00001", 3)],
        "filter": [("2401.00002", 1)],
        "aggregate": [],
        "unanswerable": [],
    }[qtype]
    gold = default_gold if gold is None else gold
    rec = {
        "id": qid,
        "type": qtype,
        "question": f"Question {qid}?",
        "reference_answer": "An answer.",
        "gold_chunks": [
            {"external_id": e, "ordinal": o, "content_sha256": sha(f"{e}#{o}")} for e, o in gold
        ],
        "expected_route": "sql" if qtype == "aggregate" else "vector",
        "expected_filters": {"published_from": "2024-01-01", "published_to": "2024-12-31"}
        if qtype == "filter"
        else None,
        "expected_sql_answer": "3" if qtype == "aggregate" else None,
        "source": {"generator": "ollama:llama3.1:8b", "seed": 42, "sampled_chunks": []},
        "verification": {
            "status": "verified",
            "by": "agent:test",
            "human_reviewed": False,
            "notes": "",
        },
        "ci_subset": ci,
    }
    rec.update(overrides)
    return rec


def full_dataset_records() -> list[dict]:
    """150 valid records with the exact mix and CI split."""
    out, n = [], 0
    for qtype, count in QUOTA.items():
        for i in range(count):
            n += 1
            out.append(make_record(f"q{n:04d}", qtype, ci=i < CI_QUOTA[qtype]))
    return out


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def tmp_dataset(tmp_path: Path):
    def _make(rows: list[dict]) -> Path:
        return write_jsonl(tmp_path / "questions.jsonl", rows)

    return _make
