"""Send ten realistic rerank requests and report server-measured latency percentiles."""

import argparse
import json
import os
from itertools import cycle, islice
from pathlib import Path
from statistics import median
from urllib.request import Request, urlopen

REQUESTS = 10
PASSAGES = 20
DEFAULT_WORDS = 225  # about 384 tokens for the fixture text, the real chunk size
URL = os.environ.get("RERANKER_URL", "http://localhost:8081/rerank")
FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "passages.txt"


def percentile_95(values: list[float]) -> float:
    """Use the nearest-rank p95, appropriate for the fixed ten-request probe."""

    ordered = sorted(values)
    return ordered[round(0.95 * len(ordered) + 0.499999) - 1]


def request_body(words: int) -> bytes:
    fixture_words = FIXTURE.read_text(encoding="utf-8").split()
    long_text = " ".join(islice(cycle(fixture_words), words))
    payload = {
        "query": "Which rerankers were evaluated on BEIR?",
        "passages": [{"id": str(index), "text": long_text} for index in range(PASSAGES)],
        "top_k": PASSAGES,
    }
    return json.dumps(payload).encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--words",
        type=int,
        default=DEFAULT_WORDS,
        help="words per passage (225 is about 384 tokens; 880 saturates max_length 512)",
    )
    payload = request_body(parser.parse_args().words)
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
