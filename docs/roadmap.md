# AgentHarness Roadmap

**Last updated:** 2026-10-03  
**Current version:** 0.2.0
**Test status:** Run the current Python and TypeScript suites for live counts;
the earlier 901-test snapshot is no longer current.

---

## Executive Summary

AgentHarness has completed Phases 1-6: a ReAct loop, PostgreSQL context and memory, RAG, a terminal UI and JSON-RPC gateway, multi-agent delegation, and production API, scheduling, observability, plugins, and security controls. The Windows test run collects 901 tests, with some platform-specific tests deselected.

Phase 7 has started with **session and agent usage controls** (shipped: durable
`llm_usage` accounting, optional token/request budgets, `ah usage` and
`usage.get`/`GET /api/v1/sessions/<id>/usage`), then improves the existing
terminal UI. The expanded research and Hermes capability program now provides
concrete durable workflow cases. See the
[program design](superpowers/specs/2026-10-04-research-hermes-program-design.md)
and the [revised LangGraph decision](research-langgraph.md). These are planned
work, not completed Phase 7 features.

---

## Current Architecture

```
ah/
├── core/
│   ├── models.py        # Domain models (Session, ContextChunk, LLMResponse, etc.)
│   ├── assembler.py     # PromptAssembler with token budget
│   ├── config.py        # Config (YAML + env vars + session overrides)
│   ├── container.py     # DI container (production / testing)
│   ├── agent.py         # ReActAgent + BaseReActAgent (Template Method)
│   ├── context.py       # ContextManager (CRUD + embedding search)
│   ├── provider.py      # LLMProvider (OpenRouter, Ollama) + rate limiting + audit
│   ├── metrics.py       # MetricsCollector (latency, counters, errors, tokens)
│   ├── exceptions.py    # Custom exception hierarchy
│   ├── serialization.py # Shared serialization utilities
│   ├── session.py       # SessionManager with TTLCache
│   ├── agent_def.py     # AgentDef personas (DB/YAML/Soul Spec/builtin)
│   ├── orchestrator.py  # Sequential/parallel delegation
│   ├── scheduler.py     # JobStore + JobRunner
│   ├── usage.py         # Durable llm_usage accounting and budgets
│   ├── cron.py          # Five-field UTC cron parser
│   ├── job_scripts.py   # User-managed scripts for script-only jobs
│   ├── session_recall.py # Agent-scoped transcript evidence discovery
│   └── text_search.py   # Shared bounded OR-tsquery builder
├── db/
│   ├── connection.py    # asyncpg pool
│   └── schema.sql       # sessions, context_chunks, context_archive, memories,
│                        # pending_memories, user_profiles, agents, agent_messages,
│                        # audit_events, jobs, persona_memories, llm_usage,
│                        # agent_beliefs, agent_belief_history, memory_provenance
├── tools/
│   ├── base.py          # ToolRegistry (decorator-based, JSON Schema inference)
│   ├── builtins.py      # web_search, web_extract, search_files
│   ├── file.py          # read_file, write_file, list_files
│   ├── terminal.py      # terminal (allowlist + SSRF protection)
│   ├── memory.py        # remember, recall
│   ├── rag.py           # index_document, search_documents
│   ├── agents.py        # delegate, list_agents
│   ├── session_recall.py # session_recall, session_recall_window
│   └── registry.py      # Re-export for backward compat
├── skills/
│   ├── registry.py      # SkillParser + SkillRegistry (SKILL.md + YAML frontmatter)
│   └── learning.py      # Opt-in post-turn skill proposals and reviews
├── memory/
│   ├── models.py        # MemoryEntry, RetrievedMemory
│   ├── store.py         # MemoryStore (CRUD)
│   ├── consolidator.py  # MemoryConsolidator (LLM extraction -> scoring -> dedup -> write)
│   ├── retriever.py     # MemoryRetriever (hybrid search + reranking)
│   ├── scorer.py        # ImportanceScorer (multi-factor)
│   ├── forgetting.py    # ForgettingModel (Ebbinghaus decay)
│   ├── approval.py      # MemoryApprovalGate (human-in-the-loop approval)
│   ├── redaction.py     # SecretRedactor (PII/secret redaction)
│   ├── shared_bus.py    # Gated cross-agent memory delivery with quarantine
│   ├── user_profile.py  # UserProfileStore (per-user preferences)
│   ├── identity.py      # IdentityGate + belief provenance/drift containment
│   ├── persona.py       # PersonaMemoryStore + EmotionTopology
│   ├── policy.py        # Group-relative memory policy trainer (research baseline)
│   └── rl.py            # RL action/reward definitions for memory
├── gateway/
│   ├── __main__.py      # stdio JSON-RPC server (protocol on private fds)
│   ├── server.py        # Gateway dispatch, turns, streaming events
│   └── features/        # Domain handlers (sessions, memory, skills, jobs, agents, config)
├── api/
│   ├── app.py           # FastAPI app (versioned /api/v1 routes, SSE chat)
│   ├── auth.py          # API-key auth (fail-closed)
│   └── rate_limit.py    # Per-peer rate limiting
├── observability/
│   ├── audit.py         # Bounded async audit-event persistence
│   ├── metrics.py       # Prometheus text exposition
│   └── tracing.py       # OpenTelemetry spans
├── security/
│   └── secrets.py       # Env/file/Vault/AWS secret resolution
├── soulspec/
│   ├── schema.py        # Soul Spec package validation + AgentDef conversion
│   ├── merge.py, adapters.py, conformance.py
├── research/
│   ├── locomo.py        # LoCoMo evidence retrieval benchmark
│   ├── train_memory_policy.py, identity_eval.py
├── rag/
│   ├── chunker.py       # RecursiveCharacterTextSplitter
│   ├── embedder.py      # OpenAIEmbedder (LRU cache + batch)
│   ├── loaders.py       # FileLoader (txt, md, code, PDF)
│   ├── pipeline.py      # RAGPipeline (index, search, index_session_context)
│   ├── reranker.py      # CohereReranker, IdentityReranker
│   └── search.py        # HybridSearch (BM25 + dense + RRF)
├── cli/
│   ├── __init__.py      # Typer CLI (chat, sessions, context, compress, skills, memory-*, doctor, init, config, usage, ...)
│   ├── launcher.py      # Starts the TypeScript UI (checks Node + ui/ deps)
│   └── output.py        # Plain Rich tables/status lines for admin commands
├── ../ui/               # TypeScript terminal UI (pi-tui) driving the gateway
└── __init__.py          # Version 0.2.0
```

---

## Phase 5: Multi-Agent Orchestration

**Goal:** Enable multiple specialized agents to collaborate on complex tasks.

### 5.1 Agent Definitions (`ah/core/agent_def.py`)

```python
@dataclass
class AgentDef:
    name: str
    description: str
    system_prompt: str
    tools: list[str]  # Restrict which tools this agent can use
    model: str | None = None
    provider: str | None = None
    max_iterations: int = 10
```

### 5.2 Agent Registry & Orchestrator (`ah/core/agent_def.py`, `ah/core/orchestrator.py`)

| Component | Description |
|---|---|
| `AgentRegistry` | Look up definitions: DB → YAML (`agents/*.yaml`) → Soul Spec packages → built-ins |
| `Orchestrator` | Run delegations sequentially or in parallel, record `agent_messages`, bounded parent-context handoff, hop-count guard |
| `delegate()` tool | Hand a standalone task to a specialist agent from a running turn |

### 5.3 Orchestration Patterns

| Pattern | Use Case |
|---|---|
| **Sequential** | Agent A -> Agent B -> Agent C (pipeline) |
| **Parallel** | Multiple agents work on independent subtasks simultaneously |
| **Hierarchical** | Orchestrator delegates to worker agents, synthesizes results |
| **Debate** | Multiple agents discuss, moderator decides |

### 5.4 New Tools

- `delegate(agent: str, task: str)` - send a task to another agent
- `list_agents()` - list available agents

---

## Phase 6: Production Hardening

**Goal:** Make AgentHarness reliable, observable, and deployable.

### 6.1 Web API Server (`ah/api/`)

| Component | Description |
|---|---|
| `api/app.py` | FastAPI application (versioned `/api/v1` routes + legacy aliases, SSE chat) |
| `api/auth.py` | Bearer / `X-API-Key` authentication, fail-closed when unset |
| `api/rate_limit.py` | Per-transport-peer request rate limiting |
| `api/__main__.py` | `python -m ah.api` entry point |

**Endpoints:**
- `POST /api/v1/sessions` - create session
- `GET /api/v1/sessions` - list sessions
- `POST /api/v1/sessions/{id}/chat` - send message (SSE stream)
- `GET /api/v1/sessions/{id}/context` - get context
- `POST /api/v1/memory` - create memory
- `GET /api/v1/memory/search` - search memories
- `POST /api/v1/documents` - index document
- `GET /api/v1/documents/search` - search documents

### 6.2 Observability (`ah/observability/`)

| Component | Description |
|---|---|
| `observability/metrics.py` | Prometheus metrics exposition (in-process collector → text format) |
| `observability/tracing.py` | OpenTelemetry tracing for agent runs |
| `observability/audit.py` | Persisted, sanitized audit events |
| `/health`, `/ready` | Liveness and DB/schema readiness (served from `ah/api/app.py`) |

### 6.3 Task Scheduling (`ah/core/scheduler.py`, `ah/core/cron.py`)

| Component | Description |
|---|---|
| `JobStore` | Persist jobs (`jobs` table); atomic claim via `FOR UPDATE SKIP LOCKED` |
| `JobRunner` | Poll loop run by the gateway/API; lease renewal; heartbeat, interval, and UTC cron kinds; bounded script-only mode |
| `next_cron_time` | Five-field cron parser in `ah/core/cron.py` |

### 6.4 Plugin System (`ah/plugins/`)

| Component | Description |
|---|---|
| `plugins/base.py` | Plugin interface (hooks for agent lifecycle) |
| `plugins/loader.py` | Dynamic plugin discovery and loading |
| `plugins/registry.py` | Plugin registry |

**Plugin hooks:**
- `pre_agent_run(session, message)` - before agent starts
- `post_agent_run(session, response)` - after agent completes
- `on_tool_call(tool_name, args)` - before tool execution
- `on_tool_result(tool_name, result)` - after tool execution
- `on_memory_extract(memories)` - after memory extraction

### 6.5 Security Hardening

| Feature | Description |
|---|---|
| Tool sandboxing | Docker-based sandbox for terminal tool |
| Input sanitization | Stricter validation on all tool inputs |
| API authentication | Bearer <_REDACTED> auth for server mode |
| Audit log persistence | Store audit logs in database (not just stdout) |
| Secret management | Integration with vaults (HashiCorp, AWS SM) |

---

## Phase 7: Usage Controls and Workflow Improvements

### 7.1 Session and agent usage controls — first implementation

Shipped in 0.2.0:

1. Prompt and completion tokens, model, provider, and call outcome are
   persisted in the `llm_usage` table, so usage survives restarts. Streamed
   and non-streamed calls, scheduled jobs, and delegated agents are all
   recorded.
2. Optional session and agent token/request budgets (`usage_*` config keys)
   are checked before a call and a budget error is returned through the CLI,
   gateway, and HTTP stream. Limits are optional and default to unlimited.
3. Usage and remaining budget are exposed through the API, CLI (`ah usage`),
   and the gateway's `usage.get`. Calls with missing provider usage keep a
   conservative reservation, so unknown usage is not free.
4. Concurrent calls, cancellation, provider errors, restart recovery, and the
   `openrouter/free` route are tested. `openrouter/free` never switches to a
   paid model automatically.

The acceptance criteria below now describe the shipped behavior: usage for a
session can be queried after an app restart; a budget stops a subsequent call
before it reaches the provider; the same result is visible from each
supported client.

### 7.2 Improve the existing terminal UI (`ui/`)

The TypeScript terminal UI and JSON-RPC gateway were delivered in Phase 4.
Improve that UI only where user workflows require it: a searchable session
browser, clearer context/memory views, and accessible streaming states. Do not
create a second Textual application.

### 7.3 Durable workflows

The delegated-task approval and restart case is now defined. Compare a
PostgreSQL-backed native checkpoint with an isolated LangGraph workflow under
the same recovery, idempotency, budget, and cancellation tests. The
[LangGraph decision](research-langgraph.md) records the acceptance gate;
production integration has not yet been selected.

### 7.5 Research and Hermes capability program

The [program design](superpowers/specs/2026-10-04-research-hermes-program-design.md)
tracks the five research directions and major Hermes capabilities. Shipped
increments now include scoped live/archive transcript recall, gated
cross-agent memory delivery, opt-in staged skill proposals, and bounded
agent-facing skill discovery and reading. Jobs can also run user-managed
scripts without inference. Evidence
retrieval has a full LoCoMo measurement; answer quality, learning precision,
durable review recovery, and broad Hermes parity remain open.

### 7.4 Testing and QA

| Feature | Description |
|---|---|
| Integration tests | Real PostgreSQL with a controlled LLM response |
| Benchmark suite | Token usage, latency, and task cost |
| Fuzz testing | Tool input validation and security boundaries |
| Coverage target | Measure the baseline, then reach 90%+ on new Phase 7 code |

---

## Dependency Graph (Target)

```
cli/__init__.py -> cli/launcher.py (spawns ui/) -> gateway/server.py -> core/agent.py
                                              -> core/assembler.py -> core/context.py -> db/connection.py
                                              -> core/provider.py
                                              -> core/session.py -> db/connection.py
                                              -> memory/ -> core/provider.py, db/connection.py
                                              -> rag/ -> core/provider.py, db/connection.py
                                              -> core/orchestrator.py -> core/agent.py
                                              -> core/scheduler.py -> core/agent.py
api/app.py -> core/agent.py, memory/, rag/, gateway/server.py
ui/ -> gateway/ (JSON-RPC over stdio)
```

---

## Implementation Priority Matrix

| Priority | Feature | Impact | Effort | Phase |
|---|---|---|---|---|
| P2 | Multi-agent orchestration | High | High | 5 |
| P2 | Web API server | High | High | 6 |
| P2 | Observability | Medium | Medium | 6 |
| P2 | Task scheduling | Medium | Medium | 6 |
| P3 | Plugin system | Medium | Medium | 6 |
| P1 | Session and agent usage controls | High | Medium | 7 |
| P2 | Existing terminal UI improvements | Medium | Medium | 7 |
| P3 | Durable workflows, if required | Medium | High | 7 |

---

## Success Criteria

### Phase 5 (Multi-Agent)
- [x] Agent definitions in YAML (`agents/*.yaml`, configurable with `AGENT_HARNESS_AGENTS_DIR`)
- [x] Sequential and parallel orchestration
- [x] `delegate()` tool working
- [x] Context handoff between agents

### Phase 6 (Production)
- [x] FastAPI server with SSE streaming and versioned routes
- [x] Prometheus metrics endpoint
- [x] Heartbeat and UTC cron scheduler
- [x] Plugin system with lifecycle hooks

### Phase 7 (Advanced)
- [x] Persist and expose per-session and per-agent LLM usage
- [x] Enforce optional token/request budgets across entry points
- [ ] Improve the existing terminal UI based on user workflows
- [ ] Decide whether durable workflows need an external graph engine
- [ ] 90%+ coverage for new Phase 7 code

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Multi-agent token costs | N agents x M iterations = expensive | Budget enforcement per agent + global cap |
| Scope creep | Too many features, none done well | Strict phase prioritization, ship incrementally |
| PostgreSQL performance | Embedding search slows at scale | HNSW indexes, connection pooling, query optimization |

---

## Open Questions

1. **Budget defaults:** Which session and agent limits should be opt-in, and
   what should happen when a provider omits token usage?
2. **Usage retention:** How long should call records remain available?
3. **Workflow durability:** Which task actually requires pause/resume after a
   process restart?

---

*This roadmap is a living document. Update it as features are completed and priorities shift.*
