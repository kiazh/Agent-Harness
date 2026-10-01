# AgentHarness Roadmap

**Last updated:** 2026-10-01  
**Current version:** 0.1.0  
**Test status:** 136 tests passing

---

## Executive Summary

AgentHarness has a solid Phase 1–2 foundation: a working ReAct loop, async PostgreSQL with pgvector, MessagePack context storage, decorator-based tool registry, skills system, and 136 passing tests. The architecture is clean with proper separation of concerns (models, assembler, container, context, provider, session).

The next phase focuses on **making the agent actually useful**: long-term memory, RAG pipeline, interactive REPL, multi-agent orchestration, and production hardening. The empty `ah/rag/` and `ah/memory/` stubs are the highest-priority gaps.

---

## Current Architecture

```
ah/
├── core/
│   ├── models.py        # Domain models (Session, ContextChunk, LLMResponse, etc.)
│   ├── assembler.py     # PromptAssembler with token budget
│   ├── container.py     # DI container (production / testing)
│   ├── agent.py         # ReAct loop (run + run_stream)
│   ├── context.py       # ContextManager (CRUD + embedding search)
│   ├── provider.py      # LLMProvider (OpenRouter, Ollama) + rate limiting + audit
│   └── session.py       # SessionManager with TTLCache
├── db/
│   ├── connection.py    # asyncpg pool
│   └── schema.sql       # 2 tables: sessions, context_chunks
├── tools/
│   ├── base.py          # ToolRegistry (decorator-based, JSON Schema inference)
│   ├── builtins.py      # web_search, web_extract, search_files
│   ├── file.py          # read_file, write_file, list_files
│   └── terminal.py      # terminal (allowlist + SSRF protection)
├── skills/
│   └── registry.py      # SkillParser + SkillRegistry (SKILL.md + YAML frontmatter)
├── memory/              # EMPTY STUB — long-term memory
├── rag/                 # EMPTY STUB — RAG pipeline
├── cli.py               # Typer CLI (chat, status, sessions, context, skills, doctor, init)
└── __init__.py          # Version 0.1.0
```

---

## Phase 3: Memory & RAG (Highest Priority)

**Goal:** Give the agent persistent memory and retrieval-augmented generation capabilities. These are the two empty stubs and the most impactful missing features.

### 3.1 Long-Term Memory System (`ah/memory/`)

| Component | Description | Depends On |
|---|---|---|
| `memory/manager.py` | MemoryManager — CRUD for long-term memories (cross-session) | `db`, `provider.embed()` |
| `memory/models.py` | Memory dataclass (id, agent_id, content, embedding, importance, created_at, last_accessed) | — |
| `db/schema.sql` | New `memories` table (id, agent_id, content, embedding vector(1536), importance float, metadata jsonb, created_at, accessed_at) | — |
| `memory/extractor.py` | Extract memory-worthy facts from conversation (LLM-based or heuristic) | `provider` |
| `memory/consolidator.py` | Merge similar memories, decay old ones, importance scoring | `memory/manager.py` |

**Key design decisions:**
- Memories are **cross-session** (unlike context chunks which are per-session)
- Importance scoring (0.0–1.0) with time-based decay
- Embedding-based retrieval using pgvector (reuse existing infrastructure)
- Automatic extraction: after each agent run, LLM identifies facts worth remembering
- Manual extraction: `remember("user prefers X")` tool

**New tools:**
- `remember(fact: str)` — explicitly store a memory
- `recall(query: str, top_k: int = 5)` — search memories by semantic similarity
- `forget(memory_id: str)` — delete a memory

**New DB table:**
```sql
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL DEFAULT 'harness',
    content TEXT NOT NULL,
    embedding vector(1536),
    importance FLOAT DEFAULT 0.5,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    accessed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id, created_at DESC);
```

### 3.2 RAG Pipeline (`ah/rag/`)

| Component | Description | Depends On |
|---|---|---|
| `rag/embed.py` | EmbeddingClient — wraps provider.embed() with batching + caching | `provider` |
| `rag/chunker.py` | Text chunker — split documents into token-bounded chunks | `assembler.TokenCounter` |
| `rag/pipeline.py` | RAG pipeline — index documents, retrieve relevant chunks | `embed`, `chunker`, `context_manager` |
| `rag/loader.py` | Document loaders — PDF, Markdown, HTML, plain text | — |

**Key design decisions:**
- Reuse `context_chunks` table for RAG storage (add `chunk_type='rag_document'`)
- Or create separate `documents` + `document_chunks` tables for cleaner separation
- Embedding caching with TTLCache to avoid redundant API calls
- Chunk overlap strategy: 10–15% overlap to preserve context at boundaries
- Configurable chunk size (default: 512 tokens)

**New tools:**
- `index_document(path: str)` — load, chunk, embed, and store a document
- `search_documents(query: str, top_k: int = 5)` — semantic search across indexed documents

### 3.3 Integration Points

- **PromptAssembler** gets a new `memory_chunks` parameter alongside `retrieved_chunks`
- **ReActAgent.run()** automatically retrieves relevant memories before LLM call
- **ContextManager** gains `search_memories()` method (parallel to `search_by_embedding()`)

---

## Phase 4: Interactive REPL & UX

**Goal:** Move beyond one-shot `ah chat "message"` to a persistent interactive experience.

### 4.1 Interactive REPL (`ah/cli/interactive.py`)

| Feature | Description |
|---|---|
| Persistent prompt | `ah` launches a REPL with `>` prompt |
| Session auto-creation | Creates a session on first message, persists across turns |
| History | Arrow-key history via `prompt_toolkit` or `readline` |
| Multi-line input | Shift+Enter for multi-line, Enter to send |
| Slash commands | `/new`, `/sessions`, `/context`, `/memory`, `/forget`, `/model`, `/verbose`, `/help` |
| Rich rendering | Markdown rendering, syntax highlighting for code blocks |
| Streaming | Real-time token streaming with `rich.live.Live` |

### 4.2 CLI Enhancements

| Command | Description |
|---|---|
| `ah chat --interactive` | Launch REPL mode |
| `ah chat --file <path>` | Send file contents as message |
| `ah memory list` | List all memories |
| `ah memory search <query>` | Search memories |
| `ah memory forget <id>` | Delete a memory |
| `ah documents index <path>` | Index a document for RAG |
| `ah documents search <query>` | Search indexed documents |
| `ah config show` | Show current configuration |
| `ah config set <key> <value>` | Update configuration |

### 4.3 Configuration System (`ah/core/config.py`)

- YAML config file at `~/.agent-harness/config.yaml`
- Environment variable overrides
- Per-session model/provider overrides
- Sensible defaults for all settings

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
| **Sequential** | Agent A → Agent B → Agent C (pipeline) |
| **Parallel** | Multiple agents work on independent subtasks simultaneously |
| **Hierarchical** | Orchestrator delegates to worker agents, synthesizes results |
| **Debate** | Multiple agents discuss, moderator decides |

### 5.4 New Tools

- `delegate(agent: str, task: str)` — send a task to another agent
- `list_agents()` — list available agents
- `spawn_agent(definition: str)` — dynamically create a new agent

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
- `POST /api/v1/sessions` — create session
- `GET /api/v1/sessions` — list sessions
- `POST /api/v1/sessions/{id}/chat` — send message (SSE stream)
- `GET /api/v1/sessions/{id}/context` — get context
- `POST /api/v1/memory` — create memory
- `GET /api/v1/memory/search` — search memories
- `POST /api/v1/documents` — index document
- `GET /api/v1/documents/search` — search documents

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
- `pre_agent_run(session, message)` — before agent starts
- `post_agent_run(session, response)` — after agent completes
- `on_tool_call(tool_name, args)` — before tool execution
- `on_tool_result(tool_name, result)` — after tool execution
- `on_memory_extract(memories)` — after memory extraction

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
cli.py → interactive.py → agent.py → assembler.py → context.py → connection.py
                                    → provider.py
                                    → session.py → connection.py
                                    → memory/ → provider.py, connection.py
                                    → rag/ → provider.py, connection.py
                                    → multi_agent/ → agent.py
                                    → scheduler/ → agent.py
server/ → agent.py, memory/, rag/
tui/ → interactive.py
```

---

## Implementation Priority Matrix

| Priority | Feature | Impact | Effort | Phase |
|---|---|---|---|---|
| P0 | Memory system (ah/memory/) | Very High | Medium | 3 |
| P0 | RAG pipeline (ah/rag/) | Very High | Medium | 3 |
| P1 | Interactive REPL | High | Medium | 4 |
| P1 | CLI enhancements | High | Low | 4 |
| P1 | Configuration system | Medium | Low | 4 |
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

### Phase 3 (Memory & RAG)
- [ ] `ah/memory/` fully implemented with CRUD + embedding search
- [ ] `ah/rag/` fully implemented with document indexing + retrieval
- [ ] Agent automatically extracts and retrieves memories
- [ ] `remember()`, `recall()`, `index_document()`, `search_documents()` tools working
- [ ] 180+ tests passing

### Phase 4 (Interactive REPL)
- [ ] `ah` launches interactive REPL by default
- [ ] Slash commands work (`/new`, `/sessions`, `/memory`, etc.)
- [ ] Streaming output in real-time
- [ ] Session persistence across turns

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

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Memory extraction quality | Irrelevant memories clutter context | Importance scoring + user feedback loop |
| RAG retrieval accuracy | Wrong context leads to wrong answers | Tune chunk size, overlap, and similarity threshold |
| Multi-agent token costs | N agents × M iterations = expensive | Budget enforcement per agent + global cap |
| Scope creep | Too many features, none done well | Strict phase prioritization, ship incrementally |
| PostgreSQL performance | Embedding search slows at scale | HNSW indexes, connection pooling, query optimization |

---

## Open Questions

1. **Memory extraction:** LLM-based (accurate but costly) vs. heuristic (fast but less accurate)?
2. **RAG storage:** Reuse `context_chunks` or separate `documents`/`document_chunks` tables?
3. **Multi-agent communication:** Shared context vs. message passing vs. blackboard pattern?
4. **Server framework:** FastAPI (async-native) vs. Flask (simpler)?
5. **TUI framework:** Textual (modern) vs. prompt_toolkit (lightweight)?

---

*This roadmap is a living document. Update it as features are completed and priorities shift.*
