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
- **Tests** (`tests/`): 1,570 lines of comprehensive tests covering unit tests, edge cases, error handling, mocked database operations, CLI tests, and provider tests. This is more test coverage than many production projects.
- **Schema** (`ah/db/schema.sql`): Well-designed with proper indexes, HNSW vector indexes, foreign keys with cascade deletes, and support for future phases (subagents, heartbeats, external context).

### Why This Is Portfolio-Ready

1. **It demonstrates systems thinking.** You didn't just call an API and wrap it in a web app. You built a context management system, a tool registry, a session manager, and a prompt assembler. Each piece is a real engineering decision.

2. **It's readable.** The codebase is ~22 Python files, each focused and well-documented. A reviewer can read `agent.py` and understand the ReAct loop in 30 seconds. The architecture is obvious from the file structure.

3. **It has real dependencies and integration.** PostgreSQL, pgvector, MessagePack, asyncpg, httpx — these are production-grade tools. You're not using toy substitutes.

4. **The test suite is serious.** 1,570 lines of tests with mocked databases, mocked HTTP providers, and CLI integration tests. This shows you understand how to test async code and complex systems.

5. **The README is honest.** It clearly states what's done, what's pending, and why the project exists. No hype, no overpromising.

---

## The Case Against Over-Engineering

### "But it doesn't have LangGraph yet"

LangGraph is listed as "Pending" in the roadmap. That's fine. The current ReAct loop is a legitimate orchestration pattern. Adding LangGraph now would be adding complexity for its own sake. The current loop works, it's understandable, and it's testable. LangGraph can be added later when the project actually needs stateful graphs and checkpointing.

### "But there's no streaming"

Streaming is a UX optimization, not a correctness issue. The agent produces correct responses; it just doesn't token-stream them. For a portfolio project, this is irrelevant. Streaming can be added in an afternoon when the TUI phase comes.

### "But the token estimation is naive"

`len(text) // 4` is a rough heuristic. Yes. But it's *consistent*, it's *fast*, and it's *good enough* for budget management. Swapping in tiktoken would add a dependency and complexity for marginal accuracy gains. The current approach works for the scale this project operates at.

### "But there's no multi-agent system yet"

The schema already has tables for `subagent_sessions`, `subagent_messages`, and `subagent_results`. The foundation is laid. Building the actual multi-agent orchestration now — before the single-agent system is battle-tested — would be premature. Get the core loop rock-solid first.

### "But there's no RAG pipeline"

The embedding search is already there (`search_by_embedding` in `context.py`). The pgvector indexes are already in the schema. A full RAG pipeline with chunking strategies and reranking is a Phase 5 concern. The current system can already retrieve relevant context by similarity.

### The Core Argument

Every feature in the roadmap (LangGraph, multi-agent, RAG, heartbeat, TUI, production hardening) is a *multi-week* effort. Building all of them before shipping means the project never ships. The current state is a **working foundation** that demonstrates the core concepts. That's what a portfolio project needs to do.

---

## The 2-3 Things That MUST Be Fixed

These are the issues that would make a reviewer say "this isn't serious." Fix these three and the project is credible.

### 1. Duplicate Tool Definitions (Bug)

**Problem:** `read_file`, `write_file`, `list_files`, and `terminal` are defined in **both** `ah/tools/builtins.py` **and** `ah/tools/file.py` / `ah/tools/terminal.py`. The implementations differ:

- `builtins.py:read_file` returns line-numbered output (`1 | content`)
- `file.py:read_file` returns raw content without line numbers
- `builtins.py:terminal` doesn't support `workdir`
- `terminal.py:terminal` supports `workdir`

The `builtins.py` file imports `file` and `terminal` at the bottom, so the later registrations win. But this is fragile, confusing, and a bug waiting to happen.

**Fix:** Delete the duplicate definitions from `builtins.py`. Keep only `web_search`, `web_extract`, and `search_files` in `builtins.py` (since those don't have their own modules). Import `file` and `terminal` modules for their side-effect registrations. This is a 15-minute fix.

**Why it matters:** A reviewer who notices duplicate code with different implementations will question the entire codebase's reliability. This is the kind of sloppiness that undermines confidence in everything else.

### 2. No Error Handling in the ReAct Loop (Robustness)

**Problem:** If the LLM provider call fails (rate limit, network error, invalid API key), the `ReActAgent.run()` method crashes with an unhandled exception. There's no retry logic, no graceful degradation, and no user-friendly error message.

**Fix:** Wrap the `provider.complete()` call in a try-except with exponential backoff retry (2-3 retries). On final failure, return an `AgentResponse` with an error message instead of crashing. This is a 30-minute fix.

**Why it matters:** An agent that crashes on the first API hiccup looks unfinished. Error handling is table stakes for any system that calls external APIs. This is the difference between "toy project" and "serious project."

### 3. No CI/CD (Credibility)

**Problem:** There's no GitHub Actions workflow, no automated testing on push, no linting on PR. The test suite exists but nothing enforces it.

**Fix:** Add a simple `.github/workflows/ci.yml` that runs `pytest` and `ruff check` on every push and PR. This is a 20-minute fix.

**Why it matters:** CI/CD is the most basic signal that a project is maintained seriously. Its absence suggests the project is a throwaway. A simple workflow that runs tests and lints takes 20 minutes to set up and instantly makes the project look professional.

---

## What Can Wait (And Why)

| Feature | Why It Can Wait |
|---|---|
| LangGraph integration | Current ReAct loop works. Add LangGraph when you need stateful graphs. |
| Streaming responses | UX optimization, not correctness. Add when building the TUI. |
| Multi-agent system | Schema is ready. Build when single-agent is battle-tested. |
| RAG pipeline | Embedding search exists. Add chunking/reranking when context volume demands it. |
| Heartbeat scheduler | Nice-to-have. Not core to the agent's functionality. |
| Production hardening | The project works. Harden when deploying. |
| Better token estimation | Current heuristic is consistent and fast. Swap for tiktoken if accuracy matters. |
| Alembic migrations | Schema is managed manually. Add Alembic when schema changes become frequent. |

---

## The Bottom Line

AgentHarness is a **working, well-architested, seriously-tested** AI agent framework. It demonstrates real engineering skill: async Python, PostgreSQL, vector search, LLM integration, tool use, and context management. The codebase is clean, readable, and honest about what it does and doesn't do.

Fix the three issues above — duplicate tools, error handling, and CI/CD — and this is a portfolio project you can be proud to show. Everything else is iteration.

**Ship it.**
