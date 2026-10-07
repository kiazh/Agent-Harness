# AgentHarness

**Self-hosted multi-agent AI you run locally — streaming terminal UI, persistent memory, tool use, scheduling, and multi-agent delegation. All state in PostgreSQL + pgvector.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Node.js 22+](https://img.shields.io/badge/node.js-22+-green.svg)](https://nodejs.org/)
[![PostgreSQL 16+](https://img.shields.io/badge/postgresql-16+-blue.svg)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/kiazh/Agent-Harness/actions/workflows/ci.yml/badge.svg)](https://github.com/kiazh/Agent-Harness/actions/workflows/ci.yml)

---

## Contents

- [What this is](#what-this-is)
- [Quick start](#quick-start)
  - [Option A — local](#option-a--local-python--node--postgres)
  - [Option B — Docker Compose](#option-b--docker-compose)
- [Terminal UI](#terminal-ui)
- [Features](#features)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [Research implemented](#research-implemented)
- [Performance & tests](#performance--tests)
- [Development](#development)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Production-ready vs research](#production-ready-vs-research)
- [Technology stack](#technology-stack)
- [License](#license)

---

## What this is

AgentHarness is a complete AI agent framework that runs on your machine. You chat with agents in a streaming terminal UI. Agents remember things across sessions, search the web, run tools in a sandbox, follow schedules, and delegate work to each other. Everything is stored in PostgreSQL with pgvector for semantic search.

```
>_ AgentHarness (v0.2.0)
   ~/agent-harness (main)
```

---

## Quick start

### One-liner (just works)

```bash
curl -fsSL https://raw.githubusercontent.com/kiazh/Agent-Harness/main/install.sh | bash
# installer: clones (or reuses checkout), venv + pip install, npm ui,
# .env with generated secrets, auto-starts local pgvector via docker if needed, ah init
```

### Prerequisites

- Python 3.11+
- Node.js 22.19+
- PostgreSQL 16+ with the pgvector extension (`pgvector/pgvector:pg16` works)

### Option A — local (Python + Node + Postgres)

```bash
git clone https://github.com/kiazh/Agent-Harness.git
cd Agent-Harness

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

npm install --ignore-scripts --prefix ui
```

```bash
cp .env.example .env   # then fill in DATABASE_URL + at least one LLM key

ah setup               # interactive: creates .env, prompts per API key
# ah setup --non-interactive  # blank .env, fill keys later via /keys

ah doctor              # checks python, node, DB, pgvector, keys
ah init                # creates schema (sessions, chunks, memories, jobs, …)
```

```bash
ah                     # interactive terminal UI
ah chat "What is the capital of France?"   # one-shot CLI
ah serve --host 127.0.0.1 --port 8000      # HTTP API server
```

### Make `ah` available everywhere (PATH)

The installer writes a shim to `~/.local/bin/ah`. If `ah` is not found, add it once:

```bash
export PATH="$HOME/.local/bin:$PATH"   # Linux/macOS/Git Bash, current shell
# persist: append that line to ~/.bashrc or ~/.zshrc, then restart the shell
```

Windows (PowerShell — run from your checkout; permanent, no `setx` truncation risk):

```powershell
$dir = "$pwd\.venv\Scripts"  # or the full path to your checkout's .venv\Scripts
$u = [Environment]::GetEnvironmentVariable('Path','User')
if (($u -split ';') -notcontains $dir) {
  [Environment]::SetEnvironmentVariable('Path', ($u.TrimEnd(';') + ';' + $dir), 'User')
}
$env:Path = $dir + ';' + $env:Path  # current shell only
# open a NEW terminal, then verify:
Get-Command ah
ah doctor
```

### Option B — Docker Compose

```bash
cp .env.example .env
# set at minimum: AGENT_HARNESS_API_KEY, OPENROUTER_API_KEY, DATABASE_URL is prewired

docker compose up --build
# app: http://127.0.0.1:8000  (ah init + ah serve run automatically)
# db:  pgvector/pgvector:pg16 with healthcheck
```

`ah setup` writes the git-ignored `.env` next to the project (or at `$AH_ENV_FILE`), one slot per model family — OpenRouter, OpenAI, Anthropic, Google, Mistral, Groq, Together, DeepSeek, xAI, Cohere. Existing values are kept when you press Enter.

---

## Terminal UI

Codex-style fullscreen TUI: `>_` brand header, filled borderless composer with accent bar, accent selection rows, braille spinner, docked composer + footer (`model · dir · branch · tokens`, then `? for shortcuts · / for commands`), follow-end scrolling, `/` menu opening upward, mouse support.

- **Streaming Markdown** — token-by-token output in a contrasting card; tool cards (`Running`/`Ran`/`Failed`) with diff coloring (`+` green, `-` red, `@@` info).
- **24 skins** — `/theme` switches live, persisted via `config.theme`: default + `codex`, `mono`, `tokyonight`, `catppuccin` (+frappe/macchiato), `dracula`, `gruvbox`, `rosepine`, `nord`, `everforest`, `matrix`, `onedark`, `one-dark`, `monokai`, `github`, `solarized`, `kanagawa`, `palenight`, `cobalt2`, `ayu`, `nightowl`, `flexoki`.
- **Keyboard** — Enter send, Shift+Enter newline, Esc stop, Tab complete, Ctrl+C exit, Ctrl+L clear, double-Esc edits last prompt. `/init`, `/review`, `/compact` (=compress), `/clear` for Codex parity.

### Keys without leaving the UI

```
/keys                  # picker: status of every key, values never shown
/keys list             # table + set/not-set
/keys set KEY VALUE    # applies now, saves to .env (--no-save = session only)
/keys clear KEY        # removes from session and .env
```

Secrets never echo, never enter history, never touch `config.yaml`.

### Models

```
/models                # picker over curated direct-provider + Ollama list
/models <query>        # filter, e.g. /models llama
/model <id>            # off-list, e.g. /model deepseek-reasoner
/provider <name>       # openrouter | openai | anthropic | google | mistral | groq | together | deepseek | xai | ollama
/effort [low|medium|high|off]  # reasoning depth (provider default when off)
/keys set DEEPSEEK_API_KEY ... # each family uses its own direct key, never routed via OpenRouter
```

### All slash commands

| Command | What it does |
|---------|--------------|
| `new`, `sessions`, `resume` | Create, list, resume sessions |
| `rename`, `goal`, `fork`, `delete`, `export` | Manage current session |
| `init`, `review` | Scaffold `AGENTS.md`; agent-driven change review |
| `search`, `recall` | Full-text session search; anchored cross-session recall |
| `context`, `compress` | Token usage; compact context |
| `memory` | `list · search · add · forget · share · pending · approve · reject · stats` |
| `skills` | `list · show · learn · proposals · approve · reject · delete · curator` |
| `agents`, `delegate` | List/show/save agents; delegate a task |
| `jobs` | `list · add · script · heartbeat · cron · on · off · delete` |
| `keys` | `list · set · clear` |
| `mode`, `approvals` | Execution mode (`ask|workspace|sandbox|full`); pending approvals (`list · allow · deny · revoke`) |
| `models`, `model`, `provider`, `effort` | Picker; custom model; provider; reasoning effort |
| `config` | Show/set settings (`--save` persists) |
| `theme` | Switch skin |
| `profile`, `profiles` | Preferences and topics |
| `status`, `usage` | Health; token/request budgets |
| `clear`, `help`, `exit` | New chat; grouped help; quit |

---

## Features

### Core agent

- **ReAct loop** — Thought → Action → Observation, streaming, configurable iterations, 50k token budget, per-tool timeouts (60s for delegation).
- **Tool registry** — decorator-based, JSON Schema inference, `bool`-safe type checks, 20k arg cap. Built-ins: `web_search`, `web_extract`, `read_file`, `write_file`, `list_files`, `search_files`, `remember`, `recall`, `delegate`, `list_agents`, `share_memory`, `session_recall`, `skill_list`, `skill_read`, `terminal`.
- **Skills** — `SKILL.md` + YAML frontmatter, trigger matching, curator health, hub publish/install, progressive disclosure (bodies via `skill_read` only), injection scanning on create/update.

### Context & memory

- **PostgreSQL context** — asyncpg pool, MessagePack payloads, pgvector, `context_archive` reversible eviction (paginated, transactional, keep-newest-10).
- **Cross-session recall** — agent-scoped FTS over live + archive with anchored windows.
- **Long-term memory** — LLM extraction with dedup (`>=` threshold + intra-batch), importance scoring (explicit always wins), Ebbinghaus forgetting, hybrid retrieval (BM25 + dense + RRF + rerank), approval gate, 17-pattern redaction (+Luhn, nested/list support), identity/belief drift gating, persona-conditioned retrieval default-on (`persona_default_emotion=trust`).
- **Compression** — budget-aware, deep-copy safe, token recount.

### RAG

Index → recursive chunk (overlap `rfind`, heading stack, `async def` aware) → OpenAI embeddings (model-keyed LRU) → hybrid BM25 + dense + RRF → Cohere/identity rerank. Redacted before store; cache keyed by model/flags.

### Multi-agent

YAML / DB / Soul Spec v0.5 definitions with tool allowlists and model overrides. Sequential + parallel delegation with hop-count inheritance. Shared memory via HMAC provenance (`AGENT_HARNESS_PROVENANCE_KEY`), quarantine on failure.

### Production

- **Usage accounting** — durable `llm_usage`, advisory-lock budgets, `reserved → complete/error`, orphan reaper.
- **Scheduling** — interval / heartbeat / UTC cron + script-only jobs; `FOR UPDATE SKIP LOCKED` claim, 300s lease + renewal, DB-clock reschedule.
- **Observability** — sanitized Postgres audit, OpenTelemetry spans, Prometheus `/metrics`, gateway log `~/.agent-harness/logs/gateway.log`.
- **Security** — allowlisted terminal (blocks `-exec`, `git -c`), jail-checked paths (symlink/O_NOFOLLOW aware), SSRF private/multicast/redirect checks, per-IP rate limit, `.env` atomic writes + newline-injection reject, Vault/AWS/file secrets.
- **Retry** — 429 backoff (provider 2x + agent guard, no double-stack); global rate buckets.

### Execution modes & permissions

`ask` (default) · `workspace` · `sandbox` · `full` — via `/mode`, `ah --mode ...`, or `ah mode`. Writes, opaque execution, and out-of-scope access pause for scoped approval instead of hard-rejecting; FULL HOST is an explicit session grant (still asks for credentials/elevation/destructive ops). Approvals: `/approvals`, TUI card, `GET/POST /api/v1/approvals`. Headless jobs pause durably as `needs_approval` and resume on a fresh claim.

### What runs when

- **Automatic:** turn pipeline (context+memory keyword retrieval, skill catalog), usage accounting, audit/metrics, redaction.
- **Conditional:** dense memory retrieval (embedding key set), document RAG (indexed docs exist), delegation (task needs a specialist), auto-compaction (over budget threshold), sandbox backend (Docker present + sandbox mode).
- **Degraded (reported, not silent):** keyword-only retrieval, passthrough rerank, unavailable sandbox.
- **Optional:** memory consolidation/extraction, learning reviews, scheduled jobs.
- **Offline research:** RL/GRPO training, LoCoMo eval, conformance trials — never part of chat.

`ah status` derives each state from live services. Full matrix: `FEATURE_LEDGER.md`.

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

UI and agent share only JSON-RPC (`communications/ui-gateway-protocol.md`). `ah serve` reuses the same core over HTTP.

---

## Configuration

Non-secrets in `~/.agent-harness/config.yaml` or `AGENT_HARNESS_<KEY>` env. **Secrets only in `.env`.**

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | — |
| `AGENT_HARNESS_TEST_DATABASE_URL` | Test DB (tests never touch prod) | — |
| `OPENROUTER_API_KEY` | Default provider key | — |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `COHERE_API_KEY` | Per-family keys | — |
| `AGENT_HARNESS_MODEL` | Model id | `openrouter/free` |
| `AGENT_HARNESS_PROVIDER` | Provider (`openrouter`, `openai`, `anthropic`, `google`, `mistral`, `groq`, `together`, `deepseek`, `xai`, `ollama`) | `openrouter` |
| `AGENT_HARNESS_REASONING_EFFORT` | Reasoning depth (`low`, `medium`, `high`, blank = provider default) | blank |
| `AGENT_HARNESS_API_KEY` | HTTP API key (fail-closed 503 if unset) | — |
| `AGENT_HARNESS_PROVENANCE_KEY` | HMAC key for memory sharing | — |
| `AGENT_HARNESS_PERSONA_MEMORY_ENABLED` | Persona rerank | `true` |
| `AGENT_HARNESS_PERSONA_DEFAULT_EMOTION` | Fallback emotion | `trust` |
| `AGENT_HARNESS_USAGE_SESSION_TOKEN_LIMIT` / `_REQUEST_LIMIT` | Session budgets (0 = unlimited) | `0` |
| `AGENT_HARNESS_USAGE_AGENT_TOKEN_LIMIT` / `_REQUEST_LIMIT` | Agent budgets | `0` |
| `AGENT_HARNESS_HTTP_RATE_LIMIT` | Req/min per IP (0 = off) | `120` |
| `AGENT_HARNESS_TERMINAL_SANDBOX` | `disabled` \| `local` \| `docker` | `disabled` |
| `SEARXNG_URL` | Self-hosted search | — |
| `AH_ENV_FILE` | `.env` override path | repo `.env` |
| `AH_GATEWAY_TOKEN` | Gateway stdio token | random per launch |

---

## HTTP API

```bash
export AGENT_HARNESS_API_KEY=...   # Bearer or X-API-Key
ah serve --host 127.0.0.1 --port 8000
```

| Method | Path | Notes |
|--------|------|-------|
| GET | `/health`, `/ready`, `/metrics` | Open (ready is generic, no oracle) |
| POST | `/api/v1/sessions` | `{title}` |
| GET | `/api/v1/sessions?limit&cursor` | Capped 200, offset-capped |
| GET/PATCH/DELETE | `/api/v1/sessions/{id}` | Resume/history, rename/goal, delete (blocked mid-turn) |
| POST | `/api/v1/sessions/{id}/prompt` | SSE stream (`text/event-stream`), `turn_timeout` enforced |
| GET | `/api/v1/sessions/{id}/context` | Redacted previews |
| GET | `/api/v1/sessions/{id}/usage` | Session + agent budgets |
| POST | `/api/v1/sessions/{id}/jobs` | `interval/heartbeat/cron`, `scriptPath` validated |
| GET/POST | `/api/v1/memory`, `/api/v1/documents` | Add/search (agent-scoped) |
| POST | `/rpc` | Generic bridge (blocks `prompt.*`, `shutdown`) |

Errors: `1002 → 404`, `1003 → 409`, `1001 → 503`, `1005 → 401`, `32601/32602 → 400`.

```bash
curl -N -H "Authorization: Bearer $AGENT_HARNESS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello"}' \
  http://127.0.0.1:8000/api/v1/sessions/<id>/prompt
```

---

## Research implemented

Five test-driven gaps:

1. **Reversible eviction** — archive-before-delete, `FOR UPDATE` claim, keep-10 floor (two concurrency wipe/double-archive bugs found by load tests).
2. **Shared identity** — HMAC provenance per memory, recipient `validate_incoming`, quarantine on tamper.
3. **Persona memory** — `PersonaMemoryStore` + Plutchik `EmotionTopology`, default-on rerank.
4. **RL memory** — action/reward + GRPO trainer (baseline, not wired into runtime).
5. **SoulSpec** — v0.5 manifest/packages, `AgentDef` round-trip, Claude/Codex adapters, most-restrictive permission merge.

Plus LoCoMo harness (`ah/research/locomo.py`), identity eval (`identity_eval.py`), memory-policy training, native-vs-LangGraph trial (`workflow_trial.py`).

---

## Performance & tests

- **Python:** 1288 passed, coverage **~80%** (gate 60%). Phase-7 modules (scheduler, recall, text_search, usage) at ~100%.
- **UI:** 85 passed, 3 e2e skipped without live DB (`npm --prefix ui test`).
- **Concurrency:** advisory-lock usage (agent→session order), atomic job claim, per-session turn locks.
- **Workflow trial:** native Postgres approval **~3.6 ms** vs LangGraph **~18.6 ms** (5.1x overhead); native retained.
- **Resilience:** 429-then-200 recovers; sustained 429 raises after retries.

```bash
pytest tests/ -q                                          # all (needs AGENT_HARNESS_TEST_DATABASE_URL for DB tests)
pytest tests/ -q -k "not test_graph" --cov=ah --cov-fail-under=60
npm --prefix ui test
```

---

## Development

```bash
pytest tests/ -q --cov=ah --cov-report=html
ruff check ah/ && ruff format --check ah/
bandit -r ah/ -ll -x ah/cli/
npm --prefix ui run typecheck && npm --prefix ui test
```

Conventions: `ruff line-length 100`, `asyncio_mode = auto`, raw SQL via asyncpg, MessagePack for blobs, `B904` (`raise … from`), no `utcnow` (aware datetimes).

---

## Project structure

```
agent-harness/
├── ah/
│   ├── core/            # agent, assembler, compression, config, container, context,
│   │                    # cron, exceptions, job_scripts, metrics, models, orchestrator,
│   │                    # provider, scheduler, serialization, session, session_recall,
│   │                    # text_search, usage, windows_job
│   ├── db/              # asyncpg pool + schema.sql
│   ├── gateway/         # JSON-RPC stdio server + features/ (agents, config, jobs,
│   │                    # memory, secrets, sessions, skills)
│   ├── api/             # FastAPI (app, auth, rate_limit)
│   ├── memory/          # store, retriever, consolidator, approval, scorer, forgetting,
│   │                    # identity, persona, policy, rl, redaction, shared_bus, user_profile
│   ├── rag/             # chunker, embedder, loaders, pipeline, reranker, search
│   ├── tools/           # registry + builtins (web/file/memory/rag/recall/skills/terminal/agents)
│   ├── skills/          # parser, registry, curator, hub, learning, runtime
│   ├── soulspec/        # schema, merge, adapters, conformance
│   ├── security/        # secrets (env/file/Vault/AWS), env_file
│   ├── observability/   # audit, metrics, tracing
│   ├── plugins/         # entry-point loader + registry
│   ├── research/        # locomo, identity_eval, train_memory_policy, workflow_*
│   └── cli/             # typer app (chat/setup/doctor/init/serve/…) + UI launcher
├── ui/src/              # pi-tui app + features/ (32 commands, 24 skins)
├── ui/test/             # node:test suites
├── tests/               # ~80 pytest modules + workflow_trial/
├── skills/              # 21 SKILL.md playbooks
├── research/            # example identity scenarios
├── Dockerfile / Dockerfile.sandbox / docker-compose.yml
└── pyproject.toml
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `database unavailable` / `503 API key not configured` | Set `DATABASE_URL`, run `ah init`; set `AGENT_HARNESS_API_KEY` |
| `pgvector` missing | Use `pgvector/pgvector:pg16`, `CREATE EXTENSION vector; pg_trgm;` |
| `401 invalid API key` | `Authorization: Bearer …` or `X-API-Key`; non-ASCII → 401 not 500 |
| `409 a turn is already running` | `prompt.cancel`, wait for `message.complete`; UI timeout is 300s = server |
| `429 rate limit exceeded` | `Retry-After` header; bump `AGENT_HARNESS_HTTP_RATE_LIMIT`, check proxy single-bucket |
| `.env path escapes allowed roots` | Default `.env` must be under cwd/repo/home/tmp; use `AH_ENV_FILE` for elsewhere |
| `script must stay inside …` | `noAgent` scripts must be relative `*.py\|*.sh\|*.bash` under `~/.agent-harness/scripts` |
| `terminal` blocked / `workdir … not within allowed` | `AGENT_HARNESS_TERMINAL_SANDBOX=local\|docker`; workdir under `agent_harness_home` |
| `graph` workflow `no pq wrapper` | `pip install "psycopg[binary]"`; native trial needs no extra deps |
| UI `TURN_IN_PROGRESS` after retry | Server holds 300s; cancel first, don't double-submit |

`ah doctor` prints versions, DB/pgvector reachability, and key presence. Gateway logs: `~/.agent-harness/logs/gateway.log`.

---

## Production-ready vs research

**Ready:** ReAct loop, Postgres context + recall, persona-on memory, RAG, delegation, scheduling, HTTP API + SSE, usage budgets, tools, skills, TUI + skins, observability, sandboxing, retries.

**Baseline (not in hot path):** RL policy trainer output; SoulSpec cross-runs (adapters, not cert); LoCoMo accuracy (needs live LLM budget).

---

## Technology stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.11+ | Async-native |
| DB | PostgreSQL 16 + pgvector | ACID + vectors |
| Queries | asyncpg + raw SQL | Control + speed |
| Blobs | MessagePack | Compact |
| Tokens | tiktoken `cl100k_base` | Accurate |
| CLI | Typer + Rich | Typed UX |
| TUI | TypeScript + pi-tui | Flicker-free |
| UI↔agent | JSON-RPC 2.0 stdio | Hermes/opencode split |
| Cloud LLM | OpenRouter + direct per-family APIs | Multi-model compat; DeepSeek/xAI/etc. hit their own endpoints |
| Local LLM | Ollama | Self-hosted |
| Search | SearXNG → DDG → Jina | Self-host first |

---

## License

[MIT](LICENSE) — © 2026 Kiarad Zafar Heidari.
