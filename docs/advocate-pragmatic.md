# AgentHarness: The Pragmatic Advocate

**Verdict: Ship it. It's good enough for a portfolio project.**

---

## The Case for "Good Enough"

AgentHarness is a working, self-hosted AI agent framework with a clean architecture, a real ReAct loop, PostgreSQL-backed context, and a functional CLI. It does what it says on the tin. For a portfolio project — something that demonstrates your ability to design and build a complex system — this is more than sufficient.

### What's Already Working

- **ReAct loop** (`ah/core/agent.py`): Thought → Action → Observation, with tool calling, iteration limits, and context persistence. The core agent loop is real and functional.
- **PostgreSQL + asyncpg** (`ah/db/connection.py`): Connection pooling, schema initialization, and raw SQL queries. No ORM overhead, just clean async database access.
- **Context management** (`ah/core/context.py`): MessagePack-serialized chunks, token budgeting, prompt assembly, and pgvector embedding search. The context system is thoughtful and efficient.
- **LLM provider abstraction** (`ah/core/provider.py`): OpenRouter and Ollama providers with a clean interface. Multi-model support out of the box.
- **Tool registry** (`ah/tools/base.py`): Decorator-based registration with JSON Schema inference. Seven built-in tools covering file ops, terminal, and web search.
- **Skills system** (`ah/skills/registry.py`): SKILL.md parser with YAML frontmatter, trigger matching, and skill loading. Works with the standard skill format.
- **CLI** (`ah/cli.py`): Eight commands — `chat`, `status`, `sessions`, `context`, `skills`, `doctor`, `init`, `version`. Clean Typer interface with Rich output.
- **Tests** (`tests/`): 136 tests covering unit tests, edge cases, error handling, mocked database operations, CLI tests, and provider tests.
- **Schema** (`ah/db/schema.sql`): Minimal 2-table schema (sessions, context_chunks) with proper indexes, HNSW vector indexes, and foreign keys with cascade deletes.

### Why This Is Portfolio-Ready

1. **It demonstrates systems thinking.** You didn't just call an API and wrap it in a web app. You built a context management system, a tool registry, a session manager, and a prompt assembler. Each piece is a real engineering decision.

2. **It's readable.** The codebase is ~22 Python files, each focused and well-documented. A reviewer can read `agent.py` and understand the ReAct loop in 30 seconds. The architecture is obvious from the file structure.

3. **It has real dependencies and integration.** PostgreSQL, pgvector, MessagePack, asyncpg, httpx — these are production-grade tools. You're not using toy substitutes.

4. **The test suite is serious.** 136 tests with mocked databases, mocked HTTP providers, and CLI integration tests. This shows you understand how to test async code and complex systems.

5. **The README is honest.** It clearly states what's done, what's pending, and why the project exists. No hype, no overpromising.

---

## The Case Against Over-Engineering

### "But it doesn't have LangGraph yet"

LangGraph is listed as "Pending" in the roadmap. That's fine. The current ReAct loop is a legitimate orchestration pattern. Adding LangGraph now would be adding complexity for its own sake. The current loop works, it's understandable, and it's testable. LangGraph can be added later when the project actually needs stateful graphs and checkpointing.

### "But there's no streaming"

**Status:** ✅ FIXED — SSE streaming added via `run_stream()` and `provider.stream_complete()`. The CLI uses Rich's `Live` display for real-time output.

### "But the token estimation is naive"

**Status:** ✅ FIXED — tiktoken with `cl100k_base` encoding added. The `TokenCounter` class in `assembler.py` provides accurate token counts with a `len//4` fallback.

### "But there's no multi-agent system yet"

The schema has been simplified to 2 tables (sessions, context_chunks). Multi-agent orchestration is a Phase 6 concern. Building it now — before the single-agent system is battle-tested — would be premature. Get the core loop rock-solid first.

### "But there's no RAG pipeline"

The embedding search is already there (`search_by_embedding` in `context.py`). The pgvector indexes are already in the schema. A full RAG pipeline with chunking strategies and reranking is a Phase 5 concern. The current system can already retrieve relevant context by similarity.

### The Core Argument

Every feature in the roadmap (LangGraph, multi-agent, RAG, heartbeat, TUI, production hardening) is a *multi-week* effort. Building all of them before shipping means the project never ships. The current state is a **working foundation** that demonstrates the core concepts. That's what a portfolio project needs to do.

---

## The Remaining Issues

### ✅ FIXED: Duplicate Tool Definitions

**Status:** ✅ FIXED — `builtins.py` now only has `web_search`, `web_extract`, and `search_files`. The duplicate `read_file`, `write_file`, `list_files`, and `terminal` definitions have been removed.

### ✅ FIXED: No Error Handling in the ReAct Loop

**Status:** ✅ FIXED — Retry logic with exponential backoff (3 retries) added to `_call_llm_with_retry()` and `_stream_llm_with_retry()`. On final failure, returns an `AgentResponse` with an error message instead of crashing.

### ⚠️ STILL VALID: No CI/CD

**Problem:** There's no GitHub Actions workflow, no automated testing on push, no linting on PR. The test suite exists but nothing enforces it.

**Fix:** Add a simple `.github/workflows/ci.yml` that runs `pytest` and `ruff check` on every push and PR. This is a 20-minute fix.

**Why it matters:** CI/CD is the most basic signal that a project is maintained seriously. Its absence suggests the project is a throwaway. A simple workflow that runs tests and lints takes 20 minutes to set up and instantly makes the project look professional.

---

## What Can Wait (And Why)

| Feature | Why It Can Wait |
|---|---|
| LangGraph integration | Current ReAct loop works. Add LangGraph when you need stateful graphs. |
| Multi-agent system | Schema is minimal. Build when single-agent is battle-tested. |
| RAG pipeline | Embedding search exists. Add chunking/reranking when context volume demands it. |
| Heartbeat scheduler | Nice-to-have. Not core to the agent's functionality. |
| Production hardening | The project works. Harden when deploying. |
| Alembic migrations | Schema is managed manually. Add Alembic when schema changes become frequent. |

---

## The Bottom Line

AgentHarness is a **working, well-architested, seriously-tested** AI agent framework. It demonstrates real engineering skill: async Python, PostgreSQL, vector search, LLM integration, tool use, and context management. The codebase is clean, readable, and honest about what it does and doesn't do.

Two of the three critical issues (duplicate tools, error handling) have been fixed. Only CI/CD remains. This is a portfolio project you can be proud to show.

**Ship it.**
