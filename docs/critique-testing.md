# Testing Strategy Critique — AgentHarness

**Verdict: The current test suite provides false confidence. 136 tests pass, yet the system would fail in production in at least a dozen ways that no test would catch.**

---

## 1. The Illusion of Coverage

The test suite reports **136 passed, 2 skipped** in 1.33 seconds. This looks healthy. It isn't.

- **Zero integration tests against a real database.** The two database tests (`test_database_connection`, `test_session_crud`) are `pytest.skip`-ped when PostgreSQL isn't available. In CI without a database, they silently pass. The "mocked" database tests replace `db` with `AsyncMock`, which means they test that the code *calls* `db.fetchrow`, not that the SQL is correct, the msgpack serialization round-trips, or the schema matches the queries.
- **Zero tests for the actual ReAct loop with real tools.** The agent tests mock the provider and mock `context_manager.add_chunk`. They verify the loop *calls* the provider twice when tool calls are present, but never verify that a tool actually executes, that its result is stored in context, or that the result is fed back to the LLM correctly.
- **Zero tests for `web_search` or `web_extract`.** These tools make external HTTP calls. They have no tests at all — not even mocked ones. If the SearXNG URL is wrong, the DuckDuckGo HTML parsing regex breaks, or Jina Reader rate-limits, no test will know.
- **Zero tests for `search_files` with `file_glob`.** The parameter exists in the signature but is never exercised.
- **Zero tests for `terminal` with `workdir`.** The parameter exists but is never exercised.

---

## 2. What's Tested vs. What Matters

### 2.1 Tested Superficially (Happy Path Only)

| Area | What's Tested | What's Missing |
|------|--------------|----------------|
| `PromptAssembler` | String inclusion (`"System" in prompt`) | Actual token budget enforcement, truncation behavior, chunk ordering |
| `ToolRegistry` | Register, list, execute sync/async | Schema inference for `Optional[X]`, `Union`, `list[str]`; duplicate names; execution with wrong types |
| `SkillParser` | Basic YAML frontmatter | Malformed YAML, missing fields, nested YAML, multi-line strings |
| `SkillRegistry` | Load from directory, match triggers | Duplicate skill names, disabled skills, `usage_count` tracking, persistence to DB |
| `ReActAgent` | Mocked provider returns canned responses | Real tool execution in the loop, malformed tool JSON, tool timeout, empty LLM response |
| `OpenRouterProvider` | Mocked HTTP 200 response | Rate limiting (429), network timeout, malformed JSON response, empty `choices` array |
| `OllamaProvider` | Mocked HTTP 200 response | Connection refused, invalid model, malformed response |
| `Database` | `connect()`/`close()` with mocked pool | `initialize_schema`, `acquire`, `execute`, `fetch`, `fetchrow`, `fetchval` with real queries |
| CLI | `version`, `doctor`, `skills`, `chat --help` | `chat` with actual agent execution, `chat --continue`, `chat --session`, error handling |

### 2.2 Not Tested At All

- **`ContextManager.search_by_embedding`** — The method exists, makes a pgvector query, and has zero tests.
- **`ContextManager.get_chunks` with `chunk_type` filter** — The filter branch is never exercised.
- **`SessionManager.get_last_active`** — Never tested.
- **`SessionManager.set_status`** — Never tested.
- **`Database.initialize_schema`** — Never tested.
- **`Database.acquire` context manager** — Never tested.
- **`memory` module** — Empty file (0 bytes), zero tests, but listed in README as a feature.
- **`rag` module** — Empty file (0 bytes), zero tests, but listed in README as a feature.
- **Schema tables: `skills`, `memories`, `agent_messages`, `heartbeat_config`, `external_context`, `subagent_sessions`, `subagent_messages`, `subagent_results`** — All created in `schema.sql`, none implemented, none tested.

---

## 3. Tests That Are Actively Misleading

### 3.1 `test_assemble_token_budget` — False Assurance

```python
def test_assemble_token_budget(self):
    assembler = PromptAssembler(session_budget=100)
    prompt = assembler.assemble(
        system_prompt="System", goal="Goal", ...
    )
    assert "System" in prompt
    assert "Goal" in prompt
    assert "query" in prompt
```

This test sets a budget of 100 tokens but the system prompt alone (`"You are AgentHarness, a self-hosted AI agent..."`) is ~200 tokens. The test asserts that all three strings are present, which means the assembler is **already violating the budget**. The test should assert `assembler._estimate_tokens(prompt) <= 100`, but it doesn't. The budget is not enforced — it's aspirational.

### 3.2 `test_agent_run_with_tool_calls` — Doesn't Test Tool Execution

The test mocks `context_manager.add_chunk` and `context_manager.get_recent_context`, then verifies the provider was called twice. But it never verifies:
- That `registry.execute("read_file", path="/tmp/test")` was actually called.
- That the tool result was stored in context.
- That the tool result was appended to `messages` for the next LLM call.
- That the tool call was recorded in `response.tool_calls`.

The test would pass even if the tool execution code were completely broken, as long as the provider is called the right number of times.

### 3.3 `test_session_crud` — Skipped in Practice

```python
async def test_session_crud(self):
    try:
        from ah.db.connection import db
        await db.connect()
    except Exception:
        pytest.skip("PostgreSQL not available")
```

This test is skipped whenever PostgreSQL isn't running. In most CI environments, it's skipped. It should use a test container or SQLite fallback, not a silent skip.

### 3.4 `test_status_command_no_db` — Asserts Failure as Success

```python
def test_status_command_no_db(self):
    result = runner.invoke(app, ["status"])
    assert result.exit_code != 0
```

This test asserts that the command **fails** when the database is unavailable. That's not a feature — it's a bug. The `status` command should degrade gracefully and report "PostgreSQL: not connected" with exit code 0, not crash.

---

## 4. What Would Break in Production

### 4.1 Agent Loop Crashes

1. **Malformed tool call JSON.** `json.loads(tc["function"]["arguments"])` will raise `json.JSONDecodeError` if the LLM returns invalid JSON. This exception is **not caught** — it propagates up and crashes the agent loop. The user sees a stack trace, not a graceful error.

2. **Tool execution timeout.** If a tool hangs (e.g., `terminal("sleep 1000")`), the agent loop hangs forever. There's no timeout on `registry.execute`.

3. **Empty LLM response.** If the provider returns `content=""` and `tool_calls=[]`, the agent returns an empty `AgentResponse` with no indication something went wrong.

4. **Non-existent tool.** If the LLM calls a tool that isn't registered, `registry.execute` raises `ValueError`, which is caught and returned as `"Error: Tool 'x' not registered"`. This string is then sent back to the LLM as a tool result, which may cause the LLM to retry the same non-existent tool in a loop until `max_iterations` is reached.

### 4.2 Data Corruption

5. **Msgpack serialization failure.** If a payload contains non-serializable objects (e.g., `datetime`, custom classes), `msgpack.packb` raises `TypeError`. This is not caught — the entire `add_chunk` operation fails.

6. **Msgpack deserialization failure.** If the database contains corrupted msgpack data, `msgpack.unpackb` raises an exception in `_row_to_chunk` or `_row_to_session`. This is not caught — the entire query fails.

7. **Concurrent session updates.** If two agents update the same session simultaneously, the last write wins. There's no optimistic locking or conflict detection.

### 4.3 Database Failures

8. **Connection pool exhaustion.** The pool has `max_size=10`. If 10 connections are in use, the 11th request hangs indefinitely. There's no timeout or queue.

9. **Database goes down mid-operation.** If the database connection drops during a query, `asyncpg` raises `ConnectionDoesNotExistError` or `ConnectionRefusedError`. These are not caught — the operation fails with a raw exception.

10. **Schema mismatch.** If the database schema doesn't match the queries (e.g., missing `vector` extension, missing `pg_cron`), queries fail with `UndefinedColumnError` or `UndefinedFunctionError`. There's no schema validation at startup.

### 4.4 Security Issues

11. **Shell injection in `terminal` tool.** The `terminal` function uses `shell=True` with user-provided input. If the LLM generates a command like `rm -rf /`, it will execute. There's no sandboxing or command validation.

12. **No input validation on tool arguments.** The `ToolRegistry.execute` method passes `**kwargs` directly to the tool function. If the LLM provides unexpected types (e.g., a string where an int is expected), the tool function may crash or behave unexpectedly.

### 4.5 Feature Gaps

13. **`memory` and `rag` modules are empty.** The README advertises memory and RAG features, but the modules are 0-byte stubs. No tests acknowledge this gap.

14. **Schema tables are unused.** The schema creates 8 tables (`skills`, `memories`, `agent_messages`, `heartbeat_config`, `external_context`, `subagent_sessions`, `subagent_messages`, `subagent_results`) that have no corresponding code. The `SkillRegistry` doesn't persist to the `skills` table. There's no memory system. There's no multi-agent messaging.

15. **No heartbeat implementation.** The `heartbeat_config` table exists but there's no code to schedule or execute heartbeats.

16. **No subagent implementation.** The `subagent_*` tables exist but there's no code to spawn or manage subagents.

---

## 5. What Real Tests Should Look Like

### 5.1 Integration Tests with a Real Database

```python
@pytest.fixture
async def real_db():
    """Spin up a test PostgreSQL container."""
    # Use testcontainers-python or a local PostgreSQL instance
    db = Database("postgresql://postgres:postgres@localhost:5432/test_agentharness")
    await db.connect()
    await db.initialize_schema()
    yield db
    await db.close()

async def test_session_crud_real(real_db):
    """Test actual SQL queries against real database."""
    session = await session_manager.create(title="test")
    assert session.id is not None
    
    loaded = await session_manager.get(session.id)
    assert loaded.title == "test"
    
    await session_manager.update_state(session.id, {"key": "value"})
    loaded = await session_manager.get(session.id)
    assert loaded.state == {"key": "value"}
```

### 5.2 Agent Loop Tests with Real Tools

```python
async def test_agent_executes_tool_for_real():
    """Test that the agent actually executes a tool and uses its result."""
    # Create a real temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("secret content")
        f.flush()
    
    # Mock provider: first call returns tool call, second returns final answer
    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=[{
            "id": "call_1",
            "function": {"name": "read_file", "arguments": json.dumps({"path": f.name})}
        }]),
        LLMResponse(content="The file contains: secret content"),
    ])
    
    agent = ReActAgent(provider=provider, max_iterations=5)
    response = await agent.run(session.id, f"Read the file {f.name}")
    
    # Verify the tool actually executed
    assert "secret content" in response.content
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["tool"] == "read_file"
```

### 5.3 Error Recovery Tests

```python
async def test_agent_handles_malformed_tool_json():
    """Test that the agent doesn't crash on invalid tool call JSON."""
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=LLMResponse(
        content="",
        tool_calls=[{
            "id": "call_1",
            "function": {"name": "read_file", "arguments": "not valid json{"}
        }]
    ))
    
    agent = ReActAgent(provider=provider, max_iterations=5)
    response = await agent.run(session.id, "test")
    
    # Should not crash — should return an error or retry
    assert response is not None
    assert response.iterations >= 1

async def test_agent_handles_tool_timeout():
    """Test that the agent doesn't hang when a tool times out."""
    # Register a tool that sleeps forever
    @registry.register()
    async def slow_tool():
        await asyncio.sleep(1000)
    
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=LLMResponse(
        content="",
        tool_calls=[{
            "id": "call_1",
            "function": {"name": "slow_tool", "arguments": "{}"}
        }]
    ))
    
    agent = ReActAgent(provider=provider, max_iterations=5)
    
    # Should timeout, not hang forever
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agent.run(session.id, "test"), timeout=5)
```

### 5.4 Token Budget Enforcement Tests

```python
def test_token_budget_actually_enforced():
    """Test that the assembler truncates output to fit the budget."""
    assembler = PromptAssembler(session_budget=50)
    prompt = assembler.assemble(
        system_prompt="You are a test agent with a very long system prompt " * 20,
        goal="This is a goal " * 20,
        recent_chunks=[{"type": "user_message", "payload": {"content": "x" * 1000}}],
        retrieved_chunks=[],
        query="This is a query " * 20,
    )
    
    estimated_tokens = assembler._estimate_tokens(prompt)
    assert estimated_tokens <= 50, f"Prompt uses {estimated_tokens} tokens, budget is 50"
```

### 5.5 Concurrency Tests

```python
async def test_concurrent_session_updates():
    """Test that concurrent updates don't lose data."""
    session = await session_manager.create(title="test")
    
    async def update_state(key, value):
        await session_manager.update_state(session.id, {key: value})
    
    # Run 10 concurrent updates
    await asyncio.gather(*[
        update_state(f"key_{i}", f"value_{i}")
        for i in range(10)
    ])
    
    # All updates should be present (last write wins for each key)
    loaded = await session_manager.get(session.id)
    assert len(loaded.state) == 10
```

---

## 6. Summary of Gaps

| Category | Count | Severity |
|----------|-------|----------|
| Tests that assert failure as success | 1 | High |
| Tests that don't test what they claim | 3 | High |
| Production crash paths untested | 4 | Critical |
| Data corruption paths untested | 3 | Critical |
| Security issues untested | 2 | Critical |
| Features advertised but not implemented | 4 | High |
| Schema tables unused | 8 | Medium |
| External service calls untested | 2 | Medium |
| Concurrency/race conditions untested | 1 | Medium |
| **Total gaps** | **28** | |

---

## 7. Recommendations

1. **Add integration tests with a real database.** Use `testcontainers-python` to spin up PostgreSQL in CI. The current mocked tests verify nothing about SQL correctness.

2. **Test the agent loop end-to-end.** Mock only the HTTP layer (provider), not the tool execution or context management. Verify that tools actually execute and their results flow back to the LLM.

3. **Add error recovery tests.** Malformed JSON, tool timeouts, empty responses, non-existent tools — these are not edge cases, they're normal LLM behavior.

4. **Enforce token budgets in tests.** Assert `estimated_tokens(prompt) <= budget`, not just string inclusion.

5. **Test `web_search` and `web_extract` with mocked HTTP.** At minimum, verify the URL construction and response parsing.

6. **Acknowledge unimplemented features.** Either implement `memory`, `rag`, heartbeat, and subagents, or remove them from the README and add `xfail` tests marking them as known gaps.

7. **Add a security test for the `terminal` tool.** Verify that shell injection is either prevented or documented as a known risk.

8. **Replace `pytest.skip` with test containers.** A skipped test is not a passing test. It's a gap in coverage disguised as a green checkmark.

---

## 8. Code Smells That Tests Don't Catch

### 8.1 Duplicate Tool Definitions

The `terminal` tool is defined **twice** with different implementations:
- `ah/tools/builtins.py` — returns `[exit code: N]` format
- `ah/tools/terminal.py` — returns `(exit code N, no output)` format, supports `workdir` parameter

Similarly, `read_file`, `write_file`, and `list_files` are defined in both `builtins.py` and `file.py` with **different output formats**:

| Tool | `builtins.py` format | `file.py` format |
|------|---------------------|------------------|
| `read_file` | `File: {path} ({total} lines, showing {start}-{end})\n` + numbered lines | Raw content, no line numbers |
| `write_file` | `Written {n} bytes to {path}` | `Successfully wrote {n} characters to {path}` |
| `list_files` | `Files in {path} ({n} total):` + `[DIR]` prefix | `{filename} ({size} bytes)` |

The `builtins.py` file imports `file` and `terminal` at the bottom, so the `file.py`/`terminal.py` registrations **overwrite** the `builtins.py` ones. This means:
- The `builtins.py` implementations are dead code
- The tests in `test_comprehensive.py` test the `builtins.py` implementations (e.g., `assert "Written" in result`), which means they're testing code that is never actually used in production
- If someone changes the `builtins.py` implementations, the tests will pass but the production behavior won't change

### 8.2 Inconsistent Error Handling

The tools have inconsistent error handling:
- `read_file` returns `"Error: File not found: {path}"` (string)
- `write_file` returns `"Error writing file: {e}"` (string)
- `list_files` returns `"Error: Directory not found: {path}"` (string)
- `terminal` returns `"Error: Command timed out after {timeout}s"` (string)
- `web_search` returns `"Search failed for '{query}'. No results."` (string)
- `web_extract` returns `"Error: HTTP {status_code} for {url}"` (string)

All errors are returned as strings, which means the LLM can't distinguish between different error types. The agent's `_compress_chunk` method treats all `result` chunks the same way, so the LLM sees `"Error: File not found"` and `"Error: Command timed out"` as equivalent.

### 8.3 No Input Validation

The `ToolRegistry.execute` method passes `**kwargs` directly to the tool function without validation:
```python
async def execute(self, name: str, **kwargs) -> Any:
    if name not in self._tools:
        raise ValueError(f"Tool '{name}' not registered")
    tool = self._tools[name]
    if tool.is_async:
        return await tool.func(**kwargs)
    else:
        return tool.func(**kwargs)
```

If the LLM provides a string where an int is expected, the tool function may crash or behave unexpectedly. The JSON Schema is sent to the LLM but never enforced on the server side.

### 8.4 No Timeout on Tool Execution

The `ReActAgent.run` method calls `registry.execute` without a timeout:
```python
result = await registry.execute(tool_name, **tool_args)
```

If a tool hangs (e.g., `web_search` with a slow network, or `terminal` with a long-running command), the agent loop hangs forever. The `terminal` tool has a `timeout` parameter, but it's not enforced by the agent.

### 8.5 No Retry Logic

The providers don't implement retries or backoff. If the LLM API returns a 429 (rate limit) or 500 (server error), the provider raises an exception and the agent crashes. There's no retry with exponential backoff, no circuit breaker, and no fallback.

### 8.6 No Logging

The code uses `rich.console.Console` for output but doesn't use Python's `logging` module. This means:
- Errors are not logged to a file
- Debugging production issues requires reproducing them
- There's no audit trail of agent actions
- There's no way to monitor the system's health

### 8.7 No Metrics

The code doesn't collect any metrics:
- No token usage tracking
- No tool execution time tracking
- No error rate tracking
- No session duration tracking

This makes it impossible to monitor the system's health or detect performance degradation.

---

## 9. Specific Test Failures That Would Occur in Production

### 9.1 `test_read_file_success` Would Fail with `file.py` Implementation

```python
def test_read_file_success(self, temp_dir):
    test_file = temp_dir / "test.txt"
    test_file.write_text("line1\nline2\nline3", encoding="utf-8")
    result = builtins.read_file(str(test_file))
    assert "line1" in result
    assert "line2" in result
```

This test imports `builtins` and calls `builtins.read_file`. But the actual `read_file` registered in the registry is from `file.py`, which returns raw content without line numbers. The test passes because it's testing the `builtins.py` implementation, not the `file.py` implementation that's actually used.

### 9.2 `test_write_file_success` Would Fail with `file.py` Implementation

```python
def test_write_file_success(self, temp_dir):
    test_file = temp_dir / "output.txt"
    result = builtins.write_file(str(test_file), "test content")
    assert "Written" in result or "Successfully" in result
    assert test_file.read_text() == "test content"
```

The `builtins.py` implementation returns `"Written {n} bytes to {path}"`, while the `file.py` implementation returns `"Successfully wrote {n} characters to {path}"`. The test accepts both, but the actual behavior depends on which implementation is registered.

### 9.3 `test_list_files_success` Would Fail with `file.py` Implementation

```python
def test_list_files_success(self, temp_dir):
    (temp_dir / "file1.txt").write_text("content1")
    (temp_dir / "file2.txt").write_text("content2")
    result = builtins.list_files(str(temp_dir))
    assert "file1.txt" in result
    assert "file2.txt" in result
```

The `builtins.py` implementation returns `"Files in {path} ({n} total):"` followed by a list, while the `file.py` implementation returns just the filenames. The test passes because it only checks that the filenames are present, but the actual output format is different.

---

## 10. The Fundamental Problem

The test suite tests **implementations**, not **behavior**. It verifies that specific functions return specific strings, not that the system works correctly. This is like testing a car by checking that the steering wheel is round and the pedals are where you'd expect them to be, without ever starting the engine.

The tests should verify:
- That the agent can complete a task from start to finish
- That the agent can recover from errors
- That the agent respects the context budget
- That the agent can handle concurrent requests
- That the agent can handle security threats

Instead, the tests verify:
- That dataclasses have the right default values
- That mocked database calls return the expected shapes
- That string concatenation works

The difference is the difference between a smoke test and a safety net. A smoke test tells you the engine is on. A safety net tells you the car won't crash when you hit a bump.

---

*The current test suite is a smoke test, not a safety net. It verifies that the code can be imported and that simple inputs produce simple outputs. It does not verify that the system works when things go wrong — and in production, things always go wrong.*
