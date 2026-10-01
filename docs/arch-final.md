# AgentHarness: Final Architecture Synthesis

**Date:** 2026-09-30
**Sources:** 10 architecture proposals (purist, pragmatic, minimalist, DDD, functional, testability, performance, security, microservices, DX)
**Verdict:** Middle ground — pragmatic incremental improvements + domain models + testability + performance + security

> **Implementation Status: Phases 1-4 complete.** All P0 and P1 items have been implemented. The codebase now has: domain models (`models.py`), `PromptAssembler` extracted (`assembler.py`), DI container (`container.py`), 2-table schema, tiktoken, PyYAML, rate limiting, retry logic, streaming, audit logging, input validation, and security hardening. 136 tests pass.

---

## Executive Summary

The 10 proposals span a spectrum from "complete rewrite" (purist, DDD, microservices) to "collapse to 5 files" (minimalist). The truth is in the middle. The current architecture is ~80% correct — the ReAct loop works, tests pass, and the dependency graph is shallow. But there are real issues that can be fixed incrementally without a rewrite.

**The final architecture takes:**
- From **pragmatic**: 5 incremental improvements, keep singletons but make them testable
- From **purist**: Break up the God Object, add domain models, explicit dependencies
- From **testability**: DI container for tests, protocol-based interfaces
- From **performance**: Caching layer, batching, query optimization
- From **security**: Audit logging, rate limiting, input validation
- From **DX**: Structured logging, consistent patterns, debugging support
- From **minimalist**: Remove unused tables, kill unused abstractions

**The final architecture rejects:**
- From **microservices**: Splitting into services (over-engineered for a CLI tool)
- From **functional**: IO monad, algebraic data types (too academic for Python)
- From **DDD**: Full aggregate roots, domain events (over-engineered for current scale)

---

## Final Architecture

### Module Structure

```
ah/
├── core/
│   ├── agent.py          # ReAct loop (orchestrator only, ~80 lines)
│   ├── assembler.py      # PromptAssembler (extracted from context.py)
│   ├── context.py        # ContextChunk + ContextManager (data access only)
│   ├── provider.py       # LLMProvider + implementations
│   ├── session.py        # Session + SessionManager
│   └── models.py         # Domain models (Session, ContextChunk, ToolCall, etc.)
├── db/
│   ├── connection.py     # Database pool (with reset() for tests)
│   └── schema.sql        # Schema (2 tables: sessions, context_chunks)
├── tools/
│   ├── base.py           # ToolRegistry (with reset() for tests)
│   ├── file.py           # File tools
│   ├── terminal.py       # Terminal tool
│   └── web.py            # Web tools (moved from builtins.py)
├── skills/
│   └── registry.py       # Skill system (with reset() for tests)
├── memory/
│   └── manager.py        # Long-term memory (new)
├── rag/
│   ├── embed.py          # Embedding client (new)
│   └── search.py         # RAG pipeline (new)
├── cli/
│   ├── app.py            # Typer CLI
│   └── interactive.py    # Interactive REPL (new)
└── tui/
    └── app.py            # Textual TUI (future)
```

### Key Design Decisions

1. **Keep singletons, add reset()** — Singletons are correct for a CLI tool. Add `reset()` class methods for testing.
2. **Extract PromptAssembler** — Move from `context.py` to `assembler.py` (separation of concerns).
3. **Add domain models** — Replace `dict[str, Any]` with typed dataclasses in `models.py`.
4. **Add DI container for tests** — `Container.production()` / `Container.testing()` for testability.
5. **Add caching layer** — LRU cache for sessions, tool definitions (5-second TTL).
6. **Add audit logging** — Structured logging of all security-relevant events.
7. **Add rate limiting** — Token bucket for LLM calls (max 10/minute).
8. **Deduplicate run()/run_stream()** — Shared `_run_loop()` method.
9. **Remove unused tables** — Keep only `sessions` and `context_chunks`.
10. **Add structured logging** — `logging.getLogger(__name__)` throughout.

### Dependency Graph

```
cli.py → agent.py → assembler.py → context.py → connection.py
                  → provider.py
                  → session.py → connection.py
                  → tools/base.py → tools/file.py, tools/terminal.py, tools/web.py
                  → models.py (new)
```

**No circular dependencies.** `tools/base.py` no longer imports from `core/provider.py`. `ToolDefinition` moves to `models.py`.

---

## Implementation Plan

### Phase 1: Domain Models + Extraction (P0)
1. Create `ah/core/models.py` with typed dataclasses
2. Extract `PromptAssembler` to `ah/core/assembler.py`
3. Move `ToolDefinition` to `models.py`
4. Update all imports

### Phase 2: Testability (P0)
5. Add `reset()` to all singleton managers
6. Create `ah/core/container.py` with DI container
7. Update tests to use DI container

### Phase 3: Performance (P1)
8. Add LRU cache for sessions and tool definitions
9. Add batching for context chunk inserts
10. Optimize queries (column projection, composite indexes)

### Phase 4: Security (P1)
11. Add audit logging
12. Add rate limiting
13. Add input validation at boundaries

### Phase 5: Cleanup (P2)
14. Remove unused database tables
15. Kill unused abstractions (embed(), sqlalchemy, alembic, pydantic)
16. Add structured logging throughout

---

## Files to Create/Modify

| File | Action | Priority |
|---|---|---|
| `ah/core/models.py` | New — domain models | P0 |
| `ah/core/assembler.py` | New — extract from context.py | P0 |
| `ah/core/container.py` | New — DI container | P0 |
| `ah/core/context.py` | Modify — remove PromptAssembler | P0 |
| `ah/core/agent.py` | Modify — use models, deduplicate run() | P0 |
| `ah/core/provider.py` | Modify — remove ToolDefinition | P0 |
| `ah/tools/base.py` | Modify — remove ToolDefinition import | P0 |
| `ah/db/connection.py` | Modify — add reset() | P0 |
| `ah/core/session.py` | Modify — add reset() | P0 |
| `ah/tools/base.py` | Modify — add reset() | P0 |
| `ah/skills/registry.py` | Modify — add reset() | P0 |
| `ah/db/schema.sql` | Modify — remove unused tables | P2 |
| `pyproject.toml` | Modify — remove unused deps | P2 |

---

## Success Criteria

- [ ] All P0 issues fixed
- [ ] All P1 issues fixed
- [ ] Test suite passes with 90%+ coverage
- [ ] No circular dependencies
- [ ] No God Object (agent.py < 100 lines)
- [ ] All singletons have reset()
- [ ] DI container works for tests
- [ ] Caching layer reduces DB queries by 50%
- [ ] Audit logging captures all security events
- [ ] Rate limiting prevents API abuse

---

*This document synthesizes findings from 10 parallel architecture proposal subagents. The original proposal files have been removed; this document preserves the key decisions.*
