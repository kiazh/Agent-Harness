"""OpenTelemetry spans when an SDK is configured by the host application."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

try:
    from opentelemetry import trace
except ImportError:  # Optional outside deployments that configure tracing.
    trace = None


@contextmanager
def span(name: str, **attributes: str) -> Iterator[None]:
    if trace is None:
        yield
        return
    tracer = trace.get_tracer("agent-harness")
    with tracer.start_as_current_span(name, attributes=attributes):
        yield
