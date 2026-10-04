# AgentHarness: Production Observability Research

**Date:** 2026-10-01  
**Scope:** What production observability looks like for an AI agent framework, mapped to AgentHarness's current architecture.

---

## Executive Summary

AgentHarness has **audit logging** (sanitized JSON events to stderr via the `ah.audit` logger, persisted asynchronously to the `audit_events` table) and **standard Python logging** (`logging.getLogger(__name__)`). This still falls short of production observability in the five key areas below:

1. **No distributed tracing** — a single agent run fans out to 10–30+ LLM calls and tool executions with no causal chain.
2. **No metrics** — token counts, latency, cost, and success rates are logged but never aggregated.
3. **No structured log schema** — audit events are ad-hoc JSON with no consistent envelope.
4. **No cost tracking** — token usage is summed per run but never attributed per-model, per-session, or per-user.
5. **No alerting or dashboards** — there is no way to detect anomalies or visualize trends.

This document maps industry best practices to AgentHarness's specific architecture and proposes a phased adoption plan.

---

## 1. Structured Logging

### Current State

AgentHarness has structured logging plus a durable, sanitized audit trail:

```python
# ah/core/provider.py — audit logger (JSON lines to stderr, plus persistence)
_audit_logger = logging.getLogger("ah.audit")
def audit_log(event_type: str, **kwargs) -> None:
    entry = {"timestamp": time.time(), "event": event_type, **kwargs}
    _audit_logger.info(json.dumps(entry, default=str))

# ah/core/agent.py — standard library logging
logger = logging.getLogger(__name__)
logger.warning("LLM call failed (attempt %d/%d): %s — retrying in %ds", ...)
```

**What works:** The audit logger already emits JSON with `event_type` and `session_id`. This is the right instinct.

**What's missing:**
- No consistent envelope (every call site invents its own fields).
- No `trace_id` / `span_id` correlation.
- No log levels for audit events (everything is INFO).
- No PII redaction — `tool_args` and `result_preview` are logged verbatim.
- No output destination — stderr + DB only, no file rotation, no log shipping.

### Industry Standard: The Log Envelope

Production agent systems use a **structured log envelope** — a consistent JSON schema for every log event:

```json
{
  "timestamp": "2026-10-01T15:30:00.123Z",
  "level": "info",
  "trace_id": "tr_8f3a2b1c",
  "span_id": "st_001",
  "agent_id": "harness",
  "session_id": "sess_abc123",
  "event_type": "tool_call",
  "tool": "web_search",
  "duration_ms": 342,
  "status": "success",
  "tokens": {"input": 1243, "output": 87},
  "cost_usd": 0.0024,
  "model": "claude-sonnet-4-6"
}
```

**Required fields for AgentHarness:**

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO-8601 | Event time with millisecond precision |
| `level` | string | `debug`, `info`, `warning`, `error` |
| `trace_id` | string | Unique ID for the entire agent run |
| `span_id` | string | Unique ID for this specific step |
| `agent_id` | string | Which agent is executing |
| `session_id` | string | Which session this belongs to |
| `event_type` | string | `llm_call`, `tool_call`, `decision`, `error`, `completion` |
| `duration_ms` | int | Wall-clock duration of this step |
| `status` | string | `success`, `error`, `timeout`, `retry` |

**Recommended fields:**

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | LLM model identifier |
| `tokens_in` | int | Input tokens for this call |
| `tokens_out` | int | Output tokens for this call |
| `cost_usd` | float | Estimated cost in USD |
| `tool_name` | string | Tool name (if event_type is tool_call) |
| `tool_input_hash` | string | SHA-256 of tool input (for deduplication without PII) |
| `retry_count` | int | How many times this step was retried |
| `parent_span_id` | string | For nested agent calls |

### PII Redaction

AgentHarness logs `tool_args` and `result_preview` verbatim. In production, these contain user data, API keys, and PII. The industry standard is:

1. **Hash identifiers** — replace user IDs, session IDs with HMAC hashes.
2. **Truncate content** — log first 200 chars, hash the rest.
3. **Redact patterns** — regex-based redaction for emails, phone numbers, API keys, credit cards.
4. **TTL-based retention** — full prompts stored for 7 days, aggregated metrics for 30 days.

### Proposed Implementation for AgentHarness

```python
# ah/observability/logging.py
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any

class StructuredLogger:
    """Consistent log envelope for all agent events."""
    
    def __init__(self, agent_id: str, session_id: str, trace_id: str | None = None):
        self.agent_id = agent_id
        self.session_id = session_id
        self.trace_id = trace_id or f"tr_{uuid.uuid4().hex[:16]}"
        self._span_counter = 0
    
    def _next_span_id(self) -> str:
        self._span_counter += 1
        return f"st_{self._span_counter:04d}"
    
    def log(self, event_type: str, level: str = "info", **kwargs) -> dict:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.%f", time.gmtime())[:-3] + "Z",
            "level": level,
            "trace_id": self.trace_id,
            "span_id": self._next_span_id(),
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "event_type": event_type,
            **kwargs,
        }
        _audit_logger.log(getattr(logging, level.upper()), json.dumps(entry, default=str))
        return entry
    
    def llm_call(self, model: str, tokens_in: int, tokens_out: int, 
                 duration_ms: int, status: str, cost_usd: float = 0.0) -> dict:
        return self.log("llm_call", model=model, tokens_in=tokens_in, 
                       tokens_out=tokens_out, duration_ms=duration_ms, 
                       status=status, cost_usd=cost_usd)
    
    def tool_call(self, tool_name: str, duration_ms: int, status: str,
                  tool_input_hash: str = "") -> dict:
        return self.log("tool_call", tool=tool_name, duration_ms=duration_ms,
                       status=status, tool_input_hash=tool_input_hash)
```

---

## 2. Metrics

### Current State

AgentHarness aggregates in-process metrics (`ah/core/metrics.py`) and exposes them as Prometheus text at `/metrics`, including per-operation latency, counters, error rates, and token totals. Open questions remain around histograms over long windows and cross-restart aggregation.

- What is the p95 latency for a tool call?
- Which model costs the most per session?
- What is the tool failure rate over the last hour?
- How many sessions hit the token budget?

### Industry Standard: The Six Metric Categories

Production agent systems track six categories of metrics:

#### 2.1 Cost Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `agent.cost.total` | Histogram | Total cost per agent run (USD) |
| `agent.cost.by_model` | Counter | Cost attributed per LLM model |
| `agent.cost.by_session` | Counter | Cost attributed per session |
| `agent.tokens.total` | Histogram | Total tokens per agent run |
| `agent.tokens.input` | Histogram | Input tokens per LLM call |
| `agent.tokens.output` | Histogram | Output tokens per LLM call |
| `agent.tokens.cache_hit` | Counter | Cache hit tokens (if using prompt caching) |

#### 2.2 Performance Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `agent.latency.e2e` | Histogram | End-to-end agent run latency |
| `agent.latency.llm` | Histogram | Per-LLM-call latency |
| `agent.latency.tool` | Histogram | Per-tool-call latency |
| `agent.latency.ttft` | Histogram | Time to first token (streaming) |
| `agent.iterations` | Histogram | Number of ReAct loop iterations |

#### 2.3 Reliability Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `agent.errors.total` | Counter | Total errors by type |
| `agent.errors.by_type` | Counter | Errors categorized: `llm_error`, `tool_error`, `timeout`, `rate_limit` |
| `agent.retries.total` | Counter | Total retry attempts |
| `agent.retries.success` | Counter | Retries that eventually succeeded |
| `agent.budget_exceeded` | Counter | Runs that hit `MAX_TOKEN_BUDGET` |
| `agent.max_iterations` | Counter | Runs that hit `max_iterations` |

#### 2.4 Quality Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `agent.tool_success_rate` | Gauge | Percentage of tool calls that succeed |
| `agent.tool_call_count` | Counter | Tool calls per run |
| `agent.replan_rate` | Gauge | Percentage of runs where the agent changed its approach |
| `agent.loop_detected` | Counter | Runs where the same tool was called with the same args 3+ times |

#### 2.5 Business Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `agent.task_completion_rate` | Gauge | Percentage of runs that complete successfully |
| `agent.human_escalation_rate` | Gauge | Percentage of runs requiring human intervention |
| `agent.session_count` | Counter | Active sessions |
| `agent.user_satisfaction` | Gauge | Thumbs up/down ratio |

#### 2.6 Token Usage Analytics

| Metric | Type | Description |
|--------|------|-------------|
| `agent.tokens.per_session` | Histogram | Total tokens consumed per session |
| `agent.tokens.per_run` | Histogram | Total tokens consumed per run |
| `agent.tokens.by_tool` | Counter | Tokens consumed by tool results |
| `agent.tokens.by_context` | Counter | Tokens consumed by context assembly |
| `agent.tokens.efficiency` | Gauge | Output tokens / input tokens ratio |

### Proposed Implementation for AgentHarness

AgentHarness should use **OpenTelemetry Metrics** (the standard) with a Prometheus exporter for local development and an OTLP exporter for production:

```python
# ah/observability/metrics.py
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.prometheus import PrometheusMetricReader

# Initialize once at startup
reader = PrometheusMetricReader()  # Or OTLPMetricReader for production
provider = MeterProvider(metric_readers=[reader])
metrics.set_meter_provider(provider)

meter = metrics.get_meter("agent_harness")

# Define instruments
token_histogram = meter.create_histogram(
    "agent.tokens.total",
    description="Total tokens consumed per agent run",
    unit="tokens",
)
cost_histogram = meter.create_histogram(
    "agent.cost.usd",
    description="Estimated cost in USD per agent run",
    unit="USD",
)
latency_histogram = meter.create_histogram(
    "agent.latency.e2e",
    description="End-to-end agent run latency",
    unit="ms",
)
iteration_histogram = meter.create_histogram(
    "agent.iterations",
    description="Number of ReAct loop iterations",
    unit="iterations",
)
error_counter = meter.create_counter(
    "agent.errors.total",
    description="Total errors by type",
)
tool_success_gauge = meter.create_gauge(
    "agent.tool_success_rate",
    description="Percentage of tool calls that succeed",
)
```

---

## 3. Tracing

### Current State

AgentHarness has OpenTelemetry spans around agent runs and tool calls when an SDK is configured. A single `ReActAgent.run()` call fans out to:
- 1 session lookup
- 1 context retrieval
- 1 prompt assembly
- N LLM calls (with retries)
- M tool executions
- N context chunk insertions

All of these are correlated only by `session_id` in audit logs. There is no causal chain, no parent-child relationship, no way to reconstruct the execution tree.

### Industry Standard: OpenTelemetry GenAI Semantic Conventions

The OpenTelemetry community has defined **GenAI semantic conventions** (stable in 2024–2025) that standardize span attributes for LLM calls:

| Attribute | Description |
|-----------|-------------|
| `gen_ai.system` | Provider name (`openai`, `anthropic`, `openrouter`) |
| `gen_ai.request.model` | Model identifier |
| `gen_ai.request.temperature` | Temperature parameter |
| `gen_ai.request.max_tokens` | Max tokens parameter |
| `gen_ai.usage.input_tokens` | Input token count |
| `gen_ai.usage.output_tokens` | Output token count |
| `gen_ai.response.finish_reason` | `stop`, `length`, `tool_calls`, `content_filter` |
| `gen_ai.response.tool_calls` | Tool calls requested by the model |
| `gen_ai.agent.name` | Agent identifier |
| `gen_ai.session.id` | Session identifier |

### What a Good Agent Trace Looks Like

```
User Request (trace_id: tr_8f3a2b1c)
├── Orchestrator Agent (span: 4200ms)
│   ├── LLM Planning Call (span: 1100ms, tokens: 450 in / 87 out)
│   ├── Research Agent (span: 2800ms)
│   │   ├── LLM Call (span: 800ms, tokens: 2100 in / 340 out)
│   │   ├── Web Search Tool (span: 1200ms)
│   │   └── LLM Summarization (span: 600ms, tokens: 1800 in / 200 out)
│   └── LLM Final Response (span: 900ms, tokens: 680 in / 150 out)
└── Response Delivered (total: 4200ms, total_tokens: 5030)
```

### Proposed Implementation for AgentHarness

AgentHarness should instrument the ReAct loop with OpenTelemetry spans:

```python
# ah/observability/tracing.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

# Initialize once at startup
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("agent_harness")

# In ReActAgent.run():
async def run(self, session_id, user_message, verbose=True):
    with tracer.start_as_current_span("agent.run") as root_span:
        root_span.set_attribute("gen_ai.agent.name", self.agent_id)
        root_span.set_attribute("gen_ai.session.id", str(session_id))
        root_span.set_attribute("agent.max_iterations", self.max_iterations)
        
        for iteration in range(self.max_iterations):
            with tracer.start_as_current_span(f"llm.call[{iteration}]") as llm_span:
                llm_span.set_attribute("gen_ai.request.model", self.provider.model)
                llm_span.set_attribute("gen_ai.request.temperature", 0.7)
                
                response = await self._call_llm_with_retry(messages, tools)
                
                llm_span.set_attribute("gen_ai.usage.input_tokens", 
                                       response.usage.get("prompt_tokens", 0))
                llm_span.set_attribute("gen_ai.usage.output_tokens", 
                                       response.usage.get("completion_tokens", 0))
                llm_span.set_attribute("gen_ai.response.finish_reason", 
                                       response.raw.get("choices", [{}])[0].get("finish_reason", "unknown"))
            
            for tc in response.tool_calls:
                with tracer.start_as_current_span(f"tool.{tc['function']['name']}") as tool_span:
                    tool_span.set_attribute("tool.name", tc["function"]["name"])
                    tool_span.set_attribute("tool.args", json.dumps(tool_args))
                    
                    result = await registry.execute(tool_name, **tool_args)
                    
                    tool_span.set_attribute("tool.result_preview", str(result)[:200])
                    tool_span.set_attribute("tool.duration_ms", duration_ms)
```

---

## 4. Dashboards

### Current State

AgentHarness has **no dashboards**. Metrics are exposed as Prometheus text; audit events persist to PostgreSQL.

### Industry Standard: The Three-Layer Dashboard

Production agent systems use a three-layer dashboard architecture:

#### Layer 1: Business KPIs (Executive View)

| Panel | Metric | Target |
|-------|--------|--------|
| Task completion rate | `agent.task_completion_rate` | > 95% |
| Average task duration | `agent.latency.e2e` (p50) | < 60s |
| Cost per task | `agent.cost.total` (p50) | < $0.15 |
| User satisfaction | `agent.user_satisfaction` | > 4.0/5.0 |

#### Layer 2: System Health (Engineering View)

| Panel | Metric | Alert Threshold |
|-------|--------|-----------------|
| Success rate by agent | `agent.task_completion_rate` by `agent_id` | < 90% |
| Failure rate by tool | `agent.errors.total` by `tool_name` | > 5% |
| Token usage trend | `agent.tokens.total` over time | > 2x baseline |
| Cost trend | `agent.cost.total` over time | > 1.5x baseline |
| Latency percentiles | `agent.latency.e2e` p50/p95/p99 | p99 > 5x p50 |

#### Layer 3: Cost and Resources (Finance View)

| Panel | Metric | Description |
|-------|--------|-------------|
| Total cost today | `agent.cost.total` (sum) | Daily spend |
| Cost by model | `agent.cost.by_model` | Which models cost the most |
| Cost by session | `agent.cost.by_session` | Which sessions cost the most |
| Token efficiency | `agent.tokens.efficiency` | Output/input ratio |
| Budget burn rate | `agent.cost.total` / budget | Days until budget exhausted |

### Proposed Implementation for AgentHarness

AgentHarness should use **Grafana** (open source) with Prometheus as the metrics backend:

```yaml
# docker-compose.observability.yml
version: "3.8"
services:
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./observability/prometheus.yml:/etc/prometheus/prometheus.yml
  
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    volumes:
      - ./observability/dashboards:/etc/grafana/provisioning/dashboards
      - ./observability/datasources:/etc/grafana/provisioning/datasources
  
  # Optional: Jaeger for trace visualization
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"
```

---

## 5. Alerting

### Current State

AgentHarness has **no alerting**. Errors are recorded in metrics and persisted audit events.

### Industry Standard: The Three-Tier Alert Framework

Production agent systems use a three-tier alert framework:

#### Tier 1: Critical — Immediate Response (PagerDuty/Opsgenie)

| Alert | Condition | Response |
|-------|-----------|----------|
| Agent error rate spike | `agent.errors.total` > 10% (5-min avg) | Page on-call |
| Cost budget exceeded | `agent.cost.total` > 200% of hourly budget | Page on-call |
| Tool failure storm | Specific tool fails 3x consecutively in 5 min | Page on-call |
| Agent loop detected | Same tool called with same args 3+ times | Page on-call |

#### Tier 2: Warning — Daily Review (Slack)

| Alert | Condition | Response |
|-------|-----------|----------|
| Agent success rate drop | `agent.task_completion_rate` drops > 5% vs previous day | Slack notification |
| Latency increase | `agent.latency.e2e` p95 increases > 50% from baseline | Slack notification |
| New error type | Unseen `error_type` in last 24 hours | Slack notification |
| Token usage spike | `agent.tokens.total` > 3x baseline | Slack notification |

#### Tier 3: Info — Weekly Report (Email)

| Alert | Condition | Response |
|-------|-----------|----------|
| Cost trend | `agent.cost.total` trending up over 7 days | Weekly email |
| Usage pattern shift | New tool usage patterns detected | Weekly email |
| Prompt efficiency change | `agent.tokens.efficiency` changing over time | Weekly email |

### The 4% Silent Failure Rule

Research shows that if an agent's silent failure rate is under 4%, it won't show up in customer complaints. Between 1% and 4% is the danger zone — most teams are there and don't know it. **Outcome metrics** (task completion rate, correction rate) are the only way to detect this.

### Proposed Implementation for AgentHarness

```python
# ah/observability/alerting.py
from dataclasses import dataclass
from typing import Callable

@dataclass
class AlertRule:
    name: str
    condition: Callable[[dict], bool]
    severity: str  # "critical", "warning", "info"
    cooldown_minutes: int = 10

ALERT_RULES = [
    AlertRule(
        name="agent_error_rate_spike",
        condition=lambda m: m.get("error_rate_5m", 0) > 0.10,
        severity="critical",
    ),
    AlertRule(
        name="cost_budget_exceeded",
        condition=lambda m: m.get("hourly_cost", 0) > m.get("hourly_budget", 10.0) * 2,
        severity="critical",
    ),
    AlertRule(
        name="agent_loop_detected",
        condition=lambda m: m.get("max_loop_count", 0) > 3,
        severity="critical",
    ),
    AlertRule(
        name="success_rate_drop",
        condition=lambda m: m.get("success_rate", 1.0) < m.get("baseline_success_rate", 0.95) * 0.95,
        severity="warning",
    ),
    AlertRule(
        name="latency_increase",
        condition=lambda m: m.get("p95_latency", 0) > m.get("baseline_p95_latency", 0) * 1.5,
        severity="warning",
    ),
]
```

---

## 6. Cost Tracking

### Current State

AgentHarness tracks `total_tokens` per run and durable per-session/per-agent usage in `llm_usage`, but does **not** calculate dollar cost. The `LLMResponse.usage` dict contains token counts but no cost estimation.

### Industry Standard: Per-Span Cost Attribution

Production agent systems track cost at multiple granularities:

| Level | What to Track | Why |
|-------|---------------|-----|
| Per LLM call | `cost_usd = (input_tokens × input_price) + (output_tokens × output_price)` | Identify expensive calls |
| Per tool call | `cost_usd = (tool_result_tokens × input_price)` | Identify expensive tools |
| Per agent run | `sum(all LLM call costs)` | Budget per task |
| Per session | `sum(all run costs)` | Budget per user session |
| Per user | `sum(all session costs)` | Budget per user |
| Per model | `sum(all costs for model)` | Model selection optimization |

### Pricing Table (Example)

| Model | Input $/1M tokens | Output $/1M tokens |
|-------|-------------------|-------------------|
| `anthropic/claude-3.5-sonnet` | $3.00 | $15.00 |
| `anthropic/claude-3-haiku` | $0.25 | $1.25 |
| `openai/gpt-4o` | $2.50 | $10.00 |
| `openai/gpt-4o-mini` | $0.15 | $0.60 |

### Proposed Implementation for AgentHarness

```python
# ah/observability/cost.py
from dataclasses import dataclass

# Pricing table — update regularly
MODEL_PRICING = {
    "anthropic/claude-3.5-sonnet": {"input": 3.00, "output": 15.00},
    "anthropic/claude-3-haiku": {"input": 0.25, "output": 1.25},
    "openai/gpt-4o": {"input": 2.50, "output": 10.00},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
}

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for an LLM call."""
    pricing = MODEL_PRICING.get(model, {"input": 0.0, "output": 0.0})
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

# In LLMProvider.complete():
async def complete(self, messages, model=None, ...):
    response = await self._make_request(messages, model, ...)
    
    # Calculate cost
    cost = estimate_cost(
        model or self.model,
        response.usage.get("prompt_tokens", 0),
        response.usage.get("completion_tokens", 0),
    )
    
    # Add cost to response
    response.cost_usd = cost
    
    return response
```

---

## 7. Token Usage Analytics

### Current State

AgentHarness tracks `total_tokens` per run, durable `llm_usage`, and stores `token_count` per context chunk, but has **no token analytics** — no breakdown by component, no trend analysis, no efficiency metrics.

### Industry Standard: Token Flow Analysis

A typical agent run consumes tokens in four stages:

```
User Input (100 tokens)
    ↓
Context Assembly (2,000 tokens) ← retrieved chunks + recent context
    ↓
LLM Call (2,100 in → 500 out) ← prompt + response
    ↓
Tool Execution (0 tokens) ← tool calls don't consume tokens directly
    ↓
Tool Result (1,000 tokens) ← tool output added to context
    ↓
Next LLM Call (3,100 in → 500 out) ← accumulated context
    ↓
Final Response (500 tokens)
```

**Total: 5,700 input tokens, 1,000 output tokens**

### Key Token Analytics

| Metric | Description | Why It Matters |
|--------|-------------|----------------|
| `tokens.per.run` | Total tokens per agent run | Cost tracking |
| `tokens.per.session` | Total tokens per session | Session budget |
| `tokens.by.context` | Tokens consumed by context assembly | Context efficiency |
| `tokens.by.tool_results` | Tokens consumed by tool results | Tool output size |
| `tokens.by.system_prompt` | Tokens consumed by system prompt | Prompt optimization |
| `tokens.efficiency` | Output / input ratio | Agent effectiveness |
| `tokens.wasted` | Tokens from truncated or irrelevant context | Context quality |

### The Token Budget Problem

AgentHarness has `MAX_TOKEN_BUDGET = 50_000` and `context_budget = 8000` per session. But there is no tracking of:
- How often the budget is exceeded
- Which component consumes the most tokens
- Whether the budget is appropriate for the task

### Proposed Implementation for AgentHarness

```python
# ah/observability/tokens.py
@dataclass
class TokenBreakdown:
    """Token usage breakdown for a single agent run."""
    user_input: int = 0
    system_prompt: int = 0
    context_assembly: int = 0
    llm_input: int = 0
    llm_output: int = 0
    tool_results: int = 0
    total: int = 0
    
    @property
    def efficiency(self) -> float:
        """Output / input ratio."""
        return self.llm_output / max(self.llm_input, 1)
    
    @property
    def context_overhead(self) -> float:
        """Context tokens / total input tokens."""
        return (self.context_assembly + self.tool_results) / max(self.llm_input, 1)

# In ReActAgent.run():
token_breakdown = TokenBreakdown(
    user_input=get_token_count(user_message),
    system_prompt=get_token_count(self.system_prompt),
    context_assembly=sum(c.token_count for c in recent),
)

# After each LLM call:
token_breakdown.llm_input += response.usage.get("prompt_tokens", 0)
token_breakdown.llm_output += response.usage.get("completion_tokens", 0)

# After each tool call:
token_breakdown.tool_results += len(result_str) // 4

# At end of run:
token_breakdown.total = token_breakdown.llm_input + token_breakdown.llm_output
```

---

## 8. Phased Adoption Plan

### Phase 1: Structured Logging + Cost Tracking (Week 1)

**Effort:** 2–3 days

1. Implement `StructuredLogger` with consistent envelope.
2. Add `trace_id` and `span_id` to all audit events.
3. Implement `estimate_cost()` in `LLMResponse`.
4. Add cost tracking to audit events.
5. Add PII redaction for `tool_args` and `result_preview`.

**Deliverable:** Every agent run produces structured JSON logs with cost attribution.

### Phase 2: Metrics + Dashboards (Weeks 2–3)

**Effort:** 1–2 weeks

1. Add OpenTelemetry Metrics instrumentation.
2. Set up Prometheus + Grafana (docker-compose).
3. Create three-layer dashboard (business, system, cost).
4. Add token usage analytics.

**Deliverable:** Real-time dashboards showing agent health, cost, and token usage.

### Phase 3: Tracing (Weeks 3–4)

**Effort:** 1–2 weeks

1. Add OpenTelemetry Tracing to ReAct loop.
2. Instrument LLM calls and tool executions with spans.
3. Set up Jaeger or Tempo for trace visualization.
4. Add GenAI semantic convention attributes.

**Deliverable:** Distributed traces showing the full causal chain of an agent run.

### Phase 4: Alerting (Week 4)

**Effort:** 3–5 days

1. Implement alert rules (error rate, cost budget, loop detection).
2. Set up PagerDuty/Opsgenie integration for critical alerts.
3. Set up Slack integration for warning alerts.
4. Set up weekly email reports for info alerts.

**Deliverable:** Proactive alerting on agent anomalies.

### Phase 5: Evaluation Pipeline (Ongoing)

**Effort:** Ongoing

1. Sample 1–5% of traces for LLM-as-judge evaluation.
2. Store evaluation scores as span attributes.
3. Alert on trailing 24-hour mean quality score by user segment.
4. Build feedback loop: evaluation → prompt improvement → re-evaluation.

---

## 9. Tooling Comparison

| Tool | Best For | Open Source | Agent-Specific | Cost |
|------|----------|-------------|----------------|------|
| **OpenTelemetry** | Standard distributed tracing | Yes | Framework-level | Free |
| **Prometheus + Grafana** | Metrics dashboards | Yes | No (DIY) | Free |
| **Jaeger** | Trace visualization | Yes | No | Free |
| **Langfuse** | Multi-framework agent tracing | Yes | Yes | Free tier |
| **Arize Phoenix** | ML observability + LLM traces | Yes | Yes | Free |
| **LangSmith** | LangChain ecosystems | No | Yes | $$$ |
| **Helicone** | LLM request logging + cost tracking | Partial | LLM-focused | Free tier |
| **Datadog LLM Observability** | Enterprise observability | No | Via integrations | $$$ |
| **SigNoz** | Open-source Datadog alternative | Yes | Yes | Free tier |

**Recommended stack for AgentHarness:**
- **OpenTelemetry** for tracing (vendor-neutral, standard)
- **Prometheus + Grafana** for metrics and dashboards (open source, flexible)
- **Langfuse** for agent-specific tracing (optional, if OTel is too low-level)

---

## 10. Key Takeaways

1. **Observability is not optional for production agents.** Without it, you cannot debug, improve, or trust your agents.

2. **The three pillars (metrics, traces, logs) still apply**, but LLM workloads need token-level cost dimensions, TTFT separated from total latency, and alerts on p99 and tail-shape ratios.

3. **OpenTelemetry GenAI semantic conventions** are the open standard. Instrumenting against them keeps you portable across backends.

4. **Cost tracking is the highest-ROI observability investment.** A single runaway agent loop can cost $10 in a minute. Token-level cost attribution catches this immediately.

5. **The 4% silent failure rule** means you need outcome metrics (task completion rate, correction rate), not just infrastructure metrics (latency, error rate).

6. **Start with structured logging and cost tracking.** This takes 2–3 days and gives you 80% of the value. Add tracing and dashboards incrementally.

7. **AgentHarness's audit logger is a good foundation.** It already emits JSON with `event_type` and `session_id`. The gap is consistency, correlation, and aggregation — not a rewrite.

---

## References

- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [OpenLLMetry](https://github.com/traceloop/openllmetry) — OpenTelemetry auto-instrumentation for LLM frameworks
- [Langfuse](https://langfuse.com/) — Open-source LLM observability platform
- [Arize Phoenix](https://github.com/Arize-ai/phoenix) — Open-source ML observability
- [Helicone](https://helicone.ai/) — LLM request logging and cost tracking
- [Datadog LLM Observability](https://www.datadoghq.com/product/llm-observability/)
- [SigNoz](https://signoz.io/) — Open-source observability platform


