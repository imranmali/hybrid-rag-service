"""Seeded chunk sampling (design D3). Every random choice goes through one `random.Random(seed)`
and every input list is sorted first, so two runs on the same database give identical samples."""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evals.dataset.corpus import Chunk, Corpus, Paper
from evals.dataset.schema import QUOTA

SEED = 42
OVERSAMPLE = 2
MIN_FACTOID_TOKENS = 150
MIN_MULTI_TOKENS = 100

CATEGORY_TOPICS = {
    "cs.IR": "information retrieval and search systems",
    "cs.CL": "natural language processing and language models",
    "cs.DB": "databases and data management",
}
UNANSWERABLE_ANGLES = (
    "a specific benchmark number for a named commercial product",
    "a proprietary system whose internals are not published",
    "a hardware cost or energy measurement",
    "a user study with human participants",
    "a legal or regulatory ruling",
    "the personal biography of a researcher",
    "a result from before 2015",
    "a deployment at a named company",
)


@dataclass
class SampleRow:
    sample_id: str
    type: str
    chunks: list[dict[str, Any]] = field(
        default_factory=list
    )  # external_id, ordinal, section_path, content_sha256
    phrase: dict[str, Any] | None = None  # filter: kind, text, expected_filters
    topic: dict[str, str] | None = None  # unanswerable: category, topic, angle
    seed: int = SEED

    def chunk_keys(self) -> list[tuple[str, int]]:
        return [(c["external_id"], c["ordinal"]) for c in self.chunks]


def _chunk_ref(c: Chunk) -> dict[str, Any]:
    return {
        "external_id": c.external_id,
        "ordinal": c.ordinal,
        "section_path": c.section_path,
        "content_sha256": c.content_sha256,
    }


def _shuffled(items: list, rng: random.Random) -> list:
    out = sorted(items, key=str)
    rng.shuffle(out)
    return out


def sample_factoid(corpus: Corpus, n: int, rng: random.Random, exclude: set) -> list[SampleRow]:
    """At most one chunk per paper on the first pass; later passes revisit papers."""
    papers = _shuffled([p.external_id for p in corpus.papers()], rng)
    pools = {
        ext: _shuffled(
            [
                c
                for c in corpus.chunks(ext)
                if c.token_count >= MIN_FACTOID_TOKENS and c.key not in exclude
            ],
            rng,
        )
        for ext in papers
    }
    rows: list[SampleRow] = []
    while len(rows) < n and any(pools.values()):
        for ext in papers:
            if len(rows) >= n:
                break
            if pools[ext]:
                chunk = pools[ext].pop()
                rows.append(SampleRow("", "factoid", [_chunk_ref(chunk)]))
    return rows


def sample_multi_chunk(corpus: Corpus, n: int, rng: random.Random, exclude: set) -> list[SampleRow]:
    papers = _shuffled([p.external_id for p in corpus.papers()], rng)
    rows: list[SampleRow] = []
    for _round in range(8):  # at most a few samples per paper
        for ext in papers:
            if len(rows) >= n:
                return rows
            by_section: dict[str, list[Chunk]] = {}
            for c in corpus.chunks(ext):
                if c.token_count >= MIN_MULTI_TOKENS and c.key not in exclude:
                    by_section.setdefault(c.top_section, []).append(c)
            if len(by_section) < 2:
                continue
            k = rng.choice([2, 2, 3, 4])
            sections = _shuffled(list(by_section), rng)[: min(k, len(by_section))]
            chosen = [rng.choice(_shuffled(by_section[s], rng)) for s in sections]
            chosen.sort(key=lambda c: c.ordinal)
            exclude |= {c.key for c in chosen}
            rows.append(SampleRow("", "multi_chunk", [_chunk_ref(c) for c in chosen]))
    return rows


def _author_paper_counts(papers: Iterable[Paper]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in papers:
        for _display, norm in p.authors:
            counts[norm] = counts.get(norm, 0) + 1
    return counts


def _filter_phrase(paper: Paper, kind: str, author_counts: dict[str, int], rng: random.Random):
    year = paper.year
    year_range = {"published_from": f"{year}-01-01", "published_to": f"{year}-12-31"}
    if kind == "author":
        eligible = [a for a in paper.authors if author_counts.get(a[1], 0) >= 2]
        if eligible:
            display, norm = rng.choice(sorted(eligible))
            return {
                "kind": "author",
                "text": f"by {display}",
                "expected_filters": {"authors": [norm]},
            }
        kind = "year"
    if kind == "category_year":
        return {
            "kind": "category_year",
            "text": f"{paper.primary_category} papers from {year}",
            "expected_filters": {"categories": [paper.primary_category], **year_range},
        }
    return {"kind": "year", "text": f"in {year}", "expected_filters": year_range}


def sample_filter(corpus: Corpus, n: int, rng: random.Random, exclude: set) -> list[SampleRow]:
    papers_all = corpus.papers()
    author_counts = _author_paper_counts(papers_all)
    papers = _shuffled(papers_all, rng)
    schedule = ["year", "year", "author", "author", "category_year"]  # 40 / 40 / 20 %
    rows: list[SampleRow] = []
    i = 0
    for _round in range(8):
        for paper in papers:
            if len(rows) >= n:
                return rows
            pool = [
                c
                for c in corpus.chunks(paper.external_id)
                if c.token_count >= MIN_FACTOID_TOKENS and c.key not in exclude
            ]
            if not pool:
                continue
            chunk = rng.choice(sorted(pool, key=lambda c: c.ordinal))
            exclude.add(chunk.key)
            phrase = _filter_phrase(paper, schedule[i % len(schedule)], author_counts, rng)
            i += 1
            rows.append(SampleRow("", "filter", [_chunk_ref(chunk)], phrase=phrase))
    return rows


def sample_unanswerable(
    corpus: Corpus, n: int, rng: random.Random, exclude: set
) -> list[SampleRow]:
    categories = sorted({p.primary_category for p in corpus.papers()})
    combos = [(c, a) for c in categories for a in UNANSWERABLE_ANGLES]
    combos = [x for x in combos if x not in exclude]
    rng.shuffle(combos)
    rows = []
    for cat, angle in combos[:n]:
        topic = CATEGORY_TOPICS.get(cat, cat)
        rows.append(
            SampleRow(
                "", "unanswerable", [], topic={"category": cat, "topic": topic, "angle": angle}
            )
        )
        exclude.add((cat, angle))
    return rows


SAMPLERS = {
    "factoid": sample_factoid,
    "multi_chunk": sample_multi_chunk,
    "filter": sample_filter,
    "unanswerable": sample_unanswerable,
}


def exclusion_keys(rows: Iterable[SampleRow]) -> set:
    """What earlier samples already consumed, so extra draws never repeat them."""
    out: set = set()
    for r in rows:
        if r.type == "unanswerable" and r.topic:
            out.add((r.topic["category"], r.topic["angle"]))
        else:
            out.update(r.chunk_keys())
    return out


def draw_samples(
    corpus: Corpus,
    qtype: str,
    n: int,
    *,
    seed: int = SEED,
    existing: list[SampleRow] | None = None,
) -> list[SampleRow]:
    """`n` new samples of one type. The stream is seeded by `(seed, type)` and skips what
    `existing` already used, so repeated calls extend rather than repeat."""
    existing = existing or []
    rng = random.Random(f"{seed}:{qtype}:{len([r for r in existing if r.type == qtype])}")
    same_type = [r for r in existing if r.type == qtype]
    rows = SAMPLERS[qtype](corpus, n, rng, exclusion_keys(same_type))
    start = len(same_type)
    for i, row in enumerate(rows, start=start + 1):
        row.sample_id = f"s-{qtype}-{i:04d}"
        row.seed = seed
    return rows


def sample_all(corpus: Corpus, seed: int = SEED, oversample: float = OVERSAMPLE) -> list[SampleRow]:
    rows: list[SampleRow] = []
    for qtype in ("factoid", "multi_chunk", "filter", "unanswerable"):
        rows += draw_samples(
            corpus, qtype, round(QUOTA[qtype] * oversample), seed=seed, existing=rows
        )
    return rows


def write_samples(path: Path, rows: list[SampleRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(r)) + "\n" for r in rows), encoding="utf-8")


def read_samples(path: Path) -> list[SampleRow]:
    if not path.exists():
        return []
    return [
        SampleRow(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
