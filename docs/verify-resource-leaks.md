# Verification: Resource Leak & Race Condition Fixes

**Date:** 2026-10-02  
**Scope:** 9 fixes across 7 files  
**Result:** All 9 fixes verified correct

---

## 1. HTTP Clients Closed in `provider.py` ✅

**File:** `ah/core/provider.py`

Both `OpenRouterProvider` and `OllamaProvider` implement an async `close()` method that calls `self.client.aclose()` on the shared `httpx.AsyncClient`. This ensures connection pools are properly released when providers are shut down.

- `OpenRouterProvider.close()` — line 428-430
- `OllamaProvider.close()` — line 629-631

**Verdict:** Correct. Both providers clean up their HTTP client resources.

---

## 2. PromptSession Closed in `interactive.py` ✅

**File:** `ah/cli/interactive.py`

The `InteractiveREPL.run()` method has a `finally` block (lines 439-444) that:
1. Sets `self._running = False`
2. Checks `hasattr(self._prompt_session, 'close')` before calling `self._prompt_session.close()`
3. Closes the database connection

This ensures the `PromptSession` (which holds terminal resources and file handles for history) is properly released on exit.

**Verdict:** Correct. PromptSession is closed in the finally block.

---

## 3. Metrics Lists Bounded in `metrics.py` ✅

**File:** `ah/core/metrics.py`

The `MetricsCollector` class defines `_MAX_LATENCY_SAMPLES = 10_000` (line 41). The `record_latency` method (lines 52-60) appends to the latency list and then trims it when it exceeds the cap:

```python
if len(latencies) > self._MAX_LATENCY_SAMPLES:
    self._latencies[operation] = latencies[-self._MAX_LATENCY_SAMPLES // 2:]
```

This keeps the most recent 5,000 samples, preventing unbounded memory growth.

**Verdict:** Correct. Latency lists are capped at 10,000 entries (trimmed to 5,000).

---

## 4. TTLCache Async-Safe in `session.py` ✅

**File:** `ah/core/session.py`

The `SessionManager` uses an `asyncio.Lock` (`self._cache_lock`) to protect all cache operations:

- `_cache_get()` — acquires lock before `self._cache.get()`
- `_cache_put()` — acquires lock before `self._cache[session.id] = session`
- `_cache_invalidate()` — acquires lock before `self._cache.pop()`

This prevents race conditions when multiple coroutines access the TTLCache concurrently.

**Verdict:** Correct. All cache operations are protected by an async lock.

---

## 5. `record_interaction` Race Fixed in `user_profile.py` ✅

**File:** `ah/memory/user_profile.py`

The `UserProfileStore.record_interaction()` method (lines 232-290) uses `async with self._lock` to ensure atomic read-modify-write of the `topics` and `last_topics` fields. Without this lock, concurrent calls could lose updates (e.g., two interactions with the same topic could both read the same `topics` dict, increment it, and one write would clobber the other).

**Verdict:** Correct. The lock prevents lost-update races on profile interaction data.

---

## 6. Global `_rag_pipeline` Thread-Safe ✅

**File:** `ah/tools/rag.py`

The `get_rag_pipeline()` function (lines 21-29) uses double-checked locking:

```python
if _rag_pipeline is None:
    async with _rag_pipeline_lock:
        if _rag_pipeline is None:
            _rag_pipeline = RAGPipeline()
```

The first check avoids lock contention in the common case. The second check (inside the lock) prevents duplicate initialization when multiple coroutines race.

**Verdict:** Correct. Double-checked locking pattern is properly implemented.

---

## 7. `str_to_embedding` Validates Input ✅

**File:** `ah/core/serialization.py`

The `str_to_embedding()` function (lines 63-86):
- Strips whitespace and brackets
- Returns `[]` for empty strings
- Splits on commas and parses each part as float
- Raises `ValueError` with a descriptive message on malformed input (e.g., non-numeric values)

**Verdict:** Correct. Input is validated and malformed data raises a clear error.

---

## 8. `read_file` Offset=0 Guard ✅

**File:** `ah/tools/file.py`

The `read_file()` function (lines 51-74) validates parameters at the top:

```python
if offset < 1:
    raise ToolError(f"offset must be >= 1, got {offset}")
if limit < 1:
    raise ToolError(f"limit must be >= 1, got {limit}")
```

This prevents `offset=0` from causing incorrect slicing behavior (since the function uses 1-based indexing internally: `lines[offset - 1:end]`).

**Verdict:** Correct. Offset and limit are validated before use.

---

## 9. `_compress_context` Fallback ✅

**File:** `ah/core/compression.py`

The `ContextCompressor.compress()` method (lines 90-187) implements a multi-level fallback strategy:

1. **LLM summarization** (preferred) — if `llm_summarize=True` and `llm_provider` is available
2. **Truncation** — if LLM summarization fails or is disabled
3. **Preserve original** — if truncation also fails, the original chunks are kept to avoid context loss

Each level is wrapped in try/except, and the final fallback ensures no data is lost even if all compression methods fail.

**Verdict:** Correct. Graceful degradation from LLM → truncate → preserve original.

---

## Summary

| # | Fix | File | Status |
|---|-----|------|--------|
| 1 | HTTP clients closed | `provider.py` | ✅ Correct |
| 2 | PromptSession closed | `interactive.py` | ✅ Correct |
| 3 | Metrics lists bounded | `metrics.py` | ✅ Correct |
| 4 | TTLCache async-safe | `session.py` | ✅ Correct |
| 5 | record_interaction race | `user_profile.py` | ✅ Correct |
| 6 | _rag_pipeline thread-safe | `rag.py` | ✅ Correct |
| 7 | str_to_embedding validates | `serialization.py` | ✅ Correct |
| 8 | read_file offset=0 guard | `file.py` | ✅ Correct |
| 9 | _compress_context fallback | `compression.py` | ✅ Correct |

**All 9 fixes are correctly implemented.**
