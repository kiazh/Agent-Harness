---
name: observability-otel
description: "Observability with OpenTelemetry — traces, spans, metrics, logs correlation, context propagation, sampling, SLOs, dashboards"
triggers:
  - "observability"
  - "opentelemetry"
  - "otel"
  - "tracing"
  - "spans"
  - "metrics"
  - "dashboards"
  - "slo"
version: 1.0.0
---

# Observability with OpenTelemetry

## Core Skills

| Skill | Practice |
|---|---|
| Tracing | Wrap a request path in spans with attributes and status |
| Metrics | Counters, histograms and gauges for latency and errors |
| Context propagation | Carry the W3C trace context across services and async tasks |
| Sampling | Head sampling by default; tail sampling for errors and slow requests |
| Resource attributes | service.name, deployment.environment on every signal |
| Log correlation | Attach trace_id and span_id to structured log records |
| SLOs | Derive error budgets from availability and latency objectives |
| Dashboards | RED (rate, errors, duration) and USE panels with actionable alerts |

## Pitfalls
- Registering the tracer after requests start silently drops early spans.
- High-cardinality metric labels and unbounded span attributes blow up storage.
- Fire-and-forget exporters lose buffered data on shutdown; flush before exit.
- Metrics without units or explicit buckets make downstream alerts noisy.

## Practice Project
Instrument one service end to end: emit spans around the handler and outbound calls, record a latency histogram, export over OTLP to a local collector, then build a RED dashboard and one SLO with an error-budget alert.

## Key Resources
- [OpenTelemetry docs](https://opentelemetry.io/docs/)
- [OTLP specification](https://opentelemetry.io/docs/specs/otlp/)
- [Google SRE Workbook — Implementing SLOs](https://sre.google/workbook/implementing-slos/)
