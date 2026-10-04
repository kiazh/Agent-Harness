# AgentHarness Roadmap

**Last updated:** 2026-10-03  
**Current version:** 0.2.0
**Test status:** 688 tests passing on Windows (one platform-specific test deselected)

---

## Executive Summary

AgentHarness has completed Phases 1-6: a ReAct loop, PostgreSQL context and memory, RAG, a terminal UI and JSON-RPC gateway, multi-agent delegation, and production API, scheduling, observability, plugins, and security controls. The Windows test run passes 688 tests, with one platform-specific test deselected.

Phase 7 starts with **session and agent usage controls**, then improves the
existing terminal UI and evaluates durable workflows when a concrete use case
requires them.

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
│   └── session.py       # SessionManager with TTLCache
├── db/
│   ├── connection.py    # asyncpg pool
│   └── schema.sql       # 3 tables: sessions, context_chunks, memories
├── tools/
│   ├── base.py          # ToolRegistry (decorator-based, JSON Schema inference)
│   ├── builtins.py      # web_search, web_extract, search_files
│   ├── file.py          # read_file, write_file, list_files
│   ├── terminal.py      # terminal (allowlist + SSRF protection)
│   ├── memory.py        # remember, recall
│   ├── rag.py           # index_document, search_documents
│   └── registry.py      # Re-export for backward compat
├── skills/
│   └── registry.py      # SkillParser + SkillRegistry (SKILL.md + YAML frontmatter)
├── memory/
│   ├── models.py        # MemoryEntry, RetrievedMemory
│   ├── store.py         # MemoryStore (CRUD)
│   ├── consolidator.py  # MemoryConsolidator (LLM extraction -> scoring -> dedup -> write)
│   ├── retriever.py     # MemoryRetriever (hybrid search + reranking)
│   ├── scorer.py        # ImportanceScorer (multi-factor)
│   └── forgetting.py    # ForgettingModel (Ebbinghaus decay)
├── rag/
│   ├── chunker.py       # RecursiveCharacterTextSplitter
│   ├── embedder.py      # OpenAIEmbedder (LRU cache + batch)
│   ├── loaders.py       # FileLoader (txt, md, code, PDF)
│   ├── pipeline.py      # RAGPipeline (index, search, index_session_context)
│   ├── reranker.py      # CohereReranker, IdentityReranker
│   └── search.py        # HybridSearch (BM25 + dense + RRF)
├── cli/
│   ├── __init__.py      # Typer CLI (chat, repl, status, sessions, context, skills, doctor, init, config, memory)
│   └── interactive.py   # Interactive REPL (prompt_toolkit)
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

### 5.2 Multi-Agent Manager (`ah/core/multi_agent.py`)

| Component | Description |
|---|---|
| `AgentRegistry` | Register and retrieve agent definitions |
| `AgentRunner` | Run a specific agent with a specific task |
| `Orchestrator` | Decompose tasks, delegate to agents, synthesize results |
| `HandoffManager` | Pass context between agents during handoffs |

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
- `spawn_agent(definition: str)` - dynamically create a new agent

---

## Phase 6: Production Hardening

**Goal:** Make AgentHarness reliable, observable, and deployable.

### 6.1 Web API Server (`ah/server/`)

| Component | Description |
|---|---|
| `server/app.py` | FastAPI application |
| `server/routes/sessions.py` | Session CRUD endpoints |
| `server/routes/chat.py` | Chat endpoint (SSE streaming) |
| `server/routes/memory.py` | Memory management endpoints |
| `server/routes/documents.py` | Document indexing/search endpoints |
| `server/middleware/auth.py` | API key authentication |
| `server/middleware/rate_limit.py` | Per-client rate limiting |

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
| `observability/metrics.py` | Prometheus metrics (token usage, latency, tool calls) |
| `observability/tracing.py` | OpenTelemetry tracing for agent runs |
| `observability/health.py` | Health check endpoint |

### 6.3 Task Scheduling (`ah/scheduler/`)

| Component | Description |
|---|---|
| `scheduler/heartbeat.py` | Periodic agent heartbeat (run agent every N minutes) |
| `scheduler/cron.py` | Cron-like task scheduler |
| `scheduler/jobs.py` | Job definitions and persistence |

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

The provider currently records LLM calls with an empty session ID, while the
agent tracks token usage in memory. Start by attributing each completed call to
its session and agent without counting the same tokens twice.

1. Persist prompt and completion tokens, model, provider, and call outcome in
   PostgreSQL so usage survives restarts. Include streamed and non-streamed
   calls, scheduled jobs, and delegated agents.
2. Add configurable session and agent token/request budgets. Check limits before
   a call and return a clear budget error through the CLI, gateway, and HTTP
   stream. Keep limits optional for existing installations.
3. Expose usage and remaining budget through the existing API, CLI, and metrics.
   Treat missing provider usage as unknown rather than zero consumption.
4. Test concurrent calls, cancellation, provider errors, restart recovery, and
   the `openrouter/free` route. Never switch to a paid model automatically.

Acceptance: usage for a session can be queried after an app restart; a budget
stops a subsequent call before it reaches the provider; the same result is
visible from each supported client.

### 7.2 Improve the existing terminal UI (`ui/`)

The TypeScript terminal UI and JSON-RPC gateway were delivered in Phase 4.
Improve that UI only where user workflows require it: a searchable session
browser, clearer context/memory views, and accessible streaming states. Do not
create a second Textual application.

### 7.3 Durable workflows, if needed

Define the crash-recovery or human-approval use case first. Add PostgreSQL
checkpoints and resumable execution to the existing agent engine if that meets
the need. Reassess LangGraph only if those requirements exceed the current
engine; see [the LangGraph research](research-langgraph.md).

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
cli/__init__.py -> interactive.py -> agent.py -> assembler.py -> context.py -> connection.py
                                    -> provider.py
                                    -> session.py -> connection.py
                                    -> memory/ -> provider.py, connection.py
                                    -> rag/ -> provider.py, connection.py
                                    -> multi_agent/ -> agent.py
                                    -> scheduler/ -> agent.py
api/ -> agent.py, memory/, rag/
ui/ -> gateway/
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
- [ ] Persist and expose per-session and per-agent LLM usage
- [ ] Enforce optional token/request budgets across entry points
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
