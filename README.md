# AgentHarness

**Self-hosted multi-agent AI you run locally — streaming terminal UI, persistent memory, tool use, scheduling, and multi-agent delegation. All state in PostgreSQL + pgvector.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Node.js 22+](https://img.shields.io/badge/node.js-22+-green.svg)](https://nodejs.org/)
[![PostgreSQL 16+](https://img.shields.io/badge/postgresql-16+-blue.svg)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

## What This Is

AgentHarness is a complete AI agent framework that runs on your machine. You chat with agents in a streaming terminal UI. The agents remember things across sessions, search the web, run tools in a sandbox, follow schedules, and delegate work to each other. Everything is stored in PostgreSQL with pgvector for semantic search.

The core thesis: most agent frameworks are black boxes. This one is built to be understood — every component is visible, documented, and reimplemented from first principles.

```
  ◆──────◆   AgentHarness
 │ AGENT  │   Enter to send · /keys for API keys · /models to switch models
 │ HARNESS│   /theme to reskin · /help for everything else
  ◆──────◆
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 22+
- PostgreSQL 16+ with the pgvector extension

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
```

### First-Launch Setup

```bash
# Interactive setup — creates .env, prompts for each API key
ah setup

# Or start blank (fill keys in later via /keys in the UI)
ah setup --non-interactive

# Verify, then create the database schema
ah doctor
ah init
```

`ah setup` writes the git-ignored `.env` next to the project (or at `$AH_ENV_FILE`), with one slot per model family — OpenRouter, OpenAI, Anthropic, Google, Mistral, Groq, Together, DeepSeek, xAI, Cohere. Existing values are kept when you press Enter; `--force` starts over.

### Usage

```bash
# Interactive terminal UI
ah

# One-shot CLI
ah chat "What is the capital of France?"

# HTTP API server
ah serve --host 0.0.0.0 --port 8000
```

---

## Terminal UI

### Streaming, Cards, and Skins

- **Streaming Markdown** — token-by-token assistant output rendered in a contrasting card, with tool-call cards showing progress.
- **Six skins** — `/theme` switches the full palette plus header ASCII art, live, no restart. Choices: `default`, `midnight`, `forest`, `sunset`, `grape`, `mono`. Saved via `config.theme`, so your skin survives restarts.
- **Session picker** — searchable browser for resuming past sessions.
- **Keyboard** — Enter sends, Shift+Enter newline, Esc stops a reply, Tab completes, Ctrl+C exits.

### API Keys Without Leaving the UI

```
/keys                  # picker menu — status of every key, values never shown
/keys list             # table of keys + set/not-set status
/keys set KEY VALUE    # applies now, saves to .env (add --no-save for session only)
/keys clear KEY        # removes from session and .env
```

Setting a key takes effect on the next turn. Secrets are never echoed, never enter input history, and never touch `config.yaml`.

### Model Switching

```
/models                # picker over a curated OpenRouter + Ollama list
/models <query>        # filter the list, e.g. /models llama
/model <id>            # anything off-list, e.g. /model anthropic/claude-3.5-sonnet
/provider <name>       # openrouter | ollama
```

### All 30 Slash Commands

| Command | What it does |
|---------|--------------|
| `new`, `sessions`, `resume` | Create, list, and resume sessions |
| `rename`, `goal`, `fork`, `delete`, `export` | Manage the current session |
| `search`, `recall` | Full-text session search; anchored cross-session recall |
| `context`, `compress` | Inspect token usage; compact context |
| `memory` | `list · search · add · forget · share · pending · approve · reject · stats` |
| `skills` | `list · show · learn · proposals · approve · reject · delete · curator` |
| `agents`, `delegate` | List/show/save agents; delegate a task |
| `jobs` | `list · add · script · heartbeat · cron · on · off · delete` |
| `keys` | API-key menu (`list · set · clear`) |
| `models`, `model`, `provider` | Curated model picker; custom model; provider |
| `config` | Show/set settings (`--save` persists to `config.yaml`) |
| `theme` | Switch UI skin |
| `profile`, `profiles` | Your preferences and topics |
| `status`, `usage` | System health; token/request budgets |
| `clear`, `help`, `exit` | Screen and app control |

---

## Features

### Core Agent

- **ReAct Loop** — Thought → Action → Observation with streaming output and configurable iteration limits.
- **Tool Registry** — decorator-based, JSON Schema inference, input validation, result caching. Built-ins: `web_search`, `web_extract`, `read_file`, `write_file`, `list_files`, `search_files`, `remember`, `recall`, `delegate`, `list_agents`, `share_memory`, `session_recall`, `skill_list`, `skill_read`, `terminal` (sandboxed).
- **Skills System** — `SKILL.md` files with YAML frontmatter. Trigger matching, curator health reports, hub publish/install, and progressive disclosure (agents list and read skills; bodies are never auto-inserted into prompts).

### Context & Memory

- **PostgreSQL Context** — asyncpg pooling, MessagePack payloads, pgvector embeddings, reversible eviction into `context_archive` (old chunks stay searchable, never deleted).
- **Cross-Session Recall** — agent-scoped full-text search across live and archived transcripts with anchored context windows.
- **Long-Term Memory** — LLM extraction, importance scoring, Ebbinghaus forgetting, hybrid retrieval (BM25 + dense + RRF), approval gate, secret redaction, identity/belief drift gating — and **persona-conditioned retrieval on by default** (emotion-weighted interpretations via `persona_default_emotion`, disable with `persona_memory_enabled=false`).
- **Context Compression** — token-budget-aware, preserves recent context.

### RAG

- **Document Pipeline** — indexing, recursive chunking, OpenAI embeddings, hybrid search (BM25 + dense + RRF), reranking with Cohere or identity-aware rerankers.

### Multi-Agent

- **Agent Definitions** — YAML files, database records, or Soul Spec packages, each with its own system prompt, tool allowlist, and model override.
- **Orchestration** — sequential and parallel delegation with bounded context handoff and hop-count guards.
- **Shared Memory** — gated cross-agent delivery with HMAC-signed provenance.

### Production

- **HTTP API** — FastAPI with versioned `/api/v1` routes, SSE chat streaming, API-key auth, per-peer rate limiting, `/health`, `/ready`, `/metrics`.
- **Usage Accounting** — durable `llm_usage` with per-session/per-agent token and request budgets that survive missing provider metadata.
- **Scheduling** — durable interval, heartbeat, and five-field UTC cron jobs; atomic claim via `FOR UPDATE SKIP LOCKED`; script-only jobs that run without an LLM.
- **Observability** — sanitized audit events in PostgreSQL, OpenTelemetry spans, Prometheus metrics, gateway file+console logging (`~/.agent-harness/logs/gateway.log`).
- **Security** — command allowlist (no shell injection), base-directory validation (no path traversal), private-IP rejection (no SSRF), rate limiting, audit logging, PII redaction, opt-in Docker-sandboxed terminal.
- **Retry Logic** — exponential backoff on LLM calls (1s/2s/4s/8s); non-429 errors pass through immediately.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Terminal UI (TypeScript)              │
│         pi-tui · streaming Markdown · skins · tools      │
└──────────────────────────┬──────────────────────────────┘
                            │ JSON-RPC 2.0 (stdio)
┌──────────────────────────▼──────────────────────────────┐
│                   Gateway (Python)                       │
│  method dispatch · turn streaming · auth · secrets · log │
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

The UI and the agent share only the JSON-RPC protocol — the same split Hermes Agent (`ui-tui` ↔ `tui_gateway`) and opencode use. The HTTP API (`ah serve`) sits beside the gateway and reuses the same core.

---

## Research Implemented

Five research gaps, each test-driven:

### 1. Reversible Eviction

Context windows are finite; deleting old chunks loses information. Evicted chunks move to `context_archive` instead — searchable alongside live context, paginated (LIMIT 100) and archived+deleted in one transaction. Load testing found two real bugs: concurrent eviction could wipe the keep-newest-10 floor (fixed with `NOT IN (SELECT id FROM context_archive)`) and double-archive rows (fixed with a `FOR UPDATE` claim).

### 2. Shared Memory Identity Propagation

When Agent A shares a memory with Agent B, B needs provenance. Each memory carries an HMAC signature from its source agent; recipients verify before accepting. Tampered or unsigned memories are rejected.

### 3. Persona-Conditioned Memory

The same fact means different things to different personas. `PersonaMemoryStore` + `EmotionTopology` (Plutchik wheel) tag memories with emotional context and re-rank retrieval per persona. Now default-on in the runtime.

### 4. End-to-End RL for Memory

Which memories to keep, forget, or promote? RL action/reward definitions plus a group-relative policy trainer learn from outcome data. Research baseline — not wired into the runtime.

### 5. SoulSpec Standard

Agent definitions are fragmented across frameworks. Soul Spec v0.5 manifest validation, package files, and `AgentDef` conversion, with cross-framework adapters (not compatibility certification).

---

## Configuration

Non-secret settings live in `~/.agent-harness/config.yaml` (or `AGENT_HARNESS_<KEY>` env vars); **secrets live only in `.env`**:

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | — |
| `OPENROUTER_API_KEY` | OpenRouter API key (default provider) | — |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `COHERE_API_KEY` | Per-family keys, as needed | — |
| `AGENT_HARNESS_MODEL` | LLM model identifier | `openrouter/free` |
| `AGENT_HARNESS_USAGE_SESSION_TOKEN_LIMIT` | Per-session token budget | unlimited |
| `AGENT_HARNESS_USAGE_AGENT_TOKEN_LIMIT` | Per-agent token budget | unlimited |
| `AGENT_HARNESS_API_KEY` | API key for HTTP server | — |
| `AGENT_HARNESS_PROVENANCE_KEY` | HMAC key for memory sharing | — |
| `AGENT_HARNESS_PERSONA_MEMORY_ENABLED` | Persona-conditioned retrieval | `true` |
| `AGENT_HARNESS_PERSONA_DEFAULT_EMOTION` | Fallback emotion weight | `trust` |
| `SEARXNG_URL` | Self-hosted SearXNG for web search | — |

---

## Performance

- **Test suite** — 1284+ Python tests pass (~83s); 63 UI tests pass; 99.81% coverage on Phase 7 modules (scheduler, session_recall, text_search, usage at 100%).
- **Concurrency** — two real eviction bugs found via load testing; usage accounting uses advisory locks with fixed agent→session ordering to prevent deadlocks.
- **Workflow trial** — native PostgreSQL start-plus-approval **3.618 ms** vs LangGraph **18.608 ms** (5.1x overhead); native path retained, LangGraph optional.
- **Provider resilience** — OpenRouter free tier hard-429s under sustained calls; verified 429-then-200 recovers, all-429 raises after 5 attempts.

Coverage journey (Phase 7: **65% → 99.81%**):

| Module | Before | After |
|--------|--------|-------|
| scheduler.py | 45% | 100% |
| session_recall.py | 60% | 100% |
| text_search.py | 71% | 100% |
| usage.py | 84% | 100% |
| learning.py | 82% | 99% |

---

## Development

```bash
# Run all tests
pytest tests/ -q

# Coverage
pytest tests/ -q --cov=ah --cov-report=html

# Lint
ruff check ah/
ruff format --check ah/

# Type check + UI tests
cd ui && npm run typecheck
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
│   ├── security/          # Secrets, .env management
│   ├── soulspec/          # Soul Spec validation
│   ├── plugins/           # Plugin system
│   ├── research/          # LoCoMo, workflow trial, memory-policy training
│   └── cli/               # Typer CLI (chat, setup, doctor, init, serve) + UI launcher
├── ui/                    # TypeScript terminal UI
│   ├── src/               # pi-tui app (30 slash commands, 6 skins)
│   └── test/              # node:test suites
├── tests/                 # Python test suite
├── skills/                # SKILL.md skill definitions
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Production-Ready vs. Research

**Production-ready:** ReAct loop, PostgreSQL context, cross-session recall, long-term memory (now persona-conditioned by default), RAG, multi-agent delegation, scheduling, HTTP API, usage accounting, tool registry, skills, terminal UI with skins, observability, security sandboxing, retry logic.

**Research baseline (not wired into runtime):** RL-trained memory policy; SoulSpec cross-framework runs (adapters, not certification); LoCoMo answer accuracy (harness works, needs live LLM budget).

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
