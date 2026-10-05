# AgentHarness

Self-hosted AI agent framework. PostgreSQL-backed context, RAG web access, streaming, long-term memory, and a custom CLI.

## Why This Exists

Most agent frameworks are black boxes. AgentHarness is built to be understood — every moving piece is visible, documented, and reimplemented from first principles. The goal is to understand how systems like Hermes Agent work by rebuilding one from scratch.

## Features

- **ReAct loop**: Thought → Action → Observation with streaming output
- **PostgreSQL-backed context**: asyncpg + connection pooling, MessagePack payloads, pgvector embeddings, reversible eviction into `context_archive`
- **Cross-session recall**: Agent-scoped full-text search of live and archived transcripts, with anchored context windows in the gateway, agent tools, and `/recall` UI command
- **Long-term memory**: LLM-based extraction, importance scoring, Ebbinghaus forgetting, hybrid retrieval, approval gate, secret redaction, persona-conditioned interpretations, identity/belief drift gating, HMAC provenance
- **RAG pipeline**: Document indexing, chunking, embedding, hybrid search (BM25 + dense + RRF), reranking
- **Multi-agent**: Named agent definitions (DB/YAML/Soul Spec), sequential/parallel delegation, per-agent tool allowlists
- **Scheduling**: Durable jobs with heartbeat, interval, and five-field UTC cron; atomic claim via `FOR UPDATE SKIP LOCKED`
- **HTTP API**: FastAPI server with versioned `/api/v1` routes, SSE chat streaming, API-key auth, per-peer rate limiting, `/health`, `/ready`, `/metrics`
- **Usage accounting**: Durable `llm_usage` with per-session/per-agent token and request budgets that cannot be bypassed by missing provider metadata
- **Tool registry**: Decorator-based with JSON Schema inference, input validation, caching
- **Skills system**: SKILL.md parser with YAML frontmatter, trigger matching, curator, hub, and agent-facing progressive disclosure through `skill_list` and `skill_read`
- **Terminal UI**: TypeScript app on [`@earendil-works/pi-tui`](https://github.com/earendil-works/pi) (MIT) — streaming Markdown, tool cards, slash-command and file autocomplete, session picker — driving the Python agent through a JSON-RPC gateway
- **Configuration**: YAML file + environment variable overrides + per-session overrides
- **Metrics**: Latency histograms, throughput counters, error rates, token usage tracking; Prometheus exposition
- **Observability**: Sanitized audit events persisted to PostgreSQL; OpenTelemetry spans when an SDK is configured; plugins via opt-in entry-point hooks
- **Security**: No shell injection, no path traversal, no SSRF, rate limiting, audit logging, PII redaction, Docker-sandboxed terminal tool (opt-in)
- **Retry logic**: Exponential backoff on LLM calls
- **DI container**: Production and testing configurations
- **Compression**: Context compression with token budget enforcement
- **User profiles**: Per-user preference and topic tracking
- **Memory approval**: Human-in-the-loop approval for sensitive memories

## Quick Start

Requires Python 3.11+, Node.js 22.19+ and PostgreSQL with pgvector.

```bash
# Python side
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate elsewhere)
pip install -e ".[dev]"

# Terminal UI dependencies (pinned, no install scripts)
npm install --ignore-scripts --prefix ui

# Configure: copy .env.example to .env and set DATABASE_URL and OPENROUTER_API_KEY
ah doctor        # check everything is wired up
ah init          # create the database schema

ah               # open the interactive UI (also: ah repl, ah chat -i)
ah chat "hello"  # one-shot reply, no UI
ah sessions      # list sessions
```

In the UI: Enter sends, Shift+Enter adds a line, Esc stops a reply, `/` opens command
autocomplete (`/new`, `/sessions`, `/resume`, `/recall`, `/model`, `/provider`, `/clear`, `/exit`),
Tab completes file paths, Ctrl+C exits.

For the HTTP API with Docker Compose, set a separate random
`AGENT_HARNESS_API_KEY` alongside `OPENROUTER_API_KEY` in `.env`, then run
`docker compose up --build`. Compose initializes the schema and serves the API at
`http://127.0.0.1:8000` (health check: `/health`). The terminal UI requires the
local Node.js setup above.

## Architecture

```
ah (Typer CLI) ──launches──> ui/  TypeScript terminal UI (pi-tui)
                                └──spawns──> python -m ah.gateway   JSON-RPC 2.0 over stdio
                                                └─> ReActAgent → LLMProvider (OpenRouter/Ollama)
                                                       ↓
                                               PostgreSQL (asyncpg + pgvector)
                                               ├── sessions
                                               ├── context_chunks (MessagePack + pgvector)
                                               ├── context_archive (reversible eviction)
                                               ├── memories / persona_memories / pending_memories
                                               ├── user_profiles
                                               ├── agents / agent_messages
                                               ├── jobs (scheduler)
                                               ├── llm_usage (usage budgets)
                                               ├── audit_events
                                               └── agent_beliefs / memory_provenance (identity)
```

The UI and the agent only share the protocol in
[`communications/ui-gateway-protocol.md`](communications/ui-gateway-protocol.md), the same split
Hermes Agent (`ui-tui` ↔ `tui_gateway`) and opencode use.

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1: Foundation | CLI, PG connection, context CRUD, LLM provider, ReAct loop | Done |
| 2: Context Efficiency | MessagePack, pgvector, prompt assembler | Done |
| 3: Memory & RAG | Long-term memory, RAG pipeline, hybrid search | Done |
| 4: Interactive UI | TypeScript terminal UI (pi-tui) + JSON-RPC gateway, slash commands, config system | Done |
| 5: Multi-Agent | Subagent system, orchestration | Done |
| 6: Production | Web API, scheduler, plugins, observability | Done |
| 7: Advanced | Usage budgets (shipped), existing UI improvements, durable workflows if needed | In progress |

### Multi-agent definitions

Add a YAML file under `agents/` (or set `AGENT_HARNESS_AGENTS_DIR` to another
directory) to define a specialist. See `agents/reviewer.yaml` for an example.
Supported fields are `name`, `description`, `system_prompt`, `tools`, `model`,
`provider`, and `max_iterations`. An empty `tools` list grants access to every
registered tool. Database definitions override YAML definitions with the same
name; built-in names cannot be replaced by YAML files.

Delegation passes a bounded summary of the parent session's recent messages and
goal to the child. The child's response is recorded in the parent session, so
later turns can use it. The `agents.run` gateway method supports sequential and
parallel steps.

### Production API and scheduling

The HTTP server exposes versioned `/api/v1` session, chat (SSE), memory,
document, and job routes. `/health` is a liveness check, `/ready` checks the
database, and authenticated `/metrics` exposes Prometheus text. Set
`AGENT_HARNESS_API_KEY` or `AGENT_HARNESS_API_KEY_FILE`; API requests are limited
to 120 per minute per transport peer by default (set
`AGENT_HARNESS_HTTP_RATE_LIMIT` to change it). The HTTP server starts its job
runner on startup. Jobs support interval, heartbeat, and five-field UTC cron
expressions through `cronExpression`. Run `ah init` after upgrading to add the
cron and audit tables/columns to an existing database.
Jobs can pin `model` and `provider` through the HTTP or gateway create call;
the TUI accepts `/jobs add 60 Check deployment --model=openrouter/free
--provider=openrouter` (and the same trailing options for `/jobs cron`).
Run `ah init` after upgrading so existing databases gain the job pin columns.

Run `ah init` after this upgrade to create the durable `llm_usage` table.
`ah usage --session <id>` and `GET /api/v1/sessions/<id>/usage` show usage
for a session and its agent; the gateway also exposes `usage.get`. Optional
`AGENT_HARNESS_USAGE_{SESSION,AGENT}_{TOKEN,REQUEST}_LIMIT` values in `.env`
set lifetime budgets (0 means unlimited). Each provider attempt counts as a
request. Calls with missing usage or a provider error keep a conservative
pre-call token reservation so budgets cannot be bypassed by absent metadata.
`openrouter/free` never switches to a paid model automatically.

Cross-session transcript recall is available through `/recall <query>` and
the `session_recall` agent tools. `/memory share <id> <agent>` sends a memory
through the recipient's provenance and identity gate. Set
`AGENT_HARNESS_PROVENANCE_KEY` to a private signing key before sharing;
unsigned or tampered memories are rejected.

Optional post-turn skill review is enabled with
`AGENT_HARNESS_LEARNING_REVIEW_ENABLED=true` (off by default). A triggered
review uses at most 512 output tokens and is capped at three reviews per
session by `AGENT_HARNESS_LEARNING_REVIEW_MAX_PER_SESSION`. It stages a
redacted suggestion without changing installed skills. Use `/skills proposals`
to see pending suggestions, then `/skills approve <id>` or
`/skills reject <id>`. Run `ah init` after upgrading to add the
`learning_reviews` and shared delivery tables. Review jobs are currently
process-local; a shutdown can leave a review marked `reviewing`.

The agent prompt includes the names of up to three skills whose triggers
match the current request. `skill_list` pages through enabled skills and
searches their names and descriptions. The agent can read an enabled skill in
800-character pages with `skill_read`; these reads increment skill usage
telemetry. Skill bodies are not inserted into the prompt automatically.

Script-only scheduled jobs can run without an LLM. Put a `.py`, `.sh`, or
`.bash` script in `~/.agent-harness/scripts` (or set
`AGENT_HARNESS_SCRIPTS_DIR`), then use `/jobs script <seconds> <file>`.
Scripts must stay inside that directory, have a 30-second time limit and a
64 KiB output limit, and receive a minimal environment without provider API
keys. Nonempty stdout is saved in the job's session; empty stdout is silent.
In Compose, the host `scripts/` directory is mounted read-only at
`/app/scripts`.

Plugins use Python entry points and are opt-in through `AGENT_HARNESS_PLUGINS`;
see [plugin documentation](docs/plugins.md). Audit events are written to
PostgreSQL while the gateway or HTTP server runs. Agent and tool spans use
OpenTelemetry when an SDK is configured by the host application.

The terminal tool is disabled by default. To enable its read-only Docker
sandbox, run `docker build -f Dockerfile.sandbox -t agent-harness-tool-sandbox:latest .`
and set `AGENT_HARNESS_TERMINAL_SANDBOX=docker`. This mode requires Docker on
the host and is not enabled by the Compose app. `local` is an explicit trusted
development mode because command allowlists cannot contain Git aliases or
test runners. Terminal output is capped at 64,000 characters; commands that
exceed the cap are stopped. Secrets can come from environment
variables, `*_FILE` mounted secrets, HashiCorp Vault KV
(`AGENT_HARNESS_SECRET_BACKEND=vault`, `AGENT_HARNESS_VAULT_ADDR`,
`AGENT_HARNESS_VAULT_PATH`, and a Vault token), or AWS Secrets Manager
(`AGENT_HARNESS_SECRET_BACKEND=aws`, `AGENT_HARNESS_AWS_SECRET_ID`, and the
`secrets` package extra). External secret responses should contain keys such
as `AGENT_HARNESS_API_KEY`, `DATABASE_URL`, and `OPENROUTER_API_KEY`.

## Technology Stack

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Async-native |
| Database | PostgreSQL 16+ + pgvector | ACID + vector search |
| ORM/Query | asyncpg + raw SQL | Performance, control |
| Binary format | MessagePack | Compact, fast |
| Token counting | tiktoken (cl100k_base) | Accurate token counts |
| YAML parsing | PyYAML | Robust frontmatter parsing |
| Caching | cachetools (TTLCache) | LRU session cache |
| CLI | Typer | Type-hint-driven |
| Terminal UI | TypeScript + @earendil-works/pi-tui (Node 22.19+) | Differential rendering, flicker-free, editor + autocomplete |
| UI ↔ agent | JSON-RPC 2.0 over stdio | Same split as Hermes / opencode |
| Provider | OpenRouter | Multi-model, OpenAI-compatible |
| Local LLM | Ollama (optional) | Free, self-hosted |

## Project Structure

```
agent-harness/
├── ah/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli/
│   │   ├── __init__.py      # Typer CLI: `ah` opens the UI; admin commands (chat, sessions, context, compress, skills, memory-*, user-profile-*, doctor, init, config, ...)
│   │   ├── launcher.py      # Starts the TypeScript UI (checks Node + ui/ deps)
│   │   └── output.py        # Plain Rich tables/status lines for admin commands
│   ├── gateway/
│   │   ├── __main__.py      # `python -m ah.gateway`: stdio JSON-RPC server (protocol on private fds)
│   │   ├── server.py        # Gateway: sessions, prompt streaming, cancel, config
│   │   └── features/        # Domain handlers (sessions, memory, skills, jobs, agents, config)
│   ├── api/
│   │   ├── app.py           # FastAPI app (versioned /api/v1 routes, SSE chat, /health, /ready, /metrics)
│   │   ├── auth.py          # API-key auth (fail-closed when unset)
│   │   └── rate_limit.py    # Per-transport-peer rate limiting
│   ├── core/
│   │   ├── agent.py         # ReActAgent + BaseReActAgent (Template Method)
│   │   ├── agent_def.py     # AgentDef personas + registry (DB/YAML/Soul Spec/builtin)
│   │   ├── assembler.py     # PromptAssembler + TokenCounter (tiktoken)
│   │   ├── compression.py   # Context compression with token budget enforcement
│   │   ├── config.py        # Config (YAML + env vars + session overrides)
│   │   ├── container.py     # DI container (production / testing)
│   │   ├── context.py       # ContextManager (CRUD, batch insert, embedding search, archive)
│   │   ├── cron.py          # Five-field UTC cron parser
│   │   ├── exceptions.py    # Custom exception hierarchy
│   │   ├── job_scripts.py   # User-managed scripts for script-only jobs (AGENT_HARNESS_SCRIPTS_DIR)
│   │   ├── session_recall.py # Agent-scoped transcript evidence discovery
│   │   ├── text_search.py   # Shared bounded OR-tsquery builder
│   │   ├── metrics.py       # MetricsCollector (latency, counters, errors, tokens)
│   │   ├── models.py        # Domain models (Session, ContextChunk, LLMResponse, etc.)
│   │   ├── orchestrator.py  # Sequential/parallel delegation, agent_messages
│   │   ├── provider.py      # LLMProvider (OpenRouter, Ollama) + rate limiting + audit
│   │   ├── scheduler.py     # JobStore + JobRunner (interval/heartbeat/cron)
│   │   ├── serialization.py # Shared serialization utilities
│   │   ├── session.py       # SessionManager (with TTLCache)
│   │   └── usage.py         # Durable llm_usage accounting and budgets
│   ├── db/
│   │   ├── connection.py    # asyncpg pool
│   │   └── schema.sql       # PostgreSQL schema and research tables
│   ├── memory/
│   │   ├── approval.py      # MemoryApprovalGate (human-in-the-loop approval gate)
│   │   ├── consolidator.py  # MemoryConsolidator (LLM extraction → scoring → dedup → write)
│   │   ├── forgetting.py    # ForgettingModel (Ebbinghaus decay)
│   │   ├── identity.py      # IdentityGate, AgentBelief, MemoryProvenance, drift containment
│   │   ├── models.py        # MemoryEntry, RetrievedMemory
│   │   ├── persona.py       # PersonaMemoryStore + EmotionTopology
│   │   ├── policy.py        # Group-relative memory-policy trainer (research baseline)
│   │   ├── redaction.py     # PII redaction for memory content
│   │   ├── retriever.py     # MemoryRetriever (hybrid search + reranking)
│   │   ├── rl.py            # RL action space + multi-level rewards (research)
│   │   ├── scorer.py        # ImportanceScorer (multi-factor)
│   │   ├── store.py         # MemoryStore (CRUD)
│   │   └── user_profile.py  # UserProfileManager (per-user preferences)
│   ├── observability/
│   │   ├── audit.py         # Bounded async persistence of sanitized audit events
│   │   ├── metrics.py       # Prometheus text exposition
│   │   └── tracing.py       # OpenTelemetry spans (when an SDK is configured)
│   ├── plugins/
│   │   ├── base.py, loader.py, registry.py  # Opt-in entry-point hooks with timeouts
│   ├── security/
│   │   └── secrets.py       # Env, *_FILE, Vault, AWS Secrets Manager resolution
│   ├── soulspec/
│   │   ├── schema.py        # Soul Spec package validation + AgentDef conversion
│   │   ├── merge.py, adapters.py, conformance.py
│   ├── research/
│   │   ├── locomo.py        # LoCoMo evidence-retrieval benchmark
│   │   ├── train_memory_policy.py  # Offline memory-policy training
│   │   ├── identity_eval.py # Identity-drift scenario evaluator
│   │   ├── workflow_trial.py  # Isolated native-vs-LangGraph approval trial
│   │   └── workflow_benchmark.py # Reproducible latency comparison
│   ├── services.py          # Shared service ops (export, compress, learn skill, status)
│   ├── rag/
│   │   ├── chunker.py       # RecursiveCharacterTextSplitter
│   │   ├── embedder.py      # OpenAIEmbedder (LRU cache + batch)
│   │   ├── loaders.py       # FileLoader (txt, md, code, PDF)
│   │   ├── pipeline.py      # RAGPipeline (index, search, index_session_context)
│   │   ├── reranker.py      # CohereReranker, IdentityReranker
│   │   └── search.py        # HybridSearch (BM25 + dense + RRF)
│   ├── skills/
│   │   ├── registry.py      # SkillParser + SkillRegistry (SKILL.md + YAML frontmatter)
│   │   ├── learning.py      # Opt-in post-turn skill proposals and reviews
│   │   └── runtime.py       # Prompt catalog for matching skills
│   └── tools/
│       ├── base.py          # ToolRegistry (decorator, validation, caching)
│       ├── builtins.py      # web_search, web_extract, search_files
│       ├── file.py          # read_file, write_file, list_files
│       ├── memory.py        # remember, recall
│       ├── rag.py           # index_document, search_documents
│       ├── agents.py        # delegate, list_agents, share_memory
│       ├── session_recall.py # session_recall, session_recall_window
│       ├── skills.py          # skill_list, skill_read
│       ├── registry.py      # Re-export for backward compat
│       └── terminal.py      # terminal (explicit sandbox opt-in)
├── ui/                     # TypeScript terminal UI (pi-tui): src/ app, gateway client, widgets; test/
├── communications/         # Design notes, incl. the UI ↔ gateway protocol
├── tests/                  # Python test suite (pytest)
├── skills/                 # Bundled skills (SKILL.md files)
├── docs/
├── pyproject.toml
└── README.md
```

## Security & Reliability

- **Terminal sandbox**: disabled by default; Docker sandbox is an explicit opt-in
- **No path traversal**: File tools validate paths against base directory
- **No SSRF**: Web tools validate URLs against private IP ranges
- **Rate limiting**: `AsyncTokenBucket` on all LLM calls
- **Input validation**: `_validate_messages`, `_validate_params`, tool arg validation
- **Retry logic**: Exponential backoff on LLM calls (3 retries)
- **Audit logging**: All security-relevant events logged as JSON
- **Streaming**: SSE streaming for real-time output

## Development

Research reproduction commands, LoCoMo evidence-retrieval results, memory
policy training, identity-drift scenarios, and Soul Spec package checks are
documented in [research evaluation](docs/research-evaluation.md).

### Research status

The `docs/research-*.md` files are surveys and design notes written while the
system was built — they describe the gap that motivated each component and the
recommended design. What is actually shipped and runnable today:

| Area | Shipped implementation | Research doc's remaining gap |
|---|---|---|
| Memory / RAG | Postgres memories + hybrid RAG + persona/emotion/identity gate | RL-trained memory policy is a research baseline only; it is not wired into the runtime |
| Cross-session recall and learning | Agent-scoped transcript search, gated memory sharing, opt-in skill proposals and approval | Answer quality, review precision, durable review recovery, and delegation text gating remain open |
| Observability | Prometheus `/metrics`, OTel spans, persisted audit events | Dashboards and alerting are addressed conceptually, not implemented |
| Multi-agent | `AgentDef` personas + sequential/parallel delegation | Orchestrator-worker decomposition is not built |
| Scheduling | Interval/heartbeat/cron durable jobs and script-only jobs | External delivery targets and retries remain open |
| Soul Spec | v0.5 manifest validation, package files, AgentDef conversion | Cross-framework runs are adapters, not compatibility certification |
| Evaluation | LoCoMo evidence retrieval, memory-policy training, identity-drift checks (`ah/research/`) | Small local benchmarks, not product answer-quality claims |

LangGraph remains an optional workflow trial rather than a production runtime
dependency. A PostgreSQL checkpointer resumed a paused approval after a new
Python process started; the current [decision](docs/research-langgraph.md)
keeps production selection open until real delegated work, budget, audit, and
streaming behavior are compared with the native PostgreSQL path.

Nothing in the research docs should be read as a claim about generated-answer
quality of a deployed model.

```bash
# Python: tests (DB-backed tests use AGENT_HARNESS_TEST_DATABASE_URL, never DATABASE_URL)
pytest
ruff check ah/ tests/

# Terminal UI
cd ui
npm run typecheck
npm test            # set AH_TEST_PYTHON + AGENT_HARNESS_TEST_DATABASE_URL to include the live-gateway test
```

## License

MIT
