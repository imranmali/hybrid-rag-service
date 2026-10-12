import pytest

from indexer.names import normalize_author_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("José  Pérez-Gómez", "jose perez-gomez"),
        ("J. R. R. Tolkien", "j r r tolkien"),
        ("Seán O'Neil", "sean o neil"),
        ("ZHANG Wei", "zhang wei"),
        ("  Jimmy   Lin ", "jimmy lin"),
    ],
)
def test_normalize_author_name(value: str, expected: str) -> None:
    assert normalize_author_name(value) == expected
