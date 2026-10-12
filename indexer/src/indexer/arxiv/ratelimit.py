"""A process-wide polite request limiter."""

import time
from collections.abc import Callable


class RateLimiter:
    """Space requests from the end of one request to the start of the next."""

    def __init__(
        self,
        min_interval: float = 3.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_finished: float | None = None

    def wait(self) -> None:
        """Wait before a network request; cache hits must not call this method."""
        if self._last_finished is None:
            return
        remaining = self.min_interval - (self._clock() - self._last_finished)
        if remaining > 0:
            self._sleep(remaining)

    def finished(self) -> None:
        """Record the end time of a network request, including unsuccessful ones."""
        self._last_finished = self._clock()
