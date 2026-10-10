from indexer.arxiv.api import parse_atom


def test_parse_atom_extracts_versioned_metadata() -> None:
    papers = parse_atom(
        """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>http://arxiv.org/abs/2403.01234v2</id><title> A title </title>
            <summary> An abstract </summary><published>2024-03-02T00:00:00Z</published>
            <updated>2024-03-03T00:00:00Z</updated><author><name>José Pérez</name></author>
            <category term="cs.IR"/><category term="cs.CL"/><arxiv:primary_category term="cs.IR"/>
          </entry>
        </feed>"""
    )

    assert papers[0].external_id == "2403.01234"
    assert papers[0].version == 2
    assert papers[0].primary_category == "cs.IR"
    assert papers[0].categories == ("cs.IR", "cs.CL")


def test_query_url_encodes_date_range_with_spaces_not_literal_plus() -> None:
    from datetime import date

    from indexer.arxiv.api import query_url

    url = query_url("cs.IR", date(2023, 1, 1), date(2023, 3, 31))
    assert "%5B202301010000+TO+202303312359%5D" in url  # '+' here is an encoded space
    assert "%2B" not in url
