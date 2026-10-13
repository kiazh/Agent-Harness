"""Sanitized failure diagnostics for optional fallback and cleanup paths."""

import logging
import time

logger = logging.getLogger(__name__)

#: Minimum seconds between warning lines for the same boundary key. Every
#: call still increments the failure metric; only the log line is throttled.
FLOOD_INTERVAL_SECONDS = 10.0

_last_log_at: dict[str, float] = {}


def record_failure(boundary: str, error: BaseException) -> None:
    from machina.core.metrics import metrics

    metrics.increment_counter("diagnostic.failures")
    key = str(boundary)
    now = time.monotonic()
    if now - _last_log_at.get(key, float("-inf")) < FLOOD_INTERVAL_SECONDS:
        # A failing optional boundary can fire per request; never flood logs.
        return
    _last_log_at[key] = now
    logger.warning("boundary failed: %s error_type=%s", key, type(error).__name__)
