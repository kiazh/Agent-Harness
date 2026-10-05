# AgentHarness Roadmap

**Last updated:** 2026-10-05  
**Current version:** 0.2.0  
**Test status:** 1279 Python tests pass, 0 skipped; 54 UI tests pass. Phase 7
modules at 99% coverage.

---

## Executive Summary

AgentHarness has completed Phases 1-7. Phases 1-6 delivered a ReAct loop,
PostgreSQL context and memory, RAG, a terminal UI and JSON-RPC gateway,
multi-agent delegation, and production API, scheduling, observability,
plugins, and security controls. Phase 7 shipped usage controls (durable
`llm_usage` accounting, optional token/request budgets, `ah usage` and
`usage.get`/`GET /api/v1/sessions/<id>/usage`), the UI workflow improvements,
and the durable-workflow decision. All 5 research gaps are implemented:
reversible eviction, shared memory identity propagation, persona-conditioned
memory, end-to-end RL for memory, and SoulSpec standard. The research
credibility layer is now measured: LoCoMo answer accuracy (token F1), learning
proposal precision, and lease-based durable review recovery. 58+ bug categories
fixed across review rounds, including two real concurrency bugs in context
eviction found by load testing. Zero TODOs/FIXMEs remain in source.

**What remains:** ongoing research direction (full-corpus runs against a live
LLM budget; broad Hermes parity) — not blocking gates.

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
│   ├── provider.py      # LLMProvider (OpenRouter, Ollama) + rate limiting + audit + 429 retry
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
│   ├── text_search.py   # Shared bounded OR-tsquery builder
│   └── windows_job.py   # Windows Job Object ownership for script trees
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
│   ├── agents.py        # delegate, list_agents, share_memory
│   ├── session_recall.py # session_recall, session_recall_window
│   ├── skills.py          # skill_list, skill_read
│   └── registry.py      # Re-export for backward compat
├── skills/
│   ├── registry.py      # SkillParser + SkillRegistry (SKILL.md + YAML frontmatter)
│   ├── learning.py      # Opt-in post-turn skill proposals and reviews
│   └── runtime.py       # Prompt catalog for matching skills
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
│   ├── server.py        # Gateway dispatch, turns, streaming events, file+console logging
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
│   ├── workflow_trial.py  # Isolated native-vs-LangGraph approval trial
│   └── workflow_benchmark.py # Reproducible latency comparison
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

## Phase 5: Multi-Agent Orchestration — COMPLETE

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

## Phase 6: Production Hardening — COMPLETE

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

## Phase 7: Usage Controls and Workflow Improvements — COMPLETE

### 7.1 Session and agent usage controls — SHIPPED

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

### 7.2 Improve the existing terminal UI (`ui/`) — DONE

The TypeScript terminal UI and JSON-RPC gateway were delivered in Phase 4.
The UI has been redesigned to Pi/Copilot style with:
- Clean, minimal dark theme with robot icon
- Input box with circle button
- Status bar
- Skin-aware colors
- OSC 11 background control
- Blocking `msvcrt.getch()` for zero-delay key input
- Autocomplete, help scrolling, arrow key navigation

**Shipped in Phase 7:**
- Searchable session browser (`/search`, `session.search`)
- Context and memory views (`ui/src/features/context.ts`, `memory.ts`)
- Streaming state feedback (tool cards, cancellation, interrupted marks)
- Session-keyed in-flight tracking (fixed six regressions: permanent session
  lock, wrong-session cancel, lost input, error/gateway-exit cleanup)

**Remaining (no reproduced target):**
- Performance with very large contexts — reported but not yet reproduced

### 7.3 Durable workflows — DECIDED (2026-10-04)

**Decision:** Keep the native PostgreSQL path as the runtime. LangGraph
stays an optional research dependency behind the `workflow-trial` extra.
The migration boundary is an isolated per-workflow opt-in if a real
delegated approval workflow ever proves a recovery benefit.

**Reason:** The trial measured 3.618 ms mean for native start-plus-approval
versus 18.608 ms for LangGraph — a 5.1x latency overhead. The native path
is simpler, faster, and already integrated. LangGraph's checkpoint tables
and dependency footprint are not justified for the current workflow scope.
The trial did not exercise usage budgets, gateway streaming, audit parity,
or external side effects, so the production decision gate was not passed.

**What would change the decision:** A real delegated approval workflow that
requires pause/resume across process restarts and proves a recovery benefit
the native path cannot provide; LangGraph demonstrating significantly better
recovery semantics for multi-step delegated tasks; or a production
requirement for graph-level checkpointing that justifies the operational
complexity.

See [LangGraph decision](research-langgraph.md) for the full trial result
and acceptance gate.

### 7.4 Testing and QA — TARGET MET

| Feature | Status |
|---|---|
| Integration tests | Real PostgreSQL with controlled LLM response |
| Benchmark suite | Token usage, latency, task cost (partial) |
| Fuzz testing | Tool input validation and security boundaries |
| Coverage target | 1279 tests pass; Phase 7 modules at 99% |

**Done:**
- 99% coverage on Phase 7 code: scheduler.py 100%, session_recall.py 100%,
  text_search.py 100%, usage.py 100%, learning.py 99% (was 45–84%)
- Extended chaos testing (`tests/test_chaos_extended.py`, 63 tests): provider
  malformed/partial output, mid-stream failure, embedder outage, delegation
  timeout, worker crash and lease reclaim, interrupted eviction, concurrent
  writes
- Load testing (`tests/test_load_concurrency.py`, 10 tests): concurrent
  sessions, usage accounting balance, context writes, gateway serialization

**Remaining:**
- Answer-quality and learning-precision measurements are implemented; the
  full-corpus runs still need a live LLM budget.

### 7.5 Research and Hermes capability program — ACTIVE

The [program design](superpowers/specs/2026-10-04-research-hermes-program-design.md)
tracks the five research directions and major Hermes capabilities. Shipped
increments now include scoped live/archive transcript recall, gated
cross-agent memory delivery, opt-in staged skill proposals, and bounded
agent-facing skill discovery and reading. Jobs can also run user-managed
scripts without inference. Evidence
retrieval has a full LoCoMo measurement, answer accuracy is scored by token F1,
and proposal precision is measured per agent. Broad Hermes parity remains open
as an ongoing direction, not a blocking gate.

**Open research items (ongoing, non-blocking):**
- Full-corpus LoCoMo answer run and learning-precision run against a live LLM
  budget (the harnesses are implemented and unit-tested offline)
- Broad Hermes parity (matching Hermes Agent capabilities)

---

## The Path to Complete — 4 Gates

**Verified state at time of writing:** 1279 tests pass, 0 skipped, ~83s. UI
suite 54 pass / 0 fail. Repo synced with `origin/main`. Phase 7 modules at 99%
coverage.

**Honest definition of "complete":** every success-criteria checkbox below is
either checked or explicitly abandoned with a written reason.

The remaining work is ordered into four gates. The order is deliberate: each
gate either protects the ones after it or is the only thing that can force
rework later.

### Gate 1 — Freeze and commit — DONE (2026-10-04)

The codebase is green and synced. Established a known-good checkpoint.

- [x] Commit `docs/roadmap.md`
- [x] Delete the `.*-tmp/` scratch directories from the repo root
- [x] Add `.*-tmp/` to `.gitignore` so they do not return
- [x] Push to `main` (commit `9512abb`)
- [x] Confirm the suite passes on the clean tree

*Note:* ten empty `.*-tmp/` directories carry a deny-ACL that needs an elevated
shell to remove; they are now untracked and ignored, so they are harmless.

### Gate 2 — Decide the one architectural question — CLOSED (2026-10-04)

Everything else left is polish or measurement. The only decision that changes
the architecture is §7.3: native PostgreSQL checkpoint vs LangGraph.

- [x] Run the trial (`ah/research/workflow_trial.py`)
- [x] Run the benchmark (`ah/research/workflow_benchmark.py`)
- [x] Score results against the acceptance gate in `research-langgraph.md`
- [x] Write the decision into this roadmap
- [x] Integrate the winner, or close the question permanently

**Decision:** Native PostgreSQL path retained. LangGraph stays optional.
See §7.3 and [LangGraph decision](research-langgraph.md).

### Gate 3 — Close the measurement gaps — DONE (2026-10-04)

This is what separates "a working framework" from "a framework you can publish
from." All items are measurement, not construction.

- [x] LoCoMo answer accuracy (`ah/research/locomo.py` token-F1 scoring)
- [x] Learning precision (`ah/skills/learning.py::compute_learning_precision`)
- [x] Durable review recovery (lease-based reclaim in `learning_reviews`)
- [x] 90%+ coverage on Phase 7 code (measured 99%)
- [x] Extended chaos testing (`tests/test_chaos_extended.py`)
- [x] Load testing for concurrent sessions (`tests/test_load_concurrency.py`)

*Why third:* if the goal is a Waterloo research team and publishing papers,
the framework is the artifact but the measurements are the contribution.

### Gate 4 — UI polish — DONE (2026-10-04)

The most visible work and the least load-bearing, which is exactly why it does
not go first. Easiest to gold-plate.

- [x] Fix the six failing in-flight tracking tests (session-keyed state)
- [x] Searchable session browser (`/search`, `session.search`)
- [x] Context and memory views (`ui/src/features/context.ts`, `memory.ts`)
- [x] Streaming state feedback (tool cards, cancellation, interrupted marks)
- [x] Performance with very large contexts — no reproduced target; a report
  exists but the lag has not been reproduced under measurement

*Why last:* it can interleave with Gate 3, but it must not jump the queue.

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
| Gate 1 | Freeze and commit | High | Low | 7 |
| Gate 2 | Durable workflow decision | High | Medium | 7 |
| Gate 3 | Measurement gaps + Phase 7 coverage | High | Medium | 7 |
| Gate 4 | UI polish (session browser, context views) | Medium | Medium | 7 |

---

## Success Criteria

### Phase 5 (Multi-Agent) — COMPLETE
- [x] Agent definitions in YAML (`agents/*.yaml`, configurable with `AGENT_HARNESS_AGENTS_DIR`)
- [x] Sequential and parallel orchestration
- [x] `delegate()` tool working
- [x] Context handoff between agents

### Phase 6 (Production) — COMPLETE
- [x] FastAPI server with SSE streaming and versioned routes
- [x] Prometheus metrics endpoint
- [x] Heartbeat and UTC cron scheduler
- [x] Plugin system with lifecycle hooks

### Phase 7 (Advanced) — COMPLETE
- [x] Persist and expose per-session and per-agent LLM usage
- [x] Enforce optional token/request budgets across entry points
- [x] Redesign terminal UI to Pi/Copilot style
- [x] Improve the existing terminal UI based on user workflows (session search, context/memory views, streaming feedback)
- [x] Decide whether durable workflows need an external graph engine — DECIDED 2026-10-04: native PostgreSQL path retained; LangGraph stays optional
- [x] 90%+ coverage for new Phase 7 code — measured 99%
- [x] Provider retry-with-backoff for 429 rate limits
- [x] Gateway file+console logging (requests, turns, errors)
- [x] UI auto-detects venv Python and passes auth token
- [x] Answer-quality measurement (LoCoMo token-F1)
- [x] Learning-precision measurement
- [x] Durable review recovery (lease-based reclaim)

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Multi-agent token costs | N agents x M iterations = expensive | Budget enforcement per agent + global cap |
| Scope creep | Too many features, none done well | Strict phase prioritization, ship incrementally |
| PostgreSQL performance | Embedding search slows at scale | HNSW indexes, connection pooling, query optimization |
| UI performance | Lag with large contexts | Virtualized lists, pagination, lazy loading |

---

## Open Questions

1. **Budget defaults:** Which session and agent limits should be opt-in, and
   what should happen when a provider omits token usage?
2. **Usage retention:** How long should call records remain available?
3. **Workflow durability:** ~~Which task actually requires pause/resume after a
   process restart?~~ DECIDED 2026-10-04 — native PostgreSQL path retained;
   LangGraph stays optional. See §7.3.
4. **UI framework:** Is pi-tui sufficient or do we need a more custom solution
   for advanced features?

---

*This roadmap is a living document. Update it as features are completed and priorities shift.*
