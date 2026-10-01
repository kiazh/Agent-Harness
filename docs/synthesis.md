# AgentHarness: Unified Critique Synthesis & Architecture Plan

**Date:** 2026-09-30
**Sources:** 10 critique documents from parallel subagents
**Verdict:** The codebase is a solid Phase 1+2 foundation with serious security, architecture, and UX issues that must be fixed before it's credible.

---

## Executive Summary

AgentHarness has a genuinely good core: clean ReAct loop, async PostgreSQL with pgvector, MessagePack context storage, decorator-based tool registry, and 136 passing tests. But the critiques reveal:

- **4 CRITICAL security vulnerabilities** (command injection, path traversal, SSRF, no auth)
- **1 God Object** (ReActAgent.run() is 130 lines doing everything)
- **7 global singletons** making the code untestable
- **Duplicate tool registrations** with different implementations
- **Zero streaming** — users stare at blank screen for 10-30s
- **Token counting off by 2-4x** (len//4 vs tiktoken)
- **No CI/CD, no error handling, no caching, no rate limiting**
- **8 unused database tables** (speculative design)
- **CLI scores 0/10** vs Claude Code/Hermes

---

## Priority Matrix

### P0 — Must Fix Before Anything Else (Security & Correctness)

| # | Issue | Severity | Fix | Effort |
|---|---|---|---|---|
| 1 | Command injection in `terminal()` | CRITICAL | Remove shell=True, add allowlist | 30 min |
| 2 | Path traversal in file tools | CRITICAL | Add base directory restriction | 30 min |
| 3 | SSRF in `web_extract` | CRITICAL | Add URL validation | 15 min |
| 4 | Duplicate tool registrations | HIGH | Remove from builtins.py | 15 min |
| 5 | No error handling in ReAct loop | HIGH | Add try-except + retry | 30 min |
| 6 | Resource leaks (httpx clients) | HIGH | Add close() calls | 15 min |
| 7 | Missing asyncpg import | HIGH | Add import | 5 min |

### P1 — Should Fix for Credibility (Robustness & UX)

| # | Issue | Severity | Fix | Effort |
|---|---|---|---|---|
| 8 | No streaming in CLI | HIGH | Add streaming | 2 hrs |
| 9 | Token counting inaccurate | HIGH | Add tiktoken | 30 min |
| 10 | No CI/CD | HIGH | Add GitHub Actions | 20 min |
| 11 | Blocking I/O in async | MEDIUM | Use asyncio.to_thread | 1 hr |
| 12 | No transaction management | MEDIUM | Add transaction ctx | 30 min |
| 13 | Fragile YAML parser | MEDIUM | Use PyYAML | 15 min |
| 14 | No rate limiting | MEDIUM | Add token bucket | 30 min |
| 15 | No input validation | MEDIUM | Add validation | 1 hr |

### P2 — Nice to Have (Scalability & Polish)

| # | Issue | Severity | Fix | Effort |
|---|---|---|---|---|
| 16 | No caching layer | MEDIUM | Add LRU cache | 1 hr |
| 17 | No horizontal scaling | MEDIUM | Design for service | 4 hrs |
| 18 | No audit logging | MEDIUM | Add logging | 1 hr |
| 19 | 8 unused tables | LOW | Remove or implement | 30 min |
| 20 | No observability | LOW | Add metrics | 2 hrs |

---

## Architecture Redesign

### Current Problems

1. **God Object:** `ReActAgent.run()` does everything
2. **Global Singletons:** `db`, `session_manager`, `context_manager`, `registry`, `skill_registry`
3. **Circular Dependencies:** `core` ↔ `tools`
4. **Leaky Abstractions:** `asyncpg.Record` types leak everywhere
5. **No Domain Model:** `Session.state` is `dict`, `ContextChunk.payload` is `dict[str, Any]`

### Proposed Architecture

```
ah/
├── core/
│   ├── agent.py          # ReAct loop (orchestrator only)
│   ├── context.py        # ContextChunk + ContextManager
│   ├── provider.py       # LLMProvider + implementations
│   ├── session.py        # Session + SessionManager
│   └── assembler.py      # PromptAssembler (extracted)
├── db/
│   ├── connection.py     # Database pool
│   └── schema.sql        # Schema
├── tools/
│   ├── base.py           # ToolRegistry
│   ├── file.py           # File tools
│   ├── terminal.py       # Terminal tool
│   └── web.py            # Web tools
├── skills/
│   └── registry.py       # Skill system
├── memory/
│   └── manager.py        # Long-term memory
├── rag/
│   ├── embed.py          # Embedding client
│   └── search.py         # RAG pipeline
├── cli/
│   ├── app.py            # Typer CLI
│   └── interactive.py    # Interactive REPL
└── tui/
    └── app.py            # Textual TUI
```

### Key Design Decisions

1. **Dependency Injection:** Pass dependencies to ReActAgent, don't use globals
2. **Domain Models:** Use dataclasses with validation, not raw dicts
3. **Error Handling:** Wrap all external calls in try-except with retry
4. **Streaming:** Use async generators for token streaming
5. **Security:** Sandbox all tool execution, validate all inputs
6. **Testing:** Mock at the boundary (DB, HTTP), not internally

---

## Implementation Plan

### Phase 1: Security Hardening (P0)
1. Fix command injection in terminal tool
2. Fix path traversal in file tools
3. Fix SSRF in web_extract
4. Remove duplicate tool registrations
5. Add error handling to ReAct loop
6. Fix resource leaks
7. Add missing imports

### Phase 2: Robustness (P1)
8. Add tiktoken for accurate token counting
9. Add CI/CD workflow
10. Fix blocking I/O
11. Add transaction management
12. Fix YAML parser
13. Add rate limiting
14. Add input validation

### Phase 3: UX (P1)
15. Add streaming to CLI
16. Build interactive REPL
17. Add progress indicators
18. Add token counters

### Phase 4: Scalability (P2)
19. Add caching layer
20. Design for horizontal scaling
21. Add audit logging
22. Clean up unused tables
23. Add observability

---

## Files to Create/Modify

| File | Action | Priority |
|---|---|---|
| `ah/tools/terminal.py` | Rewrite with security | P0 |
| `ah/tools/file.py` | Add path validation | P0 |
| `ah/tools/builtins.py` | Remove duplicates | P0 |
| `ah/tools/web.py` | Add URL validation | P0 |
| `ah/core/agent.py` | Add error handling | P0 |
| `ah/core/provider.py` | Add close() calls | P0 |
| `ah/core/context.py` | Add tiktoken | P1 |
| `ah/cli/app.py` | Add streaming | P1 |
| `ah/cli/interactive.py` | New REPL | P1 |
| `.github/workflows/ci.yml` | New CI | P1 |
| `ah/skills/registry.py` | Use PyYAML | P1 |
| `ah/core/assembler.py` | Extract from context | P2 |

---

## Success Criteria

- [ ] All P0 issues fixed
- [ ] All P1 issues fixed
- [ ] Test suite passes with 90%+ coverage
- [ ] CLI has streaming + token counters
- [ ] CI/CD runs on every push
- [ ] No security vulnerabilities
- [ ] Code review passes

---

*This document synthesizes findings from 10 parallel critique subagents. Each critique is available in `docs/critique-*.md`.*
