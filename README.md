# AgentHarness

**Self-hosted multi-agent AI orchestration framework with PostgreSQL-backed context, RAG, long-term memory, and a streaming terminal UI.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Node.js 22+](https://img.shields.io/badge/node.js-22+-green.svg)](https://nodejs.org/)
[![PostgreSQL 16+](https://img.shields.io/badge/postgresql-16+-blue.svg)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

## What This Is

AgentHarness is a complete, self-hosted AI agent framework you run locally. You get a streaming terminal UI where you chat with AI agents that have persistent memory, web access, tool use, scheduling, and the ability to delegate to other agents. All state lives in PostgreSQL with pgvector for semantic search.

The core thesis: most agent frameworks are black boxes. This one is built to be understood — every component is visible, documented, and reimplemented from first principles.

---

## Features

### Core Agent

- **ReAct Loop** — Thought → Action → Observation with streaming output, tool calls, and configurable iteration limits
- **Tool Registry** — Decorator-based with JSON Schema inference, input validation, and caching. Built-in tools: web_search, web_extract, read_file, write_file, list_files, remember, recall, delegate, session_recall, skill_list, skill_read, terminal (sandboxed)
- **Skills System** — SKILL.md files with YAML frontmatter. Trigger matching, curator health reports, hub integration, and agent-facing progressive disclosure (the agent can list and read skills, but skill bodies aren't auto-inserted into prompts)

### Context & Memory

- **PostgreSQL Context** — asyncpg connection pooling, MessagePack payloads, pgvector embeddings, reversible eviction into `context_archive` (old chunks are searchable, not deleted)
- **Cross-Session Recall** — Agent-scoped full-text search across live and archived transcripts with anchored context windows
- **Long-Term Memory** — LLM-based extraction, importance scoring, Ebbinghaus forgetting, hybrid retrieval (BM25 + dense + RRF), approval gate for sensitive memories, secret redaction, persona-conditioned interpretations, identity/belief drift gating
- **Context Compression** — Token-budget-aware compression that preserves recent context

### RAG

- **Document Pipeline** — Indexing, recursive chunking, OpenAI embeddings, hybrid search (BM25 + dense + RRF), reranking with Cohere or identity-aware rerankers

### Multi-Agent

- **Agent Definitions** — YAML files, database records, or Soul Spec packages. Each agent has its own system prompt, tool allowlist, and model override
- **Orchestration** — Sequential and parallel delegation with bounded context handoff and hop-count guards
- **Shared Memory** — Gated cross-agent memory delivery with HMAC-signed provenance

### Production

- **HTTP API** — FastAPI server with versioned `/api/v1` routes, SSE chat streaming, API-key auth, per-peer rate limiting, `/health`, `/ready`, `/metrics`
- **Usage Accounting** — Durable `llm_usage` with per-session/per-agent token and request budgets that can't be bypassed by missing provider metadata
- **Scheduling** — Durable jobs with interval, heartbeat, and five-field UTC cron; atomic claim via `FOR UPDATE SKIP LOCKED`; script-only jobs that run without an LLM
- **Observability** — Sanitized audit events persisted to PostgreSQL, OpenTelemetry spans, Prometheus metrics, gateway file+console logging (rotating file handler at `~/.agent-harness/logs/gateway.log`)
- **Security** — No shell injection (command allowlist), no path traversal (base directory validation), no SSRF (URL validation against private IPs), rate limiting, audit logging, PII redaction, Docker-sandboxed terminal tool (opt-in)
- **Retry Logic** — Exponential backoff on LLM calls (4 retries, 1s/2s/4s/8s delays); non-429 errors pass through immediately

### Terminal UI

- **Streaming Markdown** — Token-by-token output with tool cards showing progress
- **Slash Commands** — 27 commands with autocomplete: sessions, context, memory, skills, agents, jobs, config, profiles, usage, and more
- **Session Picker** — Searchable browser for resuming past sessions
- **Keyboard** — Enter sends, Shift+Enter newline, Esc stops a reply, Tab completes, Ctrl+C exits

---

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

The UI and the agent only share the JSON-RPC protocol — the same split Hermes Agent (`ui-tui` ↔ `tui_gateway`) and opencode use.

---

## Research Implemented

Five research gaps were identified and implemented with test-driven development:

### 1. Reversible Eviction

**Problem:** Context windows are finite. When a session grows too long, old chunks must be evicted — but deleting them loses information.

**Solution:** Evicted chunks move to `context_archive` instead of being deleted. The archive is searchable alongside live context. Eviction uses cursor-based pagination (LIMIT 100 per page) and batch archive+delete in a single transaction.

**Concurrency bugs found via load testing:**
- **Data loss:** Concurrent eviction could delete all chunks including the keep-newest-10 floor. Fixed by adding `NOT IN (SELECT id FROM context_archive)` to the selection query.
- **Duplicate archive rows:** Concurrent callers could archive the same chunk twice. Fixed by adding `FOR UPDATE` guard in `archive_and_delete`.

### 2. Shared Memory Identity Propagation

**Problem:** When Agent A shares a memory with Agent B, Agent B needs to know it came from Agent A and whether to trust it.

**Solution:** HMAC-signed provenance. Each memory carries a signature from the source agent. The recipient verifies the signature before accepting the memory. Tampered or unsigned memories are rejected.

### 3. Persona-Conditioned Memory

**Problem:** The same fact means different things depending on who's remembering it.

**Solution:** PersonaMemoryStore with EmotionTopology. Memories are tagged with emotional context and retrieved differently based on the agent's persona.

### 4. End-to-End RL for Memory

**Problem:** Which memories should be kept, forgotten, or promoted? Manual heuristics don't scale.

**Solution:** RL action/reward definitions for memory operations (keep, forget, promote, demote). A group-relative memory-policy trainer learns optimal policies from outcome data. This is a research baseline — not wired into the runtime.

### 5. SoulSpec Standard

**Problem:** Agent definitions are fragmented across frameworks. There's no portable format.

**Solution:** Soul Spec v0.5 manifest validation, package files, and AgentDef conversion. Cross-framework adapters (not compatibility certification).

---

## Performance

### Test Suite

- **1284 Python tests** pass, 0 failed, 0 skipped (~83s)
- **54 UI tests** pass, 0 fail
- **99.81% coverage** on Phase 7 modules (scheduler 100%, session_recall 100%, text_search 100%, usage 100%, learning 99%)

### Concurrency

- Two real bugs found via load testing (see research section above)
- Concurrent eviction now preserves exactly the newest 10 chunks with no duplicates
- Usage accounting uses advisory locks with fixed lock ordering (agent→session) to prevent deadlocks

### Workflow Trial (Native PostgreSQL vs LangGraph)

- Native start-plus-approval: **3.618 ms** mean
- LangGraph start-plus-approval: **18.608 ms** mean
- **5.1x latency overhead** for LangGraph
- Decision: native PostgreSQL path retained, LangGraph stays optional

### LLM Provider Resilience

- OpenRouter free tier hard-429s on sustained calls
- Retry logic verified: 429-then-200 succeeds, all-429 raises after 5 attempts, non-429 passes through immediately
- Live LoCoMo run blocked by 429 — needs funded API budget

### Coverage Improvement

Phase 7 went from **65% → 99.81%** through targeted test writing:

| Module | Before | After |
|--------|--------|-------|
| scheduler.py | 45% | 100% |
| session_recall.py | 60% | 100% |
| text_search.py | 71% | 100% |
| usage.py | 84% | 100% |
| learning.py | 82% | 99% |

---

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

In the UI: Enter sends, Shift+Enter adds a line, Esc stops a reply, `/` opens command autocomplete, Tab completes file paths, Ctrl+C exits.

---

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
| `AGENT_HARNESS_PROVENANCE_KEY` | HMAC signing key for memory sharing | — |
| `SEARXNG_URL` | Self-hosted SearXNG for web search | — |

---

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

---

## Project Structure

```
agent-harness/
├── ah/                    # Python package
│   ├── core/              # Agent loop, context, provider, session, usage, scheduler
│   ├── db/                # asyncpg pool + schema
│   ├── gateway/           # JSON-RPC server (stdio) + feature handlers
│   ├── api/               # FastAPI HTTP server
│   ├── memory/            # Long-term memory subsystem
│   ├── rag/               # RAG pipeline
│   ├── tools/             # Tool registry + builtins
│   ├── skills/            # Skill parser + learning
│   ├── observability/     # Audit, metrics, tracing
│   ├── security/          # Secret resolution
│   ├── soulspec/          # Soul Spec validation
│   ├── plugins/           # Plugin system
│   ├── research/          # Research harnesses (LoCoMo, workflow trial)
│   └── cli/               # Typer CLI + UI launcher
├── ui/                    # TypeScript terminal UI
│   ├── src/               # pi-tui app (27 slash commands)
│   └── test/              # Vitest tests
├── tests/                 # Python test suite (1284 tests)
├── skills/                # SKILL.md skill definitions
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Production-Ready vs. Research

**Production-ready:**
- ReAct loop, PostgreSQL context, cross-session recall
- Long-term memory (extraction, scoring, forgetting, retrieval, persona-conditioned by default)
- RAG pipeline, multi-agent delegation, scheduling
- HTTP API, usage accounting, tool registry, skills system
- Terminal UI, observability (audit + metrics + logging)
- Security (sandbox, SSRF protection, rate limiting, PII redaction)
- Retry logic with exponential backoff

**Research baseline (not wired into runtime):**
- RL-trained memory policy
- SoulSpec cross-framework runs (adapters, not certification)
- LoCoMo answer accuracy (harness works, needs live LLM budget)

---

## Technology Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Language | Python 3.11+ | Async-native |
| Database | PostgreSQL 16+ + pgvector | ACID + vector search |
| ORM/Query | asyncpg + raw SQL | Performance, control |
| Binary format | MessagePack | Compact, fast |
| Token counting | tiktoken (cl100k_base) | Accurate token counts |
| CLI | Typer | Type-hint-driven |
| Terminal UI | TypeScript + @earendil-works/pi-tui | Differential rendering, flicker-free |
| UI ↔ agent | JSON-RPC 2.0 over stdio | Same split as Hermes / opencode |
| Provider | OpenRouter | Multi-model, OpenAI-compatible |
| Local LLM | Ollama (optional) | Free, self-hosted |

---

## License

[MIT](LICENSE)
