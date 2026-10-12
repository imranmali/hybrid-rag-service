from indexer.chunk.sentences import split_sentences


def test_scientific_abbreviations_and_inline_math_do_not_split() -> None:
    text = (
        "Smith et al. compare Fig. 3, e.g. with $x. y$ values. "
        "This is the next sentence. (A final sentence.)"
    )

    assert split_sentences(text) == [
        "Smith et al. compare Fig. 3, e.g. with $x. y$ values.",
        "This is the next sentence.",
        "(A final sentence.)",
    ]
