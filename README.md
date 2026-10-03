# AgentHarness

Self-hosted AI agent framework. PostgreSQL-backed context, RAG web access, streaming, long-term memory, and a custom CLI.

## Why This Exists

Most agent frameworks are black boxes. AgentHarness is built to be understood — every moving piece is visible, documented, and reimplemented from first principles. The goal is to understand how systems like Hermes Agent work by rebuilding one from scratch.

## Features

- **ReAct loop**: Thought → Action → Observation with streaming output
- **PostgreSQL-backed context**: asyncpg + connection pooling, MessagePack payloads, pgvector embeddings
- **Long-term memory**: LLM-based extraction, importance scoring, Ebbinghaus forgetting, hybrid retrieval, approval gate
- **RAG pipeline**: Document indexing, chunking, embedding, hybrid search (BM25 + dense + RRF), reranking
- **Tool registry**: Decorator-based with JSON Schema inference, input validation, caching
- **Skills system**: SKILL.md parser with YAML frontmatter, trigger matching
- **Terminal UI**: TypeScript app on [`@earendil-works/pi-tui`](https://github.com/earendil-works/pi) (MIT) — streaming Markdown, tool cards, slash-command and file autocomplete, session picker — driving the Python agent through a JSON-RPC gateway
- **Configuration**: YAML file + environment variable overrides + per-session overrides
- **Metrics**: Latency histograms, throughput counters, error rates, token usage tracking
- **Security**: No shell injection, no path traversal, no SSRF, rate limiting, audit logging, PII redaction
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
autocomplete (`/new`, `/sessions`, `/resume`, `/model`, `/provider`, `/clear`, `/exit`),
Tab completes file paths, Ctrl+C exits.

For the HTTP API with Docker Compose, set `AGENT_HARNESS_API_KEY` in `.env` and run
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
                                              ├── memories (pgvector)
                                              ├── pending_memories (approval gate)
                                              └── user_profiles
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
| 5: Multi-Agent | Subagent system, orchestration | Pending |
| 6: Production | Web API, scheduler, plugins, observability | Pending |
| 7: Advanced | LangGraph, TUI, cost optimization | Pending |

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
│   │   └── server.py        # Gateway: sessions, prompt streaming, cancel, config
│   ├── core/
│   │   ├── agent.py         # ReActAgent + BaseReActAgent (Template Method)
│   │   ├── assembler.py     # PromptAssembler + TokenCounter (tiktoken)
│   │   ├── compression.py   # Context compression with token budget enforcement
│   │   ├── config.py        # Config (YAML + env vars + session overrides)
│   │   ├── container.py     # DI container (production / testing)
│   │   ├── context.py       # ContextManager (CRUD, batch insert, embedding search)
│   │   ├── exceptions.py    # Custom exception hierarchy
│   │   ├── metrics.py       # MetricsCollector (latency, counters, errors, tokens)
│   │   ├── models.py        # Domain models (Session, ContextChunk, LLMResponse, etc.)
│   │   ├── provider.py      # LLMProvider (OpenRouter, Ollama) + rate limiting + audit
│   │   ├── serialization.py # Shared serialization utilities
│   │   └── session.py       # SessionManager (with TTLCache)
│   ├── db/
│   │   ├── connection.py    # asyncpg pool
│   │   └── schema.sql       # PostgreSQL schema (5 tables)
│   ├── memory/
│   │   ├── approval.py      # MemoryApproval (human-in-the-loop approval gate)
│   │   ├── consolidator.py  # MemoryConsolidator (LLM extraction → scoring → dedup → write)
│   │   ├── forgetting.py    # ForgettingModel (Ebbinghaus decay)
│   │   ├── models.py        # MemoryEntry, RetrievedMemory
│   │   ├── redaction.py     # PII redaction for memory content
│   │   ├── retriever.py     # MemoryRetriever (hybrid search + reranking)
│   │   ├── scorer.py        # ImportanceScorer (multi-factor)
│   │   ├── store.py         # MemoryStore (CRUD)
│   │   └── user_profile.py  # UserProfileManager (per-user preferences)
│   ├── rag/
│   │   ├── chunker.py       # RecursiveCharacterTextSplitter
│   │   ├── embedder.py      # OpenAIEmbedder (LRU cache + batch)
│   │   ├── loaders.py       # FileLoader (txt, md, code, PDF)
│   │   ├── pipeline.py      # RAGPipeline (index, search, index_session_context)
│   │   ├── reranker.py      # CohereReranker, IdentityReranker
│   │   └── search.py        # HybridSearch (BM25 + dense + RRF)
│   ├── skills/
│   │   └── registry.py      # SkillParser + SkillRegistry (SKILL.md + YAML frontmatter)
│   └── tools/
│       ├── base.py          # ToolRegistry (decorator, validation, caching)
│       ├── builtins.py      # web_search, web_extract, search_files
│       ├── file.py          # read_file, write_file, list_files
│       ├── memory.py        # remember, recall
│       ├── rag.py           # index_document, search_documents
│       ├── registry.py      # Re-export for backward compat
│       └── terminal.py      # terminal (allowlist + SSRF protection)
├── ui/                     # TypeScript terminal UI (pi-tui): src/ app, gateway client, widgets; test/
├── communications/         # Design notes, incl. the UI ↔ gateway protocol
├── tests/                  # Python test suite (pytest)
├── skills/                 # Bundled skills (SKILL.md files)
├── docs/
├── pyproject.toml
└── README.md
```

## Security & Reliability

- **No shell injection**: `terminal` tool uses `shell=False` with command allowlist
- **No path traversal**: File tools validate paths against base directory
- **No SSRF**: Web tools validate URLs against private IP ranges
- **Rate limiting**: `AsyncTokenBucket` on all LLM calls
- **Input validation**: `_validate_messages`, `_validate_params`, tool arg validation
- **Retry logic**: Exponential backoff on LLM calls (3 retries)
- **Audit logging**: All security-relevant events logged as JSON
- **Streaming**: SSE streaming for real-time output

## Development

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
