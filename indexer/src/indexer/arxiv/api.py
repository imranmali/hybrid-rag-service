"""arXiv Atom API parsing and request construction."""

from datetime import UTC, date, datetime
from urllib.parse import urlencode

from lxml import etree

from indexer.models import Paper

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
NAMESPACES = {"atom": ATOM, "arxiv": ARXIV}
API_URL = "https://export.arxiv.org/api/query"


def parse_atom(xml: str) -> list[Paper]:
    """Parse enough of arXiv's Atom feed for version-pinned acquisition."""
    root = etree.fromstring(xml.encode("utf-8"))
    papers: list[Paper] = []
    for entry in root.xpath("./atom:entry", namespaces=NAMESPACES):
        identifier = _text(entry, "atom:id").rstrip("/").split("/")[-1]
        external_id, version = _split_identifier(identifier)
        categories = tuple(
            category.get("term")
            for category in entry.xpath("./atom:category", namespaces=NAMESPACES)
            if category.get("term")
        )
        primary = entry.find("arxiv:primary_category", NAMESPACES)
        primary_category = primary.get("term") if primary is not None else categories[0]
        papers.append(
            Paper(
                external_id=external_id,
                version=version,
                title=" ".join(_text(entry, "atom:title").split()),
                abstract=" ".join(_text(entry, "atom:summary").split()),
                authors=tuple(
                    " ".join(author.xpath("./atom:name/text()", namespaces=NAMESPACES)[0].split())
                    for author in entry.xpath("./atom:author", namespaces=NAMESPACES)
                ),
                categories=categories,
                primary_category=primary_category,
                published_on=_date(_text(entry, "atom:published")),
                updated_on=_date(_text(entry, "atom:updated")),
            )
        )
    return papers


def query_url(category: str, start: date, end: date, *, max_results: int = 50) -> str:
    """Build the deterministic category-quarter query specified by D2."""
    submitted = f"{start:%Y%m%d}0000+TO+{end:%Y%m%d}2359"
    parameters = {
        "search_query": f"cat:{category} AND submittedDate:[{submitted}]",
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "ascending",
    }
    return f"{API_URL}?{urlencode(parameters)}"


def _text(element: etree._Element, path: str) -> str:
    result = element.xpath(f"{path}/text()", namespaces=NAMESPACES)
    if not result:
        raise ValueError(f"missing required Atom field {path}")
    return str(result[0])


def _split_identifier(identifier: str) -> tuple[str, int]:
    if "v" in identifier:
        base, version = identifier.rsplit("v", 1)
        if version.isdigit():
            return base, int(version)
    return identifier, 1


def _date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).date()
