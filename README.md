# AgentHarness

**Self-hosted multi-agent AI orchestration framework with PostgreSQL-backed context, RAG, long-term memory, and a streaming terminal UI.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Node.js 22+](https://img.shields.io/badge/node.js-22+-green.svg)](https://nodejs.org/)
[![PostgreSQL 16+](https://img.shields.io/badge/postgresql-16+-blue.svg)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

## Features

- **ReAct Agent Loop** — Thought → Action → Observation with streaming output, tool calls, and configurable iteration limits
- **PostgreSQL Context** — asyncpg connection pooling, MessagePack payloads, pgvector embeddings, reversible eviction into `context_archive`
- **Cross-Session Recall** — Agent-scoped full-text search of live and archived transcripts with anchored context windows
- **Long-Term Memory** — LLM-based extraction, importance scoring, Ebbinghaus forgetting, hybrid retrieval, approval gate, secret redaction, persona-conditioned interpretations, identity/belief drift gating
- **RAG Pipeline** — Document indexing, chunking, embedding, hybrid search (BM25 + dense + RRF), reranking
- **Multi-Agent** — Named agent definitions (DB/YAML/Soul Spec), sequential/parallel delegation, per-agent tool allowlists
- **Scheduling** — Durable jobs with heartbeat, interval, and five-field UTC cron; atomic claim via `FOR UPDATE SKIP LOCKED`
- **HTTP API** — FastAPI server with versioned `/api/v1` routes, SSE chat streaming, API-key auth, per-peer rate limiting
- **Usage Accounting** — Durable `llm_usage` with per-session/per-agent token and request budgets
- **Tool Registry** — Decorator-based with JSON Schema inference, input validation, caching
- **Skills System** — SKILL.md parser with YAML frontmatter, trigger matching, curator, hub
- **Terminal UI** — TypeScript app on [`@earendil-works/pi-tui`](https://github.com/earendil-works/pi) (MIT) — streaming Markdown, tool cards, slash-command autocomplete, session picker
- **Observability** — Sanitized audit events persisted to PostgreSQL, OpenTelemetry spans, Prometheus metrics, gateway file+console logging
- **Security** — No shell injection, no path traversal, no SSRF, rate limiting, audit logging, PII redaction, Docker-sandboxed terminal tool (opt-in)
- **Retry Logic** — Exponential backoff on LLM calls (429 rate-limit resilient)

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Terminal UI (TypeScript)              │
│              pi-tui · streaming Markdown · tools         │
└──────────────────────────┬──────────────────────────────┘
                           │ JSON-RPC 2.0 (stdio)
┌──────────────────────────▼──────────────────────────────┐
│                   Gateway (Python)                       │
│    method dispatch · turn streaming · auth · logging     │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                    Agent Core (Python)                   │
│  ReAct loop · context · memory · RAG · tools · skills    │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│              PostgreSQL + pgvector                        │
│  sessions · context_chunks · memories · llm_usage · jobs │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 22+
- PostgreSQL 16+ with pgvector extension

### Installation

```bash
# Clone and enter
git clone https://github.com/kiazh/agent-harness.git
cd agent-harness

# Python environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Terminal UI dependencies
npm install --ignore-scripts --prefix ui

# Configure
cp .env.example .env
# Edit .env: set DATABASE_URL and OPENROUTER_API_KEY

# Verify setup
ah doctor

# Initialize database schema
ah init
```

### Usage

```bash
# Interactive terminal UI
ah

# One-shot CLI
ah chat "What is the capital of France?"

# HTTP API server
ah serve --host 0.0.0.0 --port 8000

# Run tests
pytest tests/ -q
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | — |
| `OPENROUTER_API_KEY` | OpenRouter API key | — |
| `AGENT_HARNESS_MODEL` | LLM model identifier | `openrouter/free` |
| `AGENT_HARNESS_USAGE_SESSION_TOKEN_LIMIT` | Per-session token budget | unlimited |
| `AGENT_HARNESS_USAGE_AGENT_TOKEN_LIMIT` | Per-agent token budget | unlimited |
| `AGENT_HARNESS_API_KEY` | API key for HTTP server | — |
| `SEARXNG_URL` | Self-hosted SearXNG for web search | — |

## Development

```bash
# Run all tests
pytest tests/ -q

# Run with coverage
pytest tests/ -q --cov=ah --cov-report=html

# Lint
ruff check ah/
ruff format --check ah/

# Type check (UI)
cd ui && npm run typecheck

# UI tests
cd ui && npm test
```

## Project Structure

```
agent-harness/
├── ah/                    # Python package
│   ├── core/              # Agent loop, context, provider, session, usage
│   ├── db/                # asyncpg pool + schema
│   ├── gateway/           # JSON-RPC server (stdio)
│   ├── api/               # FastAPI HTTP server
│   ├── memory/            # Long-term memory subsystem
│   ├── rag/               # RAG pipeline
│   ├── tools/             # Tool registry + builtins
│   ├── skills/            # Skill parser + learning
│   ├── observability/     # Audit, metrics, tracing
│   ├── security/          # Secret resolution
│   ├── soulspec/          # Soul Spec validation
│   ├── plugins/           # Plugin system
│   ├── research/          # Research harnesses
│   └── cli/               # Typer CLI + UI launcher
├── ui/                    # TypeScript terminal UI
│   ├── src/               # pi-tui app
│   └── test/              # Vitest tests
├── tests/                 # Python test suite (1284 tests)
├── skills/                # SKILL.md skill definitions
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Testing

- **1284 Python tests** — unit, integration, chaos, load, property-based
- **54 UI tests** — gateway protocol, app state, features
- **99% coverage** on Phase 7 modules (scheduler, session recall, text search, usage, learning)

## License

[MIT](LICENSE)
