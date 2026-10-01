# AgentHarness

Self-hosted multi-agent AI orchestration framework. PostgreSQL-backed context, LangGraph orchestration, RAG web access, and a custom CLI.

## Why This Exists

Most agent frameworks are black boxes. AgentHarness is built to be understood — every moving piece is visible, documented, and reimplemented from first principles. The goal is to understand how systems like Hermes Agent work by rebuilding one from scratch.

## Phase 1: Foundation (Current)

- [x] Project scaffolding with Typer CLI
- [x] PostgreSQL connection layer (asyncpg + connection pooling)
- [x] Context chunks table + basic CRUD (MessagePack payloads)
- [x] LLM provider abstraction (OpenRouter, Ollama)
- [x] ReAct loop: Thought → Action → Observation
- [x] Tool registry: `read_file`, `write_file`, `list_files`, `terminal`, `web_search`, `web_extract`, `search_files`
- [x] Session manager with goal/model/provider/context_budget
- [x] Prompt assembler with token budget
- [x] Embedding search (pgvector)
- [x] Skills system (SKILL.md parser, trigger matching)
- [x] CLI: `chat`, `status`, `sessions`, `context`, `skills`, `doctor`, `init`, `version`

## Quick Start

```bash
# Setup
python -m venv .venv
source .venv/Scripts/activate  # Windows
pip install -e .

# Configure
export OPENROUTER_API_KEY=sk-or-...
export DATABASE_URL=postgresql://postgres:ah_dev@localhost:5432/agentharness

# Run
ah doctor       # check deps
ah init         # create database schema
ah chat "hello"  # one-shot chat
ah status       # list sessions
ah context      # view context chunks
```

## Architecture

```
CLI (Typer) → ReAct Agent → LLM Provider (OpenRouter/Ollama)
                  ↓
         PostgreSQL (asyncpg)
         ├── sessions
         ├── context_chunks (MessagePack + pgvector)
         ├── skills
         ├── memories
         └── agent_messages
```

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1: Foundation | CLI, PG connection, context CRUD, LLM provider, ReAct loop | Done |
| 2: Context Efficiency | MessagePack, pgvector, prompt assembler | Done |
| 3: LangGraph | StateGraph, checkpointing, streaming | Pending |
| 4: Skills & Memory | Skill registry, triggers, long-term memory | Partial |
| 5: Heartbeat & RAG | Scheduler, SearXNG, RAG pipeline | Pending |
| 6: Multi-Agent | Subagent system, OpenCode/Codex bridge | Pending |
| 7: TUI Polish | Textual interface | Pending |
| 8: Production | Cron, profiles, plugins, tests | Pending |

## Technology Stack

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Async-native |
| Database | PostgreSQL 16+ + pgvector | ACID + vector search |
| ORM/Query | asyncpg + raw SQL | Performance, control |
| Binary format | MessagePack | Compact, fast |
| Orchestration | LangGraph (planned) | Stateful agent graphs |
| CLI | Typer | Type-hint-driven |
| Provider | OpenRouter | Multi-model, OpenAI-compatible |
| Local LLM | Ollama (optional) | Free, self-hosted |

## Project Structure

```
agent-harness/
├── ah/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py              # Typer CLI
│   ├── core/
│   │   ├── agent.py        # ReAct loop
│   │   ├── context.py      # Context assembler + budget
│   │   ├── provider.py     # LLM provider abstraction
│   │   └── session.py      # Session manager
│   ├── db/
│   │   ├── connection.py   # asyncpg pool
│   │   └── schema.sql      # PostgreSQL schema
│   ├── memory/
│   ├── rag/
│   ├── skills/
│   │   └── registry.py     # Skill loader + trigger matcher
│   └── tools/
│       ├── base.py         # Tool registry
│       ├── builtins.py     # Built-in tools
│       ├── file.py         # File operations
│       └── terminal.py     # Shell execution
├── tests/
├── skills/                 # Bundled skills
├── docs/
├── pyproject.toml
└── README.md
```

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
