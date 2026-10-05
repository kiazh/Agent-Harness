# Production Deployment Research — AgentHarness

> Research into production deployment patterns for a self-hosted AI agent framework.
> Covers Docker, Kubernetes, CI/CD, monitoring, observability, logging, secrets, and backup.

---

## 1. Current State Assessment

AgentHarness is a Python 3.11+ async framework with:

| Component | Technology | Notes |
|-----------|-----------|-------|
| CLI | Typer + Rich | `ah` command, `asyncio.run()` per invocation |
| Database | PostgreSQL + pgvector | asyncpg pool (2-10 connections), 15+ tables |
| LLM Provider | OpenRouter / Ollama | httpx async clients, token-bucket rate limiting, 429 retry-with-backoff |
| Context Store | MessagePack in BYTEA | `context_chunks` with HNSW embedding index |
| Skills | File-based registry | YAML frontmatter, loaded from `skills/` |
| Tools | In-process registry | `ah/tools/builtins.py` |
| CI | GitHub Actions | ruff + pytest, no CD pipeline |
| Config | Environment variables | `DATABASE_URL`, `OPENROUTER_API_KEY` |

**Key deployment characteristics:**
- **Stateless compute** — agent runs are ephemeral; all state lives in PostgreSQL
- **Single-process compute with two entry points** — the one-shot CLI (`ah chat`), a long-running gateway (`python -m ah.gateway`) driven by the UI, and the HTTP API server (`ah serve`) are available today. The gateway has file+console logging and the UI auto-detects the venv Python.
- **External LLM dependency** — OpenRouter API or local Ollama
- **pgvector required** — `CREATE EXTENSION vector` in schema.sql

---

## 2. Docker

### 2.1 Container Strategy

AgentHarness has two natural container boundaries:

1. **AgentHarness CLI image** — the `ah` binary + Python runtime
2. **PostgreSQL + pgvector image** — the stateful data layer

A `docker-compose.yml` for local development and single-node production:

```yaml
# docker-compose.yml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: agentharness
      POSTGRES_USER: ${POSTGRES_USER:-ah}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./backups:/backups
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-ah} -d agentharness"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  agentharness:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER:-ah}:${POSTGRES_PASSWORD}@postgres:5432/agentharness
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
      LLM_RATE_LIMIT_CALLS_PER_MINUTE: ${LLM_RATE_LIMIT:-10}
    depends_on:
      postgres:
        condition: service_healthy
    # For interactive CLI use:
    #   docker compose run --rm agentharness ah chat "hello"
    # For a long-running API server (future):
    #   command: ["ah", "serve", "--host", "0.0.0.0", "--port", "8000"]
    restart: unless-stopped

volumes:
  pgdata:
```

### 2.2 Dockerfile

```dockerfile
# Dockerfile
FROM python:3.11-slim AS base

# Security: run as non-root
RUN groupadd -r ah && useradd -r -g ah ah

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# Copy application code
COPY ah/ ./ah/
COPY skills/ ./skills/

# Initialize schema on first run (entrypoint handles this)
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER ah
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["ah", "--help"]
```

### 2.3 Entrypoint Pattern

```bash
#!/usr/bin/env bash
# docker-entrypoint.sh
set -euo pipefail

# Wait for PostgreSQL
until pg_isready -h "${DATABASE_URL#*@}" -p 5432; do
  echo "Waiting for PostgreSQL..."
  sleep 1
done

# Initialize schema (idempotent)
ah init

# Run the actual command
exec "$@"
```

### 2.4 Multi-Stage Build (Production)

```dockerfile
# Dockerfile.prod — smaller attack surface
FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim
COPY --from=builder /install /usr/local
COPY ah/ /app/ah/
COPY skills/ /app/skills/
WORKDIR /app
RUN groupadd -r ah && useradd -r -g ah ah && chown -R ah:ah /app
USER ah
ENTRYPOINT ["ah"]
```

### 2.5 Key Docker Decisions

| Decision | Rationale |
|----------|-----------|
| `pgvector/pgvector:pg16` base image | Official pgvector builds; avoids manual compilation |
| Non-root user | Security best practice; container escape mitigation |
| `depends_on: service_healthy` | Prevents race condition on first boot |
| Named volume for PGDATA | Survives container recreation |
| Multi-stage build | ~50% smaller final image, no build tools in prod |
| `pip install .` not `pip install -e .` | Production needs installed package, not editable |

---

## 3. Kubernetes

### 3.1 When K8s Makes Sense

| Scenario | Recommendation |
|----------|---------------|
| Single user / local dev | Docker Compose |
| Small team, single server | Docker Compose + systemd |
| Multi-user, HA, auto-scaling | Kubernetes |
| Enterprise, multi-team, GitOps | Kubernetes + Helm + ArgoCD |

### 3.2 Deployment Manifest

```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agentharness
  labels:
    app: agentharness
spec:
  replicas: 2
  selector:
    matchLabels:
      app: agentharness
  strategy:
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: agentharness
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 999
        fsGroup: 999
      containers:
        - name: agentharness
          image: agentharness:latest
          imagePullPolicy: Always
          ports:
            - containerPort: 8000  # future HTTP API
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: agentharness-secrets
                  key: database-url
            - name: OPENROUTER_API_KEY
              valueFrom:
                secretKeyRef:
                  name: agentharness-secrets
                  key: openrouter-api-key
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 1Gi
          livenessProbe:
            exec:
              command: ["ah", "doctor"]
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            exec:
              command: ["ah", "status"]
            initialDelaySeconds: 5
            periodSeconds: 10
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "sleep 5"]  # graceful shutdown
---
apiVersion: v1
kind: Service
metadata:
  name: agentharness
spec:
  selector:
    app: agentharness
  ports:
    - port: 80
      targetPort: 8000
  type: ClusterIP
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agentharness-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: agentharness
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

### 3.3 PostgreSQL in K8s

For production, **use an external managed PostgreSQL** (RDS, Cloud SQL, Crunchy Data operator) rather than running it in-cluster. If you must run it in K8s:

```yaml
# Use the Crunchy Data PGO operator or Bitnami PostgreSQL HA
# Simplified — use a StatefulSet with persistent volumes
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 1  # or 3 with Patroni for HA
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
        - name: postgres
          image: pgvector/pgvector:pg16
          env:
            - name: POSTGRES_DB
              value: agentharness
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: agentharness-secrets
                  key: postgres-user
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: agentharness-secrets
                  key: postgres-password
          volumeMounts:
            - name: pgdata
              mountPath: /var/lib/postgresql/data
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
  volumeClaimTemplates:
    - metadata:
        name: pgdata
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: fast-ssd
        resources:
          requests:
            storage: 50Gi
```

### 3.4 Helm Chart Structure

```
agentharness-chart/
├── Chart.yaml
├── values.yaml
├── values-production.yaml
├── templates/
│   ├── _helpers.tpl
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── hpa.yaml
│   ├── serviceaccount.yaml
│   ├── secret.yaml          # references external secret store
│   ├── networkpolicy.yaml
│   └── poddisruptionbudget.yaml
└── charts/
    └── postgresql-subchart/  # or use external
```

### 3.5 K8s Security Hardening

```yaml
# NetworkPolicy — restrict pod-to-pod traffic
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agentharness-netpol
spec:
  podSelector:
    matchLabels:
      app: agentharness
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              name: ingress-nginx
      ports:
        - protocol: TCP
          port: 8000
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: postgres
      ports:
        - protocol: TCP
          port: 5432
    - to: []  # OpenRouter API
      ports:
        - protocol: TCP
          port: 443
---
# PodDisruptionBudget — ensure availability during node drains
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: agentharness-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: agentharness
```

---

## 4. CI/CD

### 4.1 Current State

The project has two GitHub Actions workflows:
- `ci.yml` — runs on push to main + PRs: ruff check, ruff format, pytest
- `pr.yml` — same checks on PRs

**Missing:** release automation and CD pipeline (Docker images are provided: `Dockerfile`, `Dockerfile.sandbox`, `docker-compose.yml`; audit events persist to the `audit_events` table).

### 4.2 Recommended CI Pipeline

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Cache pip
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('pyproject.toml') }}

      - name: Install
        run: pip install -e ".[dev]"

      - name: Ruff check
        run: ruff check ah/

      - name: Ruff format
        run: ruff format --check ah/

      - name: Type check
        run: |
          if grep -q '\[tool.mypy\]' pyproject.toml; then
            pip install mypy
            mypy ah/
          fi

      - name: Pytest
        run: pytest tests/ -v --cov=ah --cov-report=xml

      - name: Upload coverage
        uses: codecov/codecov-action@v4
        with:
          file: coverage.xml

  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: pip-audit
        run: |
          pip install pip-audit
          pip-audit --desc

      - name: Bandit (Python SAST)
        run: |
          pip install bandit
          bandit -r ah/ -f json -o bandit-report.json || true

      - name: Upload security report
        uses: actions/upload-artifact@v4
        with:
          name: security-report
          path: bandit-report.json

  build:
    runs-on: ubuntu-latest
    needs: [lint-and-test, security]
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: false
          tags: agentharness:ci
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Trivy vulnerability scan
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: agentharness:ci
          format: sarif
          output: trivy-results.sarif
```

### 4.3 CD Pipeline (GitHub Actions → Docker Hub)

```yaml
# .github/workflows/cd.yml
name: CD

on:
  push:
    tags: ["v*"]

jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract version
        id: version
        run: echo "VERSION=${GITHUB_REF#refs/tags/v}" >> $GITHUB_OUTPUT

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ github.repository }}:latest
            ghcr.io/${{ github.repository }}:${{ steps.version.outputs.VERSION }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v1
        with:
          generate_release_notes: true
```

### 4.4 GitOps with ArgoCD (K8s)

```yaml
# Application manifest for ArgoCD
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: agentharness
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/kiazh/agent-harness
    targetRevision: HEAD
    path: k8s/
  destination:
    server: https://kubernetes.default.svc
    namespace: agentharness
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

### 4.5 CI/CD Best Practices for AgentHarness

| Practice | Implementation |
|----------|---------------|
| **Test matrix** | Python 3.11 + 3.12 |
| **Security scanning** | pip-audit, Bandit, Trivy |
| **Image signing** | Cosign + GitHub OIDC keyless signing |
| **Reproducible builds** | Lock dependencies (`pip-compile` or `uv lock`) |
| **Semantic versioning** | Tag-based releases (`v1.2.3`) |
| **Changelog** | Auto-generated from conventional commits |
| **Staging environment** | Auto-deploy PRs to staging namespace |
| **Smoke tests** | Post-deploy: `ah doctor` + `ah chat "ping"` |

---

## 5. Monitoring

### 5.1 Metrics to Expose

AgentHarness should expose Prometheus metrics. Key metrics for an AI agent framework:

| Category | Metric | Type | Description |
|----------|--------|------|-------------|
| **LLM** | `ah_llm_requests_total` | Counter | Total LLM API calls by provider/model |
| **LLM** | `ah_llm_tokens_total` | Counter | Total tokens consumed (prompt + completion) |
| **LLM** | `ah_llm_latency_seconds` | Histogram | LLM call latency |
| **LLM** | `ah_llm_errors_total` | Counter | LLM call failures by error type |
| **Agent** | `ah_agent_runs_total` | Counter | Total agent runs |
| **Agent** | `ah_agent_iterations` | Histogram | Iterations per run |
| **Agent** | `ah_agent_duration_seconds` | Histogram | End-to-end run duration |
| **Agent** | `ah_agent_token_budget_exceeded` | Counter | Runs that hit token budget |
| **Tools** | `ah_tool_calls_total` | Counter | Tool invocations by tool name |
| **Tools** | `ah_tool_duration_seconds` | Histogram | Tool execution time |
| **Tools** | `ah_tool_errors_total` | Counter | Tool execution failures |
| **DB** | `ah_db_pool_connections` | Gauge | Current pool size / available |
| **DB** | `ah_db_query_duration_seconds` | Histogram | Query latency |
| **Sessions** | `ah_active_sessions` | Gauge | Currently active sessions |
| **Context** | `ah_context_chunks_total` | Counter | Context chunks stored |
| **Context** | `ah_context_token_usage` | Gauge | Tokens per session |

### 5.2 Prometheus Instrumentation

```python
# ah/observability/metrics.py
from prometheus_client import Counter, Histogram, Gauge, Info, generate_latest
from functools import wraps
import time

# LLM metrics
LLM_REQUESTS = Counter(
    "ah_llm_requests_total",
    "Total LLM API calls",
    ["provider", "model"],
)
LLM_TOKENS = Counter(
    "ah_llm_tokens_total",
    "Total tokens consumed",
    ["provider", "model", "type"],  # type: prompt | completion
)
LLM_LATENCY = Histogram(
    "ah_llm_latency_seconds",
    "LLM call latency",
    ["provider", "model"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0],
)
LLM_ERRORS = Counter(
    "ah_llm_errors_total",
    "LLM call failures",
    ["provider", "model", "error_type"],
)

# Agent metrics
AGENT_RUNS = Counter("ah_agent_runs_total", "Total agent runs", ["agent_id"])
AGENT_ITERATIONS = Histogram("ah_agent_iterations", "Iterations per run", ["agent_id"])
AGENT_DURATION = Histogram("ah_agent_duration_seconds", "Agent run duration", ["agent_id"])
AGENT_BUDGET_EXCEEDED = Counter("ah_agent_token_budget_exceeded", "Token budget exceeded")

# Tool metrics
TOOL_CALLS = Counter("ah_tool_calls_total", "Tool invocations", ["tool_name"])
TOOL_DURATION = Histogram("ah_tool_duration_seconds", "Tool execution time", ["tool_name"])
TOOL_ERRORS = Counter("ah_tool_errors_total", "Tool failures", ["tool_name"])

# DB metrics
DB_POOL_SIZE = Gauge("ah_db_pool_connections", "DB pool connections", ["state"])
DB_QUERY_DURATION = Histogram("ah_db_query_duration_seconds", "DB query latency")

# Session metrics
ACTIVE_SESSIONS = Gauge("ah_active_sessions", "Currently active sessions")
CONTEXT_CHUNKS = Counter("ah_context_chunks_total", "Context chunks stored", ["chunk_type"])

# App info
APP_INFO = Info("agentharness", "AgentHarness application info")


def track_llm_call(provider: str, model: str):
    """Context manager to track an LLM call."""
    LLM_REQUESTS.labels(provider=provider, model=model).inc()
    start = time.monotonic()
    try:
        yield
    except Exception as e:
        LLM_ERRORS.labels(provider=provider, model=model, error_type=type(e).__name__).inc()
        raise
    finally:
        LLM_LATENCY.labels(provider=provider, model=model).observe(time.monotonic() - start)


def track_tool_call(tool_name: str):
    """Context manager to track a tool execution."""
    TOOL_CALLS.labels(tool_name=tool_name).inc()
    start = time.monotonic()
    try:
        yield
    except Exception:
        TOOL_ERRORS.labels(tool_name=tool_name).inc()
        raise
    finally:
        TOOL_DURATION.labels(tool_name=tool_name).observe(time.monotonic() - start)
```

### 5.3 Metrics Endpoint

```python
# ah/observability/server.py
from aiohttp import web
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

async def metrics_handler(request):
    return web.Response(
        body=generate_latest(),
        content_type=CONTENT_TYPE_LATEST,
    )

async def health_handler(request):
    # Check DB connectivity
    try:
        from ah.db.connection import db
        await db.fetchval("SELECT 1")
        return web.json_response({"status": "healthy"})
    except Exception as e:
        return web.json_response({"status": "unhealthy", "error": str(e)}, status=503)

def create_metrics_app():
    app = web.Application()
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/health", health_handler)
    return app
```

### 5.4 Grafana Dashboard

```json
{
  "dashboard": {
    "title": "AgentHarness Overview",
    "panels": [
      {
        "title": "LLM Requests/sec",
        "type": "timeseries",
        "targets": [{"expr": "rate(ah_llm_requests_total[5m])"}]
      },
      {
        "title": "Token Usage",
        "type": "timeseries",
        "targets": [{"expr": "rate(ah_llm_tokens_total[5m])"}]
      },
      {
        "title": "LLM Latency (p95)",
        "type": "timeseries",
        "targets": [{"expr": "histogram_quantile(0.95, rate(ah_llm_latency_seconds_bucket[5m]))"}]
      },
      {
        "title": "Agent Runs",
        "type": "stat",
        "targets": [{"expr": "sum(ah_agent_runs_total)"}]
      },
      {
        "title": "Tool Call Distribution",
        "type": "piechart",
        "targets": [{"expr": "sum by (tool_name) (ah_tool_calls_total)"}]
      },
      {
        "title": "Error Rate",
        "type": "timeseries",
        "targets": [{"expr": "rate(ah_llm_errors_total[5m])"}]
      },
      {
        "title": "DB Pool Saturation",
        "type": "gauge",
        "targets": [{"expr": "ah_db_pool_connections{state='used'} / ah_db_pool_connections{state='total'}"}]
      }
    ]
  }
}
```

### 5.5 Alerting Rules

```yaml
# prometheus-rules.yaml
groups:
  - name: agentharness
    rules:
      - alert: AgentHarnessHighErrorRate
        expr: rate(ah_llm_errors_total[5m]) / rate(ah_llm_requests_total[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High LLM error rate (>10%)"

      - alert: AgentHarnessHighLatency
        expr: histogram_quantile(0.95, rate(ah_llm_latency_seconds_bucket[5m])) > 30
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "LLM p95 latency > 30s"

      - alert: AgentHarnessDBDown
        expr: up{job="agentharness-postgres"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "PostgreSQL is down"

      - alert: AgentHarnessTokenBudgetExhaustion
        expr: rate(ah_agent_token_budget_exceeded[1h]) > 5
        for: 15m
        labels:
          severity: info
        annotations:
          summary: "Frequent token budget exhaustion — consider raising limits"

      - alert: AgentHarnessDBPoolExhausted
        expr: ah_db_pool_connections{state="used"} / ah_db_pool_connections{state="total"} > 0.9
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "DB connection pool >90% utilized"
```

---

## 6. Observability

### 6.1 OpenTelemetry Integration

The OpenTelemetry GenAI semantic conventions provide standard attributes for LLM operations:

| Attribute | Description |
|-----------|-------------|
| `gen_ai.request.model` | Model used (e.g., `anthropic/claude-3.5-sonnet`) |
| `gen_ai.usage.input_tokens` | Prompt tokens |
| `gen_ai.usage.output_tokens` | Completion tokens |
| `gen_ai.response.finish_reasons` | Why generation stopped |
| `gen_ai.system_instructions` | System prompt (opt-in) |
| `gen_ai.input.messages` | Full input messages (opt-in) |
| `gen_ai.output.messages` | Full output messages (opt-in) |

```python
# ah/observability/tracing.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.semconv_ai import (
    SpanAttributes,
    GenAIAttributes,
)
import os

def setup_tracing(service_name: str = "agentharness"):
    """Initialize OpenTelemetry tracing."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return  # Tracing disabled

    provider = TracerProvider()
    exporter = OTLPSpanExporter(
        endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"],
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


class AgentSpan:
    """Context manager for agent run spans."""

    def __init__(self, tracer, session_id: str, agent_id: str):
        self._tracer = tracer
        self._session_id = session_id
        self._agent_id = agent_id
        self._span = None

    def __enter__(self):
        self._span = self._tracer.start_span(
            "agent.run",
            attributes={
                SpanAttributes.GEN_AI_AGENT_ID: self._agent_id,
                "session.id": self._session_id,
            },
        )
        return self._span

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._span.set_attribute("error", True)
            self._span.set_attribute("error.message", str(exc_val))
        self._span.end()
        return False


class LLMCallSpan:
    """Context manager for LLM call spans."""

    def __init__(self, tracer, provider: str, model: str):
        self._tracer = tracer
        self._provider = provider
        self._model = model
        self._span = None

    def __enter__(self):
        self._span = self._tracer.start_span(
            "llm.call",
            attributes={
                SpanAttributes.GEN_AI_REQUEST_MODEL: self._model,
                SpanAttributes.GEN_AI_PROVIDER_NAME: self._provider,
            },
        )
        return self._span

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._span.set_attribute("error", True)
        self._span.end()
        return False

    def set_token_usage(self, prompt_tokens: int, completion_tokens: int):
        self._span.set_attribute(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, prompt_tokens)
        self._span.set_attribute(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, completion_tokens)
```

### 6.2 Structured Logging with Correlation IDs

```python
# ah/observability/logging.py
import logging
import json
import sys
import uuid
from datetime import datetime, timezone
from contextvars import ContextVar

# Context variable for request/session correlation
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")


class StructuredLogFormatter(logging.Formatter):
    """JSON formatter with correlation IDs."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "session_id": session_id_var.get(),
            "source": {
                "file": record.filename,
                "line": record.lineno,
                "function": record.funcName,
            },
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO"):
    """Configure structured JSON logging."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredLogFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]

    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
```

### 6.3 Log Pipeline Architecture

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  AgentHarness │───▶│  Fluent Bit  │───▶│  OpenSearch  │───▶│  Dashboards  │
│  (JSON logs)  │    │  (shipper)   │    │  (storage)   │    │  (Kibana)    │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │  S3 (archive)│
                    └──────────────┘
```

### 6.4 Audit Logging (Already Present)

AgentHarness already has audit logging in `ah/core/provider.py`:

```python
_audit_logger = logging.getLogger("ah.audit")
# Logs: agent_run_start, agent_run_complete, tool_call_start, tool_call_complete, llm_call_start, llm_call_complete
```

**Production enhancement:** ship audit logs to a separate sink (immutable storage) for compliance:

```python
# ah/observability/audit.py
import json
import time
import uuid
from datetime import datetime, timezone

_audit_events = []

def audit_log(event_type: str, **kwargs):
    """Structured audit event with full context."""
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "request_id": request_id_var.get(),
        "session_id": session_id_var.get(),
        **kwargs,
    }
    # Write to stdout (captured by Fluent Bit → S3/OpenSearch)
    print(json.dumps(entry, default=str), flush=True)
```

---

## 7. Logging

### 7.1 Log Levels and What to Log

| Level | What | Example |
|-------|------|---------|
| **DEBUG** | Full LLM prompts/responses, tool args/results | Development only |
| **INFO** | Agent run start/complete, tool calls, session lifecycle | Production default |
| **WARNING** | Retry attempts, rate limit hits, budget warnings | Production |
| **ERROR** | LLM failures, tool execution failures, DB errors | Production |
| **CRITICAL** | DB connection loss, provider auth failure | Production |

### 7.2 What NOT to Log

| Data | Reason |
|------|--------|
| `OPENROUTER_API_KEY` | Secret — never log |
| `DATABASE_URL` (with password) | Secret — log only host/port/db |
| Full user messages (in prod) | PII concern — log metadata only |
| Full LLM responses (in prod) | Cost/size — log token counts + preview |
| Tool arguments containing secrets | Redact known secret patterns |

### 7.3 Log Retention

| Environment | Hot Storage | Cold Archive | Retention |
|-------------|-------------|--------------|-----------|
| Development | Local stdout | None | N/A |
| Staging | OpenSearch (7d) | S3 (30d) | 30 days |
| Production | OpenSearch (30d) | S3 Glacier (1yr) | 1 year |
| Compliance | OpenSearch (90d) | S3 Glacier (7yr) | 7 years |

### 7.4 Docker Compose Logging

```yaml
# docker-compose.yml (logging section)
services:
  agentharness:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
        labels: "service,environment"
        tag: "{{.ImageName}}/{{.Name}}/{{.ID}}"
```

### 7.5 K8s Logging

```yaml
# Fluent Bit ConfigMap for K8s log shipping
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-config
data:
  fluent-bit.conf: |
    [INPUT]
        Name              tail
        Path              /var/log/containers/agentharness-*.log
        Parser            docker
        Tag               agentharness.*
        Refresh_Interval  5

    [FILTER]
        Name              kubernetes
        Match             agentharness.*
        Kube_URL          https://kubernetes.default.svc:443
        Merge_Log         On

    [OUTPUT]
        Name              opensearch
        Match             agentharness.*
        Host              opensearch-cluster
        Port              9200
        Index             agentharness-logs
        Suppress_Type_Name On
```

---

## 8. Secrets Management

### 8.1 Current State

AgentHarness reads secrets from environment variables (`DATABASE_URL`, `OPENROUTER_API_KEY`) loaded via `python-dotenv`, with `*_FILE` mounted-secret support and optional HashiCorp Vault or AWS Secrets Manager backends (`AGENT_HARNESS_SECRET_BACKEND`). Environment variables are still the default; the external backends cover production secret rotation.

### 8.2 Secrets Inventory

| Secret | Source | Rotation Frequency |
|--------|--------|-------------------|
| `DATABASE_URL` | PostgreSQL credentials | 90 days |
| `OPENROUTER_API_KEY` | OpenRouter dashboard | On compromise |
| `POSTGRES_PASSWORD` | PostgreSQL user password | 90 days |
| `JWT_SECRET` (future) | Session signing key | 180 days |
| `ENCRYPTION_KEY` (future) | Data encryption at rest | 365 days |

### 8.3 Secret Management Solutions

| Solution | Best For | Integration |
|----------|---------|-------------|
| **Docker Secrets** | Single-node Swarm | `echo "secret" \| docker secret create db_password -` |
| **Kubernetes Secrets** | K8s deployments | `kubectl create secret generic agentharness-secrets` |
| **HashiCorp Vault** | Enterprise, multi-app | `vault kv get secret/agentharness` |
| **AWS Secrets Manager** | AWS deployments | `aws secretsmanager get-secret-value` |
| **Doppler** | Team-friendly SaaS | `doppler secrets download --format env` |
| **SOPS + Age** | Git-encrypted secrets | `sops -d secrets.enc.yaml` |

### 8.4 Kubernetes Secrets (with External Secrets Operator)

```yaml
# External Secrets Operator — sync from AWS Secrets Manager
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: agentharness-secrets
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: agentharness-secrets
    creationPolicy: Owner
  data:
    - secretKey: database-url
      remoteRef:
        key: agentharness/production
        property: database-url
    - secretKey: openrouter-api-key
      remoteRef:
        key: agentharness/production
        property: openrouter-api-key
```

### 8.5 Runtime Secret Injection

The current runtime in `ah/security/secrets.py` resolves each named secret from
the environment variable, then the matching `NAME_FILE` path, then the
configured Vault or AWS Secrets Manager backend. It does not scan mounted
secret directories. For a mounted secret, point the matching variable at the
file, such as `OPENROUTER_API_KEY_FILE=/run/secrets/openrouter-api-key`.

### 8.6 Secret Rotation Strategy

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Generate   │────▶│   Deploy    │────▶│   Verify    │
│   new secret │     │   new secret│     │   rotation  │
└─────────────┘     └─────────────┘     └─────────────┘
       │                                       │
       │         ┌─────────────┐               │
       └────────▶│   Revoke    │◀──────────────┘
                 │   old secret│
                 └─────────────┘
```

1. Generate new secret (e.g., new API key)
2. Deploy alongside old secret (dual-key period)
3. Verify new secret works
4. Revoke old secret
5. Update secret store

---

## 9. Backup Strategies

### 9.1 What Needs Backing Up

| Data | Location | Backup Method | Frequency |
|------|----------|---------------|-----------|
| PostgreSQL data | `pgdata` volume / PVC | `pg_dump` + WAL archiving | Daily full + continuous WAL |
| Session state | `sessions.state_msgpack` | Included in pg_dump | — |
| Context chunks | `context_chunks` table | Included in pg_dump | — |
| Embeddings | `context_chunks.embedding` | Included in pg_dump | — |
| Skills | `skills/` directory | Git + S3 sync | On change |
| Configuration | `.env` / K8s secrets | Git (encrypted) + Vault | On change |
| Audit logs | OpenSearch / S3 | S3 cross-region replication | Continuous |

### 9.2 PostgreSQL Backup Strategy

```bash
#!/usr/bin/env bash
# scripts/backup.sh — run via cron or Kubernetes CronJob

set -euo pipefail

BACKUP_DIR="/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=30
S3_BUCKET="s3://agentharness-backups"

# Full backup with pg_dump
pg_dump "$DATABASE_URL" \
    --format=custom \
    --compress=9 \
    --file="${BACKUP_DIR}/agentharness_${TIMESTAMP}.dump"

# Verify backup
pg_restore --list "${BACKUP_DIR}/agentharness_${TIMESTAMP}.dump" > /dev/null

# Upload to S3
aws s3 cp "${BACKUP_DIR}/agentharness_${TIMESTAMP}.dump" "${S3_BUCKET}/daily/"

# Clean old local backups
find "${BACKUP_DIR}" -name "*.dump" -mtime +${RETENTION_DAYS} -delete

# Clean old S3 backups (lifecycle policy also handles this)
aws s3 ls "${S3_BUCKET}/daily/" | \
    awk -v date="$(date -d "${RETENTION_DAYS} days ago" +%Y%m%d)" \
    '$1 < date {print $4}' | \
    xargs -I {} aws s3 rm "${S3_BUCKET}/daily/{}"

echo "Backup completed: agentharness_${TIMESTAMP}.dump"
```

### 9.3 Point-in-Time Recovery (PITR)

```yaml
# postgresql.conf — WAL archiving for PITR
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://agentharness-backups/wal/%f'
max_wal_senders = 3
```

```bash
# Recovery procedure
# 1. Stop PostgreSQL
# 2. Restore base backup
pg_restore -d "$DATABASE_URL" agentharness_20241001_000000.dump

# 3. Create recovery.conf
echo "restore_command = 'aws s3 cp s3://agentharness-backups/wal/%f %p'" > $PGDATA/recovery.signal
echo "recovery_target_time = '2024-10-01 12:34:56'" >> $PGDATA/recovery.signal

# 4. Start PostgreSQL — it replays WAL up to target time
```

### 9.4 Kubernetes CronJob for Backups

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: agentharness-backup
spec:
  schedule: "0 2 * * *"  # 2 AM daily
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: backup
              image: pgvector/pgvector:pg16
              command:
                - /bin/bash
                - -c
                - |
                  pg_dump "$DATABASE_URL" --format=custom --compress=9 \
                    | aws s3 cp - s3://agentharness-backups/daily/agentharness_$(date +%Y%m%d).dump
              env:
                - name: DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: agentharness-secrets
                      key: database-url
              resources:
                requests:
                  cpu: 100m
                  memory: 256Mi
          restartPolicy: OnFailure
```

### 9.5 Backup Verification

```bash
#!/usr/bin/env bash
# scripts/verify-backup.sh — weekly backup verification

set -euo pipefail

LATEST_BACKUP=$(aws s3 ls s3://agentharness-backups/daily/ | sort | tail -1 | awk '{print $4}')
TEMP_DB="agentharness_verify_$(date +%s)"

# Download and restore to temp database
aws s3 cp "s3://agentharness-backups/daily/${LATEST_BACKUP}" /tmp/verify.dump
createdb "$TEMP_DB"
pg_restore -d "$TEMP_DB" /tmp/verify.dump

# Run verification queries
psql "$TEMP_DB" -c "SELECT COUNT(*) FROM sessions;" > /dev/null
psql "$TEMP_DB" -c "SELECT COUNT(*) FROM context_chunks;" > /dev/null
psql "$TEMP_DB" -c "SELECT pg_size_pretty(pg_database_size('${TEMP_DB}'));"

# Cleanup
dropdb "$TEMP_DB"
rm /tmp/verify.dump

echo "Backup verification passed: ${LATEST_BACKUP}"
```

### 9.6 Disaster Recovery Runbook

| Scenario | RTO | RPO | Procedure |
|----------|-----|-----|-----------|
| Single container crash | 1 min | 0 | Docker/K8s auto-restart |
| PostgreSQL corruption | 30 min | 5 min | Restore from PITR |
| Full node failure | 1 hour | 1 hour | Redeploy on new node from backups |
| Region outage | 4 hours | 1 hour | Cross-region S3 restore |
| Accidental data deletion | 15 min | 5 min | PITR to just before deletion |

### 9.7 Backup Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Production                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐          │
│  │ AgentHarness│    │ PostgreSQL│    │  Skills  │          │
│  └─────┬────┘    └─────┬────┘    └─────┬────┘          │
│        │               │               │                 │
└────────┼───────────────┼───────────────┼─────────────────┘
         │               │               │
         ▼               ▼               ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  S3 Standard  │  │  S3 Standard  │  │  Git + S3    │
│  (daily dumps)│  │  (WAL archive)│  │  (versioned) │
└──────┬───────┘  └──────┬───────┘  └──────────────┘
       │                 │
       ▼                 ▼
┌──────────────┐  ┌──────────────┐
│  S3 Glacier  │  │  S3 Glacier  │
│  (30d → 1yr) │  │  (30d → 7yr) │
└──────────────┘  └──────────────┘
       │
       ▼
┌──────────────┐
│  S3 Cross-   │
│  Region      │
│  Replication │
└──────────────┘
```

---

## 10. Production Readiness Checklist

### 10.1 Immediate (Pre-Production)

- [ ] Add `prometheus-client` dependency
- [ ] Add OpenTelemetry SDK dependency
- [ ] Create `Dockerfile` + `docker-compose.yml`
- [ ] Add `/health` and `/metrics` endpoints
- [ ] Move secrets to environment-based config (no hardcoded DSN)
- [ ] Add structured JSON logging
- [ ] Add `pg_dump` backup script
- [ ] Add Trivy + Bandit to CI
- [ ] Add `.dockerignore`
- [ ] Create production `values.yaml` for Helm

### 10.2 Short-Term (1–3 Months)

- [ ] Add HTTP API server (FastAPI/aiohttp) for remote agent invocation
- [ ] Add authentication (API keys or OAuth2)
- [ ] Add rate limiting per user/session
- [ ] Set up Grafana dashboards
- [ ] Set up alerting rules
- [ ] Add distributed tracing with Jaeger or Tempo
- [ ] Implement secret rotation automation
- [ ] Add integration tests with testcontainers
- [ ] Create Helm chart
- [ ] Set up staging environment

### 10.3 Medium-Term (3–6 Months)

- [ ] Multi-agent orchestration (parallel tool execution)
- [ ] Horizontal scaling with K8s HPA
- [ ] Database read replicas for query scaling
- [ ] Redis caching layer for session state
- [ ] CI/CD with GitOps (ArgoCD)
- [ ] Chaos engineering tests
- [ ] Load testing suite
- [ ] Documentation site
- [ ] Community onboarding guide

---

## 11. Architecture Diagram

```
                         ┌─────────────────────────────────────┐
                         │           Users / Clients            │
                         └──────────────┬──────────────────────┘
                                        │
                         ┌──────────────▼──────────────────────┐
                         │     Ingress / Load Balancer          │
                         │     (nginx / Traefik / ALB)          │
                         └──────────────┬──────────────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
          ┌─────────▼────────┐ ┌────────▼─────────┐ ┌──────▼──────────┐
          │  AgentHarness    │ │  AgentHarness    │ │  AgentHarness   │
          │  Pod 1           │ │  Pod 2           │ │  Pod N          │
          │                  │ │                  │ │                 │
          │  ┌────────────┐  │ │  ┌────────────┐  │ │  ┌───────────┐ │
          │  │ ReAct Loop │  │ │  │ ReAct Loop │  │ │  │ ReAct Loop│ │
          │  └─────┬──────┘  │ │  └─────┬──────┘  │ │  └─────┬─────┘ │
          │        │         │ │        │         │ │        │       │
          │  ┌─────▼──────┐  │ │  ┌─────▼──────┐  │ │  ┌─────▼─────┐ │
          │  │  Provider   │  │ │  │  Provider   │  │ │  │  Provider  │ │
          │  │ (httpx)     │  │ │  │ (httpx)     │  │ │  │ (httpx)    │ │
          │  └─────┬──────┘  │ │  └─────┬──────┘  │ │  └─────┬─────┘ │
          └────────┼─────────┘ └────────┼─────────┘ └──────┼───────┘
                   │                    │                   │
                   └────────────────────┼───────────────────┘
                                        │
                              ┌─────────▼─────────┐
                              │   PostgreSQL      │
                              │   + pgvector      │
                              │                   │
                              │  ┌─────────────┐  │
                              │  │  sessions   │  │
                              │  ├─────────────┤  │
                              │  │context_chunks│ │
                              │  └─────────────┘  │
                              └─────────┬─────────┘
                                        │
                              ┌─────────▼─────────┐
                              │   S3 / Glacier    │
                              │   (backups)       │
                              └───────────────────┘

          ┌──────────────────────────────────────────────────────┐
          │              Observability Stack                       │
          │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
          │  │Prometheus│  │  Grafana │  │  Jaeger / Tempo  │   │
          │  │(metrics) │  │(dashboard)│  │  (traces)        │   │
          │  └──────────┘  └──────────┘  └──────────────────┘   │
          │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
          │  │Fluent Bit│  │OpenSearch│  │  S3 (archive)    │   │
          │  │(shipper) │  │(logs)    │  │                  │   │
          │  └──────────┘  └──────────┘  └──────────────────┘   │
          └──────────────────────────────────────────────────────┘
```

---

## 12. Technology Stack Summary

| Layer | Technology | Alternative |
|-------|-----------|-------------|
| **Container** | Docker + Compose | Podman, containerd |
| **Orchestration** | Kubernetes + Helm | Docker Swarm, Nomad |
| **CI/CD** | GitHub Actions + ArgoCD | GitLab CI, Jenkins, Flux |
| **Metrics** | Prometheus | VictoriaMetrics, Datadog |
| **Dashboards** | Grafana | Datadog, New Relic |
| **Tracing** | OpenTelemetry + Jaeger | Zipkin, Honeycomb |
| **Logging** | Fluent Bit + OpenSearch | Loki, Datadog |
| **Secrets** | Vault + External Secrets Operator | AWS Secrets Manager, Doppler |
| **Backups** | pg_dump + S3 | WAL-G, pgBackRest |
| **Database** | PostgreSQL 16 + pgvector | Neon, Supabase, Crunchy Data |
| **LLM Gateway** | LiteLLM (future) | Direct provider APIs |

---

## 13. References

- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [pgvector documentation](https://github.com/pgvector/pgvector)
- [Crunchy Data PostgreSQL Operator](https://github.com/CrunchyData/postgres-operator)
- [External Secrets Operator](https://external-secrets.io/)
- [kagent — Kubernetes-native agent framework](https://kagent.dev/)
- [Intel Enterprise Agent Toolkit](https://github.com/intel/enterprise-agent-toolkit)
- [Langship deployment guide](https://docs.lyzr.ai/open-source/langship/deployment)
- [AgentArea deployment patterns](https://docs.agentarea.ai/deployment)
- [Docker Agent](https://docker.github.io/docker-agent/)
