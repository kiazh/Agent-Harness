# AgentHarness Scalability Critique

**Verdict:** This architecture will not scale beyond a single-user toy project. It is a well-structured prototype that demonstrates the ReAct pattern, but every layer — from the database connection pool to the agent loop to the context management — has hard ceilings that will be hit within minutes of concurrent or sustained use.

---

## 1. Connection Pool: Hardcoded and Starved

**Code:** `ah/db/connection.py:22-27`

```python
self._pool = await asyncpg.create_pool(
    self.dsn,
    min_size=2,
    max_size=10,
    command_timeout=30,
)
```

**What breaks at scale:**

- `max_size=10` means at most 10 concurrent database operations. A single ReAct iteration performs at least 3-4 DB calls (session get, context add, context fetch, session update). With 3 concurrent users, the pool is saturated.
- `min_size=2` means cold-start latency: the first 8 connections are created on demand, adding 50-200ms per new connection under load.
- No connection retry, no pool timeout configuration, no health checks. If a connection dies (PostgreSQL restart, network blip), the pool hands out dead connections until asyncpg's internal recovery kicks in.
- The pool is a process-global singleton (`db = Database()`). There is no way to shard connections by tenant, priority, or query type.

**What breaks concretely:** With 5 concurrent agent sessions, each running 10-iteration ReAct loops, the pool is exhausted. New `acquire()` calls block for up to `command_timeout=30` seconds, then raise `asyncpg.PoolTimeoutError`. The agent loop has no retry logic — it crashes.

**Fix:**
- Make pool size configurable via environment variable (`DB_POOL_MIN`, `DB_POOL_MAX`).
- Set `max_size` to `min(50, (CPU cores * 2) + effective_spindle_count)` — for a typical 8-core box, ~20-30.
- Add `pool_timeout` (e.g., 5s) so callers fail fast instead of hanging.
- Add a connection health check (`SELECT 1`) on acquire.
- For multi-tenant scale, use PgBouncer in transaction mode in front of PostgreSQL.

---

## 2. Zero Caching Layer

**Code:** Entire codebase — no Redis, no Memcached, no LRU cache, no `functools.lru_cache`.

**What breaks at scale:**

- **Session lookups:** Every ReAct iteration calls `session_manager.get(session_id)` which does a `SELECT ... FROM sessions WHERE id = $1`. Under 100 concurrent sessions, this is 100 identical queries per second for the same data.
- **Context retrieval:** `get_recent_context()` runs `SELECT ... FROM context_chunks WHERE session_id = $1 ORDER BY created_at DESC LIMIT 5` on every iteration. The result is identical within a single agent turn but is re-fetched every time.
- **Tool definitions:** `registry.get_tool_definitions()` rebuilds the full JSON Schema list on every LLM call. This is pure CPU waste — the tool set doesn't change at runtime.
- **Token counting:** `get_token_usage()` does `SELECT SUM(token_count) FROM context_chunks WHERE session_id = $1` — a full table scan on every call.

**What breaks concretely:** At 50 concurrent sessions, the database sees ~500 queries/second for data that changes at most once per second. PostgreSQL spends 90% of its time serving redundant reads.

**Fix:**
- Add an LRU cache (e.g., `cachetools.TTLCache`) for session objects with a 5-second TTL.
- Cache tool definitions at startup — they're immutable during a run.
- Cache recent context within a single agent turn (pass it through, don't re-query).
- Use Redis for cross-instance caching if you ever run more than one worker.
- Materialize token usage as a running counter on the session row instead of `SUM()` on every call.

---

## 3. No Horizontal Scaling — Single Process, Single Instance

**Code:** `ah/cli.py` — the entire application is a Typer CLI that runs `asyncio.run()` per command.

**What breaks at scale:**

- There is no HTTP server, no WebSocket endpoint, no message queue consumer. The only way to use AgentHarness is to invoke `ah chat "..."` from a shell, which boots a new Python process, creates a new DB pool, runs one agent turn, and exits.
- No way to run multiple agent workers against the same database. If you try to run two `ah chat` processes simultaneously, they share the same `db` singleton (which is fine — it's per-process), but there is no coordination, no distributed locking, no leader election.
- The `agent_messages` table exists for inter-agent communication, but there is no consumer polling it. It's a write-only table.
- The `heartbeat_config` table exists, but there is no heartbeat scheduler process. Heartbeats only fire if the CLI is running.

**What breaks concretely:** You cannot deploy this as a service. You cannot have a web UI, a Slack bot, and a CLI all talking to the same agent. You cannot run a background worker that processes agent messages. The architecture is fundamentally a batch CLI, not a service.

**Fix:**
- Extract the agent loop into a FastAPI/Starlette service with `/chat`, `/sessions`, `/stream` endpoints.
- Add a Celery/RQ/ARQ task queue for background agent execution.
- Implement a heartbeat scheduler as a separate async task (e.g., `asyncio.create_task(heartbeat_loop())`).
- Use Redis or PostgreSQL `LISTEN/NOTIFY` for inter-agent message delivery.

---

## 4. Single-Threaded Agent Loop — No Parallelism

**Code:** `ah/core/agent.py:95-175` — the ReAct loop is strictly sequential.

```python
for iteration in range(self.max_iterations):
    response = await self.provider.complete(...)
    for tc in response.tool_calls:
        result = await registry.execute(tool_name, **tool_args)
```

**What breaks at scale:**

- **Tool calls are serialized:** If the LLM returns 3 tool calls (e.g., read_file, search_files, web_search), they execute one at a time. `read_file` takes 50ms, `search_files` takes 200ms, `web_search` takes 2s. Total: 2.25s. If parallelized: 2s. At 10 iterations, that's 22.5s vs 20s — a 12% latency penalty that compounds.
- **No speculative execution:** The agent cannot start embedding the next query while waiting for the LLM response.
- **No concurrent sub-agents:** The `subagent_sessions` table exists, but there is no code to spawn sub-agents in parallel. The `multi-agent` skill is aspirational.
- **The `terminal` tool blocks the event loop:** `subprocess.run()` is synchronous. A 30-second `terminal` call blocks the entire async event loop, stalling all other coroutines.

**What breaks concretely:** A single agent turn with 5 tool calls takes 30+ seconds. With 10 concurrent users, the event loop is saturated. The `terminal` tool is the worst offender — a single `sleep 60` command blocks everything.

**Fix:**
- Use `asyncio.gather()` to execute independent tool calls in parallel.
- Replace `subprocess.run()` with `asyncio.create_subprocess_exec()` for the terminal tool.
- Implement a sub-agent spawner that uses `asyncio.TaskGroup` (Python 3.11+) to run concurrent sub-agents.
- Add a semaphore to limit concurrent tool executions per session.

---

## 5. No Backpressure — Unbounded Queues and Memory

**Code:** `ah/core/context.py` — context chunks are inserted with no limit.

```python
async def add_chunk(self, session_id, agent_id, chunk_type, payload, token_count=0, embedding=None):
    # INSERT — no check on total session size, no eviction
```

**What breaks at scale:**

- **Unbounded context growth:** A session that runs for 100 iterations accumulates 100+ context chunks. Each chunk has a MessagePack payload and optionally a 1536-dim embedding vector. At ~1KB per chunk, that's 100KB per session. At 10,000 sessions, that's 1GB in the `context_chunks` table — with no archival or eviction.
- **No backpressure on the LLM:** If the LLM returns a 10,000-token response, it's stored in full. There's no truncation, no summarization, no sliding window.
- **The `messages` list grows unboundedly within a turn:** Each tool call appends 2 messages (assistant + tool result). At 10 iterations with 3 tool calls each, that's 60+ messages sent to the LLM on the final iteration. Most LLM APIs have a 128K token limit — this will blow through it.
- **No rate limiting on tool execution:** A malicious or buggy agent could call `terminal("while true; do echo hi; done")` in a loop, spawning infinite subprocesses.

**What breaks concretely:** After ~50 iterations, the prompt exceeds the LLM's context window. The API returns a 400 error. The agent has no recovery logic — it crashes. The database grows without bound until disk is full.

**Fix:**
- Implement a sliding window or summarization strategy: when `token_count` exceeds `context_budget`, summarize older chunks and replace them.
- Add a hard limit on `messages` list size (e.g., keep last 20 messages + system prompt).
- Add a per-session tool execution rate limit (e.g., max 10 tool calls per minute).
- Implement context eviction: delete chunks older than N days or when total tokens exceed budget.
- Add a `max_tool_calls_per_iteration` parameter to the agent.

---

## 6. Database Bottlenecks — Every Operation Hits PostgreSQL

**Code:** All managers (`ContextManager`, `SessionManager`) query PostgreSQL directly on every operation.

**What breaks at scale:**

- **No read replicas:** All reads and writes go to the same PostgreSQL instance. At 100 concurrent sessions, the DB is the bottleneck.
- **No query batching:** Each context chunk is inserted individually. A 10-iteration turn with 3 tool calls per iteration = 30 individual `INSERT` statements. Batching them into a single `INSERT ... VALUES (...), (...), (...)` would reduce round-trips by 10x.
- **No connection pooling strategy for long-running transactions:** The `acquire()` context manager holds a connection for the duration of the query. A slow query (e.g., embedding search over 1M vectors) blocks a pool connection.
- **The HNSW index on `context_chunks.embedding` is expensive:** Every `INSERT` with an embedding updates the HNSW index. At 100 inserts/second, the index becomes a write bottleneck. `ef_construction=64` is aggressive for write-heavy workloads.
- **No partitioning:** The `context_chunks` table has no time-based partitioning. A query for `WHERE session_id = $1 ORDER BY created_at DESC LIMIT 5` scans all chunks for that session, even though only the last 5 are needed.

**What breaks concretely:** At 10,000 context chunks, the `ORDER BY created_at DESC LIMIT 5` query takes 50ms. At 1,000,000 chunks, it takes 5+ seconds. The HNSW index search degrades similarly.

**Fix:**
- Add time-based partitioning to `context_chunks` (e.g., by month).
- Batch inserts using `executemany()` or multi-row `INSERT`.
- Reduce `ef_construction` to 16-32 for better write performance.
- Add a covering index: `CREATE INDEX idx_context_chunks_recent ON context_chunks(session_id, created_at DESC) INCLUDE (payload_msgpack, chunk_type, token_count)`.
- Use read replicas for read-heavy queries (session listing, context retrieval).
- Consider TimescaleDB for time-series context data.

---

## 7. Memory Growth — Unbounded In-Memory State

**Code:** `ah/core/agent.py:88-90` — the `messages` list grows without bound.

```python
messages = [{"role": "user", "content": prompt}]
# ... in the loop:
messages.append({"role": "assistant", ...})
messages.append({"role": "tool", ...})
```

**What breaks at scale:**

- **Within a single turn:** The `messages` list grows by 2 entries per tool call. At 10 iterations with 3 tool calls each, that's 60+ messages. Each message can be 1-10KB. Total: 600KB per turn in memory.
- **Across turns:** The `messages` list is local to `run()`, so it's freed after each turn. But the context chunks in the database grow forever (see #5).
- **The `tool_calls_made` list:** Stores full tool arguments and result previews. At 100 tool calls, that's 100 dicts with potentially large strings.
- **No streaming:** The LLM response is buffered in full before being processed. A 10,000-token response is ~40KB in memory before the agent sees any of it.

**What breaks concretely:** A long-running session (100+ turns) will have accumulated 10,000+ context chunks in the database. Assembling the prompt requires fetching and deserializing all of them. The `PromptAssembler.assemble()` method creates a single string that can be 100KB+ — this is sent to the LLM as a single user message, which may exceed the API's request size limit.

**Fix:**
- Implement prompt summarization: when `messages` exceeds N entries, summarize the middle and keep the first and last few.
- Stream the LLM response using `httpx.AsyncClient.stream()` to reduce memory pressure.
- Add a `max_context_chunks` parameter to `get_recent_context()` and enforce it.
- Use a generator-based approach for prompt assembly instead of building a single string.

---

## 8. Blocking I/O in Async Context

**Code:** `ah/tools/builtins.py:87-107` — `subprocess.run()` is synchronous.

```python
@registry.register(description="Run a shell command")
def terminal(command: str, timeout: int = 60) -> str:
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
```

**What breaks at scale:**

- `subprocess.run()` blocks the entire asyncio event loop. While a `terminal` command runs, no other coroutine can execute — no other agent can make progress, no DB queries can complete, no LLM calls can be made.
- The `read_file` and `write_file` tools use synchronous `open()`. For small files this is fine, but for large files (100MB+), the event loop is blocked during the entire read/write.
- The `web_search` and `web_extract` tools use synchronous `httpx.get()` instead of `httpx.AsyncClient`. This blocks the event loop during the HTTP request.

**What breaks concretely:** A single `terminal("sleep 60")` call blocks the entire application for 60 seconds. All other agent sessions, all DB connections, all LLM calls are frozen.

**Fix:**
- Replace `subprocess.run()` with `asyncio.create_subprocess_exec()`.
- Replace `httpx.get()` with `httpx.AsyncClient().get()`.
- Use `aiofiles` for async file I/O.
- Add a timeout to all blocking operations.

---

## 9. No Streaming — High Latency

**Code:** `ah/core/provider.py:102` — `resp = await self.client.post("/chat/completions", json=payload)`.

**What breaks at scale:**

- The LLM response is buffered in full before being returned. For a 1,000-token response, the user waits 10-30 seconds before seeing anything.
- No Server-Sent Events (SSE) or WebSocket streaming. The CLI user sees nothing until the entire response is ready.
- The `verbose` mode prints tool calls as they happen, but the LLM reasoning is invisible until the full response arrives.

**What breaks concretely:** Users perceive the agent as "hung" during long responses. There's no way to cancel a response mid-stream. If the LLM generates a 10,000-token response, the user waits 60+ seconds with no feedback.

**Fix:**
- Use `httpx.AsyncClient.stream("POST", ...)` to stream the response.
- Parse SSE chunks and print them incrementally.
- Add a cancellation token so users can interrupt long-running responses.

---

## 10. No Multi-Tenancy or Isolation

**Code:** `ah/db/schema.sql` — all tables use a single `session_id` foreign key with no tenant/organization concept.

**What breaks at scale:**

- All sessions share the same `sessions` table. There is no `organization_id` or `user_id` column.
- The `context_chunks` table has no tenant isolation. A query for `WHERE session_id = $1` is safe, but a query for `SELECT COUNT(*) FROM context_chunks` (as in `ah status`) scans all tenants' data.
- No row-level security (RLS) policies. Any code with DB access can read any session's context.
- No quota enforcement. A single user can create 10,000 sessions and consume all DB resources.

**What breaks concretely:** The moment you have 2+ users, one user can see another user's session list, context chunks, and memories. There is no access control.

**Fix:**
- Add `organization_id` or `user_id` to all tables.
- Implement PostgreSQL row-level security policies.
- Add per-tenant quotas (max sessions, max context chunks, max tokens).
- Add authentication and authorization to the CLI.

---

## 11. No Observability — Flying Blind

**Code:** Entire codebase — no metrics, no structured logging, no tracing.

**What breaks at scale:**

- No way to know how many agent turns are running, how long they take, or how many tokens they consume.
- No way to debug a slow agent turn — was it the LLM, the DB, or a tool?
- No way to detect a runaway agent that's stuck in an infinite loop.
- No way to alert when the DB pool is exhausted or the context table is growing too fast.

**What breaks concretely:** When (not if) the system breaks, you have no idea why. You'd need to add logging and metrics after the fact, which means redeploying.

**Fix:**
- Add structured logging (e.g., `structlog` or `logging` with JSON format).
- Add Prometheus metrics: `agent_turns_total`, `agent_turn_duration_seconds`, `db_pool_connections_active`, `context_chunks_total`.
- Add OpenTelemetry tracing for the agent loop.
- Add a `/health` endpoint that checks DB connectivity, pool status, and LLM API reachability.

---

## 12. Schema Design Issues

**Code:** `ah/db/schema.sql`

**What breaks at scale:**

- **`context_chunks` has no partitioning:** At 10M+ rows, every query slows down. Time-based partitioning would keep recent queries fast.
- **`memories` table is unused:** The `MemoryManager` class doesn't exist. The table is created but never written to. Dead schema.
- **`agent_messages` has no consumer:** The table exists for inter-agent communication, but no code reads from it. It's a write-only sink.
- **`subagent_sessions` and `subagent_messages` are unused:** No code creates sub-agents. These tables are aspirational.
- **`external_context` is unused:** No code writes to or reads from this table.
- **No archival strategy:** Old sessions and context chunks are never archived or deleted. The database grows forever.
- **The `embedding` column is `vector(1536)`:** This is OpenAI's `text-embedding-3-small` dimension. If you switch to a different embedding model (e.g., `nomic-embed-text` at 768 dims), the schema breaks. There's no version column or flexible dimension.

**Fix:**
- Implement time-based partitioning for `context_chunks`.
- Remove unused tables or implement the missing managers.
- Add an archival job that moves old sessions to a cold storage table.
- Make the embedding dimension configurable.

---

## 13. No Configuration Management

**Code:** `ah/db/connection.py:10` — hardcoded DSN default.

```python
DEFAULT_DSN = "postgresql://postgres:***@localhost:5432/agentharness"
```

**What breaks at scale:**

- The default DSN has a hardcoded password (`postgres`). This is a security risk and makes it impossible to deploy to different environments without code changes.
- Pool size, command timeout, and all other settings are hardcoded.
- No way to configure the agent (max iterations, model, temperature) without code changes.
- The `.env.example` file exists but there's no `.env` loading in the library code (only in `provider.py` via `load_dotenv()`).

**Fix:**
- Use a configuration management library (e.g., `pydantic-settings`) to load settings from environment variables.
- Never hardcode credentials.
- Make all agent parameters configurable via CLI flags or config files.

---

## 14. No Error Recovery or Retry Logic

**Code:** `ah/core/agent.py:136-138` — tool execution errors are caught and returned as strings.

```python
try:
    result = await registry.execute(tool_name, **tool_args)
except Exception as e:
    result = f"Error: {e}"
```

**What breaks at scale:**

- Transient errors (LLM API rate limit, DB connection timeout, network blip) are treated the same as permanent errors. There's no retry with exponential backoff.
- If the LLM API returns a 429 (rate limit), the agent crashes. It should wait and retry.
- If the DB connection dies mid-turn, the agent crashes. It should reconnect and retry.
- If a tool fails, the error string is sent to the LLM as the tool result. The LLM may not understand that it's an error and may try the same tool again with the same arguments, creating an infinite loop.

**Fix:**
- Add retry with exponential backoff for LLM calls and DB operations.
- Distinguish between transient and permanent errors.
- Add a circuit breaker for the LLM provider — if it fails N times in a row, stop trying for a cooldown period.
- Send structured error messages to the LLM (e.g., `{"error": "rate_limit", "retry_after": 30}`) so it can adjust its behavior.

---

## 15. The `asyncio.run()` Anti-Pattern

**Code:** `ah/cli.py:30-32`

```python
def _run(coro):
    return asyncio.run(coro)
```

**What breaks at scale:**

- `asyncio.run()` creates a new event loop, runs the coroutine, and closes the loop. This is fine for a one-shot CLI command, but it means:
  - The DB pool is created and destroyed on every invocation. No connection reuse across commands.
  - No background tasks can run (e.g., heartbeat scheduler, metrics collector).
  - No way to keep a long-running agent session alive across multiple user inputs.
- If you ever wrap this in a server (e.g., FastAPI), you'll have multiple event loops, and the `db` singleton will be bound to the wrong loop.

**Fix:**
- For a CLI, `asyncio.run()` is acceptable. But the architecture should separate the "agent service" from the "CLI wrapper."
- The agent service should be a long-running async process that owns the event loop and DB pool.
- The CLI should connect to the agent service via HTTP or gRPC, not embed the agent directly.

---

## Summary: What Would It Take to Scale?

| Layer | Current State | Required for Scale |
|-------|--------------|-------------------|
| **Database** | Single PostgreSQL, 10-conn pool | PgBouncer, read replicas, partitioning, connection pooling per tenant |
| **Caching** | None | Redis for sessions, context, tool definitions |
| **Architecture** | CLI batch job | FastAPI service + Celery workers + Redis queue |
| **Agent Loop** | Sequential, blocking I/O | Parallel tool execution, async I/O, streaming |
| **Context** | Unbounded growth | Sliding window, summarization, eviction |
| **Backpressure** | None | Rate limiting, quotas, circuit breakers |
| **Observability** | None | Prometheus metrics, OpenTelemetry tracing, structured logging |
| **Multi-tenancy** | None | Org/user IDs, RLS, authentication |
| **Error Handling** | Catch-and-continue | Retry with backoff, circuit breakers, health checks |

**Bottom line:** AgentHarness is a clean, well-organized prototype that demonstrates the ReAct pattern effectively. But it is architecturally incapable of serving more than one user at a time. Every layer — from the 10-connection pool to the unbounded context growth to the blocking subprocess calls — has a hard ceiling that will be hit under concurrent load. Scaling it would require rewriting the core architecture, not just tuning parameters.
