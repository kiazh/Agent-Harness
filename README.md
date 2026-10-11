# AgentHarness

**Self-hosted general-purpose AI agent you run locally — streaming terminal UI, persistent memory, tool use, subagent fan-out, scheduling, and 42 bundled skill workflows that activate only on real evidence. All state in PostgreSQL + pgvector.**

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
  - [Persona and runtime learning](#persona-and-runtime-learning)
- [Features](#features)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [Research implemented](#research-implemented)
- [Performance & tests](#performance--tests)
- [Development](#development)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Production release status and research](#production-release-status-and-research)
- [Technology stack](#technology-stack)
- [License](#license)

---

## What this is

AgentHarness is a self-hosted AI agent framework that runs on your machine. It is a **general agent first**: it answers greetings and questions directly, acts on tasks with the right tool for the job, and never goes looking for work. Specialist skill workflows (42 curated, bundled offline) activate only on clear evidence — a named skill, a trigger hit, or genuinely matching task language. You chat in a streaming terminal UI. Agents remember things across sessions, search the web, run tools in a sandbox, follow schedules, and fan out to subagents for parallelizable work. Everything is stored in PostgreSQL with pgvector for semantic search.

Default-on runtime self-improvement (RSI) retains learned preferences and uses execution evidence and human feedback to adapt memory retrieval, task approaches, relevant skill ranking and available-tool ordering. Persona revisions persist across sessions and models, with inspectable history and rollback.

The production completion release is still gated on verification recorded in [the completion ledger](docs/plans/PRODUCTION_COMPLETION_PROGRESS.md).

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

`ah setup` writes the git-ignored `.env` next to the project (or at `$AH_ENV_FILE`), one slot per model family — OpenRouter, OpenAI, Anthropic, Google, Mistral, Groq, Together, DeepSeek, xAI, Cohere — plus the default provider (`AGENT_HARNESS_PROVIDER`) and model (`AGENT_HARNESS_MODEL`). Existing values are kept when you press Enter.

---

## Terminal UI

Codex-style fullscreen TUI: `>_` brand header, filled borderless composer with accent bar, accent selection rows, braille spinner, docked composer + footer (`model · dir · branch · tokens`, then `? for shortcuts · / for commands`), follow-end scrolling, `/` menu opening upward, mouse support. Response boxes never touch: one blank row separates transcript blocks and the chrome sections.

- **Streaming Markdown** — token-by-token output in a contrasting card; tool cards (`Running`/`Ran`/`Failed`) with diff coloring (`+` green, `-` red, `@@` info). Notices carry text markers (`✓`/`!`/`✗`) as well as color, so state survives monochrome skins.
- **Interactive management commands** — bare `/persona`, `/growth`, `/keys`, `/models`, `/theme`, `/sessions` and friends open pickers/forms instead of making you memorize flags. Expert argument forms still work (`/persona --clear`, `/keys set …`).
- **Persona** — `/persona` opens Add/Reset: Add edits the installation default in a multiline editor (2000 chars, prefilled); Reset confirms yes/no and keeps learned notes. The chat draft is never submitted as a persona.
- **Quiet learning feedback** — a tiny square at the bottom-right pulses briefly when growth evidence arrives, at most once every 30 seconds. Details stay in `/growth show`; routine updates do not add chat messages.
- **21 skins** — `/theme` switches live **without clearing the chat** and persists via `config.theme`: default, `codex`, `mono`, `tokyonight`, `catppuccin`, `dracula`, `gruvbox`, `rosepine`, `nord`, `everforest`, `matrix`, `cobalt2`, `ayu`, `nightowl`, `flexoki`, `onedark`, `monokai`, `github`, `solarized`, `kanagawa`, `palenight`.
- **Keyboard** — `?` on empty input opens shortcuts; F1 opens them while keeping your draft. Arrow keys browse the panel, Enter/Esc close it. Enter sends, Shift+Enter adds a newline, Esc stops, Tab completes, Ctrl+C stops/clears/exits, Ctrl+L clears, and double-Esc edits the last prompt. `/init`, `/review`, `/compact` (=compress), `/clear` for Codex parity.

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
/models                # picker over curated direct-provider + Ollama list (this session)
/models <query>        # filter, e.g. /models llama
/default-model         # menu to pick the saved default model
/default-model <id>    # off-list default, e.g. /default-model deepseek-reasoner
/provider <name>       # openrouter | openai | anthropic | google | mistral | groq | together | deepseek | xai | ollama
/effort [low|medium|high|off]  # reasoning depth (provider default when off)
/keys set DEEPSEEK_API_KEY ... # each family uses its own direct key, never routed via OpenRouter
```

### Persona and runtime learning

RSI and personality growth are **enabled by default**. After updating, restart the harness; startup applies the additive growth migration, and `ah init` installs it on a fresh database. The same agent lifecycle serves CLI, TUI/gateway, HTTP, scheduled jobs and delegation.

Use `/persona` to choose Add or Reset. Add saves your requested tone and character as the installation default; Reset keeps learned notes. Learned preferences, styles and lessons belong to each agent and carry into later sessions automatically.

```text
/persona
/growth                         # View progress / Give feedback / Teach / Forget / Restore
/growth teach style verbosity Prefer short answers with one concrete example
/growth feedback good           # rate the latest completed, unrated approach
/growth feedback bad <episode-id>
/growth show                    # notes, operational evidence, human ratings, trial and history
/growth rollback                # choose a retained revision and confirm restoration
/growth rollback 0              # original baseline, if still retained
```

The model can also save a durable preference or verified lesson with `learn_personality` during normal tool use. Agent-written notes start a persona trial. Three human ratings of completed turns using that exact revision retain the trial when their average is nonnegative or restore the previous stable baseline when negative. Direct human teaching establishes a stable revision; later ratings of older trials cannot undo that choice.

Actual execution outcomes provide weak operational evidence, and human ratings replace an episode's automatic reward. Successful response prose alone earns no positive evidence; cancelled and approval-paused turns do not train. Automatic evidence can adjust routing but cannot promote a persona trial. Relevant skill filters, explicit skill requests, tool allowlists and permission checks remain authoritative. RSI adds no extra per-turn model request.

History retains up to 50 revisions and 500 episodes per agent. Restore replaces learned notes and retrieval-policy weights, records a new revision, and preserves routing experience. Inspection distinguishes human ratings from operational observations. These are runtime learning heuristics; LLM weight training, autonomous application rewriting and measured quality or speed gains are outside the verified feature.

CLI management uses the same store:

```bash
ah growth show --session <session-id>
ah growth rollback <revision> --session <session-id>
```

Set `rsi_enabled: false` to pause automatic RSI evidence and routing adaptation while keeping notes and human feedback available. Setting `personality_growth_enabled` or `memory_enabled` to false also stops automatic personality growth and injection. Inspection, explicit forgetting and restoration remain available. See the [operating guide](docs/PERSONALITY_GROWTH.md), [design](docs/plans/PERSONA_RSI_DESIGN.md) and [implementation evidence](docs/plans/PERSONA_RSI_PLAN.md).

### All slash commands

| Command | What it does |
|---------|--------------|
| `new`, `sessions`, `resume` | Create, list, resume sessions (menu when empty) |
| `rename`, `goal`, `fork`, `delete`, `export` | Manage current session |
| `init`, `review` | Scaffold `AGENTS.md`; agent-driven change review |
| `search`, `recall` | Full-text session search; anchored cross-session recall |
| `context`, `compress` | Token usage; compact context |
| `memory` | `list · search · add · forget · share · pending · approve · reject · stats` |
| `growth` | Interactive learning menu; `show · teach · feedback · forget · rollback` |
| `persona` | Manage the default persona (menu when empty; `--clear` resets) |
| `skills` | `list · show · learn · proposals · approve · reject · delete · curator` |
| `agents`, `delegate` | List/show/save agents; delegate a task (parallel fan-out) |
| `jobs` | `list · add · script · heartbeat · cron · on · off · delete` |
| `keys` | `list · set · clear` (menu when empty) |
| `mode`, `approvals` | Execution mode (`ask|workspace|sandbox|full`); pending approvals (`list · allow · deny · revoke`) |
| `models`, `default-model`, `provider`, `effort` | Session picker; saved default model; provider; reasoning effort |
| `config` | Show/set settings (`--save` persists) |
| `theme` | Switch skin (menu when empty; chat stays) |
| `profile`, `profiles` | Preferences and topics |
| `status`, `usage` | Health; token/request budgets |
| `clear`, `help`, `exit` | New chat; grouped help; quit |

---

## Features

### Core agent

- **ReAct loop** — Thought → Action → Observation, streaming, configurable iterations, per-tool timeouts (60s for delegation). Independent tool calls in one block run concurrently.
- **Per-model turn budget** — the spend cap scales with the turn model's documented context window (quarter-window, 8k floor, 50k ceiling) instead of a fixed 8k; an explicit `context_budget` always wins. See `ah/core/context_windows.py`.
- **Bounded history** — tool results are append-capped and stale rounds condense to head+tail previews before each LLM call (paged skill reads exempt so offsets keep resolving); full records stay in Postgres for recall/compaction.
- **Tool registry** — decorator-based, JSON Schema inference, `bool`-safe type checks, 20k arg cap. Built-ins: `web_search`, `web_extract`, `read_file`, `write_file`, `list_files`, `search_files`, `remember`, `recall`, `delegate`, `list_agents`, `share_memory`, `session_recall`, `skill_list`, `skill_read`, `terminal`.
- **Skills** — 42 curated [ECC workflows](docs/ECC_SKILLS.md) bundled offline. They activate only on clear evidence (named skill, whole-word trigger, or genuinely matching task); vague input gets a generalist, and turns with no match inject zero skill context. Bodies load bounded and lazily; no manual activation or enable flag. Local skills retain YAML metadata, curator health, hub publish/install and injection scanning.

### Subagents

- **Roster** — three native types plus your customs (`/agents save`): `harness` (general, all tools), `researcher` (read-only gathering), `coder` (edits code, runs tests). `list_agents` shows who is available.
- **Auto fan-out** — tasks that split into independent subtasks delegate in parallel: one `delegate` call per subtask in a single block (max three), type picked from the roster, standalone task each. Children inherit the parent's restrictions, cannot approve actions, and time out fast. Delegation is never used to evade approval.
- **Safety** — hop-count depth cap, session ownership checks, exact approval binding per action.

### Context & memory

- **PostgreSQL context** — asyncpg pool, MessagePack payloads, pgvector, `context_archive` reversible eviction (paginated, transactional, keep-newest-10).
- **Cross-session recall** — agent-scoped FTS over live + archive with anchored windows.
- **Long-term memory** — LLM extraction with dedup (`>=` threshold + intra-batch), importance scoring (explicit always wins), Ebbinghaus forgetting, hybrid retrieval (BM25 + dense + RRF + rerank), approval gate, 17-pattern redaction (+Luhn, nested/list support), identity/belief drift gating, persona-conditioned retrieval default-on (`persona_default_emotion=trust`).
- **Persona and runtime self-improvement** — durable preferences, style and lessons; evidence-backed memory retrieval, task approaches, relevant skill ranking and available-tool ordering. Enabled by default, with persona trials, revision history and rollback through the interactive `/growth` menu. No extra per-turn model request. See [personality growth](docs/PERSONALITY_GROWTH.md).
- **Compression** — budget-aware, deep-copy safe, token recount.

### RAG

Index → recursive chunk (overlap `rfind`, heading stack, `async def` aware) → OpenAI embeddings (model-keyed LRU) → hybrid BM25 + dense + RRF → Cohere/identity rerank. Redacted before store; cache keyed by model/flags.

### Production

- **Usage accounting** — durable `llm_usage`, advisory-lock budgets, `reserved → complete/error`, orphan reaper. Streamed turns report real token counts, including direct-vendor streams (`include_usage` where documented, estimates elsewhere) — the footer never reads zero on a non-empty turn.
- **Scheduling** — interval / heartbeat / UTC cron + script-only jobs; `FOR UPDATE SKIP LOCKED` claim, 300s lease + renewal, DB-clock reschedule.
- **Observability** — sanitized Postgres audit, OpenTelemetry spans, Prometheus `/metrics`, gateway log `~/.agent-harness/logs/gateway.log`.
- **Security** — allowlisted terminal in `ask` mode (blocks `-exec`, `git -c`; `full` mode deliberately resolves host commands instead), jail-checked paths (symlink/O_NOFOLLOW aware), SSRF private/multicast/redirect checks, per-IP rate limit, `.env` atomic writes + newline-injection reject, Vault/AWS/file secrets.
- **Retry** — 429 backoff (provider 2x + agent guard, no double-stack); global rate buckets.

### Execution modes & permissions

`ask` (default) · `workspace` · `sandbox` · `full` — via `/mode`, `ah --mode ...`, or `ah mode`. Session activation (`scope=session`, the default) never changes the global default future sessions inherit; only explicit `scope=global` (or saving `execution_mode`) does. To run everything full by default, persist `execution_mode: full` — new sessions auto-grant at creation. Routine project reads/writes, local Git inspection/add/commit, built-in web reads, document retrieval, and common project test/build/lint commands proceed without approval. Credential access, elevation, security-settings changes, and destructive system ops still pause for exact approval **in every mode, including full**; FULL HOST is an explicit session grant, never sensitive consent. Approval cards show the exact command (argv), cwd, backend, timeout, and file-write diffs. Approvals: `/approvals`, TUI card, `GET/POST /api/v1/approvals`. Headless jobs pause durably as `needs_approval` (resumable via `jobs.resume`) and resume on a fresh claim.

### What runs when

- **Automatic:** turn pipeline (context+memory keyword retrieval, gated skill routing), usage accounting, audit/metrics, redaction.
- **Conditional:** dense memory retrieval (embedding key set), document RAG (indexed docs exist), delegation (task splits into subtasks), auto-compaction (over budget threshold), sandbox backend (Docker present + sandbox mode).
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
| `AGENT_HARNESS_EXECUTION_MODE` | `ask` \| `workspace` \| `sandbox` \| `full` (saved default for new sessions) | `ask` |
| `AGENT_HARNESS_CONTEXT_BUDGET` | Explicit per-turn spend cap (wins over the per-model derived budget) | `8000` |
| `AGENT_HARNESS_API_KEY` | HTTP API key (fail-closed 503 if unset) | — |
| `AGENT_HARNESS_PROVENANCE_KEY` | HMAC key for memory sharing | — |
| `AGENT_HARNESS_PERSONA_DESCRIPTION` | Requested tone and character; `/persona` saves the config equivalent | empty |
| `AGENT_HARNESS_MEMORY_ENABLED` | Automatic memory retrieval and personality growth | `true` |
| `AGENT_HARNESS_PERSONALITY_GROWTH_ENABLED` | Learned personality notes and live feedback decisions | `true` |
| `AGENT_HARNESS_RSI_ENABLED` | Automatic runtime evidence, task strategy and capability ranking | `true` |
| `AGENT_HARNESS_PERSONA_MEMORY_ENABLED` | Persona rerank | `true` |
| `AGENT_HARNESS_PERSONA_DEFAULT_EMOTION` | Fallback emotion | `trust` |
| `AGENT_HARNESS_USAGE_SESSION_TOKEN_LIMIT` / `_REQUEST_LIMIT` | Session budgets (0 = unlimited) | `0` |
| `AGENT_HARNESS_USAGE_AGENT_TOKEN_LIMIT` / `_REQUEST_LIMIT` | Agent budgets | `0` |
| `AGENT_HARNESS_HTTP_RATE_LIMIT` | Req/min per IP (0 = off) | `120` |
| `AGENT_HARNESS_TERMINAL_SANDBOX` | `disabled` \| `local` \| `docker` | `disabled` |
| `SEARXNG_URL` | Self-hosted search | — |
| `AH_ENV_FILE` | `.env` override path | repo `.env` |
| `AH_GATEWAY_TOKEN` | Gateway stdio token | random per launch |
| `AH_UI_REDUCED_MOTION` | `1` or `true` uses a static, brief learning-square acknowledgment | off |

---

## HTTP API

```bash
export AGENT_HARNESS_API_KEY=...   # Bearer or X-API-Key
ah serve --host 127.0.0.1 --port 8000
```

| Method | Path | Notes |
|--------|------|-------|
| GET | `/health`, `/ready` | Public liveness and generic database/schema readiness |
| GET | `/metrics` | Bearer-authenticated Prometheus metrics |
| POST | `/api/v1/sessions` | `{title}` |
| GET | `/api/v1/sessions?limit` | Capped 100 (default 20) |
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
4. **RL memory and personality growth** — offline GRPO plus live runtime adaptation from execution evidence and human feedback; persistent persona trials, bounded capability ranking, history and restoration across sessions and models. See [personality growth](docs/PERSONALITY_GROWTH.md).
5. **SoulSpec** — v0.5 manifest/packages, `AgentDef` round-trip, Claude/Codex adapters, most-restrictive permission merge.

Plus LoCoMo harness (`ah/research/locomo.py`), identity eval (`identity_eval.py`), memory-policy training, native-vs-LangGraph trial (`workflow_trial.py`).

---

## Performance & tests

Verified checkout snapshot, 2026-10-11 (dedicated test database and synthetic providers):

- **Python:** **2092 passed, 5 skipped**, including **43 RSI tests**. DB-gated suites skip without `AGENT_HARNESS_TEST_DATABASE_URL`.
- **UI:** **147 passed, 5 environment-gated skips** in `npm --prefix ui run check`; typecheck and eslint passed. Includes quiet learning feedback, cooldown/cleanup/reduced motion, enhanced `?` key encodings, draft preservation and small-terminal shortcuts. Real Node/Python gateway restart, history and restoration checks ran through the backend suite's isolated UI integration test.
- **RSI verification:** touched-file Ruff check/format passed; Bandit found no issues in the RSI/growth modules; `git diff --check` passed. No live-provider requests or production DB access. Exact commands, review fixes and clean-HEAD comparison are in the [evidence plan](docs/plans/PERSONA_RSI_PLAN.md).
- **Concurrency:** advisory-lock usage (agent→session order), atomic job claim, per-session turn locks, parallel in-block tool calls.
- **Historical workflow trial:** native Postgres approval **~3.6 ms** vs LangGraph **~18.6 ms** (5.1x overhead); native retained. This benchmark does not measure RSI quality or performance.
- **Resilience:** 429-then-200 recovers; sustained 429 raises after retries.

```bash
# Set AGENT_HARNESS_TEST_DATABASE_URL to a dedicated test database first.
.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider
.venv/bin/python -m pytest -q tests/test_rsi.py tests/test_rsi_agent.py tests/test_rsi_management.py
npm --prefix ui run check
```

---

## Development

```bash
pytest tests/ -q --cov=ah --cov-report=html
ruff check ah/ && ruff format --check ah/
bandit -r ah/ -ll -x ah/cli/
npm --prefix ui run typecheck && npm --prefix ui test
```

Conventions: `ruff line-length 100`, `asyncio_mode = auto`, raw SQL via asyncpg, MessagePack for blobs, `B904` (`raise … from`), no `utcnow` (aware datetimes). Tests never touch the developer database or live config: the autouse fixture isolates the test DB DSN, snapshots in-memory config, resets session modes, and clears caches; tests that pin a non-default mode isolate it explicitly.

---

## Project structure

```
agent-harness/
├── ah/
│   ├── core/            # agent, assembler, compression, config, container, context,
│   │                    # context_windows, cron, exceptions, job_scripts, metrics, models,
│   │                    # orchestrator, provider, scheduler, serialization, session,
│   │                    # session_mode, session_recall, text_search, usage, windows_job
│   ├── db/              # asyncpg pool + schema.sql
│   ├── gateway/         # JSON-RPC stdio server + features/ (agents, config, jobs,
│   │                    # memory, secrets, sessions, skills)
│   ├── api/             # FastAPI (app, auth, rate_limit)
│   ├── memory/          # store, retriever, consolidator, approval, scorer, forgetting,
│   │                    # identity, persona, policy, rl, redaction, shared_bus, user_profile
│   ├── rag/             # chunker, embedder, loaders, pipeline, reranker, search
│   ├── tools/           # registry + builtins (web/file/memory/rag/recall/skills/terminal/agents)
│   ├── skills/          # parser, registry, curator, hub, learning, runtime, workflow
│   ├── soulspec/        # schema, merge, adapters, conformance
│   ├── security/        # secrets (env/file/Vault/AWS), env_file
│   ├── observability/   # audit, metrics, tracing
│   ├── plugins/         # entry-point loader + registry
│   ├── research/        # locomo, identity_eval, train_memory_policy, workflow_*
│   └── cli/             # typer app (chat/setup/doctor/init/serve/…) + UI launcher
├── ui/src/              # pi-tui app + features/ (37 commands, 21 skins)
├── ui/test/             # node:test suites
├── tests/               # ~90 pytest modules + workflow_trial/
├── skills/              # local playbooks + 42 curated pinned ECC skills and resources
├── research/            # example identity scenarios
├── Dockerfile / Dockerfile.sandbox / docker-compose.yml
└── pyproject.toml
```

---

## Hang prevention

All end-of-turn cleanup is bounded and lifecycle-managed:

- **Learning tasks**: 2s bounded join, then leftovers cancelled (never blocks answer completion)
- **Provider close**: 5s bounded timeout, claim release in independent outer finally
- **Auto-compaction**: fire-and-forget background task, never blocks [DONE] or stream close
- **Claim release**: always runs in outer finally, independent of provider close errors or cancellation
- **Progress stages**: sanitized stage tracking (turn_started → building_agent → waiting_for_provider → running_tool → joining_optional_work → closing_owned_clients → releasing_ownership → compaction_maintenance → answer_complete) for hang diagnosis

See `FEATURE_LEDGER.md` for detailed fix mapping (H-01 through H-07, R-01 through R-04).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `database unavailable` / `503 API key not configured` | Set `DATABASE_URL`, run `ah init`; set `AGENT_HARNESS_API_KEY` |
| `pgvector` missing | Use `pgvector/pgvector:pg16`, `CREATE EXTENSION vector; pg_trgm;` |
| `401 invalid API key` | `Authorization: Bearer …` or `X-API-Key`; non-ASCII → 401 not 500 |
| `409 a turn is already running` | `prompt.cancel`, wait for `message.complete`; UI timeout is 300s = server |
| `429 rate limit exceeded` | `Retry-After` header; bump `AGENT_HARNESS_HTTP_RATE_LIMIT`, check proxy single-bucket |
| Too many approval prompts | `/mode full` for this session, or persist `execution_mode: full` for new sessions (credentials/elevation/destructive still ask) |
| `terminal` allows more than expected | `full` mode deliberately resolves host commands instead of the `ask`-mode allowlist; switch back to `ask`/`workspace` to narrow it |
| `.env path escapes allowed roots` | Default `.env` must be under cwd/repo/home/tmp; use `AH_ENV_FILE` for elsewhere |
| `script must stay inside …` | `noAgent` scripts must be relative `*.py\|*.sh\|*.bash` under `~/.agent-harness/scripts` |
| `terminal` blocked / `workdir … not within allowed` | `AGENT_HARNESS_TERMINAL_SANDBOX=local\|docker`; workdir under `agent_harness_home` |
| `graph` workflow `no pq wrapper` | `pip install "psycopg[binary]"`; native trial needs no extra deps |
| UI `TURN_IN_PROGRESS` after retry | Server holds 300s; cancel first, don't double-submit |

`ah doctor` prints versions, DB/pgvector reachability, and key presence. Gateway logs: `~/.agent-harness/logs/gateway.log`.

---

## Production release status and research

**Implemented:** general agent loop with gated skill routing, subagent fan-out, Postgres context + recall, persona-on memory, default-on RSI with feedback and revision restoration, RAG, delegation, scheduling, HTTP API + SSE, usage budgets, tools, skills, TUI + skins, observability, Docker backend, retries. Production readiness additionally requires the [release gates](docs/plans/PRODUCTION_COMPLETION_PROGRESS.md); a passing unit suite alone is insufficient.

**Baseline (not in hot path):** RL policy trainer output; SoulSpec cross-runs (adapters, not cert); LoCoMo accuracy (needs live LLM budget).

**MCP connector development:** the connector library has real SDK stdio/loopback HTTP coverage and bounded teardown from separate gateway tasks. Agent-loop tool exposure, OAuth and connector management UI remain deferred; fixture round-trips do not establish complete harness integration. See the [connector progress and next steps](docs/plans/MCP_CONNECTORS_PROGRESS.md).

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

## Production security and operations

[SECURITY.md](SECURITY.md) defines approval authority, exact action bindings,
worker fencing, secret redaction, and the limitations of host execution, plugins,
trusted script dependencies, external providers, and resistant cancellation.
[OPERATIONS.md](OPERATIONS.md) specifies configuration precedence, migration,
backup/restore, rollback, staging validation, monitoring, and retention.

Session modes are durable across workers/restarts; changing the global default
affects future sessions. HTTP approval resume executes the original reviewed tool
on a fresh fenced turn and consumes approval once. HTTP clients opt into v2 with
`protocolVersion: 2`; the TUI negotiates v2. Both use the shared
[versioned event contract](communications/ui-gateway-protocol.md). Claim-release
uncertainty reports cleanup pending without terminal success.

Script jobs use an administrator-controlled allowlist and revalidated declared
dependency hashes under `.script-policy.json`; this is the documented trusted
dependency boundary, not a complete dependency snapshot. Keep the script root
outside the agent workspace with administrator-owned permissions/ACLs.
