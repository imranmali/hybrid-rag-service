"""Cached, rate-limited version-pinned arXiv HTML retrieval."""

import time
from collections.abc import Callable

import httpx

from indexer.arxiv.cache import ArxivCache
from indexer.arxiv.ratelimit import RateLimiter

RETRY_DELAYS = (5.0, 15.0, 45.0)
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class HtmlFetcher:
    """Fetch arXiv resources while preserving cache and politeness invariants."""

    def __init__(
        self,
        cache: ArxivCache,
        limiter: RateLimiter,
        *,
        contact: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache = cache
        self.limiter = limiter
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._owns_client = client is None
        self._sleep = sleep
        self.network_requests = 0
        agent = "hybrid-rag-indexer/0.1 (research portfolio project"
        self.headers = {
            "User-Agent": f"{agent}; mailto:{contact})" if contact else f"{agent})",
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_api(self, url: str) -> str:
        cached = self.cache.read_api(url)
        if cached is not None:
            return cached
        content = self._get(url)
        self.cache.write_api(url, content)
        return content

    def get_html(self, external_id: str, version: int) -> str | None:
        cached = self.cache.read_html(external_id, version)
        if cached is not None:
            return cached
        if self.cache.html_is_missing(external_id, version):
            return None
        url = f"https://arxiv.org/html/{external_id}v{version}"
        try:
            content = self._get(url)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                self.cache.mark_missing(external_id, version)
                return None
            raise
        self.cache.write_html(external_id, version, content)
        return content

    def _get(self, url: str) -> str:
        retry_after = 0.0
        for attempt in range(len(RETRY_DELAYS) + 1):
            if retry_after:
                self._sleep(retry_after)
                retry_after = 0.0
            self.limiter.wait()
            try:
                self.network_requests += 1
                response = self._client.get(url, headers=self.headers)
            except httpx.ConnectError:
                self.limiter.finished()
                if attempt == len(RETRY_DELAYS):
                    raise
                retry_after = RETRY_DELAYS[attempt]
                continue
            self.limiter.finished()
            if response.status_code in RETRYABLE_STATUSES and attempt < len(RETRY_DELAYS):
                retry_after = max(
                    RETRY_DELAYS[attempt],
                    _retry_after_seconds(response.headers.get("Retry-After")),
                )
                continue
            response.raise_for_status()
            return response.text
        raise AssertionError("unreachable")


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0
