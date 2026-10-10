"""Structured stdout logging for request-level reranker events."""

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format record extras as one compact JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
        }
        for key in (
            "n_passages",
            "top_k",
            "queue_ms",
            "inference_ms",
            "latency_ms",
            "model",
            "load_seconds",
        ):
            value = getattr(record, key, None)
            if value is not None:
                data[key] = value
        return json.dumps(data, separators=(",", ":"), sort_keys=True)


def configure_logging() -> logging.Logger:
    """Configure the service logger once and return it."""

    logger = logging.getLogger("reranker")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
