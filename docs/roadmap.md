# AgentHarness Roadmap

**Last updated:** 2026-10-01  
**Current version:** 0.1.0  
**Test status:** 332 tests passing

---

## Executive Summary

AgentHarness has completed Phases 1-4: a working ReAct loop, async PostgreSQL with pgvector, MessagePack context storage, decorator-based tool registry, skills system, long-term memory with LLM-based extraction and hybrid retrieval, RAG pipeline with hybrid search and reranking, interactive REPL with prompt_toolkit, configuration system, and 332 passing tests. The architecture is clean with proper separation of concerns (models, assembler, container, context, provider, session, memory, rag).

The next phase focuses on **multi-agent orchestration, production hardening, and advanced features**.

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
└── __init__.py          # Version 0.1.0
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

## Phase 7: Advanced Features

### 7.1 LangGraph Integration (`ah/graph/`)

- StateGraph definition for complex agent workflows
- Checkpointing to PostgreSQL (reuse existing tables)
- Conditional branching based on tool results
- Human-in-the-loop approval for dangerous operations

### 7.2 TUI (`ah/tui/`)

- Textual-based terminal interface
- Split-pane: chat + context/memory view
- Real-time streaming with syntax highlighting
- Session browser with fuzzy search

### 7.3 Cost Optimization (`ah/optimization/`)

- Token usage tracking per session/agent
- Automatic model downgrade when budget is low
- Prompt caching for repeated context
- Batch embedding API calls

### 7.4 Testing & QA

| Feature | Description |
|---|---|
| Integration tests | Real PostgreSQL + mocked LLM |
| Benchmark suite | Token usage, latency, cost per task |
| Fuzz testing | Tool input fuzzing for security |
| Coverage target | 90%+ code coverage |

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
server/ -> agent.py, memory/, rag/
tui/ -> interactive.py
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
| P3 | LangGraph integration | Medium | High | 7 |
| P3 | TUI | Low | High | 7 |
| P3 | Cost optimization | Low | Medium | 7 |

---

## Success Criteria

### Phase 5 (Multi-Agent)
- [ ] Agent definitions in YAML
- [ ] Sequential and parallel orchestration
- [ ] `delegate()` tool working
- [ ] Context handoff between agents

### Phase 6 (Production)
- [ ] FastAPI server with SSE streaming
- [ ] Prometheus metrics endpoint
- [ ] Heartbeat scheduler
- [ ] Plugin system with hooks

### Phase 7 (Advanced)
- [ ] LangGraph integration
- [ ] Textual TUI
- [ ] Cost optimization
- [ ] 90%+ code coverage

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Multi-agent token costs | N agents x M iterations = expensive | Budget enforcement per agent + global cap |
| Scope creep | Too many features, none done well | Strict phase prioritization, ship incrementally |
| PostgreSQL performance | Embedding search slows at scale | HNSW indexes, connection pooling, query optimization |

---

## Open Questions

1. **Multi-agent communication:** Shared context vs. message passing vs. blackboard pattern?
2. **Server framework:** FastAPI (async-native) vs. Flask (simpler)?
3. **TUI framework:** Textual (modern) vs. prompt_toolkit (lightweight)?

---

*This roadmap is a living document. Update it as features are completed and priorities shift.*
