# Performance Fixes Verification

**Date:** 2026-10-02  
**Commit:** f6501c7 (P2 fixes: session management, context compression, skills, memory approval, docs)  
**Tests:** 464 passing

## Summary

| # | Fix | Status | Notes |
|---|-----|--------|-------|
| 1 | Context chunk caching | ✅ Verified | LRU cache in ContextManager |
| 2 | RAG result caching | ✅ Verified | TTL cache in RAGPipeline |
| 3 | Tool result caching | ✅ Verified | TTL cache in ToolRegistry |
| 4 | Prompt caching | ❌ Not implemented | No prompt cache exists |
| 5 | Budget enforcement | ✅ Verified | Token budget checked in agent loop |
| 6 | Accurate token counting | ✅ Verified | tiktoken with cl100k_base |
| 7 | Tool result truncation increased | ✅ Verified | 500/200/1000 char limits |
| 8 | Connection pool configurable | ✅ Verified | Env vars for min/max/timeout |
| 9 | HTTP clients consolidated | ⚠️ Partial | Per-provider clients, not shared |
| 10 | Prepared statements | ✅ Verified | asyncpg prepared statement cache |
| 11 | Eviction policies | ✅ Verified | LRU + TTL across all caches |
| 12 | Parameterized queries | ✅ Verified | All queries use $1, $2, ... |

---

## Detailed Findings

### 1. Context Chunk Caching ✅

**File:** `ah/core/context.py`

**Implementation:**
- `ContextManager.__init__()` creates `self._recent_cache: OrderedDict[uuid.UUID, list[dict]]` with `cache_size=128`
- `get_recent_context()` checks cache first, returns cached results if available
- Cache is invalidated on `add_chunk()` and `add_chunks_batch()` via `self._recent_cache.pop(session_id, None)`
- LRU eviction: `self._recent_cache.popitem(last=False)` when cache exceeds max size
- Cache is also invalidated in `evict_old_chunks()`

**Verdict:** Correct and complete.

---

### 2. RAG Result Caching ✅

**File:** `ah/rag/pipeline.py`

**Implementation:**
- `RAGPipeline` has class-level `_search_cache: dict[tuple, tuple[float, list[SearchResult]]]`
- Cache key: `(session_id, hashlib.sha256(query.encode()).hexdigest())`
- TTL: 300 seconds (5 minutes)
- Max size: 256 entries
- Eviction: oldest entry removed when cache is full (`min(self._search_cache, key=...)`)
- Cache checked at start of `search()`, results stored after retrieval

**Verdict:** Correct and complete.

---

### 3. Tool Result Caching ✅

**File:** `ah/tools/base.py`

**Implementation:**
- `ToolRegistry.__init__()` creates `self._result_cache: dict[tuple[str, str], tuple[float, Any]]`
- Cache key: `(tool_name, sha256(json.dumps(kwargs, sort_keys=True, default=str)))`
- TTL: 60 seconds (`self._cache_ttl = 60.0`)
- Max size: 256 entries
- Side-effect tools excluded: `name.startswith(("write_", "delete_", "create_", "update_", "send_", "post_"))`
- Eviction: oldest entry removed when cache is full

**Verdict:** Correct and complete.

---

### 4. Prompt Caching ❌

**File:** `ah/core/assembler.py`

**Finding:** No prompt caching mechanism exists. The `PromptAssembler.assemble()` method rebuilds the prompt from scratch on every call. There is no cache of assembled prompts.

**Impact:** Prompts are assembled on every agent iteration. For long conversations, this means re-serializing the same context chunks repeatedly.

**Recommendation:** Consider adding a prompt cache keyed on (session_id, last_chunk_id, query_hash) to avoid re-assembling prompts when context hasn't changed between iterations.

**Verdict:** Not implemented.

---

### 5. Budget Enforcement ✅

**File:** `ah/core/agent.py`

**Implementation:**
- `MAX_TOKEN_BUDGET = 50_000` (fallback constant)
- `ReActAgent.run()`: checks `if total_tokens >= MAX_TOKEN_BUDGET` before each LLM call
- `ReActAgent.run_stream()`: uses `effective_budget = min(session.context_budget, MAX_TOKEN_BUDGET)`
- Budget exceeded triggers audit log and early return with partial results
- `session.context_budget` is the per-session budget (default 8000)

**Verdict:** Correct and complete.

---

### 6. Accurate Token Counting ✅

**File:** `ah/core/assembler.py`

**Implementation:**
- `TokenCounter` class uses `tiktoken.get_encoding("cl100k_base")`
- `count()` method: `len(self._encoding.encode(text))` when tiktoken available
- Fallback: `len(text) // 4` when tiktoken not available
- Global `_token_counter = TokenCounter()` singleton
- `get_token_count(text)` function used throughout the codebase

**Verdict:** Correct and complete.

---

### 7. Tool Result Truncation Increased ✅

**File:** `ah/core/agent.py`

**Implementation:**
- `result_preview` in context storage: `result_str[:500]` (500 chars)
- `result_preview` in tool_calls_made: `result_str[:200]` (200 chars)
- Message content for LLM: `result_str[:1000]` (1000 chars)
- Verbose console output: `result_str[:200]`

**Verdict:** Correct and complete.

---

### 8. Connection Pool Configurable ✅

**File:** `ah/db/connection.py`

**Implementation:**
- `Database.connect()` reads environment variables:
  - `AGENT_HARNESS_DB_MIN_POOL` (default: "2")
  - `AGENT_HARNESS_DB_MAX_POOL` (default: "10")
  - `AGENT_HARNESS_DB_TIMEOUT` (default: "30")
- Passed to `asyncpg.create_pool(min_size=..., max_size=..., command_timeout=...)`

**Verdict:** Correct and complete.

---

### 9. HTTP Clients Consolidated ⚠️

**Files:** `ah/core/provider.py`, `ah/rag/embedder.py`, `ah/rag/reranker.py`, `ah/tools/builtins.py`

**Finding:** Each component creates its own `httpx.AsyncClient`:
- `OpenRouterProvider`: `httpx.AsyncClient(base_url=..., headers=..., timeout=120.0)`
- `OllamaProvider`: `httpx.AsyncClient(base_url=..., timeout=120.0)`
- `OpenAIEmbedder`: `httpx.AsyncClient(base_url=..., headers=..., timeout=30.0)`
- `Reranker`: `httpx.AsyncClient(...)`
- `ah/tools/builtins.py`: Uses `httpx.get()` (synchronous, one-off)
- `ah/cli/__init__.py`: Uses `httpx.get()` (synchronous, one-off)

**Impact:** Multiple HTTP clients means multiple connection pools. For a single-user agent this is acceptable, but consolidating would reduce connection overhead.

**Verdict:** Partial — each provider reuses its own client, but clients are not shared across providers.

---

### 10. Prepared Statements ✅

**File:** `ah/db/connection.py`

**Implementation:**
- `Database.fetchval_cached()`: Uses asyncpg's automatic prepared statement cache
- `Database.execute_prepared()`: Uses asyncpg's prepared statement cache
- `Database.fetch_prepared()`: Uses asyncpg's prepared statement cache
- Batch inserts use `executemany()` in `ContextManager.add_chunks_batch()` and `RAGPipeline.index_session_context()`
- All queries use parameterized `$1, $2, ...` syntax

**Verdict:** Correct and complete.

---

### 11. Eviction Policies ✅

**Files:** Multiple

**Implementation:**

| Cache | Policy | Location |
|-------|--------|----------|
| Context recent cache | LRU (OrderedDict) | `ah/core/context.py` |
| RAG search cache | TTL + max size | `ah/rag/pipeline.py` |
| Tool result cache | TTL + max size | `ah/tools/base.py` |
| Session cache | TTL (60s) | `ah/core/session.py` |
| Embedding cache | LRU (OrderedDict) | `ah/rag/embedder.py` |
| Context chunk eviction | LRU (preserve recent 10) | `ah/core/context.py` |
| Memory eviction | Importance threshold | `ah/memory/store.py` |

**Verdict:** Correct and complete.

---

### 12. Parameterized Queries ✅

**Files:** All files with SQL queries

**Implementation:**
- All SQL queries use `$1, $2, ...` parameterized format
- No string interpolation of user data into SQL
- Dynamic WHERE clauses built with parameterized conditions
- `ILIKE` patterns use parameterized `f"%{kw}%"` with `$param_idx`

**Verdict:** Correct and complete.

---

## Issues Found

### 1. Prompt Caching Not Implemented
- **Severity:** Medium
- **Impact:** Repeated prompt assembly for unchanged context
- **Recommendation:** Add prompt cache with invalidation on context change

### 2. HTTP Clients Not Fully Consolidated
- **Severity:** Low
- **Impact:** Multiple connection pools (acceptable for single-user)
- **Recommendation:** Consider shared HTTP client for production deployments

---

## Test Coverage

Existing tests cover:
- Token counting (`test_estimate_tokens`, `test_estimate_tokens_unicode`)
- Context compression (`test_compress_with_truncation`, `test_compress_llm_fallback_to_truncation`)
- Database pool (`test_database_pool_not_connected`)
- Agent budget handling (implicit in chaos tests)

Missing tests for:
- Context chunk cache hit/miss
- RAG search cache hit/miss
- Tool result cache hit/miss
- Connection pool configuration
- Prepared statement usage
- Eviction policies

---

## Conclusion

**10 of 12 fixes are correctly implemented.**  
**1 fix (prompt caching) is not implemented.**  
**1 fix (HTTP client consolidation) is partially implemented.**

The performance fixes that are present follow best practices:
- LRU caches with proper invalidation
- TTL caches with max size limits
- Parameterized queries throughout
- Configurable connection pooling
- Accurate token counting with tiktoken
