"""An in-memory corpus (sqlite behind the Corpus protocol) for dataset-generation tests."""

from __future__ import annotations

import sqlite3
from typing import Any

from conftest import sha

from evals.dataset.corpus import Chunk, Paper

SECTIONS = ["1 Introduction", "2 Method > 2.1 Index", "3 Experiments", "4 Conclusion"]

# (external_id, title, primary, categories, published_on, [(display, normalized), ...])
PAPER_ROWS = [
    ("2401.00001", "Dense retrieval at scale", "cs.IR", ("cs.IR", "cs.CL"), "2024-03-02",
     [("Jimmy Lin", "jimmy lin"), ("Ana Pérez", "ana perez")]),
    ("2401.00002", "Listwise reranking", "cs.IR", ("cs.IR",), "2024-05-10",
     [("Jimmy Lin", "jimmy lin")]),
    ("2402.00003", "Sparse retrieval revisited", "cs.IR", ("cs.IR", "cs.CL"), "2025-01-20",
     [("Jimmy Lin", "jimmy lin"), ("Wei Zhang", "wei zhang")]),
    ("2402.00004", "Query planning in databases", "cs.DB", ("cs.DB",), "2024-07-07",
     [("Wei Zhang", "wei zhang")]),
    ("2403.00005", "Learned indexes", "cs.DB", ("cs.DB", "cs.IR"), "2025-02-02",
     [("Sam Roe", "sam roe")]),
    ("2403.00006", "Instruction tuning", "cs.CL", ("cs.CL",), "2025-04-04",
     [("Ana Pérez", "ana perez")]),
    ("2404.00007", "Long context models", "cs.CL", ("cs.CL",), "2023-09-09",
     [("Wei Zhang", "wei zhang"), ("Ana Pérez", "ana perez")]),
    ("2404.00008", "Vector databases", "cs.DB", ("cs.DB", "cs.IR"), "2023-11-11",
     [("Sam Roe", "sam roe"), ("Jimmy Lin", "jimmy lin")]),
]  # fmt: skip


class FixtureCorpus:
    def __init__(self, paper_rows=PAPER_ROWS, chunks_per_paper: int = 4, tokens: int = 200):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            create table documents (id integer primary key, external_id text, title text,
                                    primary_category text, published_on text);
            create table authors (id integer primary key, name text, name_normalized text unique);
            create table paper_authors (document_id integer, author_id integer, position integer);
            create table paper_categories (document_id integer, category text, is_primary integer);
            """
        )
        self._papers: list[Paper] = []
        self._chunks: dict[str, list[Chunk]] = {}
        author_ids: dict[str, int] = {}
        for doc_id, (ext, title, primary, cats, published, authors) in enumerate(paper_rows, 1):
            self.db.execute(
                "insert into documents values (?,?,?,?,?)", (doc_id, ext, title, primary, published)
            )
            for pos, (display, norm) in enumerate(authors, 1):
                if norm not in author_ids:
                    author_ids[norm] = len(author_ids) + 1
                    self.db.execute(
                        "insert into authors values (?,?,?)", (author_ids[norm], display, norm)
                    )
                self.db.execute(
                    "insert into paper_authors values (?,?,?)", (doc_id, author_ids[norm], pos)
                )
            for cat in cats:
                self.db.execute(
                    "insert into paper_categories values (?,?,?)",
                    (doc_id, cat, int(cat == primary)),
                )
            self._papers.append(Paper(ext, title, primary, tuple(cats), published, tuple(authors)))
            self._chunks[ext] = [
                Chunk(
                    ext,
                    i,
                    SECTIONS[i % len(SECTIONS)],
                    f"Content of {ext} chunk {i}.",
                    sha(f"{ext}#{i}"),
                    tokens,
                )
                for i in range(chunks_per_paper)
            ]

    def papers(self) -> list[Paper]:
        return sorted(self._papers, key=lambda p: p.external_id)

    def chunks(self, external_id: str) -> list[Chunk]:
        return list(self._chunks.get(external_id, []))

    def fetch(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        return list(self.db.execute(sql, params).fetchall())
