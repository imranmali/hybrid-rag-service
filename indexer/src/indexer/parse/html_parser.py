"""Parse arXiv's LaTeXML HTML into clean, section-scoped text."""

import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString, Tag

from indexer.models import ParsedDocument, Section

_SECTION_CLASSES = {"ltx_section", "ltx_subsection", "ltx_subsubsection", "ltx_appendix"}
_ACKNOWLEDGEMENTS = re.compile(r"^acknowledg(e)?ments?$", re.IGNORECASE)


def parse_html(html: str) -> ParsedDocument | None:
    """Return normalized article sections or ``None`` when arXiv HTML is unavailable."""
    soup = BeautifulSoup(html, "lxml")
    article = soup.select_one("article.ltx_document")
    if article is None:
        return None
    _remove_noise(article)
    title_node = article.select_one("h1.ltx_title_document")
    title = _text(title_node) if title_node else ""
    sections: list[Section] = []
    abstract = article.select_one("div.ltx_abstract")
    if abstract:
        abstract_text = _block_text(abstract)
        if abstract_text:
            sections.append(Section("Abstract", abstract_text))
    for section in article.find_all("section", recursive=False):
        _collect_section(section, (), sections)
    # Some LaTeXML variants place sections inside a wrapper.
    if not any(_is_section(section) for section in article.find_all("section", recursive=False)):
        for section in article.find_all("section"):
            if _is_section(section) and section.find_parent("section") is None:
                _collect_section(section, (), sections)
    return ParsedDocument(title=title, sections=tuple(sections))


def _remove_noise(article: Tag) -> None:
    for node in article.select(
        "section.ltx_bibliography, .ltx_note, nav, header, footer, .ltx_page_footer, .ltx_ERROR"
    ):
        node.decompose()
    for section in article.find_all("section"):
        heading = section.select_one(":scope > .ltx_title")
        if heading and _ACKNOWLEDGEMENTS.match(_text(heading, exclude_tag_numbers=True)):
            section.decompose()


def _collect_section(section: Tag, ancestors: tuple[str, ...], output: list[Section]) -> None:
    if not _is_section(section):
        return
    heading = section.select_one(":scope > .ltx_title")
    label = _text(heading) if heading else ""
    path = ancestors + ((label,) if label else ())
    own_text = _section_own_text(section)
    if own_text and path:
        output.append(Section(" > ".join(path), own_text))
    for child in section.find_all("section", recursive=False):
        _collect_section(child, path, output)


def _is_section(node: Tag) -> bool:
    return bool(_SECTION_CLASSES.intersection(node.get("class", [])))


def _section_own_text(section: Tag) -> str:
    fragments: list[str] = []
    for child in section.children:
        if (
            not isinstance(child, Tag)
            or child.name == "section"
            or "ltx_title" in child.get("class", [])
        ):
            continue
        if child.name == "table" or "ltx_tabular" in child.get("class", []):
            table = _table_text(child)
            if table:
                fragments.append(table)
        elif child.name == "figcaption":
            caption = _block_text(child)
            if caption:
                fragments.append(f"Figure: {caption}")
        elif child.name in {"div", "p", "ul", "ol", "figure"}:
            fragments.extend(_descendant_blocks(child))
    return _normalize(" ".join(fragments))


def _descendant_blocks(node: Tag) -> list[str]:
    blocks: list[str] = []
    for block in node.select("div.ltx_para, li, figcaption, table.ltx_tabular"):
        if block.find_parent("section") is not node.find_parent("section"):
            continue
        if block.name == "figcaption":
            text = _block_text(block)
            if text:
                blocks.append(f"Figure: {text}")
        elif block.name == "table":
            text = _table_text(block)
            if text:
                blocks.append(text)
        else:
            text = _block_text(block)
            if text:
                blocks.append(text)
    if not blocks and node.name in {"p", "div"}:
        text = _block_text(node)
        if text:
            blocks.append(text)
    return blocks


def _table_text(table: Tag) -> str:
    rows = []
    for row in table.select("tr")[:40]:
        cells = [_block_text(cell) for cell in row.select(":scope > th, :scope > td")]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _block_text(node: Tag) -> str:
    clone = BeautifulSoup(str(node), "lxml")
    for math in clone.select("math[alttext]"):
        math.replace_with(NavigableString(f"${math['alttext']}$"))
    return _normalize(clone.get_text(" ", strip=True))


def _text(node: Tag | None, *, exclude_tag_numbers: bool = False) -> str:
    if node is None:
        return ""
    clone = BeautifulSoup(str(node), "lxml")
    if exclude_tag_numbers:
        for tag in clone.select(".ltx_tag"):
            tag.decompose()
    return _normalize(clone.get_text(" ", strip=True))


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())
