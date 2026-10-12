"""HTTP client for the query service (contract C6)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class ServiceError(RuntimeError):
    """The service could not be reached or `/v1/info` failed."""


@dataclass
class QueryResult:
    e2e_ms: float
    status: int | None  # None when the request never completed (timeout, connection error)
    body: dict[str, Any] | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None


class ServiceClient:
    def __init__(
        self, base_url: str, *, timeout_s: float = 120.0, client: httpx.Client | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=5.0))

    def info(self) -> dict[str, Any]:
        try:
            resp = self._client.get(f"{self.base_url}/v1/info", timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ServiceError(f"cannot read {self.base_url}/v1/info: {exc}") from exc

    def query(
        self, question: str, pipeline: dict[str, Any], *, timeout_s: float | None = None
    ) -> QueryResult:
        """POST /v1/query with the config's pipeline verbatim; never raises."""
        payload = {"question": question, "pipeline": pipeline}
        start = time.perf_counter()
        try:
            timeout = (
                httpx.Timeout(timeout_s, connect=5.0) if timeout_s else httpx.USE_CLIENT_DEFAULT
            )
            resp = self._client.post(f"{self.base_url}/v1/query", json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            return QueryResult(
                (time.perf_counter() - start) * 1000, None, None, f"{type(exc).__name__}: {exc}"
            )
        e2e_ms = (time.perf_counter() - start) * 1000
        if resp.status_code != 200:
            return QueryResult(
                e2e_ms, resp.status_code, None, f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return QueryResult(e2e_ms, 200, resp.json(), None)
        except ValueError as exc:
            return QueryResult(e2e_ms, 200, None, f"invalid JSON body: {exc}")
