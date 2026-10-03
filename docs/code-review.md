# AgentHarness Code Review Report

**Date:** 2026-10-02  
**Scope:** Full codebase review of 20 core files  
**Reviewer:** Automated Code Review

---

## Executive Summary

AgentHarness is a well-structured multi-agent AI orchestration framework with strong security foundations. The codebase demonstrates good practices in SQL injection prevention, path traversal protection, and command injection prevention. However, several issues were found including a hardcoded database password, logic inconsistencies between streaming/non-streaming paths, significant code duplication, and missing test coverage for CLI and visual components.

**Overall Grade:** B+ (Good, with notable issues to address)

---

## 1. Security Review

### 1.1 Hardcoded Secrets — CRITICAL

| File | Line | Issue |
|------|------|-------|
| `ah/db/connection.py` | 21 | Hardcoded database password `2017` in default DSN |

```python
self.dsn = dsn or config.get("database_url") or "postgresql://postgres:***@2017@127.0.0.1:5432/agentharness"
```

**Risk:** The password `2017` is visible in source code. Even though it's a local development default, it violates secret management practices and could leak through logs, error messages, or source control history.

**Recommendation:** Remove the hardcoded password. Use an environment variable or fail with a clear error message if no DSN is configured.

### 1.2 SSRF Protection — GOOD with Caveats

| File | Function | Assessment |
|------|----------|------------|
| `ah/tools/builtins.py` | `_is_safe_url()` | Good — rejects private IPs, localhost, non-HTTP protocols |
| `ah/tools/builtins.py` | `web_extract()` | Good — pins resolved IP to prevent DNS rebinding |
| `ah/cli/__init__.py` | `learn()` | Good — validates URL before fetching |

**Caveat:** The `_is_safe_url()` function has a TOCTOU (Time-of-Check-to-Time-of-Use) race condition. The DNS resolution in `_is_safe_url()` and the actual HTTP request in `web_extract()` happen at different times. The `web_extract()` function mitigates this by pinning the resolved IP, but `_is_safe_url()` alone is not sufficient for security-critical contexts.

### 1.3 Path Traversal Protection — GOOD

| File | Function | Assessment |
|------|----------|------------|
| `ah/tools/file.py` | `_resolve_path()` | Good — resolves and validates against base directory |
| `ah/tools/builtins.py` | `_resolve_path()` | Good — delegates to same base directory |
| `ah/cli/__init__.py` | `learn()` | Good — checks for `..` in path |
| `ah/cli/__init__.py` | `export()` | Good — uses `_resolve_path()` |

### 1.4 Command Injection Prevention — GOOD

| File | Function | Assessment |
|------|----------|------------|
| `ah/tools/terminal.py` | `terminal()` | Good — uses `shlex.split()`, `shell=False`, allowlist, dangerous character rejection |

The terminal tool has a robust allowlist approach:
- Only 16 commands allowed (git, ls, cat, grep, find, pytest, echo, pwd, cd, mkdir, touch, head, tail, wc, diff)
- Dangerous characters rejected: `;|&$()`<>\n`
- `shell=False` prevents shell injection
- Workdir validated against allowed prefixes

### 1.5 SQL Injection Prevention — GOOD

All database queries use parameterized queries (`$1`, `$2`, etc.) via asyncpg. No string interpolation of user input was found in SQL queries. The `ah/memory/retriever.py` `_keyword_search()` method correctly builds ILIKE conditions with parameterized queries.

### 1.6 eval/exec Usage — NONE FOUND

No `eval()` or `exec()` calls were found in the codebase.

### 1.7 Secret Redaction — GOOD

The `_sanitize_value()` function in `ah/core/provider.py` redacts:
- API keys (sk-*, sk-ant-*, sk-or-*)
- Bearer tokens
- Passwords
- Database connection strings
- AWS access keys
- Private keys

### 1.8 Prompt Injection Detection — GOOD

The `SkillParser._validate_content()` in `ah/skills/registry.py` detects common prompt injection patterns:
- "ignore previous instructions"
- "disregard prior instructions"
- "forget previous instructions"
- System prompt overrides
- Instruction tags ([INST], <|im_start|>, etc.)

---

## 2. Logic Errors

### 2.1 Inconsistent Token Budget Handling — MEDIUM

| File | Line | Issue |
|------|------|-------|
| `ah/core/agent.py` | 465 | `run()` uses `MAX_TOKEN_BUDGET` directly |
| `ah/core/agent.py` | 598 | `run_stream()` uses `min(session.context_budget, MAX_TOKEN_BUDGET)` |

The `run()` method uses a hardcoded `MAX_TOKEN_BUDGET` (50,000) while `run_stream()` respects the session's `context_budget`. This means the same agent behaves differently depending on which method is called.

**Recommendation:** Use `min(session.context_budget, MAX_TOKEN_BUDGET)` consistently in both methods.

### 2.2 TTLCache TTL Mismatch — LOW

| File | Line | Issue |
|------|------|-------|
| `ah/core/session.py` | 22 | Docstring says "5-second LRU cache" |
| `ah/core/session.py` | 26 | Actual TTL is 60 seconds |

```python
self._cache: TTLCache = TTLCache(maxsize=128, ttl=60)  # Docstring says 5 seconds
```

### 2.3 Fragile String Parsing for Delete Results — MEDIUM

| File | Line | Issue |
|------|------|-------|
| `ah/core/session.py` | 173 | `int(result.split()[-1]) > 0` |
| `ah/memory/store.py` | 190 | `result != "DELETE 0"` |
| `ah/memory/store.py` | 258 | `int(result.split()[-1])` |
| `ah/core/context.py` | 253 | `int(result.split()[-1])` |

These rely on asyncpg's command tag format ("DELETE N", "UPDATE N", etc.). While this is asyncpg's documented behavior, it's fragile and could break if the library changes its format.

### 2.4 Missing UUID Validation in CLI Commands — MEDIUM

| File | Line | Issue |
|------|------|-------|
| `ah/cli/__init__.py` | 418 | `uuid.UUID(session_id)` without try/except in `export` |
| `ah/cli/__init__.py` | 556 | Same in `context` command |
| `ah/cli/__init__.py` | 598 | Same in `compress` command |

These commands use `uuid.UUID()` directly instead of the `_parse_uuid()` helper function, which means invalid UUIDs will raise an unhandled exception instead of showing a user-friendly error.

### 2.5 Redundant Eviction Logic — LOW

| File | Line | Issue |
|------|------|-------|
| `ah/core/context.py` | 291-332 | Complex conditional logic with redundant checks |

The eviction loop has three separate break conditions that could be simplified:
```python
if (max_tokens is not None and tokens_freed >= tokens_to_evict) and \
   (max_chunks is not None and chunks_freed >= chunks_to_evict):
    break
if max_tokens is not None and tokens_freed >= tokens_to_evict and max_chunks is None:
    break
if max_chunks is not None and chunks_freed >= chunks_to_evict and max_tokens is None:
    break
```

### 2.6 Inconsistent Exception Types — LOW

| File | Line | Issue |
|------|------|-------|
| `ah/core/provider.py` | 142 | `ValueError` instead of `ValidationError` for missing content |

The `_validate_messages()` function raises `ValidationError` for missing "role" but `ValueError` for missing "content". This inconsistency makes error handling harder for callers.

### 2.7 Circular Import Risk — LOW

| File | Line | Issue |
|------|------|-------|
| `ah/memory/retriever.py` | 193 | `from ah.db.connection import db` inside method |

This deferred import suggests a circular dependency. While it works, it's a code smell that could cause issues if the module structure changes.

---

## 3. Code Quality

### 3.1 Code Duplication — HIGH

#### 3.1.1 Agent Tool Execution Duplication

The tool execution logic in `ah/core/agent.py` is duplicated between `_execute_tool_calls()` (lines 259-403) and the inline version in `run_stream()` (lines 700-830). Both contain:
- Missing tool name handling
- JSON argument parsing with error handling
- Tool execution with timing
- Context storage
- Message updates

**Impact:** ~130 lines of duplicated logic. Bug fixes must be applied in two places.

#### 3.1.2 Provider Duplication

`OpenRouterProvider` and `OllamaProvider` in `ah/core/provider.py` share significant structural similarity:
- Input validation
- Rate limiting
- Audit logging
- Metrics recording
- Error handling

**Recommendation:** Extract common logic into a base class or mixin.

### 3.2 Large Files — MEDIUM

| File | Lines | Issue |
|------|-------|-------|
| `ah/cli/visual.py` | ~2000 | Very large, could be split |
| `ah/cli/animations.py` | ~1964 | Very large, could be split |
| `ah/cli/interactive_win.py` | ~1568 | Very large, could be split |
| `ah/cli/__init__.py` | 1441 | CLI commands could be in separate modules |
| `ah/core/agent.py` | 847 | Moderate, acceptable |

### 3.3 Dead Code — LOW

| File | Function | Issue |
|------|----------|-------|
| `ah/core/provider.py` | `fetchval_cached()` | Just calls `fetchval()` — no actual caching |
| `ah/core/provider.py` | `execute_prepared()` | Just calls `execute()` — no actual prepared statement benefit |
| `ah/core/provider.py` | `fetch_prepared()` | Just calls `fetch()` — no actual prepared statement benefit |

These methods claim to use prepared statement caching but asyncpg handles this automatically. They add complexity without benefit.

### 3.4 Naming Inconsistencies — LOW

| Issue | Location |
|-------|----------|
| `usage_count` vs `use_count` | `ah/skills/registry.py` — Skill dataclass has both fields |
| `DENSE_WEIGHT`/`SPARSE_WEIGHT` vs actual weights | `ah/memory/retriever.py` — weights are 0.7/0.3 but not configurable |

### 3.5 Missing Type Hints — LOW

Some functions lack return type annotations:
- `ah/tools/terminal.py`: `terminal()` -> `str` (has it)
- `ah/tools/builtins.py`: `web_search()` -> `str` (has it)
- `ah/memory/store.py`: `_row_to_entry()` -> `MemoryEntry` (has it)

Overall type hint coverage is good.

---

## 4. Tests

### 4.1 Test Coverage Summary

| Test File | Lines | Coverage Area |
|-----------|-------|---------------|
| `test_basic.py` | 126 | Basic functionality |
| `test_chaos.py` | 869 | Chaos/fuzz testing |
| `test_comprehensive.py` | 1942 | Comprehensive integration |
| `test_compression.py` | 388 | Context compression |
| `test_integration.py` | 1021 | Integration tests |
| `test_memory.py` | 854 | Memory system |
| `test_memory_approval.py` | 485 | Memory approval workflow |
| `test_property_based.py` | 625 | Property-based testing |
| `test_rag.py` | 689 | RAG pipeline |
| `test_security.py` | 380 | Security tests |
| **Total** | **7,251** | |

### 4.2 Security Tests — GOOD

33 security test functions covering:
- SSRF protection (6 tests)
- Path traversal (4 tests)
- Skill content validation (6 tests)
- Secret redaction (7 tests)
- SQL injection prevention (2 tests)
- Database configuration (3 tests)

### 4.3 Missing Test Coverage — MEDIUM

| Area | Issue |
|------|-------|
| CLI commands | No tests for `ah/cli/__init__.py` commands |
| Animations | No tests for `ah/cli/animations.py` |
| Visual system | No tests for `ah/cli/visual.py` |
| Interactive REPL | No tests for `ah/cli/interactive_win.py` |
| Provider streaming | No tests for `stream_complete()` methods |
| Session cache | No tests for TTLCache behavior |
| Context eviction | No tests for `evict_old_chunks()` |

### 4.4 Brittle Tests — LOW

Some tests may be brittle due to:
- Dependency on external services (OpenRouter, Ollama, Cohere)
- Time-dependent assertions
- Database state dependencies

---

## 5. Documentation

### 5.1 Research Documentation — GOOD

16 research documents covering:
- Deployment, context, memory, scheduling, self-improvement
- LangGraph integration, multi-agent, observability, RAG, TUI
- Testing, performance, resource leaks, security

### 5.2 Missing Documentation — MEDIUM

| Document | Status |
|----------|--------|
| API reference | Missing |
| Architecture diagram | Missing |
| Contributing guide | Missing |
| CLI command reference | Missing (only in `--help`) |
| Configuration reference | Missing (only in code) |

### 5.3 Code Comments — GOOD

The codebase generally has good docstrings and inline comments:
- Module-level docstrings explain purpose
- Function docstrings describe parameters and returns
- Complex logic has explanatory comments
- Security-critical code has warnings

### 5.4 README — Not Reviewed

The README.md was not part of the review scope.

---

## 6. Summary of Findings

### Critical (1)
1. Hardcoded database password in `ah/db/connection.py`

### Medium (7)
1. Inconsistent token budget handling between `run()` and `run_stream()`
2. Fragile string parsing for database command results
3. Missing UUID validation in CLI commands
4. Code duplication in agent tool execution
5. Large files that could be split
6. Missing test coverage for CLI, animations, visual system
7. Missing API and architecture documentation

### Low (6)
1. TTLCache TTL mismatch (docstring vs code)
2. Redundant eviction logic
3. Inconsistent exception types
4. Circular import risk
5. Dead code in provider methods
6. Naming inconsistencies

---

## 7. Recommendations

### Immediate Actions
1. **Remove hardcoded password** from `ah/db/connection.py` — use environment variable or fail fast
2. **Fix UUID validation** in `export`, `context`, and `compress` commands — use `_parse_uuid()`
3. **Unify token budget logic** in `ah/core/agent.py` — use `min(session.context_budget, MAX_TOKEN_BUDGET)` in both methods

### Short-term
4. **Extract common tool execution logic** in `ah/core/agent.py` to reduce duplication
5. **Add tests** for CLI commands, animations, and visual system
6. **Create API documentation** using pydoc or similar

### Long-term
7. **Split large files** into smaller, focused modules
8. **Extract provider common logic** into a base class
9. **Add architecture diagram** to documentation
10. **Implement proper secret management** (e.g., integration with vault)

---

## 8. File-by-File Assessment

| File | Security | Logic | Quality | Tests | Docs | Grade |
|----------|----------|-------|---------|-------|------|-------|
| `ah/cli/__init__.py` | B+ | B- | B | D | C+ | B- |
| `ah/cli/interactive_win.py` | A- | B+ | B | D | B | B |
| `ah/cli/animations.py` | A | B+ | B | D | B+ | B+ |
| `ah/cli/visual.py` | A | B+ | B | D | B+ | B+ |
| `ah/core/agent.py` | A- | B- | B- | B | B+ | B |
| `ah/core/provider.py` | A- | B | B- | B | B+ | B+ |
| `ah/core/session.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/core/context.py` | A | B | B+ | B | B+ | B+ |
| `ah/core/config.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/db/connection.py` | C | B+ | B+ | B | B+ | B |
| `ah/tools/terminal.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/tools/file.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/tools/builtins.py` | A- | B+ | B+ | B | B+ | B+ |
| `ah/memory/store.py` | A | B | B+ | B | B+ | B+ |
| `ah/memory/retriever.py` | A | B | B+ | B | B+ | B+ |
| `ah/memory/consolidator.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/rag/pipeline.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/rag/embedder.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/rag/reranker.py` | A | B+ | B+ | B | B+ | B+ |
| `ah/skills/registry.py` | A- | B+ | B+ | B | B+ | B+ |

---

*End of Report*
