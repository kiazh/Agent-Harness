"""Sanitized failure diagnostics for optional fallback and cleanup paths."""

import logging

logger = logging.getLogger(__name__)


def record_failure(boundary: str, error: BaseException) -> None:
    from ah.core.metrics import metrics

    metrics.increment_counter("diagnostic.failures")
    logger.warning("boundary failed: %s error_type=%s", boundary, type(error).__name__)
