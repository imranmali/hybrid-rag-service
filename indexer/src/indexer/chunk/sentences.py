"""Conservative sentence splitting for scientific arXiv prose."""

import re

ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "et al.",
    "fig.",
    "figs.",
    "eq.",
    "sec.",
    "tab.",
    "vs.",
    "cf.",
    "approx.",
    "no.",
)
_BOUNDARY = re.compile(r"[.!?]\s+(?=[A-Z0-9(])")


def split_sentences(text: str) -> list[str]:
    """Split only likely prose boundaries, never boundaries inside `$…$` math."""
    boundaries: list[int] = []
    for match in _BOUNDARY.finditer(text):
        punctuation_position = match.start()
        if _in_math(text, punctuation_position):
            continue
        prefix = text[: punctuation_position + 1].lower()
        if any(prefix.endswith(abbreviation) for abbreviation in ABBREVIATIONS):
            continue
        boundaries.append(match.end())
    starts = [0, *boundaries]
    ends = [*boundaries, len(text)]
    sentences = (text[start:end].strip() for start, end in zip(starts, ends, strict=True))
    return [sentence for sentence in sentences if sentence]


def _in_math(text: str, position: int) -> bool:
    """Return whether a position lies within an unescaped inline math span."""
    unescaped_dollars = 0
    for index, character in enumerate(text[:position]):
        if character == "$" and (index == 0 or text[index - 1] != "\\"):
            unescaped_dollars += 1
    return bool(unescaped_dollars % 2)
