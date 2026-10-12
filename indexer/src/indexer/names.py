"""Cross-language author name normalization contract."""

import re
import unicodedata


def normalize_author_name(name: str) -> str:
    """Produce the C4 lookup key for an arXiv author display name."""
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    normalized = re.sub(r"[^a-z0-9 -]", " ", without_marks.lower())
    return " ".join(normalized.split())
