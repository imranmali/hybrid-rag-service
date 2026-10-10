"""Dataset record schema and validation (contract C8)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from evals.config_schema import QUESTION_TYPES

QUOTA: dict[str, int] = {
    "factoid": 60,
    "multi_chunk": 37,
    "filter": 30,
    "aggregate": 15,
    "unanswerable": 8,
}
CI_QUOTA: dict[str, int] = {
    "factoid": 20,
    "multi_chunk": 12,
    "filter": 10,
    "aggregate": 5,
    "unanswerable": 3,
}
TOTAL = sum(QUOTA.values())
CI_TOTAL = sum(CI_QUOTA.values())

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^q\d{4}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldChunk(_Strict):
    external_id: str
    ordinal: int = Field(ge=0)
    content_sha256: str

    @field_validator("content_sha256")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _SHA256.match(v):
            raise ValueError("content_sha256 must be 64 lowercase hex chars")
        return v

    @property
    def key(self) -> tuple[str, int]:
        return (self.external_id, self.ordinal)


class SampledChunk(_Strict):
    external_id: str
    ordinal: int


class ExpectedFilters(_Strict):
    categories: list[str] | None = None
    authors: list[str] | None = None
    published_from: str | None = None
    published_to: str | None = None


class Source(_Strict):
    generator: str
    seed: int
    sampled_chunks: list[SampledChunk] = Field(default_factory=list)


class Verification(_Strict):
    status: Literal["verified"]
    by: str
    human_reviewed: bool
    notes: str = ""


class Record(_Strict):
    id: str
    type: Literal["factoid", "multi_chunk", "filter", "aggregate", "unanswerable"]
    question: str = Field(min_length=1)
    reference_answer: str
    gold_chunks: list[GoldChunk]
    expected_route: Literal["sql", "vector"]
    expected_filters: ExpectedFilters | None = None
    expected_sql_answer: str | None = None
    source: Source
    verification: Verification
    ci_subset: bool

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("id must look like q0042")
        return v


def rule_violations(rec: Record) -> list[str]:
    """Contract C8 rules that a field-level schema cannot express."""
    out: list[str] = []
    n = len(rec.gold_chunks)
    if rec.type in ("factoid", "filter") and n < 1:
        out.append(f"{rec.type} needs at least 1 gold chunk")
    if rec.type == "multi_chunk" and not 2 <= n <= 4:
        out.append(f"multi_chunk needs 2-4 gold chunks, has {n}")
    if rec.type in ("aggregate", "unanswerable") and n != 0:
        out.append(f"{rec.type} must have no gold chunks, has {n}")
    if len({g.key for g in rec.gold_chunks}) != n:
        out.append("duplicate gold chunks")
    expected_route = "sql" if rec.type == "aggregate" else "vector"
    if rec.expected_route != expected_route:
        out.append(f"{rec.type} must have expected_route {expected_route!r}")
    if rec.type == "aggregate" and not rec.expected_sql_answer:
        out.append("aggregate needs expected_sql_answer")
    if rec.type != "aggregate" and rec.expected_sql_answer is not None:
        out.append("expected_sql_answer only allowed for aggregate")
    if rec.type == "filter" and rec.expected_filters is None:
        out.append("filter needs expected_filters")
    if rec.type != "filter" and rec.expected_filters is not None:
        out.append("expected_filters only allowed for filter")
    return out


@dataclass
class ValidationReport:
    records: list[Record] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    ci_counts: dict[str, int] = field(default_factory=dict)
    sha256: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_records(path: Path) -> tuple[list[Record], list[str]]:
    """Parse a JSONL file; returns (valid records, error strings)."""
    records: list[Record] = []
    errors: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {lineno}: invalid JSON ({exc.msg})")
            continue
        label = data.get("id", f"line {lineno}") if isinstance(data, dict) else f"line {lineno}"
        try:
            rec = Record.model_validate(data)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(f"{label}: {loc}: {err['msg']}")
            continue
        for problem in rule_violations(rec):
            errors.append(f"{rec.id}: {problem}")
        records.append(rec)
    return records, errors


def load_dataset(path: Path) -> list[Record]:
    """Load a dataset for running; raises ValueError listing every problem."""
    records, errors = load_records(path)
    if errors:
        raise ValueError("invalid dataset:\n  " + "\n  ".join(errors))
    return records


def validate_dataset(
    path: Path,
    manifest_path: Path | None = None,
    *,
    quota: dict[str, int] | None = QUOTA,
    ci_quota: dict[str, int] | None = CI_QUOTA,
) -> ValidationReport:
    """Schema + rules + counts + manifest hash. Pass `quota=None` to skip the count checks."""
    report = ValidationReport()
    report.records, report.errors = load_records(path)
    report.sha256 = sha256_file(path)

    ids = Counter(r.id for r in report.records)
    report.errors += [f"{i}: duplicate id" for i, n in ids.items() if n > 1]
    report.counts = {t: 0 for t in QUESTION_TYPES}
    report.ci_counts = {t: 0 for t in QUESTION_TYPES}
    for r in report.records:
        report.counts[r.type] += 1
        if r.ci_subset:
            report.ci_counts[r.type] += 1

    if quota is not None and report.counts != quota:
        report.errors.append(f"type counts {report.counts} != required {quota}")
    if ci_quota is not None and report.ci_counts != ci_quota:
        report.errors.append(f"ci_subset counts {report.ci_counts} != required {ci_quota}")

    if manifest_path is not None:
        if not manifest_path.exists():
            report.errors.append(f"manifest not found: {manifest_path}")
        else:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("sha256") != report.sha256:
                report.errors.append(
                    f"manifest sha256 {manifest.get('sha256')} != file sha256 {report.sha256}"
                )
            if manifest.get("counts") and manifest["counts"] != report.counts:
                report.errors.append("manifest counts differ from the file's counts")
    return report
