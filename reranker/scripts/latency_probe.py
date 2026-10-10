"""Send ten realistic rerank requests and report server-measured latency percentiles."""

import json
import os
from pathlib import Path
from statistics import median
from urllib.request import Request, urlopen

REQUESTS = 10
PASSAGES = 20
URL = os.environ.get("RERANKER_URL", "http://localhost:8081/rerank")
FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "passages.txt"


def percentile_95(values: list[float]) -> float:
    """Use the nearest-rank p95, appropriate for the fixed ten-request probe."""

    ordered = sorted(values)
    return ordered[round(0.95 * len(ordered) + 0.499999) - 1]


def request_body() -> bytes:
    text = FIXTURE.read_text(encoding="utf-8")
    long_text = " ".join([text] * 7)
    payload = {
        "query": "Which rerankers were evaluated on BEIR?",
        "passages": [{"id": str(index), "text": long_text} for index in range(PASSAGES)],
        "top_k": PASSAGES,
    }
    return json.dumps(payload).encode()


def main() -> None:
    payload = request_body()
    samples: list[float] = []
    for _ in range(REQUESTS):
        request = Request(
            URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=120) as response:
            samples.append(float(json.load(response)["latency_ms"]))
    print(
        json.dumps(
            {"requests": REQUESTS, "p50_ms": median(samples), "p95_ms": percentile_95(samples)}
        )
    )


if __name__ == "__main__":
    main()
