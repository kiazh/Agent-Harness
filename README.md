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
- **Interactive REPL**: prompt_toolkit with autocomplete, slash commands, streaming
- **Configuration**: YAML file + environment variable overrides + per-session overrides
- **Metrics**: Latency histograms, throughput counters, error rates, token usage tracking
- **Security**: No shell injection, no path traversal, no SSRF, rate limiting, audit logging, PII redaction
- **Retry logic**: Exponential backoff on LLM calls
- **DI container**: Production and testing configurations
- **Compression**: Context compression with token budget enforcement
- **User profiles**: Per-user preference and topic tracking
- **Memory approval**: Human-in-the-loop approval for sensitive memories

## Quick Start

```bash
# Setup
python -m venv .venv
source .venv/Scripts/activate  # Windows
pip install -e .

# Configure
export OPENROUTER_API_KEY=sk-or-...
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness

# Run
ah doctor       # check deps
ah init         # create database schema
ah chat "hello"  # one-shot chat
ah repl          # interactive REPL
ah status       # list sessions
ah context      # view context chunks
```

## Architecture

```
CLI (Typer) → ReActAgent → LLMProvider (OpenRouter/Ollama)
                  ↓
         PostgreSQL (asyncpg)
         ├── sessions
         ├── context_chunks (MessagePack + pgvector)
         ├── memories (pgvector)
         ├── pending_memories (approval gate)
         └── user_profiles
```

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1: Foundation | CLI, PG connection, context CRUD, LLM provider, ReAct loop | Done |
| 2: Context Efficiency | MessagePack, pgvector, prompt assembler | Done |
| 3: Memory & RAG | Long-term memory, RAG pipeline, hybrid search | Done |
| 4: Interactive REPL | prompt_toolkit REPL, slash commands, config system | Done |
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
| REPL | prompt_toolkit | Autocomplete, history |
| Provider | OpenRouter | Multi-model, OpenAI-compatible |
| Local LLM | Ollama (optional) | Free, self-hosted |

## Project Structure

```
agent-harness/
├── ah/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli/
│   │   ├── __init__.py      # Typer CLI (31 commands: chat, repl, status, sessions, sessions-search, export, fork, delete, context, compress, skills, learn, curator, hub, doctor, init, version, config, config-set, memory-list, memory-search, memory-forget, memory-pending, memory-approve, memory-reject, memory-approve-all, memory-reject-all, memory-stats, user-profile, user-profile-update, user-profile-list)
│   │   └── interactive.py   # Interactive REPL (prompt_toolkit)
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
├── tests/                  # 436 tests
├── skills/                 # Bundled skills (20 SKILL.md files)
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
# Run tests
pytest

# Lint
ruff check ah/

# Format
ruff format ah/
```

## License

MIT
