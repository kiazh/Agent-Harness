# Code Quality Advocate Report — AgentHarness

**Verdict: The codebase demonstrates genuine engineering quality. It is well-structured, follows established Python and async best practices, and exhibits a sound, layered architecture. While not perfect, the design decisions are deliberate and defensible.**

---

## 1. Architecture: Clean Layered Design

The project follows a **clear separation of concerns** that maps to how production AI agent systems are actually built:

```
CLI (Typer) → ReAct Agent → LLM Provider (OpenRouter/Ollama)
                  ↓
         PostgreSQL (asyncpg)
         ├── sessions
         └── context_chunks (MessagePack + pgvector)
```

**Why this is good:**

- **The `ah/core/` package is the brain.** `agent.py`, `assembler.py`, `context.py`, `provider.py`, `session.py`, `models.py`, and `container.py` are the pillars. Each has a single, well-defined responsibility. The agent loop doesn't know about SQL. The provider doesn't know about sessions. The context manager doesn't know about the agent. This is textbook separation of concerns.

- **The `ah/tools/` package is the hands.** Tools are isolated behind a registry pattern with a clean decorator API. The agent loop calls `registry.execute(name, **kwargs)` — it never imports a tool directly. This is dependency inversion done right.

- **The `ah/db/` package is the memory.** A single `Database` class wraps asyncpg's pool. All SQL lives in one place. The schema is in a dedicated `schema.sql` file, not scattered through Python string literals.

- **The `ah/skills/` package is the knowledge.** Skills are loaded from SKILL.md files with YAML frontmatter — a convention borrowed from the broader agent ecosystem (Claude Code, Cursor, etc.). This makes the system extensible without code changes.

**Defense against criticism:** "Why not use an ORM?" — Because asyncpg with raw SQL gives you full control over query performance, especially for pgvector operations like `embedding <=> $1::vector`. ORMs add abstraction overhead for vector search queries that don't map cleanly to relational models. The choice of raw SQL + asyncpg is a deliberate performance decision, not a lack of sophistication.

---

## 2. The ReAct Agent Loop: Simple and Correct

The core loop in `ah/core/agent.py` is a **clean implementation of the ReAct pattern**:

```python
for iteration in range(self.max_iterations):
    response = await self.provider.complete(messages=messages, tools=tool_defs)
    if not response.tool_calls:
        return AgentResponse(...)  # Final answer
    for tc in response.tool_calls:
        result = await registry.execute(tool_name, **tool_args)
        messages.append(...)  # Feed result back
```

**Why this is good:**

- **Bounded execution.** `max_iterations` prevents infinite loops. This is a production concern that many toy implementations skip.
- **Tool results are persisted to context.** Every tool call and result is stored as a `ContextChunk` with MessagePack serialization. This means the agent's reasoning is auditable and resumable.
- **Error isolation.** Tool execution is wrapped in try/except — a failing tool doesn't crash the agent loop. The error message is fed back to the LLM, which can decide to try a different approach.
- **Token tracking.** The loop accumulates `total_tokens` across iterations, giving visibility into cost.

**Defense against criticism:** "Why not use LangGraph?" — The README explicitly lists LangGraph as a Phase 3 item. The current ReAct loop is intentionally simple: it's 182 lines, fully testable, and has no external orchestration dependency. Adding LangGraph before the core loop is proven would be premature optimization. The current design is the right foundation to build on.

---

## 3. Provider Abstraction: Strategy Pattern Done Right

`ah/core/provider.py` implements a **clean strategy pattern** for LLM providers:

- `LLMProvider` is an abstract base with `complete()` and `embed()` methods.
- `OpenRouterProvider` and `OllamaProvider` are concrete implementations.
- `get_provider()` is a factory function that returns the right provider based on a string.

**Why this is good:**

- **Testability.** The comprehensive test suite mocks `provider.complete()` to return canned `LLMResponse` objects. This means the agent loop can be tested without making real API calls. The test `test_agent_run_with_tool_calls` uses `side_effect=[tool_call_response, final_response]` to simulate a multi-turn conversation — this is exactly how you test an agent loop.
- **Interoperability.** OpenRouter and Ollama have different APIs (OpenAI-compatible vs. native), but the `LLMResponse` dataclass normalizes them. The agent loop doesn't care which provider it's talking to.
- **Graceful degradation.** The `web_search` tool in `builtins.py` tries SearXNG first, then falls back to DuckDuckGo HTML scraping. This is defensive programming for real-world reliability.

---

## 4. Context Management: Token-Efficient and Embedding-Aware

`ah/core/context.py` implements a **prompt assembly strategy** that is more sophisticated than most open-source agent frameworks:

- **Token budget enforcement.** `PromptAssembler` tracks token usage and stops adding context when the budget is exhausted. The `TokenCounter` class uses `tiktoken` with `cl100k_base` encoding for accurate token counts, with a `len//4` fallback if tiktoken is unavailable.
- **Chunk compression.** The `_compress_chunk()` method converts structured context (tool calls, results, memories) into compact text representations. A tool call becomes `[tool_call] read_file(path=/tmp/test)` instead of a full JSON dump.
- **Embedding search.** The `search_by_embedding()` method uses pgvector's cosine distance operator (`<=>`) with an HNSW index. This is the same vector search strategy used by production RAG systems.
- **LRU tracking.** The `accessed_at` column and `mark_accessed()` method support future LRU eviction — the schema is designed for it even if eviction isn't implemented yet.

**Defense against criticism:** "Why not use a proper tokenizer?" — tiktoken is now used with `cl100k_base` encoding for accurate token counts. The `TokenCounter` class in `assembler.py` provides exact counts with a `len//4` fallback if tiktoken is unavailable.

---

## 5. Tool Registry: Decorator-Based with Schema Inference

`ah/tools/base.py` implements a **decorator-based tool registry** with automatic JSON Schema inference:

```python
@registry.register(description="Read a file from disk")
def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    ...
```

**Why this is good:**

- **Developer experience.** Adding a new tool is a one-liner. The decorator infers the JSON Schema from type hints, so the LLM gets proper tool definitions without manual schema writing.
- **Async-aware.** The registry detects `inspect.iscoroutinefunction(func)` and handles async tools correctly. This is essential for an async-native codebase.
- **Separation of concerns.** The registry (`base.py`) is separate from the built-in tools (`builtins.py`, `file.py`, `terminal.py`). Third-party tools can be registered without modifying the core registry code.

**Defense against criticism:** "Why not use Pydantic for schema validation?" — The current `_infer_schema()` method handles the common types (str, int, float, bool, list, dict) and defaults to "string" for unknown types. This is sufficient for LLM function calling, where the schema is a hint to the model, not a strict validation boundary. Adding Pydantic would add a dependency and complexity for marginal benefit at this stage.

---

## 6. Database Schema: Production-Ready

`ah/db/schema.sql` is a **well-designed PostgreSQL schema** with:

- **Proper constraints.** `CHECK` constraints on `status`, `chunk_type`. This enforces data integrity at the database level.
- **Foreign keys with cascade.** `ON DELETE CASCADE` for context chunks. This prevents orphaned records.
- **HNSW indexes for vector search.** `USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)` — these are production-grade pgvector index parameters.
- **Composite indexes.** `idx_context_chunks_session ON context_chunks(session_id, created_at DESC)` covers the most common query pattern.
- **Partial indexes.** `idx_sessions_active_last_activity ON sessions(status, last_activity DESC) WHERE status = 'active'` — this is a sophisticated PostgreSQL feature that keeps the index small.

**Defense against criticism:** "Why not use Alembic for migrations?" — The README lists Alembic as a dependency and the `alembic-migrations` skill exists. The current `schema.sql` is a baseline; Alembic would be added when the schema evolves beyond v1. For a v0.1.0 project, a single schema file is the right starting point.

---

## 7. Testing: Comprehensive and Well-Organized

The test suite (`tests/test_comprehensive.py`, 136 tests) is **genuinely comprehensive**:

- **Unit tests** for every major class: `PromptAssembler`, `ToolRegistry`, `SkillParser`, `SkillRegistry`, dataclasses.
- **Edge case tests** for empty strings, unicode, special characters, very large inputs.
- **Error handling tests** for file not found, permission denied, timeout, invalid commands.
- **Integration tests** with mocked database using `AsyncMock` and `patch`.
- **CLI tests** using Typer's `CliRunner`.
- **Provider tests** with mocked HTTP responses.
- **Agent tests** with mocked providers, including multi-turn conversations with tool calls.

**Why this is good:**

- **Tests are organized by concern.** Each class has its own test class with clear docstrings.
- **Async tests use `pytest-asyncio`.** The `asyncio_mode = "auto"` configuration means async test functions just work.
- **Mocking is done at the right boundary.** Database tests mock `db.fetchrow`, not the entire `Database` class. Provider tests mock `provider.client.post`, not the entire HTTP stack. This means tests verify business logic, not mock behavior.
- **The test suite is self-documenting.** Reading `test_agent_run_with_tool_calls` tells you exactly how the agent loop handles tool calls.

**Defense against criticism:** "Why only 136 tests?" — The test-to-code ratio is reasonable for a project at this stage. More importantly, the tests cover the critical paths: the agent loop, tool execution, context assembly, and provider interaction. The schema and CLI are thinner and have proportionally fewer tests.

---

## 8. CLI: User-Friendly and Well-Structured

`ah/cli.py` uses **Typer** to build a clean CLI with:

- **Multiple commands:** `chat`, `status`, `sessions`, `context`, `skills`, `doctor`, `init`, `version`.
- **Rich output.** Uses `rich.console.Console`, `rich.table.Table`, and `rich.panel.Panel` for formatted output.
- **Proper async handling.** The `_run()` helper wraps `asyncio.run()` for each command, and `db.close()` is called in `finally` blocks.
- **Session management.** The `chat` command supports `--continue` (resume last session) and `--session` (resume specific session), making the agent stateful across invocations.

**Defense against criticism:** "Why not use a TUI?" — The README lists a Textual TUI as Phase 7. The current CLI is the right foundation: it's scriptable, testable, and works over SSH. A TUI would add complexity without adding capability at this stage.

---

## 9. Skills System: Extensible by Convention

`ah/skills/registry.py` implements a **file-based skill system**:

- Skills are loaded from `skills/<name>/SKILL.md` files.
- YAML frontmatter provides metadata (name, description, triggers, version).
- The `match_triggers()` method does simple keyword matching against the query.
- 20 skills are bundled with the project, covering topics from `langgraph` to `security-sandboxing`.

**Why this is good:**

- **Zero-code extensibility.** Adding a new skill is creating a directory and writing a SKILL.md file. No Python code required.
- **Convention over configuration.** The SKILL.md format is borrowed from the broader agent ecosystem, making it familiar to users of Claude Code, Cursor, etc.
- **The parser uses PyYAML.** `SkillParser` uses `yaml.safe_load()` for robust YAML frontmatter parsing, with graceful fallback on parse errors.

---

## 10. Genuine Quality vs. Superficial Quality

### Genuine Quality (defensible under scrutiny):

| Aspect | Why It's Genuine |
|--------|-----------------|
| **Async-native design** | Every I/O operation is async. The agent loop, database, and HTTP clients are all `async`/`await`. No blocking calls in async contexts. |
| **Connection pooling** | `asyncpg.create_pool(min_size=2, max_size=10)` with `command_timeout=30`. This is production-grade connection management. |
| **MessagePack serialization** | Context payloads are serialized with MessagePack, not JSON. This is faster and more compact, and the `use_bin_type=True` flag ensures proper binary handling. |
| **pgvector integration** | The schema uses `vector(1536)` columns with HNSW indexes. The `search_by_embedding()` method uses the `<=>` cosine distance operator. This is the same approach used by production RAG systems. |
| **Error isolation** | Tool execution errors are caught and fed back to the LLM. Database connection failures are handled gracefully in CLI commands. |
| **Test mocking strategy** | Tests mock at the right boundary (database fetch methods, HTTP client post methods), not at the class level. This means tests verify real behavior. |
| **Schema design** | Check constraints, foreign keys with cascade, partial indexes, and HNSW vector indexes. This is DDL written by someone who knows PostgreSQL. |

### Superficial Quality (would not survive scrutiny):

| Aspect | Why It's Superficial |
|--------|---------------------|
| **`web_search` fallback** | The DuckDuckGo HTML scraping uses a simple regex that could break if DuckDuckGo changes their HTML. This is fragile but acceptable as a fallback. |
| **`DEFAULT_DSN` hardcoded** | The default DSN `postgresql://postgres:***@localhost:5432/agentharness` has a placeholder password. This is fine for development but would need to be overridden in production. |

---

## 11. What's Missing (Honest Assessment)

The advocate role requires acknowledging weaknesses:

1. **No CI/CD pipeline.** There's no GitHub Actions workflow or similar. Tests are run manually with `pytest`.
2. **No type checking in CI.** The code uses type hints (`from __future__ import annotations`, `str | None`, etc.) but there's no `mypy` or `pyright` configuration.
3. **No metrics or tracing.** Audit logging is present, but there are no Prometheus metrics or OpenTelemetry tracing.
4. **No interactive REPL.** The CLI is one-shot commands only. An interactive REPL with slash commands is not yet implemented.
5. **The `memory/` and `rag/` packages are empty.** These are listed in the README as Phase 4 and Phase 5 items, but the directories exist with only `__init__.py` files.

These are all **acceptable gaps for a v0.1.0 project**. The foundation is solid, and these items are explicitly listed in the roadmap.

---

## 12. Conclusion

AgentHarness is a **well-architected, genuinely engineered AI agent framework**. The code is not just superficially clean — it demonstrates real understanding of:

- **Async Python** (async/await, connection pooling, async context managers)
- **PostgreSQL** (constraints, indexes, pgvector, HNSW)
- **LLM integration** (provider abstraction, function calling, token budgets)
- **Agent design** (ReAct loop, tool registry, context management)
- **Testing** (mocking at the right boundary, comprehensive coverage)

The architecture is **extensible by design**: new tools are added with a decorator, new providers are added by subclassing `LLMProvider`, new skills are added by creating a SKILL.md file. The schema is **production-ready** with proper constraints and indexes. The test suite is **comprehensive and well-organized**.

This is not a toy project. It is a **solid foundation for a production AI agent system**, built by someone who understands both the theory and the practice of building LLM-powered applications.

---

*Advocate report generated: 2026-09-30*
*Updated: 2026-10-01*
*Codebase version: 0.1.0*
*Tests: 136 passing*
