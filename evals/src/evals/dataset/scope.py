"""Records how much of the corpus existed when candidates were drawn.

The dataset may be built while indexing is still running; `work/scope.jsonl` keeps one entry per
sampling/generation step so the dataset card can state the scope honestly."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.dataset import work
from evals.dataset.corpus import Corpus


def record_scope(
    corpus: Corpus, step: str, path: Path = work.SCOPE, **extra: Any
) -> dict[str, Any]:
    docs = int(corpus.fetch("select count(*) from documents", {})[0][0])
    chunks = int(corpus.fetch("select count(*) from chunks", {})[0][0])
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "step": step,
        "documents": docs,
        "chunks": chunks,
        **extra,
    }
    work.append_jsonl(path, entry)
    return entry
