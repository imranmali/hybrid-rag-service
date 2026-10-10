"""Freeze verified candidates into `questions.<version>.jsonl` (design D5)."""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.dataset import work
from evals.dataset.review import latest_by_candidate
from evals.dataset.schema import CI_QUOTA, QUOTA, sha256_file
from evals.drift import GoldStore

TYPE_ORDER = ("factoid", "multi_chunk", "filter", "aggregate", "unanswerable")
HUMAN_LABEL_COUNT = 30


class FreezeError(Exception):
    """The candidates cannot be frozen as requested (CLI exit code 3)."""


@dataclass
class FreezeInputs:
    candidates: list[dict[str, Any]]
    prechecks: list[dict[str, Any]]
    verdicts: list[dict[str, Any]]
    generator_digest: str | None
    pipeline_fingerprint: str


def eligible_candidates(inp: FreezeInputs) -> dict[str, list[dict[str, Any]]]:
    """Accepted/edited candidates (final text applied) whose precheck passed, grouped by type."""
    verdicts = latest_by_candidate(inp.verdicts)
    prechecks = latest_by_candidate(inp.prechecks)
    pools: dict[str, list[dict[str, Any]]] = {t: [] for t in TYPE_ORDER}
    for cand in sorted(inp.candidates, key=lambda c: c["candidate_id"]):
        v = verdicts.get(cand["candidate_id"])
        p = prechecks.get(cand["candidate_id"])
        if v is None or v["status"] not in ("accept", "edit") or not (p and p.get("passed")):
            continue  # no verdict, rejected or failed precheck: never frozen
        pools[cand["type"]].append(
            {
                **cand,
                "question": v.get("question") or cand["question"],
                "reference_answer": v.get("reference_answer") or cand["reference_answer"],
                "_verdict": v,
            }
        )
    return pools


def select_and_build(
    inp: FreezeInputs,
    store: GoldStore,
    *,
    seed: int,
    quota: dict[str, int] = QUOTA,
    ci_quota: dict[str, int] = CI_QUOTA,
) -> list[dict[str, Any]]:
    pools = eligible_candidates(inp)
    short = {t: (len(pools[t]), n) for t, n in quota.items() if len(pools[t]) < n}
    if short:
        detail = ", ".join(f"{t}: {have} accepted of {need}" for t, (have, need) in short.items())
        hints = "; ".join(f"`evals dataset generate --type {t}`" for t in short)
        raise FreezeError(
            f"not enough accepted candidates ({detail}). "
            f"Top up with {hints}, then precheck and review."
        )

    chosen: dict[str, list[dict[str, Any]]] = {}
    for t in TYPE_ORDER:
        pool = list(pools[t])
        random.Random(f"{seed}:freeze:{t}").shuffle(pool)
        chosen[t] = pool[: quota[t]]

    keys = sorted(
        {
            (g["external_id"], g["ordinal"])
            for t in TYPE_ORDER
            for c in chosen[t]
            for g in c["gold_chunks"]
        }
    )
    db = store.content_hashes(keys)
    ci_ids: set[str] = set()
    for t in TYPE_ORDER:
        picked = list(chosen[t])
        random.Random(f"{seed}:ci:{t}").shuffle(picked)
        ci_ids |= {c["candidate_id"] for c in picked[: ci_quota[t]]}

    records: list[dict[str, Any]] = []
    n = 0
    for t in TYPE_ORDER:
        for cand in chosen[t]:
            n += 1
            gold = []
            for g in cand["gold_chunks"]:
                sha = db.get((g["external_id"], g["ordinal"]))
                if sha is None:
                    raise FreezeError(
                        f"{cand['candidate_id']}: gold chunk {g['external_id']}#{g['ordinal']} "
                        "not in the database"
                    )
                if sha != g["content_sha256"]:
                    raise FreezeError(
                        f"{cand['candidate_id']}: gold chunk {g['external_id']}#{g['ordinal']} "
                        "changed since generation; regenerate"
                    )
                gold.append(
                    {
                        "external_id": g["external_id"],
                        "ordinal": g["ordinal"],
                        "content_sha256": sha,
                    }
                )
            v = cand["_verdict"]
            records.append(
                {
                    "id": f"q{n:04d}",
                    "type": t,
                    "question": cand["question"],
                    "reference_answer": cand["reference_answer"],
                    "gold_chunks": gold,
                    "expected_route": "sql" if t == "aggregate" else "vector",
                    "expected_filters": cand["expected_filters"] if t == "filter" else None,
                    "expected_sql_answer": cand["expected_sql_answer"]
                    if t == "aggregate"
                    else None,
                    "source": cand["source"],
                    "verification": {
                        "status": "verified",
                        "by": v["by"],
                        "human_reviewed": False,
                        "notes": (v.get("notes") or "")
                        + (f" [{v['status']}]" if v["status"] == "edit" else ""),
                    },
                    "ci_subset": cand["candidate_id"] in ci_ids,
                }
            )
    return records


def render_card(
    manifest: dict[str, Any], generator: str, verifiers: list[str], precheck_model: str | None
) -> str:
    counts = manifest["counts"]
    return (
        f"""# Dataset card: questions.{manifest["version"]}

Frozen evaluation set for the Hybrid RAG Service. {sum(counts.values())} questions over the indexed
arXiv corpus. sha256 `{manifest["sha256"]}`.

## Verification status: human review is pending

Candidates were **LLM-generated** and **agent-verified** against the source chunks. No human has
reviewed them yet; every record has `human_reviewed: false`. Treat the numbers in
`evals/results.md` as measured against an agent-verified, not human-verified, set.

## Mix

| Type | Count | CI subset |
|---|---|---|
"""
        + "".join(f"| {t} | {counts[t]} | {manifest['ci_counts'][t]} |\n" for t in TYPE_ORDER)
        + f"""
## How it was built

1. **Sampling.** Seeded (seed {manifest["seed"]}) samples of indexed chunks, 2x oversampled:
   factoid from one chunk, multi-chunk from 2-4 chunks of one paper in different sections, filter
   questions from a metadata phrase (year, author or category + year), unanswerable from topic
   seeds only.
2. **Generation.** Generator model `{generator}`
   (digest `{manifest.get("generator_digest") or "n/a"}`),
   temperature 0, seed {manifest["seed"]}, versioned prompts `qgen-*-v1`. Aggregate questions come
   from deterministic templates; their expected answers are computed with SQL, never by the LLM.
3. **Automated precheck** (`precheck-v1`, model `{precheck_model or generator}`): it must judge
   that the gold passages fully answer the question with the reference answer (for unanswerable
   questions, that the service's top-5 passages do not).
4. **Agent verification.** The verifying agent(s) {", ".join(f"`{v}`" for v in verifiers)}
   read every
   candidate against its gold chunk text, recorded an accept / reject / edit verdict with notes in
   `evals/dataset/work/verification.jsonl`, and confirmed each filter question's expected filters
   against what the service extracts. Only accepted or edited candidates were frozen.
5. **Freeze.** Seeded selection to the quota, ids `q0001`... in type order, `content_sha256`
   copied from the database. Index pipeline fingerprint: `{manifest["index_pipeline_fingerprint"]}`.

## Known biases and limitations

- The generator, the precheck and the answer judge are all the same model family (llama3.1:8b by
  default), so self-preference bias is possible; judge agreement against human labels is tooled
  (`evals judge-agreement`, `human_labels.template.jsonl`) but pending.
- Questions are written from single chunks or small chunk groups, which favours retrieval of that
  chunk; real user questions are messier.
- Author names are matched by normalized name, so homonymous authors can be conflated.
- Small n per type (8 unanswerable, 15 aggregate): differences of a few points are not significant.

## Regenerating

Gold chunks are keyed by `(external_id, ordinal)` with `content_sha256`, so the dataset survives a
re-index of the same manifest and configuration (the runner checks the hashes). To rebuild from
scratch: `evals dataset sample`, `generate`, `precheck`, `check-filters`, review every candidate,
then `freeze`. A new dataset is a new version file; `questions.{manifest["version"]}.jsonl`
is never edited.
"""
    )


def freeze(
    inp: FreezeInputs,
    store: GoldStore,
    *,
    version: str = "v1",
    seed: int = 42,
    dataset_dir: Path = work.DATASET_DIR,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    quota: dict[str, int] = QUOTA,
    ci_quota: dict[str, int] = CI_QUOTA,
) -> dict[str, Any]:
    out = dataset_dir / f"questions.{version}.jsonl"
    if out.exists():
        raise FreezeError(
            f"{out.name} already exists; a frozen version is never edited, pick a new --version"
        )
    records = select_and_build(inp, store, seed=seed, quota=quota, ci_quota=ci_quota)

    dataset_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )

    counts = Counter(r["type"] for r in records)
    ci_counts = Counter(r["type"] for r in records if r["ci_subset"])
    generators = Counter(
        r["source"]["generator"]
        for r in records
        if not r["source"]["generator"].startswith("sql-template")
    )
    generator = generators.most_common(1)[0][0] if generators else "n/a"
    verifiers = sorted({r["verification"]["by"] for r in records})
    manifest = {
        "version": version,
        "created_at": now().isoformat(),
        "sha256": sha256_file(out),
        "counts": {t: counts[t] for t in TYPE_ORDER},
        "ci_counts": {t: ci_counts[t] for t in TYPE_ORDER},
        "seed": seed,
        "generator": generator,
        "generator_digest": inp.generator_digest,
        "verifier": verifiers[0] if len(verifiers) == 1 else verifiers,
        "index_pipeline_fingerprint": inp.pipeline_fingerprint,
    }
    (dataset_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    precheck_models = {p.get("model") for p in inp.prechecks if p.get("model")}
    (dataset_dir / "DATASET_CARD.md").write_text(
        render_card(manifest, generator, verifiers, next(iter(precheck_models), None)),
        encoding="utf-8",
    )

    answerable = [r["id"] for r in records if r["type"] in ("factoid", "multi_chunk", "filter")]
    labelled = random.Random(f"{seed}:human-labels").sample(
        answerable, min(HUMAN_LABEL_COUNT, len(answerable))
    )
    template = [
        {"id": qid, "config": "06-routing", "faithfulness": None, "answer_relevance": None}
        for qid in sorted(labelled)
    ]
    work.write_jsonl(dataset_dir / "human_labels.template.jsonl", template)
    return manifest
