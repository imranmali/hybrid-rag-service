from indexer.arxiv.ratelimit import RateLimiter


def test_requests_are_spaced_from_previous_completion() -> None:
    now = [0.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(clock=lambda: now[0], sleep=sleep)
    limiter.wait()
    limiter.finished()
    now[0] += 1.0
    limiter.wait()

    assert waits == [2.0]
