from pathlib import Path

import httpx
import respx

from indexer.arxiv.cache import ArxivCache
from indexer.arxiv.html_fetch import HtmlFetcher
from indexer.arxiv.ratelimit import RateLimiter


@respx.mock
def test_html_fetch_retries_429_then_caches(tmp_path: Path) -> None:
    url = "https://arxiv.org/html/2401.00001v1"
    route = respx.get(url).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, text="<article />"),
        ]
    )
    waits: list[float] = []
    fetcher = HtmlFetcher(
        ArxivCache(tmp_path),
        RateLimiter(min_interval=0),
        client=httpx.Client(),
        sleep=waits.append,
    )

    assert fetcher.get_html("2401.00001", 1) == "<article />"
    assert route.call_count == 2
    assert waits == [5.0]
    assert fetcher.get_html("2401.00001", 1) == "<article />"
    assert fetcher.network_requests == 2
