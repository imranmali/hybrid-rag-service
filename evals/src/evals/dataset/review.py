"""Agent verification workflow (design D4): `review` prints what the verifier needs, `verdict`
appends one auditable decision per candidate to `work/verification.jsonl`."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.dataset import work
from evals.dataset.check_filters import Extractor, check_candidate
from evals.dataset.corpus import Corpus, chunk_lookup
from evals.dataset.precheck import Retriever

STATUSES = ("accept", "reject", "edit")


class VerdictError(ValueError):
    """The verdict violates a verification rule (reported to the verifier, nothing is written)."""


def latest_by_candidate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Append-only log -> the last entry per candidate."""
    return {r["candidate_id"]: r for r in rows}


def pending_candidates(
    candidates: list[dict[str, Any]],
    prechecks: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prechecked candidates that do not have a verdict yet, in candidate order."""
    prechecked = {c for c, p in latest_by_candidate(prechecks).items() if not p.get("error")}
    judged = {v["candidate_id"] for v in verdicts}
    return [
        c for c in candidates if c["candidate_id"] in prechecked and c["candidate_id"] not in judged
    ]


def _clip(text: str, max_chars: int | None, focus: str = "") -> str:
    """Clip to `max_chars`, choosing the window that overlaps most with the `focus` words (the
    question and reference answer), so the evidence for the answer stays visible."""
    if max_chars is None or len(text) <= max_chars:
        return text
    words = {w for w in re.findall(r"[a-z0-9]{3,}", focus.lower())}
    step = max(max_chars // 4, 1)
    best, best_score = 0, -1
    for start in range(0, len(text) - max_chars + step, step):
        window = text[start : start + max_chars].lower()
        score = sum(1 for w in words if w in window)
        if score > best_score:
            best, best_score = start, score
    start = min(best, len(text) - max_chars)
    head = "[...] " if start > 0 else ""
    tail = " [...]" if start + max_chars < len(text) else ""
    return head + text[start : start + max_chars] + tail


def render_review(
    cand: dict[str, Any],
    precheck: dict[str, Any] | None,
    filter_check: dict[str, Any] | None,
    corpus: Corpus,
    retriever: Retriever | None = None,
    max_chars: int | None = None,
) -> str:
    focus = f"{cand['question']} {cand['reference_answer']}"
    lines = [
        "=" * 78,
        f"{cand['candidate_id']}  type={cand['type']}  generator={cand['source']['generator']}",
        f"QUESTION: {cand['question']}",
        f"REFERENCE ANSWER: {cand['reference_answer']}",
    ]
    if precheck:
        lines.append(
            f"PRECHECK: verdict={precheck.get('verdict')} passed={precheck.get('passed')} "
            f"({precheck.get('method')})"
        )
    if cand["type"] == "filter":
        lines.append(f"FILTER PHRASE: {cand.get('filter_phrase')}")
        lines.append(f"EXPECTED FILTERS: {json.dumps(cand.get('expected_filters'))}")
        if filter_check:
            state = (
                "MATCH" if filter_check["match"] else f"MISMATCH on {filter_check['differences']}"
            )
            lines.append(f"SERVICE EXTRACTED: {json.dumps(filter_check['extracted'])} -> {state}")
        else:
            lines.append("SERVICE EXTRACTED: (not checked; run `evals dataset check-filters`)")
    if cand["type"] == "aggregate":
        sql = cand.get("sql") or {}
        lines += [f"SQL: {sql.get('query')}", f"PARAMS: {json.dumps(sql.get('params'))}"]
    if cand["gold_chunks"]:
        keys = [(g["external_id"], g["ordinal"]) for g in cand["gold_chunks"]]
        lookup = chunk_lookup(corpus, keys)
        for k in keys:
            c = lookup.get(k)
            lines.append(
                f"--- gold {k[0]}#{k[1]}" + (f" [{c.section_path}]" if c else " (MISSING)")
            )
            lines.append(_clip(c.content, max_chars, focus) if c else "")
    if cand["type"] == "unanswerable" and retriever is not None:
        for i, chunk in enumerate(retriever(cand["question"]), start=1):
            lines.append(f"--- service top-{i}: {chunk.get('external_id')}#{chunk.get('ordinal')}")
            lines.append(_clip(chunk.get("content", ""), max_chars, focus))
    return "\n".join(lines)


def record_verdict(
    *,
    candidate_id: str,
    status: str,
    by: str,
    notes: str,
    question: str | None = None,
    answer: str | None = None,
    candidates_path: Path = work.CANDIDATES,
    precheck_path: Path = work.PRECHECK,
    filter_checks_path: Path = work.FILTER_CHECKS,
    verification_path: Path = work.VERIFICATION,
    extractor: Extractor | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    if status not in STATUSES:
        raise VerdictError(f"status must be one of {STATUSES}")
    if not by.strip():
        raise VerdictError("verifier identity (--by) is required, e.g. agent:<model id>")
    if status != "accept" and not notes.strip():
        raise VerdictError(f"{status} needs --notes explaining why")
    cand = next(
        (c for c in work.read_jsonl(candidates_path) if c["candidate_id"] == candidate_id), None
    )
    if cand is None:
        raise VerdictError(f"unknown candidate {candidate_id}")
    precheck = latest_by_candidate(work.read_jsonl(precheck_path)).get(candidate_id)
    if precheck is None:
        raise VerdictError(
            f"{candidate_id} has no precheck entry; run `evals dataset precheck` first"
        )
    if status in ("accept", "edit") and not precheck.get("passed"):
        raise VerdictError(f"{candidate_id} failed the automated precheck; record a reject instead")
    if status == "edit" and not (question or answer):
        raise VerdictError("edit needs --question and/or --answer")
    if status != "edit" and (question or answer):
        raise VerdictError("--question/--answer are only valid with --status edit")

    final_question = question or cand["question"]
    if status == "edit":
        notes = (
            f"{notes.strip()} | original question: {cand['question']} | "
            f"original answer: {cand['reference_answer']}"
        )
    if cand["type"] == "filter" and status in ("accept", "edit"):
        checks = [
            c for c in work.read_jsonl(filter_checks_path) if c["candidate_id"] == candidate_id
        ]
        if question and question != cand["question"]:
            if extractor is None:
                raise VerdictError(
                    "editing a filter question needs the live service to re-check its filters"
                )
            check = check_candidate(cand, extractor, question)
            work.append_jsonl(filter_checks_path, check)
        else:
            check = next((c for c in reversed(checks) if c["question"] == final_question), None)
        if check is None:
            raise VerdictError(
                f"{candidate_id}: run `evals dataset check-filters` before accepting a filter "
                "question"
            )
        if not check["match"]:
            raise VerdictError(
                f"{candidate_id}: the service extracts different filters ({check['differences']}); "
                "edit the question until they match"
            )
    entry = {
        "candidate_id": candidate_id,
        "status": status,
        "by": by.strip(),
        "notes": notes.strip(),
        "question": question,
        "reference_answer": answer,
        "at": now().isoformat(),
    }
    work.append_jsonl(verification_path, entry)
    return entry
