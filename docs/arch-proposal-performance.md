# Performance Architecture Proposal

**Author:** Performance Advocate  
**Date:** 2026-09-30  
**Status:** Draft

---

## Executive Summary

AgentHarness has **fundamental performance flaws** that will cause it to collapse under real workloads. The architecture treats PostgreSQL as a hot-path dependency for every micro-operation, uses a connection pool sized for a toy app, has zero caching, and executes synchronous I/O on the async event loop. This proposal identifies the bottlenecks and provides concrete fixes.

---

## 1. Connection Pool Tuning

### Current State

```python
# ah/db/connection.py
self._pool = await asyncpg.create_pool(
    self.dsn,
    min_size=2,
    max_size=10,
    command_timeout=30,
)
```

**Problems:**

| Issue | Impact |
|-------|--------|
| `min_size=2` | Cold-start latency. First 2 concurrent requests block until connections are established. |
| `max_size=10` | Hard ceiling. A single agent run with 5 tool calls + context retrieval can exhaust the pool. Concurrent CLI invocations will queue. |
| No `max_inactive_time` | Idle connections accumulate, wasting server resources. |
| No `setup` callback | Connections aren't validated or configured (e.g., `search_path`, `application_name`). |
| No `init` callback | No prepared statement pre-warming. |
| No `max_queries` | Connections are never recycled — memory leaks accumulate over time. |
| `command_timeout=30` | Too long for interactive queries. A stuck query blocks a pool slot for 30s. |

### Proposed Fix

```python
self._pool = await asyncpg.create_pool(
    self.dsn,
    min_size=5,              # Warm pool — no cold-start stalls
    max_size=20,             # Headroom for concurrent agents + CLI
    max_inactive_time=300,   # Recycle idle connections after 5 min
    max_queries=10_000,      # Recycle connections to prevent memory bloat
    command_timeout=10,      # Fail fast — interactive queries should be <1s
    max_cached_statement_lifetime=300,  # Refresh prepared statements
    setup=_setup_connection,  # Set application_name, search_path
    init=_init_connection,    # Pre-warm prepared statements
)

async def _setup_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        'json', encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
    )

async def _init_connection(conn: asyncpg.Connection) -> None:
    """Pre-warm frequently used prepared statements."""
    await conn.prepare("SELECT 1")  # Validate connection is alive
```

**Expected improvement:** 3-5× throughput under concurrent load; elimination of cold-start stalls.

---

## 2. Caching Layer

### Current State

**Zero caching.** Every operation hits PostgreSQL:

- `session_manager.get()` — fetches session from DB on every agent iteration
- `context_manager.get_recent_context()` — fetches context chunks from DB on every iteration
- `registry.get_tool_definitions()` — rebuilds tool definition list on every LLM call
- `session_manager.update_activity()` — writes to DB on every iteration

### Proposed Fix

#### 2.1 LRU Cache for Sessions

```python
from functools import lru_cache
from cachetools import TTLCache

class SessionManager:
    def __init__(self):
        self._cache: TTLCache = TTLCache(maxsize=128, ttl=30)  # 30s TTL

    async def get(self, session_id: uuid.UUID) -> Session | None:
        if session_id in self._cache:
            return self._cache[session_id]
        row = await db.fetchrow("SELECT ... FROM sessions WHERE id = $1", session_id)
        session = self._row_to_session(row) if row else None
        if session:
            self._cache[session_id] = session
        return session

    async def update_activity(self, session_id: uuid.UUID) -> None:
        # Fire-and-forget — don't block the agent loop
        asyncio.create_task(self._update_activity_async(session_id))
        # Invalidate cache
        self._cache.pop(session_id, None)
```

#### 2.2 Context Chunk Cache

```python
class ContextManager:
    def __init__(self):
        self._recent_cache: TTLCache = TTLCache(maxsize=64, ttl=10)

    async def get_recent_context(self, session_id: uuid.UUID, limit: int = 10):
        cache_key = (session_id, limit)
        if cache_key in self._recent_cache:
            return self._recent_cache[cache_key]
        rows = await db.fetch("SELECT ... FROM context_chunks WHERE ...")
        results = [...]
        self._recent_cache[cache_key] = results
        return results
```

#### 2.3 Tool Definition Cache

```python
class ToolRegistry:
    def __init__(self):
        self._tool_defs: list[ToolDefinition] | None = None

    def get_tool_definitions(self) -> list[ToolDefinition]:
        if self._tool_defs is None:
            self._tool_defs = [
                ToolDefinition(name=t.name, description=t.description, parameters=t.parameters)
                for t in self._tools.values()
            ]
        return self._tool_defs
```

**Expected improvement:** 10-100× reduction in DB round-trips for repeated access patterns; sub-millisecond session/context reads from cache.

---

## 3. Async Batching

### Current State

The agent loop does **N+1 writes** — each tool call result is inserted individually:

```python
# ah/core/agent.py — inside the tool execution loop
for tc in response.tool_calls:
    # ... execute tool ...
    await context_manager.add_chunk(...)  # ← Individual INSERT per tool call
```

A single agent run with 5 tool calls generates 5 separate INSERT round-trips. Each round-trip is ~1-2ms on localhost, ~5-10ms over network.

### Proposed Fix

#### 3.1 Batch Context Chunk Insertion

```python
class ContextManager:
    async def add_chunks_batch(self, chunks: list[ContextChunk]) -> None:
        """Insert multiple context chunks in a single round-trip."""
        if not chunks:
            return
        # Use asyncpg's copy_records_to_table or executemany
        async with db.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                [
                    (c.session_id, c.agent_id, c.chunk_type,
                     msgpack.packb(c.payload, use_bin_type=True),
                     c.token_count, _format_embedding(c.embedding))
                    for c in chunks
                ],
            )
```

#### 3.2 Deferred Write Queue

```python
class WriteQueue:
    """Batches writes and flushes periodically or on threshold."""

    def __init__(self, flush_interval: float = 0.5, max_batch: int = 20):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._flush_interval = flush_interval
        self._max_batch = max_batch
        self._task = asyncio.create_task(self._flush_loop())

    async def enqueue(self, chunk: ContextChunk):
        await self._queue.put(chunk)

    async def _flush_loop(self):
        while True:
            batch = []
            try:
                # Wait for first item
                item = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
                batch.append(item)
                # Collect more items without blocking
                while len(batch) < self._max_batch:
                    try:
                        item = self._queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                continue
            if batch:
                await context_manager.add_chunks_batch(batch)
```

**Expected improvement:** 5-10× reduction in DB write round-trips; agent loop iterations become CPU-bound instead of I/O-bound.

---

## 4. Lazy Loading

### Current State

#### 4.1 Eager Payload Deserialization

```python
# ah/core/context.py
def _row_to_chunk(self, row: asyncpg.Record) -> ContextChunk:
    payload = msgpack.unpackb(row["payload_msgpack"], raw=False)  # ← Always deserializes
    embedding = None
    if row["embedding"] is not None:
        embedding = [float(x) for x in str(row["embedding"]).strip("[]").split(",")]  # ← Always parses
    return ContextChunk(...)
```

Every `get_recent_context()` call deserializes the full MessagePack payload and parses the embedding vector — even though the prompt assembler only needs the payload text and never uses the embedding.

#### 4.2 Eager Session State Loading

```python
# ah/core/session.py
async def get(self, session_id: uuid.UUID) -> Session | None:
    row = await db.fetchrow(
        "SELECT id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, created_at, last_activity FROM sessions WHERE id = $1",
        session_id,
    )
    return self._row_to_session(row) if row else None
```

The `state_msgpack` column can be large (LangGraph checkpoints), but it's fetched on every `session_manager.get()` call — even when the caller only needs `context_budget` or `goal`.

#### 4.3 Eager Tool Definition Construction

```python
# ah/tools/registry.py
def get_tool_definitions(self) -> list[ToolDefinition]:
    return [
        ToolDefinition(name=t.name, description=t.description, parameters=t.parameters)
        for t in self._tools.values()
    ]
```

New `ToolDefinition` objects are allocated on every call — the underlying data never changes.

### Proposed Fix

#### 4.1 Lazy Payload Deserialization

```python
class ContextChunk:
    id: uuid.UUID
    session_id: uuid.UUID
    agent_id: str
    chunk_type: str
    _payload_msgpack: bytes | None = None  # Raw bytes, lazily deserialized
    _payload: dict | None = None
    token_count: int = 0
    _embedding_str: str | None = None
    _embedding: list[float] | None = None

    @property
    def payload(self) -> dict[str, Any]:
        if self._payload is None and self._payload_msgpack is not None:
            self._payload = msgpack.unpackb(self._payload_msgpack, raw=False)
        return self._payload or {}

    @property
    def embedding(self) -> list[float] | None:
        if self._embedding is None and self._embedding_str is not None:
            self._embedding = [float(x) for x in self._embedding_str.strip("[]").split(",")]
        return self._embedding
```

#### 4.2 Column Projection for Sessions

```python
async def get(self, session_id: uuid.UUID, columns: list[str] | None = None) -> Session | None:
    if columns is None:
        columns = ["id", "title", "agent_id", "status", "goal", "model", "provider", "context_budget", "created_at", "last_activity"]
    # Only fetch requested columns — skip state_msgpack unless needed
    col_str = ", ".join(columns)
    row = await db.fetchrow(f"SELECT {col_str} FROM sessions WHERE id = $1", session_id)
    return self._row_to_session(row) if row else None
```

#### 4.3 Cached Tool Definitions

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._tool_defs_cache: list[ToolDefinition] | None = None

    def get_tool_definitions(self) -> list[ToolDefinition]:
        if self._tool_defs_cache is None:
            self._tool_defs_cache = [
                ToolDefinition(name=t.name, description=t.description, parameters=t.parameters)
                for t in self._tools.values()
            ]
        return self._tool_defs_cache

    def register(self, ...):
        # ... existing logic ...
        self._tool_defs_cache = None  # Invalidate cache on new registration
```

**Expected improvement:** 2-5× reduction in CPU time per agent iteration; 50% reduction in memory allocation.

---

## 5. Query Optimization

### Current State

#### 5.1 `get_recent_context` Fetches Unnecessary Columns

```python
rows = await db.fetch(
    """
    SELECT payload_msgpack, chunk_type, token_count
    FROM context_chunks
    WHERE session_id = $1
    ORDER BY created_at DESC
    LIMIT $2
    """,
    session_id, limit,
)
```

This fetches `payload_msgpack` (potentially large) for every chunk, but the prompt assembler only needs the payload for the last 3 chunks. The query also doesn't use the `idx_context_chunks_session` index efficiently because `created_at` is not the leading column in the index.

#### 5.2 `search_by_embedding` Does `SELECT *`

```python
rows = await db.fetch(
    """
    SELECT *, 1 - (embedding <=> $1::vector) AS similarity
    FROM context_chunks
    WHERE session_id = $2 AND embedding IS NOT NULL
    ORDER BY embedding <=> $1::vector
    LIMIT $3
    """,
    ...
)
```

`SELECT *` fetches the full `payload_msgpack` and `embedding` vector for every result — but the caller only needs the chunk ID and similarity score for filtering.

#### 5.3 No Pagination on `list_sessions`

```python
rows = await db.fetch(
    """
    SELECT id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, created_at, last_activity
    FROM sessions
    ORDER BY last_activity DESC
    LIMIT $1
    """,
    limit,
)
```

Fetches `state_msgpack` for every session — this can be megabytes of data per session.

#### 5.4 Missing Composite Index

The index `idx_context_chunks_session` is on `(session_id, created_at DESC)`, but the query filters by `session_id` and orders by `created_at DESC` — this is actually fine. However, there's no index on `(session_id, chunk_type, created_at DESC)` for the filtered query.

### Proposed Fix

#### 5.1 Optimized `get_recent_context`

```python
async def get_recent_context(self, session_id: uuid.UUID, limit: int = 10) -> list[dict[str, Any]]:
    """Get recent context — only fetch payload for the most recent chunks."""
    rows = await db.fetch(
        """
        SELECT id, chunk_type, token_count, payload_msgpack
        FROM context_chunks
        WHERE session_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        session_id, limit,
    )
    results = []
    for r in rows:
        # Only deserialize payload for the first 3 chunks (used by prompt assembler)
        if len(results) < 3:
            payload = msgpack.unpackb(r["payload_msgpack"], raw=False)
        else:
            payload = None  # Lazy — don't deserialize if not needed
        results.append({
            "id": r["id"],
            "type": r["chunk_type"],
            "payload": payload,
            "tokens": r["token_count"],
        })
    return results
```

#### 5.2 Optimized `search_by_embedding`

```python
async def search_by_embedding(self, session_id, query_embedding, top_k=5, threshold=0.7):
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
    rows = await db.fetch(
        """
        SELECT id, chunk_type, 1 - (embedding <=> $1::vector) AS similarity
        FROM context_chunks
        WHERE session_id = $2 AND embedding IS NOT NULL
        ORDER BY embedding <=> $1::vector
        LIMIT $3
        """,
        embedding_str, session_id, top_k,
    )
    # Only fetch full chunks for results above threshold
    results = []
    for row in rows:
        if row["similarity"] < threshold:
            continue
        # Fetch full chunk only if needed
        chunk = await self.get_chunk(row["id"])
        results.append((chunk, row["similarity"]))
    return results
```

#### 5.3 Paginated `list_sessions` with Column Projection

```python
async def list_sessions(self, status=None, limit=20, offset=0, include_state=False):
    columns = "id, title, agent_id, status, goal, model, provider, context_budget, created_at, last_activity"
    if include_state:
        columns += ", state_msgpack"
    # ... fetch with columns ...
```

#### 5.4 Add Missing Indexes

```sql
-- Composite index for filtered context queries
CREATE INDEX IF NOT EXISTS idx_context_chunks_session_type_created
    ON context_chunks(session_id, chunk_type, created_at DESC);

-- Covering index for session list queries
CREATE INDEX IF NOT EXISTS idx_sessions_status_activity_covering
    ON sessions(status, last_activity DESC)
    INCLUDE (id, title, agent_id, goal, model, provider, context_budget);

-- Partial index for active sessions only
CREATE INDEX IF NOT EXISTS idx_sessions_active
    ON sessions(last_activity DESC)
    WHERE status = 'active';
```

**Expected improvement:** 2-5× faster queries; 50% reduction in data transferred from DB; index-only scans for common access patterns.

---

## 6. Additional Performance Issues

### 6.1 Synchronous I/O on the Async Event Loop

The `terminal` tool uses `subprocess.run()` (blocking) and `web_search`/`web_extract` use `httpx.get()` (blocking). These block the entire event loop, stalling all concurrent async operations.

**Fix:** Use `asyncio.create_subprocess_exec()` and `httpx.AsyncClient` (already used in providers, but not in tools).

### 6.2 No Connection Pool Health Monitoring

There's no metrics collection on pool utilization, wait times, or query latency. You can't optimize what you can't measure.

**Fix:** Add a metrics middleware that tracks:
- Pool wait time (time spent waiting for a connection)
- Query execution time
- Cache hit/miss ratios

### 6.3 MessagePack Serialization is CPU-Bound

Every context chunk read/write does `msgpack.packb()` / `msgpack.unpackb()`. For large payloads, this is expensive.

**Fix:** Consider `orjson` for JSON-serializable payloads (2-3× faster than msgpack for JSON-compatible data), or cache the serialized form.

---

## Summary of Expected Improvements

| Optimization | Metric | Improvement |
|---|---|---|
| Connection pool tuning | Concurrent throughput | 3-5× |
| Caching layer | DB round-trips | 10-100× reduction |
| Async batching | Write latency | 5-10× reduction |
| Lazy loading | CPU per iteration | 2-5× reduction |
| Query optimization | Query latency | 2-5× faster |
| **Combined** | **Agent loop iteration time** | **10-50× faster** |

---

## Implementation Priority

1. **P0 — Connection pool tuning** (1 hour, immediate impact)
2. **P0 — Tool definition cache** (30 minutes, trivial change)
3. **P1 — Session LRU cache** (2 hours, high impact)
4. **P1 — Lazy payload deserialization** (2 hours, high impact)
5. **P1 — Query optimization + indexes** (2 hours, high impact)
6. **P2 — Async batching for context writes** (4 hours, medium impact)
7. **P2 — Context chunk cache** (3 hours, medium impact)
8. **P3 — Write queue** (4 hours, architectural change)
9. **P3 — Metrics collection** (4 hours, operational visibility)
