"""Deterministic stratified arXiv corpus selection."""

import json
import random
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from indexer.models import Paper
from indexer.names import normalize_author_name

CATEGORIES = ("cs.IR", "cs.CL", "cs.DB")


@dataclass(frozen=True)
class SelectedPaper:
    paper: Paper
    selection_reason: str

    def manifest_record(self) -> dict[str, object]:
        return {
            "external_id": self.paper.external_id,
            "version": self.paper.version,
            "primary_category": self.paper.primary_category,
            "published_on": self.paper.published_on.isoformat(),
            "selection_reason": self.selection_reason,
            "title": self.paper.title,
            "abstract": self.paper.abstract,
            "authors": list(self.paper.authors),
            "categories": list(self.paper.categories),
            "updated_on": self.paper.updated_on.isoformat(),
        }


def select_papers(
    papers: Iterable[Paper],
    *,
    target: int = 220,
    seed: int = 42,
    usable_html: Callable[[Paper], bool] = lambda _: True,
) -> list[SelectedPaper]:
    """Select prolific-author papers first, then a seeded round-robin stratified fill."""
    eligible = [
        paper for paper in papers if paper.primary_category in CATEGORIES and usable_html(paper)
    ]
    author_counts = Counter(
        normalize_author_name(author)
        for paper in eligible
        for author in paper.authors
        if normalize_author_name(author)
    )
    prolific = sorted(
        (name for name, count in author_counts.items() if count >= 3),
        key=lambda name: (-author_counts[name], name),
    )[:8]
    selected: list[SelectedPaper] = []
    seen: set[str] = set()
    for author in prolific:
        candidates = sorted(
            (
                paper
                for paper in eligible
                if author in {normalize_author_name(value) for value in paper.authors}
            ),
            key=lambda paper: (paper.published_on, paper.external_id),
        )
        for paper in candidates[:5]:
            if paper.external_id not in seen and len(selected) < target:
                selected.append(SelectedPaper(paper, f"prolific_author:{author}"))
                seen.add(paper.external_id)

    remaining = target - len(selected)
    groups: dict[tuple[str, int], list[Paper]] = defaultdict(list)
    for paper in eligible:
        if paper.external_id not in seen:
            groups[(paper.primary_category, paper.published_on.year)].append(paper)
    rng = random.Random(seed)
    strata = [(category, year) for category in CATEGORIES for year in range(2023, 2027)]
    queues: dict[tuple[str, int], deque[Paper]] = {}
    quota, extras = divmod(remaining, len(strata))
    for position, stratum in enumerate(strata):
        candidates = groups[stratum]
        rng.shuffle(candidates)
        queues[stratum] = deque(candidates[: quota + (position < extras)])

    # If a stratum was short, retain its remaining papers for the specified borrowing order.
    spillovers = {
        stratum: deque(
            paper
            for paper in groups[stratum]
            if paper.external_id not in {candidate.external_id for candidate in queues[stratum]}
        )
        for stratum in strata
    }
    while len(selected) < target and any(queues.values()):
        for stratum in strata:
            if len(selected) == target:
                break
            if queues[stratum]:
                paper = queues[stratum].popleft()
                selected.append(SelectedPaper(paper, f"stratified:{stratum[0]}:{stratum[1]}"))
                seen.add(paper.external_id)
    # Borrow from other years in the same category, then all remaining categories.
    for category in CATEGORIES:
        for stratum in (item for item in strata if item[0] == category):
            while spillovers[stratum] and len(selected) < target:
                paper = spillovers[stratum].popleft()
                if paper.external_id not in seen:
                    selected.append(SelectedPaper(paper, f"borrowed:{stratum[0]}:{stratum[1]}"))
                    seen.add(paper.external_id)
    for stratum in strata:
        while spillovers[stratum] and len(selected) < target:
            paper = spillovers[stratum].popleft()
            if paper.external_id not in seen:
                selected.append(SelectedPaper(paper, f"borrowed:{stratum[0]}:{stratum[1]}"))
                seen.add(paper.external_id)
    if len(selected) < target:
        raise ValueError(f"only {len(selected)} eligible papers available; target is {target}")
    return selected


def write_manifest(path: Path, papers: Iterable[SelectedPaper]) -> None:
    """Write stable JSONL without incidental whitespace or key ordering differences."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for selected in papers:
            handle.write(json.dumps(selected.manifest_record(), sort_keys=True) + "\n")


def read_manifest(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def quarter_windows(now: date) -> list[tuple[int, int, date, date]]:
    """Return the 48 category-independent windows ending 2026 at the run date."""
    windows: list[tuple[int, int, date, date]] = []
    for year in range(2023, 2027):
        for quarter in range(1, 5):
            month = (quarter - 1) * 3 + 1
            start = date(year, month, 1)
            next_start = date(year + 1, 1, 1) if quarter == 4 else date(year, month + 3, 1)
            end = next_start.fromordinal(next_start.toordinal() - 1)
            if year == now.year and end > now:
                end = now
            if start <= now:
                windows.append((year, quarter, start, end))
    return windows
