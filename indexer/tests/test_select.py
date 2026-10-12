from datetime import date

from indexer.arxiv.select import CATEGORIES, select_papers
from indexer.models import Paper


def _paper(category: str, year: int, number: int, author: str = "Author") -> Paper:
    return Paper(
        external_id=f"{year % 100:02d}01.{number:05d}",
        version=1,
        title=f"{category} {year} {number}",
        abstract="abstract",
        authors=(author,),
        categories=(category,),
        primary_category=category,
        published_on=date(year, 1, 1),
        updated_on=date(year, 1, 1),
    )


def test_selection_is_deterministic_and_replaces_unusable_candidates() -> None:
    papers = [
        _paper(category, year, number)
        for category in CATEGORIES
        for year in range(2023, 2027)
        for number in range(3)
    ]
    unavailable = papers[0].external_id
    first = select_papers(
        papers, target=12, seed=42, usable_html=lambda paper: paper.external_id != unavailable
    )
    second = select_papers(
        papers, target=12, seed=42, usable_html=lambda paper: paper.external_id != unavailable
    )

    assert [item.manifest_record() for item in first] == [item.manifest_record() for item in second]
    assert unavailable not in {item.paper.external_id for item in first}
    assert len({item.paper.primary_category for item in first}) == 3


def test_prolific_authors_are_prefixed_and_capped() -> None:
    papers = [_paper("cs.IR", 2024, number, "Prolific") for number in range(7)]
    papers.extend(_paper("cs.CL", 2024, number, f"Other {number}") for number in range(7, 15))

    selected = select_papers(papers, target=10, seed=42)

    prolific = [item for item in selected if item.selection_reason.startswith("prolific_author:")]
    assert len(prolific) == 5
    assert selected[:5] == prolific
