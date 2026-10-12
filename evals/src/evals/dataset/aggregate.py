"""Deterministic aggregate-question templates T1-T6 (design D3).

Expected answers are computed with SQL over the structured tables and never by an LLM. The SQL
uses `:name` placeholders and only portable constructs (date ranges instead of `extract`)."""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from evals.dataset.corpus import Corpus, Paper

TEMPLATE_MIX = {"T1": 6, "T2": 6, "T3": 8, "T4": 3, "T5": 4, "T6": 3}  # 30 candidates

T1_SQL = (
    "select count(distinct pa.document_id) from paper_authors pa "
    "join authors a on a.id = pa.author_id where a.name_normalized = :author"
)
T2_SQL = (
    "select count(distinct d.id) from documents d "
    "join paper_categories pc on pc.document_id = d.id "
    "where pc.category = :category and d.published_on >= :start and d.published_on < :end"
)
T3_SQL = (
    "select count(distinct d.id) from documents d "
    "join paper_authors pa on pa.document_id = d.id join authors a on a.id = pa.author_id "
    "where a.name_normalized = :author and d.published_on >= :start and d.published_on < :end"
)
T4_SQL = (
    "select a.name, count(distinct pa.document_id) as n from authors a "
    "join paper_authors pa on pa.author_id = a.id "
    "join paper_categories pc on pc.document_id = pa.document_id "
    "where pc.category = :category group by a.id, a.name order by n desc, a.name limit 2"
)
T5_SQL = (
    "select distinct d.title from documents d join paper_authors pa on pa.document_id = d.id "
    "join authors a on a.id = pa.author_id where a.name_normalized = :author order by d.title"
)
T6_SQL = (
    "select count(distinct d.id) from documents d "
    "join paper_categories p1 on p1.document_id = d.id and p1.category = :cat_a "
    "join paper_categories p2 on p2.document_id = d.id and p2.category = :cat_b"
)


@dataclass
class AggregateCandidate:
    template: str
    question: str
    expected_sql_answer: str
    sql: str
    params: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "question": self.question,
            "expected_sql_answer": self.expected_sql_answer,
            "sql": {"query": self.sql, "params": self.params},
        }


def _year_params(year: int) -> dict[str, str]:
    return {"start": f"{year}-01-01", "end": f"{year + 1}-01-01"}


def _scalar(corpus: Corpus, sql: str, params: dict[str, Any]) -> int:
    return int(corpus.fetch(sql, params)[0][0])


def t1(corpus: Corpus, display: str, norm: str) -> AggregateCandidate | None:
    params = {"author": norm}
    n = _scalar(corpus, T1_SQL, params)
    if n < 1:
        return None
    return AggregateCandidate(
        "T1", f"How many papers by {display} are in the corpus?", str(n), T1_SQL, params
    )


def t2(corpus: Corpus, category: str, year: int) -> AggregateCandidate | None:
    params = {"category": category, **_year_params(year)}
    n = _scalar(corpus, T2_SQL, params)
    if n < 1:
        return None
    return AggregateCandidate(
        "T2", f"How many {category} papers were published in {year}?", str(n), T2_SQL, params
    )


def t3(corpus: Corpus, display: str, norm: str, year: int) -> AggregateCandidate | None:
    params = {"author": norm, **_year_params(year)}
    n = _scalar(corpus, T3_SQL, params)
    if n < 1:
        return None
    return AggregateCandidate(
        "T3", f"How many papers did {display} publish in {year}?", str(n), T3_SQL, params
    )


def t4(corpus: Corpus, category: str) -> AggregateCandidate | None:
    rows = corpus.fetch(T4_SQL, {"category": category})
    if not rows or (len(rows) > 1 and rows[0][1] == rows[1][1]):
        return None  # empty or tied: no unique answer
    return AggregateCandidate(
        "T4",
        f"Which author has the most {category} papers?",
        str(rows[0][0]),
        T4_SQL,
        {"category": category},
    )


def t5(corpus: Corpus, display: str, norm: str) -> AggregateCandidate | None:
    params = {"author": norm}
    titles = [r[0] for r in corpus.fetch(T5_SQL, params)]
    if not 2 <= len(titles) <= 4:
        return None
    return AggregateCandidate(
        "T5",
        f"List the titles of the papers by {display} in the corpus.",
        json.dumps(titles),
        T5_SQL,
        params,
    )


def t6(corpus: Corpus, cat_a: str, cat_b: str) -> AggregateCandidate | None:
    params = {"cat_a": cat_a, "cat_b": cat_b}
    n = _scalar(corpus, T6_SQL, params)
    if n < 1:
        return None
    return AggregateCandidate(
        "T6",
        f"How many papers are cross-listed in both {cat_a} and {cat_b}?",
        str(n),
        T6_SQL,
        params,
    )


def generate_aggregates(
    corpus: Corpus, total: int = 30, seed: int = 42, mix: dict[str, int] | None = None
) -> list[AggregateCandidate]:
    """Up to `total` unique aggregate candidates drawn per template quota; shortfalls (e.g. tied T4
    answers) are filled from T1/T3 so the pool still reaches `total` when the corpus allows."""
    rng = random.Random(f"{seed}:aggregate")
    mix = dict(mix or TEMPLATE_MIX)
    papers: list[Paper] = corpus.papers()
    authors: dict[str, str] = {}
    author_years: dict[str, set[int]] = {}
    author_papers: Counter[str] = Counter()
    for p in papers:
        for display, norm in p.authors:
            authors.setdefault(norm, display)
            author_years.setdefault(norm, set()).add(p.year)
            author_papers[norm] += 1
    author_norms = sorted(authors)
    categories = sorted({c for p in papers for c in p.categories})
    years = sorted({p.year for p in papers})
    pairs = sorted({(a, b) for p in papers for a in p.categories for b in p.categories if a < b})

    def draw(template: str) -> Iterator[AggregateCandidate]:
        """Lazily yield candidates for one template in seeded order."""
        if template == "T1":
            pool = list(author_norms)
            rng.shuffle(pool)
            results = (t1(corpus, authors[n], n) for n in pool)
        elif template == "T2":
            pool2 = [(c, y) for c in categories for y in years]
            rng.shuffle(pool2)
            results = (t2(corpus, c, y) for c, y in pool2)
        elif template == "T3":
            pool3 = [(n, y) for n in author_norms for y in sorted(author_years[n])]
            rng.shuffle(pool3)
            results = (t3(corpus, authors[n], n, y) for n, y in pool3)
        elif template == "T4":
            cats = list(categories)
            rng.shuffle(cats)
            results = (t4(corpus, c) for c in cats)
        elif template == "T5":
            pool = [n for n in author_norms if 2 <= author_papers[n] <= 4]
            rng.shuffle(pool)
            results = (t5(corpus, authors[n], n) for n in pool)
        else:
            pool6 = list(pairs)
            rng.shuffle(pool6)
            results = (t6(corpus, a, b) for a, b in pool6)
        return (c for c in results if c is not None)

    chosen: list[AggregateCandidate] = []
    seen: set[str] = set()
    leftovers: list[AggregateCandidate] = []
    for template, quota in mix.items():
        taken = 0
        for cand in draw(template):
            if cand.question in seen:
                continue
            if taken < quota:
                chosen.append(cand)
                seen.add(cand.question)
                taken += 1
            else:
                leftovers.append(cand)
                if len(leftovers) >= total:
                    break
    for cand in leftovers:
        if len(chosen) >= total:
            break
        if cand.question not in seen:
            chosen.append(cand)
            seen.add(cand.question)
    return chosen[:total]


def recompute_answer(
    corpus: Corpus, template: str, query: str, params: dict[str, Any]
) -> str | None:
    """Re-run a candidate's recorded SQL; the answer in the same format `generate` stored."""
    rows = corpus.fetch(query, params)
    if not rows:
        return None
    if template == "T5":
        return json.dumps([r[0] for r in rows])
    return str(rows[0][0])
