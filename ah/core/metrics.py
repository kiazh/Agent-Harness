"""Metrics tracking — latency histograms, throughput counters, error rates, token usage."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("ah.metrics")


class JsonFormatter(logging.Formatter):
    """Format log records as structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "getMessage",
                "exc_info",
                "exc_text",
                "stack_info",
            }:
                log_data[key] = value
        return json.dumps(log_data, default=str)


class MetricsCollector:
    """Thread-safe metrics collector for latency, throughput, errors, and token usage."""

    _MAX_LATENCY_SAMPLES = 10_000  # Cap per-operation latency list size

    def __init__(self) -> None:
        # Reentrant: get_all_metrics() holds the lock while calling
        # get_latency_stats()/get_error_rate(), which lock again. A plain Lock
        # deadlocked there as soon as any metric had been recorded.
        self._lock = threading.RLock()
        self._latencies: dict[str, list[float]] = defaultdict(list)
        self._latency_totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "sum": 0.0}
        )
        self._counters: dict[str, int] = defaultdict(int)
        self._errors: dict[str, int] = defaultdict(int)
        self._token_usage: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._token_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def record_latency(self, operation: str, duration_ms: float) -> None:
        """Record a latency measurement in milliseconds."""
        with self._lock:
            latencies = self._latencies[operation]
            latencies.append(duration_ms)
            self._latency_totals[operation]["count"] += 1
            self._latency_totals[operation]["sum"] += duration_ms
            # Cap the list size to prevent unbounded memory growth
            if len(latencies) > self._MAX_LATENCY_SAMPLES:
                # Keep the most recent half to preserve recent latency data
                self._latencies[operation] = latencies[-self._MAX_LATENCY_SAMPLES // 2 :]

    def increment_counter(self, operation: str, value: int = 1) -> None:
        """Increment a throughput counter."""
        with self._lock:
            self._counters[operation] += value

    def record_error(self, operation: str) -> None:
        """Record an error for an operation."""
        with self._lock:
            self._errors[operation] += 1

    def record_tokens(self, session_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Record token usage for a session."""
        with self._lock:
            tu = self._token_usage.setdefault(
                session_id,
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
            self._token_usage.move_to_end(session_id)
            if len(self._token_usage) > 10_000:
                self._token_usage.popitem(last=False)
            tu["prompt_tokens"] += prompt_tokens
            tu["completion_tokens"] += completion_tokens
            tu["total_tokens"] += prompt_tokens + completion_tokens
            self._token_totals["prompt_tokens"] += prompt_tokens
            self._token_totals["completion_tokens"] += completion_tokens
            self._token_totals["total_tokens"] += prompt_tokens + completion_tokens

    def record_llm_call(
        self,
        session_id: str,
        model: str,
        duration_ms: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        is_error: bool = False,
    ) -> None:
        """Record a complete LLM call with all metrics."""
        self.record_latency(f"llm.{model}", duration_ms)
        self.increment_counter(f"llm.{model}.calls")
        if is_error:
            self.record_error(f"llm.{model}")
        if session_id and (prompt_tokens or completion_tokens):
            self.record_tokens(session_id, prompt_tokens, completion_tokens)

    def record_db_call(self, operation: str, duration_ms: float, is_error: bool = False) -> None:
        """Record a database call with timing."""
        self.record_latency(f"db.{operation}", duration_ms)
        self.increment_counter(f"db.{operation}.calls")
        if is_error:
            self.record_error(f"db.{operation}")

    def get_latency_stats(self, operation: str) -> dict[str, float]:
        """Get latency statistics for an operation."""
        with self._lock:
            latencies = self._latencies.get(operation, [])
        if not latencies:
            return {
                "count": 0,
                "min": 0.0,
                "max": 0.0,
                "mean": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
            }
        sorted_lat = sorted(latencies)
        n = len(sorted_lat)
        return {
            "count": n,
            "min": sorted_lat[0],
            "max": sorted_lat[-1],
            "mean": sum(sorted_lat) / n,
            "p50": sorted_lat[min(int(n * 0.50), n - 1)],
            "p95": sorted_lat[min(int(n * 0.95), n - 1)],
            "p99": sorted_lat[min(int(n * 0.99), n - 1)],
        }

    def get_counter(self, operation: str) -> int:
        """Get counter value for an operation."""
        with self._lock:
            return self._counters.get(operation, 0)

    def get_error_count(self, operation: str) -> int:
        """Get error count for an operation."""
        with self._lock:
            return self._errors.get(operation, 0)

    def get_error_rate(self, operation: str) -> float:
        """Get error rate for an operation (0.0 to 1.0)."""
        with self._lock:
            total = self._counters.get(operation, 0)
            errors = self._errors.get(operation, 0)
        return errors / total if total > 0 else 0.0

    def get_token_usage(self, session_id: str) -> dict[str, int]:
        """Get token usage for a session."""
        with self._lock:
            return dict(self._token_usage.get(session_id, {}))

    def get_all_metrics(self) -> dict[str, Any]:
        """Get all metrics as a dictionary."""
        with self._lock:
            operations = (
                set(self._latencies.keys()) | set(self._counters.keys()) | set(self._errors.keys())
            )
            return {
                "latencies": {op: self.get_latency_stats(op) for op in operations},
                "latency_totals": {op: dict(value) for op, value in self._latency_totals.items()},
                "counters": dict(self._counters),
                "errors": dict(self._errors),
                "error_rates": {op: self.get_error_rate(op) for op in operations},
                "token_usage": {sid: dict(tu) for sid, tu in self._token_usage.items()},
                "token_totals": dict(self._token_totals),
            }

    def reset(self) -> None:
        """Reset all metrics."""
        with self._lock:
            self._latencies.clear()
            self._latency_totals.clear()
            self._counters.clear()
            self._errors.clear()
            self._token_usage.clear()
            self._token_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# Global singleton
metrics = MetricsCollector()


@contextmanager
def timed(operation: str) -> Generator[None, None, None]:
    """Context manager to time an operation and record metrics."""
    start = time.monotonic()
    try:
        yield
    except Exception:
        metrics.record_error(operation)
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        metrics.record_latency(operation, duration_ms)
        metrics.increment_counter(f"{operation}.calls")


def setup_structured_logging(level: int = logging.INFO) -> None:
    """Set up structured JSON logging for the ah.metrics logger."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
