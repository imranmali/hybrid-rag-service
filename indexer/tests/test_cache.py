from pathlib import Path

from indexer.arxiv.cache import ArxivCache


def test_cache_round_trip_and_negative_entry(tmp_path: Path) -> None:
    cache = ArxivCache(tmp_path)
    url = "https://example.test/query"
    cache.write_api(url, "<feed />")
    cache.write_html("2401.00001", 2, "<article />")

    assert cache.read_api(url) == "<feed />"
    assert cache.read_html("2401.00001", 2) == "<article />"
    cache.mark_missing("2401.00002", 1)
    assert cache.html_is_missing("2401.00002", 1)
