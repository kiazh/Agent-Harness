# AgentHarness Testing Audit — Brutal Critique

**Date:** 2026-10-01  
**Scope:** Entire test suite (330 tests across 7 files)  
**Verdict:** The test suite is a **smokescreen** — it gives the illusion of coverage while leaving critical paths untested.

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total tests | 330 |
| Pass rate | 100% (330/330) |
| Test files | 7 |
| conftest.py | **Missing** |
| Coverage measurement | **Not installed** |
| CI/CD integration | **None** |
| Avg. test runtime | ~32s |
| Tests using mocks | ~70% |
| Tests hitting real DB | ~5% (skipped if PG unavailable) |
| Duplicate test names | **1** (`test_load_directory` in `TestFileLoader`) |

**Bottom line:** This test suite is a **unit test masquerading as an integration test suite**. It tests that functions don't crash, not that they work correctly. The chaos tests are particularly egregious — they assert `response is not None` after injecting failures, which is the lowest possible bar.

---

## 1. Critical Failures

### 1.1 No `conftest.py` — Zero Test Infrastructure

There is no `conftest.py` anywhere in the project. This means:
- No shared fixtures (each test file redefines `mock_session`, `temp_dir`, `mock_db`)
- No custom markers (`@pytest.mark.slow`, `@pytest.mark.integration`)
- No test ordering or dependency management
- No global setup/teardown
- No async fixture support beyond `pytest-asyncio` auto mode

**Impact:** Tests are not composable. You can't run a subset with shared state. You can't add global fixtures without modifying every test file.

### 1.2 No Coverage Measurement

`pytest-cov` is not installed. There is no way to know what percentage of code is actually tested. The `pyproject.toml` lists `pytest` and `pytest-asyncio` as dev dependencies but omits `pytest-cov`.

**Impact:** You cannot answer the question "what is not tested?" — which is the entire point of a test audit.

### 1.3 No CI/CD Integration

No `.github/workflows/`, no `.gitlab-ci.yml`, no `tox.ini`, no `Makefile` with test targets. Tests are run manually.

**Impact:** Tests rot. They pass today and break tomorrow with no one noticing.

### 1.4 Global Singleton Pollution

The codebase uses global singletons (`registry`, `context_manager`, `session_manager`, `db`, `memory_store`, `skill_registry`). Tests patch these with `unittest.mock.patch()` but **never reset them**. This creates:

- **Order-dependent tests:** If test A registers a tool and test B doesn't expect it, test B fails.
- **Leaked state:** Tools registered in one test persist into the next.
- **False positives:** Tests pass because a previous test left the global state in a compatible configuration.

The `Container` class has a `reset()` method, but it's never used in tests.

### 1.5 Duplicate Test Name

In `tests/test_rag.py`, the `TestFileLoader` class has **two methods named `test_load_directory`**:

```python
def test_load_directory(self, loader, tmp_path):  # line 597 — expects ValueError
    ...

def test_load_directory(self, loader, tmp_path):  # line 629 — expects list[Document]
    ...
```

The second definition **silently shadows** the first. The first test (expecting `ValueError` when loading a directory) **never runs**. This is a silent test loss.

---

## 2. Test Quality Issues

### 2.1 Chaos Tests Are Useless

The chaos test file (`test_chaos.py`) is 869 lines of theater. Examples:

```python
async def test_agent_handles_malformed_llm_response(self, mock_session):
    """Agent should handle completely malformed LLM response."""
    provider.complete = AsyncMock(return_value=LLMResponse(
        content=None,  # type: ignore
        tool_calls=None,  # type: ignore
    ))
    ...
    try:
        response = await agent.run(...)
        assert response is not None
    except (TypeError, AttributeError):
        # Acceptable if it raises a specific error
        pass  # ← THIS IS NOT A TEST
```

**Problem:** The test passes if the agent crashes with `TypeError` or `AttributeError`. This is not "handling" a malformed response — it's "crashing predictably." A real chaos test would assert that the agent returns a meaningful error message or retries.

```python
async def test_agent_handles_db_connection_drop(self, mock_session):
    ...
    try:
        response = await agent.run(...)
        assert response is not None
    except Exception:
        pass  # ← ANY exception is acceptable
```

**Problem:** This test asserts nothing. It's a 30-line no-op.

### 2.2 Property-Based Tests Are Trivial

The property-based tests (`test_property_based.py`) use Hypothesis but test only the most basic invariants:

```python
@given(text=st.text(min_size=0, max_size=5000))
def test_token_count_non_negative(self, text):
    """Property: Token count is always non-negative."""
    count = get_token_count(text)
    assert count >= 0  # ← This is not a property, it's a type check
```

**Missing properties:**
- Token count is monotonic with respect to string length
- Token count of concatenated strings ≤ sum of individual counts + overhead
- Prompt assembly never exceeds budget by more than X%
- Tool schema inference always produces valid JSON Schema
- Context compression is idempotent
- Memory importance scores are always in [0, 1]
- Forgetting model strength decreases monotonically over time

### 2.3 Integration Tests Don't Integrate

The "integration" tests (`test_integration.py`) mock the database:

```python
async def test_session_crud_with_mock_db(self):
    mock_db = AsyncMock()
    mock_db.fetchrow = AsyncMock(return_value={...})
    with patch("ah.core.session.db", mock_db):
        session = await session_manager.create(title="Test Session")
        assert session.title == "Test Session"
```

**Problem:** This tests that `SessionManager` calls `db.fetchrow` and maps the result. It does NOT test:
- SQL query correctness
- Transaction handling
- Connection pool behavior
- Concurrent access
- Actual database constraints

The only real DB tests are in `TestRealDatabase` which are **skipped if PostgreSQL is not available** — meaning they never run in CI.

### 2.4 No Test Isolation

Tests share global state via module-level singletons. For example:

```python
# test_basic.py
def test_tool_registry():
    from ah.tools import builtins  # noqa: F401 — registers web_search, web_extract, search_files
    from ah.tools import file  # noqa: F401 — registers read_file, write_file, list_files
    from ah.tools import terminal  # noqa: F401 — registers terminal
    tools = registry.list_tools()
    assert "read_file" in tools
```

This test **depends on import order**. If another test file resets the registry, this test fails. There's no `autouse` fixture to reset state between tests.

---

## 3. Missing Test Coverage

### 3.1 Entire Modules With Zero Tests

| Module | Lines | Tests | Coverage |
|--------|-------|-------|----------|
| `ah/cli/__init__.py` | ~400 | **0** | 0% |
| `ah/cli/interactive.py` | ~200 | **0** | 0% |
| `ah/cli/visual.py` | ~150 | **0** | 0% |
| `ah/cli/animations.py` | ~300 | **0** | 0% |
| `ah/cli/theme.py` | ~200 | **0** | 0% |
| `ah/core/config.py` | ~150 | **0** | 0% |
| `ah/core/container.py` | ~150 | **2** | ~10% |
| `ah/tools/builtins.py` | ~150 | **0** | 0% |
| `ah/tools/memory.py` | ~150 | **0** | 0% |
| `ah/tools/rag.py` | ~150 | **0** | 0% |
| `ah/tools/terminal.py` | ~100 | **0** | 0% |
| `ah/db/connection.py` | ~100 | **0** | 0% |

**Total: ~2,000 lines of production code with zero test coverage.**

### 3.2 Critical Untested Functions

| Function | Risk | Why It Matters |
|----------|------|----------------|
| `_is_safe_url()` in `builtins.py` | **HIGH** | SSRF protection — prevents internal network access |
| `AsyncTokenBucket.acquire()` | **HIGH** | Rate limiting — prevents API abuse |
| `_call_llm_with_retry()` | **HIGH** | Retry logic — ensures reliability |
| `terminal()` | **HIGH** | Command execution — security critical |
| `_validate_workdir()` | **MEDIUM** | Path traversal protection |
| `Container.start()/stop()` | **MEDIUM** | Lifecycle management |
| `Config.load()/save()` | **MEDIUM** | Configuration persistence |
| `SkillParser.parse()` | **MEDIUM** | YAML parsing — crash on malformed input |
| `MemoryConsolidator.consolidate_session()` | **MEDIUM** | Core memory pipeline |
| `RAGPipeline.index_document()` | **MEDIUM** | Core RAG pipeline |
| `HybridSearch._bm25_search()` | **MEDIUM** | Search quality |
| `OpenAIEmbedder.embed()` | **MEDIUM** | Embedding quality |
| `ForgettingModel.current_strength()` | **LOW** | Memory decay |

### 3.3 No Security Tests

The terminal tool has an allowlist and dangerous character detection, but there are **zero tests** for:
- Command injection attempts (`;`, `|`, `&`, `$()`, backticks)
- Path traversal (`../../etc/passwd`)
- Workdir escape
- Timeout handling
- Unicode attacks

The `_is_safe_url()` function has **no tests** for:
- DNS rebinding
- Redirect following
- IPv6 addresses
- URL parsing edge cases

### 3.4 No Performance Tests

- No benchmarks for prompt assembly
- No load tests for concurrent agent runs
- No memory leak detection
- No query performance tests

### 3.5 No Edge Case Tests

- Empty strings, very long strings, unicode, null bytes
- Concurrent access to shared resources
- Database connection failures mid-transaction
- Malformed LLM responses (partially valid JSON, missing fields)
- Tool execution timeouts
- Context window overflow

---

## 4. Test Design Issues

### 4.1 Over-Mocking

~70% of tests use `AsyncMock` or `MagicMock`. While mocks are appropriate for unit tests, they create a false sense of security. The test suite tests **that the code calls the right functions**, not **that the functions produce the right results**.

Example:
```python
async def test_agent_stores_context_chunks(self, mock_session):
    ...
    mock_cm.add_chunk = AsyncMock()
    ...
    await agent.run(mock_session.id, "test message", verbose=False)
    assert mock_cm.add_chunk.call_count >= 2  # ← Tests that add_chunk was called, not that it worked
```

### 4.2 No Negative Tests

Almost no tests verify that invalid inputs are rejected. For example:
- No test for `Session` with invalid `context_budget` (negative, zero, huge)
- No test for `LLMResponse` with missing required fields
- No test for `ToolDefinition` with invalid JSON Schema
- No test for `MemoryEntry` with invalid category (only one test exists)

### 4.3 No Boundary Tests

- Token budget exactly at limit
- Context chunks exactly at limit
- Tool call with exactly max arguments
- Session with exactly max title length

### 4.4 No Concurrency Tests

The `test_concurrent_agent_runs` test uses `asyncio.gather()` but all agents share the same `mock_session` and the same `provider` mock. This doesn't test:
- Race conditions in global state
- Database connection pool exhaustion
- Tool registry concurrent access

### 4.5 No Regression Tests

There are no tests that document and verify fixes for past bugs. When a bug is fixed, there's no test to prevent it from regressing.

---

## 5. Specific Test File Critique

### `test_basic.py` (7 tests)
- **Smoke tests only.** Verifies imports and basic dataclass creation.
- `test_database_connection` and `test_session_crud` are skipped if PostgreSQL is unavailable — meaning they never run in most environments.
- **Missing:** Test for `__version__` format, test for all dataclass defaults.

### `test_chaos.py` (25 tests)
- **The worst offender.** 869 lines of tests that assert `response is not None` or `pass`.
- Many tests have `try/except Exception: pass` which makes them vacuous.
- **Missing:** Assertions on error message content, recovery behavior, state consistency after failure.

### `test_comprehensive.py` (~100 tests)
- **Decent unit tests** for `PromptAssembler`, `ToolRegistry`, `SkillParser`, `SkillRegistry`.
- **Missing:** Tests for `Config`, `Container`, `Database`, `LLMProvider`.
- **Duplicate:** Some tests overlap with `test_basic.py`.

### `test_integration.py` (~30 tests)
- **Misleading name.** These are unit tests with mocked databases.
- The `TestRealDatabase` class has 3 tests that are skipped without PostgreSQL.
- **Missing:** Real integration tests with actual database, actual LLM provider (with VCR cassettes), actual file system.

### `test_memory.py` (~50 tests)
- **Good coverage** of `MemoryEntry`, `ImportanceScorer`, `ForgettingModel`.
- **Missing:** Tests for `MemoryStore` with real database, `MemoryRetriever` hybrid search, `MemoryConsolidator` deduplication.
- **Weak:** `test_retrieve_updates_access` asserts `isinstance(mock_store.update_access, AsyncMock)` — this is a tautology.

### `test_property_based.py` (~30 tests)
- **Good use of Hypothesis** but properties are trivial.
- **Missing:** Meaningful invariants, shrinking examples, stateful testing.
- **Bug:** `test_infer_schema_valid_json_schema` has a comment acknowledging that `int` type mapping is broken: `assert schema["properties"]["b"]["type"] in ("integer", "string")` — this should be a bug fix, not a test.

### `test_rag.py` (~50 tests)
- **Decent coverage** of `Embedder`, `Chunker`, `Reranker`, `FileLoader`.
- **Bug:** Duplicate `test_load_directory` method — the first one (expecting `ValueError`) is silently shadowed.
- **Missing:** Tests for `RAGPipeline` end-to-end, `HybridSearch` with real data, `OpenAIEmbedder` with mocked HTTP.

---

## 6. Recommendations

### Immediate (P0)

1. **Add `conftest.py`** with:
   - `autouse` fixture to reset all global singletons between tests
   - Shared fixtures for `mock_session`, `temp_dir`, `mock_db`
   - Custom markers for `integration`, `slow`, `security`

2. **Install `pytest-cov`** and set a coverage threshold (start at 30%, aim for 70%).

3. **Fix the duplicate `test_load_directory`** — rename one to `test_load_directory_raises`.

4. **Add CI/CD** (GitHub Actions) with:
   - PostgreSQL service container
   - `pytest --cov` with coverage reporting
   - Linting (`ruff`)

5. **Rewrite chaos tests** to assert meaningful behavior:
   - After a tool failure, verify the agent retries or reports the error
   - After an LLM timeout, verify the agent returns an error message
   - After a DB failure, verify the agent doesn't corrupt state

### Short-term (P1)

6. **Add tests for all CLI commands** using `typer.testing.CliRunner`.

7. **Add security tests** for terminal tool, URL validation, path traversal.

8. **Add real integration tests** with PostgreSQL (not mocked).

9. **Add tests for `Config`, `Container`, `Database`, `LLMProvider`.**

10. **Add negative tests** for all public functions.

### Medium-term (P2)

11. **Add performance benchmarks** for prompt assembly, tool execution, memory retrieval.

12. **Add concurrency tests** with real parallel execution.

13. **Add regression tests** for all discovered bugs.

14. **Add property-based tests** with meaningful invariants.

15. **Add mutation testing** to verify test quality.

---

## 7. Conclusion

The AgentHarness test suite is **a house of cards**. It has 330 tests that all pass, but:

- **~2,000 lines of production code have zero coverage** (CLI, config, container, tools, DB).
- **Critical security functions are untested** (SSRF protection, command injection).
- **Chaos tests are vacuous** — they assert nothing.
- **Integration tests don't integrate** — they mock everything.
- **Global state leaks between tests** — order-dependent failures are inevitable.
- **One test is silently shadowed** by a duplicate method name.

**The test suite provides false confidence.** It will not catch regressions in the CLI, config, security, or database layers. It will not catch bugs in error handling, retry logic, or concurrent access. It will not catch bugs in the RAG pipeline, memory system, or tool execution.

**Priority: Add `conftest.py`, install `pytest-cov`, add CI/CD, and rewrite the chaos tests. Then add tests for the untested modules.**
