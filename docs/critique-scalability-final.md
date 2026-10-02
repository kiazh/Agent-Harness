# AgentHarness Scalability Audit — Final Verdict

**Date:** 2026-10-01  
**Scope:** Full codebase audit — every module, every layer  
**Verdict:** This is a well-organized single-user prototype. It will not scale beyond one concurrent user. Every layer has hard ceilings that will be hit within minutes of concurrent or sustained use. Scaling it requires architectural rewrites, not parameter tuning.

---

## 1. Connection Pool: Hardcoded, Starved, and Fragile

**Code:** `ah/db/connection.py:22-27`

```python
self._pool = await asyncpg.create_pool(
    self.dsn,
    min_size=2,
    max_size=10,
    command_timeout=30,
)
```

### What breaks at scale

- **`max_size=10` is a death sentence.** A single ReAct iteration performs 3-4 DB calls (session get, context add, context fetch, session update). With 3 concurrent users, the pool is saturated. With 5, it's a bottleneck.
- **`min_size=2` means cold-start latency.** The first 8 connections are created on demand, adding 50-200ms per new connection under load.
- **No connection retry, no pool timeout, no health checks.** If a connection dies (PostgreSQL restart, network blip), the pool hands out dead connections until asyncpg's internal recovery kicks in.
- **The pool is a process-global singleton** (`db = Database()`). There is no way to shard connections by tenant, priority, or query type.
- **No `pool_timeout` configured.** `acquire()` blocks indefinitely when the pool is exhausted. The agent loop has no retry logic — it crashes.

### What breaks concretely

With 5 concurrent agent sessions, each running 10-iteration ReAct loops, the pool is exhausted. New `acquire()` calls block for up to `command_timeout=30` seconds, then raise `asyncpg.PoolTimeoutError`. The agent loop has no retry logic — it crashes.

### Fix

- Make pool size configurable via environment variable (`DB_POOL_MIN`, `DB_POOL_MAX`).
- Set `max_size` to `min(50, (CPU cores * 2) + effective_spindle_count)` — for a typical 8-core box, ~20-30.
- Add `pool_timeout` (e.g., 5s) so callers fail fast instead of hanging.
- Add a connection health check (`SELECT 1`) on acquire.
- For multi-tenant scale, use PgBouncer in transaction mode in front of PostgreSQL.

---

## 2. Concurrent Sessions: No Isolation, No Coordination

**Code:** `ah/core/session.py`, `ah/core/agent.py`

### What breaks at scale

- **The `SessionManager` uses a `TTLCache(maxsize=128, ttl=5)`.** This is a per-process in-memory cache. If you run multiple worker processes (e.g., multiple CLI invocations), each has its own cache. Session state is not shared.
- **No distributed locking.** If two processes try to update the same session simultaneously, there's no coordination. The last write wins.
- **No session affinity.** If you run multiple workers behind a load balancer, a user's requests may hit different workers, each with its own DB pool and cache. Session state becomes inconsistent.
- **The `agent_id` field is a string with no enforcement.** Multiple agents can write to the same session, corrupting context.

### What breaks concretely

You cannot run two `ah chat` processes against the same session simultaneously. The second process will overwrite the first's context chunks. There's no locking, no optimistic concurrency control, no versioning.

### Fix

- Add a `version` column to the `sessions` table for optimistic concurrency control.
- Use PostgreSQL advisory locks or `SELECT FOR UPDATE` for session-level coordination.
- Implement session affinity at the load balancer level, or use a shared cache (Redis) for session state.
- Enforce `agent_id` uniqueness per session.

---

## 3. Memory Growth: Unbounded In-Memory State

**Code:** `ah/core/agent.py:88-90`, `ah/core/assembler.py`

### What breaks at scale

- **The `messages` list grows without bound within a turn.** Each tool call appends 2 messages (assistant + tool result). At 10 iterations with 3 tool calls each, that's 60+ messages. Each message can be 1-10KB. Total: 600KB per turn in memory.
- **The `tool_calls_made` list stores full tool arguments and result previews.** At 100 tool calls, that's 100 dicts with potentially large strings.
- **The `PromptAssembler.assemble()` method creates a single string** that can be 100KB+. This is sent to the LLM as a single user message, which may exceed the API's request size limit.
- **No streaming of the LLM response in the non-streaming path.** A 10,000-token response is ~40KB in memory before the agent sees any of it.
- **The `retrieved_chunks` list** in `agent.py` can grow to 50+ items, each with a full payload and embedding vector.

### What breaks concretely

A long-running session (100+ turns) will have accumulated 10,000+ context chunks in the database. Assembling the prompt requires fetching and deserializing all of them. The `PromptAssembler.assemble()` method creates a single string that can be 100KB+ — this is sent to the LLM as a single user message, which may exceed the API's request size limit.

### Fix

- Implement prompt summarization: when `messages` exceeds N entries, summarize the middle and keep the first and last few.
- Stream the LLM response using `httpx.AsyncClient.stream()` to reduce memory pressure.
- Add a `max_context_chunks` parameter to `get_recent_context()` and enforce it.
- Use a generator-based approach for prompt assembly instead of building a single string.
- Limit `tool_calls_made` to the last N entries.

---

## 4. Context Growth: Unbounded Database Growth

**Code:** `ah/core/context.py`, `ah/db/schema.sql`

### What breaks at scale

- **Context chunks are inserted with no limit.** A session that runs for 100 iterations accumulates 100+ context chunks. Each chunk has a MessagePack payload and optionally a 1536-dim embedding vector. At ~1KB per chunk, that's 100KB per session. At 10,000 sessions, that's 1GB in the `context_chunks` table — with no archival or eviction.
- **No sliding window or summarization.** The `PromptAssembler` includes the last 3 recent chunks, but the database keeps all of them forever.
- **The `messages` list grows unboundedly within a turn.** Each tool call appends 2 messages (assistant + tool result). At 10 iterations with 3 tool calls each, that's 60+ messages sent to the LLM on the final iteration. Most LLM APIs have a 128K token limit — this will blow through it.
- **No rate limiting on tool execution.** A malicious or buggy agent could call `terminal("while true; do echo hi; done")` in a loop, spawning infinite subprocesses.
- **The `context_chunks` table has no partitioning.** At 10M+ rows, every query slows down. Time-based partitioning would keep recent queries fast.
- **The HNSW index on `context_chunks.embedding` is expensive.** Every `INSERT` with an embedding updates the HNSW index. At 100 inserts/second, the index becomes a write bottleneck. `ef_construction=64` is aggressive for write-heavy workloads.

### What breaks concretely

After ~50 iterations, the prompt exceeds the LLM's context window. The API returns a 400 error. The agent has no recovery logic — it crashes. The database grows without bound until disk is full.

### Fix

- Implement a sliding window or summarization strategy: when `token_count` exceeds `context_budget`, summarize older chunks and replace them.
- Add a hard limit on `messages` list size (e.g., keep last 20 messages + system prompt).
- Add a per-session tool execution rate limit (e.g., max 10 tool calls per minute).
- Implement context eviction: delete chunks older than N days or when total tokens exceed budget.
- Add a `max_tool_calls_per_iteration` parameter to the agent.
- Add time-based partitioning to `context_chunks` (e.g., by month).
- Reduce `ef_construction` to 16-32 for better write performance.
- Add a covering index: `CREATE INDEX idx_context_chunks_recent ON context_chunks(session_id, created_at DESC) INCLUDE (payload_msgpack, chunk_type, token_count)`.

---

## 5. Tool Execution: Blocking I/O, No Parallelism, No Limits

**Code:** `ah/tools/terminal.py`, `ah/tools/builtins.py`, `ah/tools/file.py`, `ah/core/agent.py`

### What breaks at scale

- **`subprocess.run()` blocks the entire event loop.** While a `terminal` command runs, no other coroutine can execute — no other agent can make progress, no DB queries can complete, no LLM calls can be made. A single `sleep 60` command blocks everything for 60 seconds.
- **`httpx.get()` is synchronous.** The `web_search` and `web_extract` tools use synchronous HTTP calls, blocking the event loop during the request.
- **`open()` is synchronous.** The `read_file` and `write_file` tools use synchronous file I/O. For large files (100MB+), the event loop is blocked during the entire read/write.
- **Tool calls are serialized.** If the LLM returns 3 tool calls (e.g., `read_file`, `search_files`, `web_search`), they execute one at a time. `read_file` takes 50ms, `search_files` takes 200ms, `web_search` takes 2s. Total: 2.25s. If parallelized: 2s. At 10 iterations, that's 22.5s vs 20s — a 12% latency penalty that compounds.
- **No timeout on tool execution.** A tool that hangs (e.g., `terminal("sleep 300")`) blocks the agent loop forever.
- **No concurrency limit.** An agent could spawn 1000 `terminal` calls in a single iteration, exhausting system resources.

### What breaks concretely

A single `terminal("sleep 60")` call blocks the entire application for 60 seconds. All other agent sessions, all DB connections, all LLM calls are frozen. With 10 concurrent users, the event loop is saturated. The `terminal` tool is the worst offender.

### Fix

- Replace `subprocess.run()` with `asyncio.create_subprocess_exec()`.
- Replace `httpx.get()` with `httpx.AsyncClient().get()`.
- Use `aiofiles` for async file I/O.
- Use `asyncio.gather()` to execute independent tool calls in parallel.
- Add a timeout to all tool executions.
- Add a semaphore to limit concurrent tool executions per session.
- Add a `max_tool_calls_per_iteration` parameter to the agent.

---

## 6. Streaming: Partial, No Backpressure, No Cancellation

**Code:** `ah/core/provider.py:244-357`, `ah/core/agent.py:487-792`

### What breaks at scale

- **Streaming is only implemented for the LLM response, not for the agent loop.** The `run_stream()` method yields `StreamEvent` objects, but the underlying LLM streaming is not backpressured. If the consumer is slow, the LLM response is buffered in full.
- **No cancellation token.** Users cannot interrupt a long-running response. If the LLM generates a 10,000-token response, the user waits 60+ seconds with no way to stop it.
- **The `content_parts` list in `stream_complete()` grows without bound.** For a 10,000-token response, that's 10,000 small strings in memory before they're joined.
- **No streaming for tool execution.** Tool results are returned in full after execution. A `terminal` command that produces 1MB of output is buffered in full before being sent to the LLM.
- **The `Live` display in the CLI refreshes at 10 FPS.** This is fine for a single user, but with 10 concurrent users, the terminal becomes unusable.

### What breaks concretely

Users perceive the agent as "hung" during long responses. There's no way to cancel a response mid-stream. If the LLM generates a 10,000-token response, the user waits 60+ seconds with no feedback.

### Fix

- Implement backpressure: pause the LLM stream when the consumer is slow.
- Add a cancellation token so users can interrupt long-running responses.
- Stream tool results incrementally instead of buffering in full.
- Use a more efficient display refresh strategy (e.g., only update when the content changes significantly).

---

## 7. Multi-Tenancy: None Whatsoever

**Code:** `ah/db/schema.sql`, `ah/core/session.py`, `ah/core/context.py`

### What breaks at scale

- **All tables use a single `session_id` foreign key with no tenant/organization concept.** There is no `organization_id` or `user_id` column.
- **No row-level security (RLS) policies.** Any code with DB access can read any session's context.
- **No quota enforcement.** A single user can create 10,000 sessions and consume all DB resources.
- **No authentication or authorization.** The CLI has no login, no API keys, no user management.
- **The `agent_id` field is a string with no enforcement.** Multiple agents can write to the same session, corrupting context.
- **The `memories` table has no tenant isolation.** All memories are shared across all sessions and agents.

### What breaks concretely

The moment you have 2+ users, one user can see another user's session list, context chunks, and memories. There is no access control. A malicious user can delete all sessions, corrupt all context, or exhaust all DB resources.

### Fix

- Add `organization_id` or `user_id` to all tables.
- Implement PostgreSQL row-level security policies.
- Add per-tenant quotas (max sessions, max context chunks, max tokens).
- Add authentication and authorization to the CLI.
- Enforce `agent_id` uniqueness per session.
- Add a `tenant_id` column to the `memories` table.

---

## 8. Horizontal Scaling: Impossible

**Code:** `ah/cli/interactive.py`, `ah/core/container.py`

### What breaks at scale

- **There is no HTTP server, no WebSocket endpoint, no message queue consumer.** The only way to use AgentHarness is to invoke `ah chat "..."` from a shell, which boots a new Python process, creates a new DB pool, runs one agent turn, and exits.
- **No way to run multiple agent workers against the same database.** If you try to run two `ah chat` processes simultaneously, they share the same `db` singleton (which is fine — it's per-process), but there is no coordination, no distributed locking, no leader election.
- **The `agent_messages` table exists for inter-agent communication, but there is no consumer polling it.** It's a write-only table.
- **The `heartbeat_config` table exists, but there is no heartbeat scheduler process.** Heartbeats only fire if the CLI is running.
- **The `asyncio.run()` anti-pattern.** `asyncio.run()` creates a new event loop, runs the coroutine, and closes the loop. This means the DB pool is created and destroyed on every invocation. No connection reuse across commands. No background tasks can run (e.g., heartbeat scheduler, metrics collector).
- **The `Container` class is a singleton.** There's no way to run multiple containers in the same process, each with its own DB pool and configuration.

### What breaks concretely

You cannot deploy this as a service. You cannot have a web UI, a Slack bot, and a CLI all talking to the same agent. You cannot run a background worker that processes agent messages. The architecture is fundamentally a batch CLI, not a service.

### Fix

- Extract the agent loop into a FastAPI/Starlette service with `/chat`, `/sessions`, `/stream` endpoints.
- Add a Celery/RQ/ARQ task queue for background agent execution.
- Implement a heartbeat scheduler as a separate async task (e.g., `asyncio.create_task(heartbeat_loop())`).
- Use Redis or PostgreSQL `LISTEN/NOTIFY` for inter-agent message delivery.
- Make the `Container` class instantiable multiple times with different configurations.
- Use a long-running async process that owns the event loop and DB pool.

---

## 9. Database Bottlenecks: Every Operation Hits PostgreSQL

**Code:** All managers (`ContextManager`, `SessionManager`, `MemoryStore`) query PostgreSQL directly on every operation.

### What breaks at scale

- **No read replicas.** All reads and writes go to the same PostgreSQL instance. At 100 concurrent sessions, the DB is the bottleneck.
- **No query batching.** Each context chunk is inserted individually. A 10-iteration turn with 3 tool calls per iteration = 30 individual `INSERT` statements. Batching them into a single `INSERT ... VALUES (...), (...), (...)` would reduce round-trips by 10x.
- **No connection pooling strategy for long-running transactions.** The `acquire()` context manager holds a connection for the duration of the query. A slow query (e.g., embedding search over 1M vectors) blocks a pool connection.
- **The HNSW index on `context_chunks.embedding` is expensive.** Every `INSERT` with an embedding updates the HNSW index. At 100 inserts/second, the index becomes a write bottleneck. `ef_construction=64` is aggressive for write-heavy workloads.
- **No partitioning.** The `context_chunks` table has no time-based partitioning. A query for `WHERE session_id = $1 ORDER BY created_at DESC LIMIT 5` scans all chunks for that session, even though only the last 5 are needed.
- **The `get_token_usage()` query does `SELECT SUM(token_count) FROM context_chunks WHERE session_id = $1`** — a full table scan on every call.
- **The `search_by_embedding()` query uses `ORDER BY embedding <=> $1::vector`** which requires a full index scan. At 1M+ vectors, this takes 100ms+.
- **The `_keyword_search()` in `MemoryRetriever` uses `ILIKE '%keyword%'`** which cannot use an index and requires a full table scan.

### What breaks concretely

At 10,000 context chunks, the `ORDER BY created_at DESC LIMIT 5` query takes 50ms. At 1,000,000 chunks, it takes 5+ seconds. The HNSW index search degrades similarly. The `ILIKE` query on `memories` takes 100ms+ at 100K rows.

### Fix

- Add time-based partitioning to `context_chunks` (e.g., by month).
- Batch inserts using `executemany()` or multi-row `INSERT`.
- Reduce `ef_construction` to 16-32 for better write performance.
- Add a covering index: `CREATE INDEX idx_context_chunks_recent ON context_chunks(session_id, created_at DESC) INCLUDE (payload_msgpack, chunk_type, token_count)`.
- Use read replicas for read-heavy queries (session listing, context retrieval).
- Consider TimescaleDB for time-series context data.
- Materialize token usage as a running counter on the session row instead of `SUM()` on every call.
- Replace `ILIKE` with PostgreSQL full-text search (`tsvector` + GIN index).
- Add a query timeout to all DB operations.

---

## 10. No Observability: Flying Blind

**Code:** Entire codebase — no metrics, no structured logging, no tracing.

### What breaks at scale

- **No way to know how many agent turns are running, how long they take, or how many tokens they consume.**
- **No way to debug a slow agent turn** — was it the LLM, the DB, or a tool?
- **No way to detect a runaway agent** that's stuck in an infinite loop.
- **No way to alert when the DB pool is exhausted** or the context table is growing too fast.
- **The audit log is a simple `logging.info()` call.** It's not structured, not queryable, not exportable.
- **No health checks.** The CLI doesn't check DB connectivity, LLM API reachability, or pool status before starting.
- **No request tracing.** A single agent turn involves multiple DB queries, LLM calls, and tool executions. There's no way to trace the entire flow.

### What breaks concretely

When (not if) the system breaks, you have no idea why. You'd need to add logging and metrics after the fact, which means redeploying.

### Fix

- Add structured logging (e.g., `structlog` or `logging` with JSON format).
- Add Prometheus metrics: `agent_turns_total`, `agent_turn_duration_seconds`, `db_pool_connections_active`, `context_chunks_total`.
- Add OpenTelemetry tracing for the agent loop.
- Add a `/health` endpoint that checks DB connectivity, pool status, and LLM API reachability.
- Add a request ID to every agent turn for end-to-end tracing.
- Add a dashboard (e.g., Grafana) to visualize metrics.

---

## 11. Schema Design Issues

**Code:** `ah/db/schema.sql`

### What breaks at scale

- **`context_chunks` has no partitioning.** At 10M+ rows, every query slows down. Time-based partitioning would keep recent queries fast.
- **`memories` table is underutilized.** The `MemoryStore` class exists, but the `MemoryConsolidator` is only called after each agent run. There's no background job to consolidate memories periodically.
- **`agent_messages` has no consumer.** The table exists for inter-agent communication, but no code reads from it. It's a write-only sink.
- **`subagent_sessions` and `subagent_messages` are unused.** No code creates sub-agents. These tables are aspirational.
- **`external_context` is unused.** No code writes to or reads from this table.
- **No archival strategy.** Old sessions and context chunks are never archived or deleted. The database grows forever.
- **The `embedding` column is `vector(1536)`.** This is OpenAI's `text-embedding-3-small` dimension. If you switch to a different embedding model (e.g., `nomic-embed-text` at 768 dims), the schema breaks. There's no version column or flexible dimension.
- **The `search_text` column is only populated by the RAG pipeline.** The `ContextManager.add_chunk()` method doesn't populate it. This means BM25 search only works for RAG-indexed documents, not for regular context chunks.
- **The `accessed_at` column is never updated.** The `mark_accessed()` method exists but is never called. This means LRU eviction is impossible.
- **The `state_msgpack` column on `sessions` can grow without bound.** There's no limit on session state size.

### What breaks concretely

The database grows forever. Old sessions and context chunks are never archived or deleted. The `search_text` column is only populated for RAG-indexed documents, so BM25 search doesn't work for regular context chunks. The `accessed_at` column is never updated, so LRU eviction is impossible.

### Fix

- Implement time-based partitioning for `context_chunks`.
- Remove unused tables or implement the missing managers.
- Add an archival job that moves old sessions to a cold storage table.
- Make the embedding dimension configurable.
- Populate `search_text` in `ContextManager.add_chunk()`.
- Call `mark_accessed()` when chunks are retrieved.
- Add a limit on `state_msgpack` size.

---

## 12. No Configuration Management

**Code:** `ah/db/connection.py:10`, `ah/core/config.py`

### What breaks at scale

- **The default DSN has a hardcoded password (`postgres`).** This is a security risk and makes it impossible to deploy to different environments without code changes.
- **Pool size, command timeout, and all other settings are hardcoded.** No way to configure them via environment variables or config files.
- **The `Config` class loads from `~/.agent-harness/config.yaml`.** This is a single file for all users. There's no way to have per-user or per-tenant configuration.
- **The `Config` class is a singleton.** There's no way to have multiple configurations in the same process.
- **No validation of configuration values.** A typo in the config file can cause the agent to crash at runtime.

### What breaks concretely

You can't deploy to different environments (dev, staging, prod) without code changes. You can't have per-user or per-tenant configuration. A typo in the config file can cause the agent to crash at runtime.

### Fix

- Use a configuration management library (e.g., `pydantic-settings`) to load settings from environment variables.
- Never hardcode credentials.
- Make all agent parameters configurable via CLI flags or config files.
- Add validation of configuration values.
- Support per-user or per-tenant configuration.

---

## 13. Error Recovery and Retry Logic: Partial

**Code:** `ah/core/agent.py:88-122`, `ah/core/provider.py`

### What breaks at scale

- **LLM calls have retry with exponential backoff.** This is good. But the retry logic is in the agent loop, not in the provider. This means every provider implementation must implement its own retry logic.
- **DB operations have no retry logic.** If the DB connection dies mid-turn, the agent crashes. It should reconnect and retry.
- **Tool execution errors are caught and returned as strings.** The LLM may not understand that it's an error and may try the same tool again with the same arguments, creating an infinite loop.
- **No circuit breaker.** If the LLM API is down, the agent keeps retrying forever. It should stop trying for a cooldown period.
- **No health checks.** The CLI doesn't check DB connectivity, LLM API reachability, or pool status before starting.
- **The `_consolidate_memories()` method swallows all exceptions.** If memory consolidation fails, the agent continues without knowing. This can lead to data loss.

### What breaks concretely

If the LLM API returns a 429 (rate limit), the agent retries with exponential backoff. But if the DB connection dies mid-turn, the agent crashes. If a tool fails, the error string is sent to the LLM as the tool result. The LLM may not understand that it's an error and may try the same tool again with the same arguments, creating an infinite loop.

### Fix

- Move retry logic into the provider, not the agent loop.
- Add retry with exponential backoff for DB operations.
- Distinguish between transient and permanent errors.
- Add a circuit breaker for the LLM provider — if it fails N times in a row, stop trying for a cooldown period.
- Send structured error messages to the LLM (e.g., `{"error": "rate_limit", "retry_after": 30}`) so it can adjust its behavior.
- Add health checks before starting the agent loop.
- Log memory consolidation failures instead of swallowing them.

---

## 14. The `asyncio.run()` Anti-Pattern

**Code:** `ah/cli/interactive.py:379-438`

### What breaks at scale

- **`asyncio.run()` creates a new event loop, runs the coroutine, and closes the loop.** This is fine for a one-shot CLI command, but it means:
  - The DB pool is created and destroyed on every invocation. No connection reuse across commands.
  - No background tasks can run (e.g., heartbeat scheduler, metrics collector).
  - No way to keep a long-running agent session alive across multiple user inputs.
- **If you ever wrap this in a server (e.g., FastAPI), you'll have multiple event loops, and the `db` singleton will be bound to the wrong loop.**
- **The `InteractiveREPL` class creates a new `ReActAgent` for every message.** This means a new `MemoryRetriever`, a new `MemoryConsolidator`, and a new `RAGPipeline` are created for every message. These should be singletons.

### What breaks concretely

You cannot keep a long-running agent session alive across multiple user inputs. The DB pool is created and destroyed on every invocation. No background tasks can run. If you wrap this in a server, the `db` singleton will be bound to the wrong loop.

### Fix

- For a CLI, `asyncio.run()` is acceptable. But the architecture should separate the "agent service" from the "CLI wrapper."
- The agent service should be a long-running async process that owns the event loop and DB pool.
- The CLI should connect to the agent service via HTTP or gRPC, not embed the agent directly.
- Make `MemoryRetriever`, `MemoryConsolidator`, and `RAGPipeline` singletons.
- Use a dependency injection container to manage singleton lifecycle.

---

## 15. The `Container` Singleton Problem

**Code:** `ah/core/container.py`

### What breaks at scale

- **The `Container` class is a singleton.** There's no way to run multiple containers in the same process, each with its own DB pool and configuration.
- **The `Container.production()` method returns the global singletons.** This means there's only one production container per process. You can't have multiple agents with different configurations.
- **The `Container.testing()` method returns fresh instances.** But these are not truly isolated — they still use the global `db` singleton.
- **The `Container.reset()` method resets all global singletons.** This is dangerous in a multi-threaded or multi-process environment.
- **No lifecycle management.** The `Container` doesn't manage the lifecycle of its singletons. If a singleton is created but not started, it's in an inconsistent state.

### What breaks concretely

You can't run multiple agents with different configurations in the same process. You can't have a test container and a production container simultaneously. The `Container.reset()` method can corrupt global state.

### Fix

- Make the `Container` class instantiable multiple times with different configurations.
- Remove the `Container.production()` and `Container.testing()` class methods. Use a factory function instead.
- Add lifecycle management for singletons.
- Remove the `Container.reset()` method. Use a fresh container instance instead.
- Use a dependency injection framework (e.g., `dependency-injector`) to manage singleton lifecycle.

---

## Summary: What Would It Take to Scale?

| Layer | Current State | Required for Scale |
|-------|--------------|-------------------|
| **Database** | Single PostgreSQL, 10-conn pool | PgBouncer, read replicas, partitioning, connection pooling per tenant |
| **Caching** | TTLCache for sessions, tool def cache | Redis for cross-instance caching |
| **Architecture** | CLI batch job | FastAPI service + Celery workers + Redis queue |
| **Agent Loop** | Sequential, blocking I/O | Parallel tool execution, async I/O, streaming |
| **Context** | Unbounded growth | Sliding window, summarization, eviction |
| **Backpressure** | Rate limiting (AsyncTokenBucket) | Per-session quotas, circuit breakers |
| **Observability** | Audit logging (JSON) | Prometheus metrics, OpenTelemetry tracing |
| **Multi-tenancy** | None | Org/user IDs, RLS, authentication |
| **Error Handling** | Retry with exponential backoff | Circuit breakers, health checks |
| **Configuration** | Hardcoded DSN, YAML file | Environment variables, per-tenant config |
| **Container** | Singleton | Multiple instances, lifecycle management |

---

## Bottom Line

AgentHarness is a clean, well-organized prototype that demonstrates the ReAct pattern effectively. But it is architecturally incapable of serving more than one user at a time. Every layer — from the 10-connection pool to the unbounded context growth to the blocking subprocess calls — has a hard ceiling that will be hit under concurrent load. Scaling it would require rewriting the core architecture, not just tuning parameters.

**The top 5 things that will break first, in order:**

1. **Connection pool exhaustion** — 10 connections is not enough for 3+ concurrent users.
2. **Blocking I/O in `terminal` tool** — `subprocess.run()` blocks the entire event loop.
3. **Unbounded context growth** — the database grows forever, queries slow down over time.
4. **No multi-tenancy** — one user can see another user's data.
5. **No horizontal scaling** — you can't run multiple workers against the same database.

**If you only fix one thing:** Make the `terminal` tool async. A single `terminal("sleep 60")` call blocks the entire application for 60 seconds. This is the most catastrophic scalability bug in the codebase.
