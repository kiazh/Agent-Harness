# AgentHarness — Brutal Code Quality Audit

**Date:** 2026-10-01  
**Scope:** Full codebase (9,419 LOC across 33 Python files + 1 SQL schema)  
**Verdict:** Functional but riddled with quality issues that will compound as the codebase grows.

---

## 1. Naming

### 1.1 Inconsistent Naming Conventions

| Issue | Location | Detail |
|-------|----------|--------|
| `snake_case` vs `camelCase` mix | `ah/rag/chunker.py` | `RecursiveCharacterTextSplitter` (PascalCase class) but `split_text`, `split_markdown`, `split_code` (snake_case methods) — acceptable, but `is_separator_regex` is a boolean flag that reads like a question. Rename to `use_regex_separators`. |
| Abbreviated names | `ah/core/provider.py` | `_TIKTOKEN_AVAILABLE` — fine. But `tc` (tool call), `rm` (retrieved memory), `rr` (rerank result) are cryptic loop variables used throughout. |
| Misleading names | `ah/tools/base.py` | `get_tool_names()` is an alias for `list_tools()` — two names for the same thing. |
| Inconsistent prefix | `ah/core/models.py` | `ToolCall` has `function_name` + `function_arguments` + `arguments` — three fields for the same concept. `function_arguments` is a JSON string, `arguments` is a parsed dict. This is a naming disaster. |
| `Optional` vs `\| None` | Multiple files | `ah/core/agent.py` uses `Optional` from typing; `ah/core/session.py` uses `Optional`; but `ah/core/models.py` uses `\| None`. Inconsistent. |
| Boolean flags | `ah/core/agent.py` | `verbose: bool = True` — should be `verbose: bool = False` (quiet by default). The name is fine but the default is wrong for a "verbose" flag. |

### 1.2 Poor Variable Names

```python
# ah/core/agent.py:326
function_data = tc.get("function", {})
tool_name = function_data.get("name")
```
`tc` is a tool call dict, not a self-documenting name. Should be `tool_call`.

```python
# ah/memory/retriever.py:207
entry = self.store._row_to_entry(row)
```
Accessing a private method `_row_to_entry` from outside the class. This is a naming/encapsulation violation.

---

## 2. Documentation

### 2.1 Missing Docstrings

| File | Missing |
|------|---------|
| `ah/db/connection.py` | `Database.fetchval()` has no return type annotation or docstring |
| `ah/tools/terminal.py` | `ALLOWED_COMMANDS` has no docstring explaining the security model |
| `ah/tools/builtins.py` | `_is_safe_url()` has a docstring but `web_search()` and `web_extract()` have minimal ones |
| `ah/cli/animations.py` | 930 lines of animation code, most classes have one-line docstrings at best |
| `ah/cli/visual.py` | `VisualContext` methods have no docstrings |

### 2.2 Inadequate Docstrings

```python
# ah/core/context.py:23
async def add_chunk(self, ...) -> ContextChunk:
    """Add a context chunk."""
```
This docstring is useless. It doesn't explain what a "context chunk" is, what the parameters mean, or what happens on failure.

```python
# ah/core/provider.py:34
def audit_log(event_type: str, **kwargs) -> None:
    """Log a security-relevant event as JSON."""
```
Doesn't explain the audit log format, where it goes, or what events are logged.

### 2.3 Outdated/Incorrect Docstrings

```python
# ah/core/provider.py:15
from ah.core.models import LLMResponse, StreamEvent, ToolDefinition  # noqa: F401 — re-exported for backward compat
```
The comment says "re-exported for backward compat" but this is a code smell — it's a circular dependency workaround, not a re-export.

---

## 3. Type Hints

### 3.1 Missing Type Hints

```python
# ah/db/connection.py:65
async def fetchval(self, query: str, *args):
```
No return type. Should be `-> Any` or a more specific type.

```python
# ah/tools/base.py:162
async def execute(self, name: str, **kwargs) -> Any:
```
`Any` is a cop-out. Should be `-> str | dict | list` or a union of possible return types.

```python
# ah/cli/interactive.py:181
def get_completions(self, document: Document, complete_event) -> list[Completion]:
```
`complete_event` has no type hint.

### 3.2 Incorrect Type Hints

```python
# ah/core/models.py:50
arguments: dict = field(default_factory=dict)
```
`dict` without type parameters. Should be `dict[str, Any]`.

```python
# ah/core/models.py:69
parameters: dict  # JSON Schema
```
Same issue. Should be `dict[str, Any]`.

```python
# ah/core/models.py:80
tool_calls: list[dict] = field(default_factory=list)
```
Should be `list[dict[str, Any]]`.

### 3.3 `Any` Overuse

The codebase uses `Any` excessively:
- `ah/core/context.py`: `payload: dict[str, Any]` — acceptable
- `ah/core/agent.py`: `tool_calls: list[dict[str, Any]]` — acceptable
- `ah/tools/base.py`: `-> Any` — lazy
- `ah/rag/search.py`: `db: Any` — should be `Database`
- `ah/cli/visual.py`: `content: Any` — should be `str | Text | Markdown`

---

## 4. Error Handling

### 4.1 Bare `except Exception` — Pervasive

This is the single worst quality issue in the codebase. There are **47 instances** of bare `except Exception` across the codebase.

```python
# ah/tools/builtins.py:89
except Exception:
    pass
```
Silently swallowing all exceptions in `web_search()`. If the SearXNG server returns malformed JSON, the error is invisible.

```python
# ah/memory/retriever.py:112
except Exception:
    pass  # Don't fail retrieval due to access update failure
```
This one at least has a comment, but it's still dangerous — a database connection error would be silently ignored.

```python
# ah/core/agent.py:84
except Exception as e:
    logger.warning("RAG retrieval failed: %s", e)
    return []
```
Better — at least it logs. But it catches `Exception` instead of specific exceptions.

### 4.2 Inconsistent Error Handling Patterns

| Pattern | Locations | Problem |
|---------|-----------|---------|
| Return error string | `ah/tools/file.py`, `ah/tools/terminal.py`, `ah/tools/builtins.py` | Mixing return values with exceptions. Caller must check `if result.startswith("Error:")`. |
| Raise exception | `ah/core/provider.py`, `ah/core/session.py` | Proper, but inconsistent with the above. |
| Log and continue | `ah/core/agent.py`, `ah/memory/retriever.py` | Silent failures. |
| `raise last_exception  # type: ignore[misc]` | `ah/core/agent.py:122,160` | Suppressing type checker instead of fixing the issue. |

### 4.3 Missing Error Handling

```python
# ah/core/context.py:37
row = await db.fetchrow(...)
return self._row_to_chunk(row)
```
If `db.fetchrow` returns `None` (no row inserted), `_row_to_chunk(None)` will raise `TypeError`. No check.

```python
# ah/memory/store.py:44
row = await db.fetchrow(...)
memory = self._row_to_entry(row)
```
Same issue.

### 4.4 `type: ignore` Comments

```python
# ah/core/agent.py:122
raise last_exception  # type: ignore[misc]
```
This suppresses a type checker warning instead of fixing the root cause. The proper fix is to assert or cast.

---

## 5. Logging

### 5.1 Inconsistent Logger Setup

```python
# ah/core/provider.py:24-31
_audit_logger = logging.getLogger("ah.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [AUDIT] %(message)s"
    ))
    _audit_logger.addHandler(_handler)
```
This is the only place with a custom log handler. All other modules use `logger = logging.getLogger(__name__)` with no configuration. The audit logger writes to stdout, not stderr, and has no way to disable it.

### 5.2 Inconsistent Log Levels

| Level | Usage |
|-------|-------|
| `logger.debug` | `ah/memory/consolidator.py:86` — "No chunks to consolidate" |
| `logger.info` | `ah/memory/consolidator.py:156` — "Consolidated session..." |
| `logger.warning` | `ah/core/agent.py:85` — "RAG retrieval failed" |
| `logger.error` | `ah/core/agent.py:278` — "LLM call ultimately failed" |
| `logger.exception` | `ah/core/agent.py:394` — "Tool execution failed" |

The levels are mostly appropriate, but `logger.exception` is only used once (for tool execution), while other equally important errors use `logger.error` without traceback.

### 5.3 Missing Log Context

```python
# ah/core/agent.py:248
logger.warning(
    "Token budget exceeded (%d >= %d) — stopping agent loop",
    total_tokens,
    MAX_TOKEN_BUDGET,
)
```
This log message doesn't include the `session_id`, making it impossible to trace which session hit the budget.

### 5.4 Audit Log Pollution

```python
# ah/core/provider.py:34
def audit_log(event_type: str, **kwargs) -> None:
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **kwargs,
    }
    _audit_logger.info(json.dumps(entry, default=str))
```
The audit log includes `tool_args` which may contain sensitive data (file paths, search queries). No redaction or filtering.

---

## 6. Code Duplication

### 6.1 `_row_to_chunk` — Duplicated 4 Times

The exact same method exists in:
1. `ah/core/context.py:218` — `ContextManager._row_to_chunk`
2. `ah/rag/pipeline.py:305` — `RAGPipeline._row_to_chunk`
3. `ah/rag/search.py:238` — `HybridSearch._row_to_chunk`
4. `ah/memory/store.py:286` — `MemoryStore._row_to_entry` (slightly different but same pattern)

This should be a shared utility function or a mixin.

### 6.2 Embedding String Conversion — Duplicated 6 Times

```python
embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
```
Appears in:
1. `ah/core/context.py:36`
2. `ah/core/context.py:73`
3. `ah/core/context.py:174`
4. `ah/memory/store.py:42`
5. `ah/memory/store.py:140`
6. `ah/rag/pipeline.py:133`
7. `ah/rag/pipeline.py:270`
8. `ah/rag/search.py:113`

Should be a utility function: `def embedding_to_pgvector(embedding: list[float]) -> str`.

### 6.3 Tool Call Execution — Duplicated in `run()` and `run_stream()`

The entire tool call execution block (lines 324-447 in `run()` and lines 646-775 in `run_stream()`) is nearly identical:
- Missing tool name handling
- JSON decode error handling
- Tool execution with timing
- Context storage
- Message appending

This is ~120 lines of duplicated code.

### 6.4 Retry Logic — Duplicated

`_call_llm_with_retry` and `_stream_llm_with_retry` in `ah/core/agent.py` have identical retry logic (4 attempts, exponential backoff, same log messages). Should be a shared decorator or context manager.

### 6.5 Provider `complete()` Methods — Duplicated

`OpenRouterProvider.complete()` and `OllamaProvider.complete()` have nearly identical structure:
1. Validate messages
2. Validate params
3. Rate limit
4. Build payload
5. Audit log start
6. HTTP call
7. Parse response
8. Audit log complete
9. Return LLMResponse

The only differences are the API format and response parsing. Should use a template method pattern.

---

## 7. Dead Code

### 7.1 Unused Imports

| File | Unused Import |
|------|---------------|
| `ah/core/agent.py:10` | `Optional` from typing (not used) |
| `ah/core/provider.py:10` | `Optional` from typing (not used) |
| `ah/core/session.py:6` | `Optional` from typing (not used) |
| `ah/tools/base.py:9` | `Optional` from typing (used in `get_tool` but could be `\| None`) |
| `ah/cli/interactive.py:15` | `dataclass, field` — `field` is not used |
| `ah/cli/visual.py:16` | `Optional` from typing (not used) |
| `ah/rag/search.py:5` | `asyncio` — not used directly |
| `ah/rag/search.py:8` | `field` from dataclasses — not used |

### 7.2 Unused Variables

```python
# ah/core/context.py:20
self._batch_size = batch_size
self._pending: list[dict[str, Any]] = []
```
`_batch_size` and `_pending` are never used. The batch insert method `add_chunks_batch` doesn't use them.

```python
# ah/memory/store.py:26
def __init__(self) -> None:
    pass
```
Empty `__init__` — dead code.

### 7.3 Unused Methods

| Method | Location | Issue |
|--------|----------|-------|
| `get_tool_names()` | `ah/tools/base.py:187` | Alias for `list_tools()`, never called |
| `retrieve_by_embedding()` | `ah/memory/retriever.py:117` | Never called in production code |
| `consolidate_from_text()` | `ah/memory/consolidator.py:165` | Never called in production code |
| `index_session_context()` | `ah/rag/pipeline.py:236` | Never called in production code |
| `clear_cache()` | `ah/rag/embedder.py:216` | Never called |
| `cache_size` property | `ah/rag/embedder.py:220` | Never called |
| `get_terminal_width()` | `ah/cli/visual.py:417` | Never called |
| `get_terminal_height()` | `ah/cli/visual.py:425` | Never called |
| `create_box()` | `ah/cli/visual.py:449` | Never called |
| `pad_text()` | `ah/cli/visual.py:440` | Never called |
| `animate_text()` | `ah/cli/animations.py:764` | Never called |
| `async_animate_text()` | `ah/cli/animations.py:780` | Never called |
| `loading_bar()` | `ah/cli/animations.py:796` | Never called |
| `progress_bar_with_label()` | `ah/cli/animations.py:809` | Never called |
| `spinner_frame()` | `ah/cli/animations.py:823` | Never called |
| `square_loader_frame()` | `ah/cli/animations.py:829` | Never called |
| `thinking_frame()` | `ah/cli/animations.py:835` | Never called |
| `tool_execution_frame()` | `ah/cli/animations.py:841` | Never called |
| `error_frame()` | `ah/cli/animations.py:847` | Never called |
| `success_frame()` | `ah/cli/animations.py:853` | Never called |
| `get_ascii_logo()` | `ah/cli/animations.py:920` | Never called |
| `print_ascii_logo()` | `ah/cli/animations.py:925` | Never called |
| `AnimationRunner` | `ah/cli/animations.py:719` | Never used |
| `FrameAnimation` | `ah/cli/animations.py:430` | Never used |
| `TypingEffect` | `ah/cli/animations.py:318` | Never used |
| `FadeTransition` | `ah/cli/animations.py:355` | Never used |
| `StreamingAnimation` | `ah/cli/animations.py:581` | Never used |
| `ErrorAnimation` | `ah/cli/animations.py:610` | Never used |
| `SuccessAnimation` | `ah/cli/animations.py:663` | Never used |
| `ToolExecutionAnimation` | `ah/cli/animations.py:538` | Never used |
| `ThinkingAnimation` | `ah/cli/animations.py:481` | Never used |
| `SquareLoader` | `ah/cli/animations.py:251` | Never used |
| `LoadingDots` | `ah/cli/animations.py:198` | Never used |
| `ProgressBar` | `ah/cli/animations.py:121` | Never used |
| `Spinner` | `ah/cli/animations.py:37` | Never used |

**The entire `ah/cli/animations.py` file (930 lines) is dead code.** None of its classes or functions are imported or used anywhere in the codebase.

### 7.4 Unused Schema Columns

```sql
-- ah/db/schema.sql:41
search_text TEXT,
```
This column is populated by the RAG pipeline but never queried by `ContextManager`. Only `HybridSearch._bm25_search` uses it. If RAG is disabled, this column is wasted space.

---

## 8. Complexity

### 8.1 `ReActAgent.run()` — 300+ Lines

The `run()` method in `ah/core/agent.py` is 300+ lines with nested loops, conditionals, and exception handling. It should be decomposed into:
- `_prepare_prompt()`
- `_execute_tool_call()`
- `_handle_llm_response()`
- `_check_budget()`

### 8.2 `ReActAgent.run_stream()` — 300+ Lines

Same issue. The streaming variant duplicates most of `run()`'s logic.

### 8.3 `InteractiveREPL._handle_slash_command()` — Long If-Else Chain

```python
# ah/cli/interactive.py:447-500
if cmd_name in ("exit", "quit"):
    ...
elif cmd_name == "help":
    ...
elif cmd_name == "status":
    ...
# ... 12 more elif branches
```
Should use a dispatch dict or command pattern.

### 8.4 `PromptAssembler.assemble()` — Complex Budget Logic

The token budget calculation in `assemble()` is fragile:
```python
remaining = self.session_budget - used_tokens
if retrieved_chunks and remaining > 100:
    ...
    if chunk_tokens > remaining:
        compressed = compressed[:remaining * 4]
        ...
        break
    ...
    if remaining < 50:
        break
```
The magic numbers 100, 50, and 4 are unexplained. The truncation `compressed[:remaining * 4]` assumes 4 chars per token, which is the same heuristic used elsewhere but never validated.

### 8.5 `MemoryRetriever._keyword_search()` — Dynamic SQL

```python
# ah/memory/retriever.py:191
rows = await db.fetch(
    f"""
    SELECT ... FROM memories
    WHERE {where_clause}
    ...
    """,
    *params,
)
```
Building SQL with f-strings is dangerous. While the inputs are parameterized, the `where_clause` is constructed by string concatenation. If any condition contained user input, this would be a SQL injection vector. The code is safe only because all conditions are hardcoded.

---

## 9. Readability

### 9.1 Magic Numbers

| Number | Location | Meaning |
|--------|----------|---------|
| `4` | `ah/core/assembler.py:101` | Chars per token (assumed) |
| `100` | `ah/core/assembler.py:92` | Minimum remaining tokens to include retrieved chunks |
| `50` | `ah/core/assembler.py:106` | Stop adding chunks when remaining < 50 |
| `200` | `ah/core/assembler.py:127` | Max result string length |
| `500` | `ah/core/agent.py:426` | Max result preview length |
| `1000` | `ah/core/agent.py:445` | Max tool result in message |
| `50_000` | `ah/core/agent.py:40` | Max token budget |
| `128` | `ah/core/session.py:25` | Cache max size |
| `5` | `ah/core/session.py:25` | Cache TTL in seconds |
| `0.7` | `ah/core/provider.py:121` | Default temperature |
| `4096` | `ah/core/provider.py:122` | Default max tokens |
| `10` | `ah/core/provider.py:77` | Default rate limit |
| `0.85` | `ah/memory/consolidator.py:24` | Dedup threshold |
| `0.2` | `ah/memory/consolidator.py:106` | Min importance to keep |
| `0.05` | `ah/memory/forgetting.py:25` | Eviction threshold |
| `14.0` | `ah/memory/forgetting.py:19` | Half-life in days |
| `0.1` | `ah/memory/forgetting.py:22` | Access boost |
| `0.7` | `ah/memory/retriever.py:26` | Dense weight |
| `0.3` | `ah/memory/retriever.py:27` | Sparse weight |
| `0.3` | `ah/memory/retriever.py:30` | Min similarity threshold |
| `60` | `ah/rag/search.py:43` | RRF k parameter |
| `1536` | `ah/rag/embedder.py:99` | Embedding dimensions |

None of these are named constants. They should be module-level constants with descriptive names.

### 9.2 Long Lines

Many lines exceed 120 characters:
- `ah/core/agent.py:380` — 128 chars
- `ah/core/provider.py:166` — 110 chars
- `ah/memory/retriever.py:271` — 130+ chars
- `ah/cli/interactive.py:710` — 140+ chars

### 9.3 Deep Nesting

```python
# ah/core/agent.py:324-447
for iteration in range(self.max_iterations):
    if verbose:
        ...
    if total_tokens >= MAX_TOKEN_BUDGET:
        ...
    try:
        response = await self._call_llm_with_retry(...)
    except Exception as e:
        ...
    if not response.tool_calls:
        ...
    for tc in response.tool_calls:
        if not tool_name:
            ...
        try:
            tool_args = json.loads(...)
        except json.JSONDecodeError as e:
            ...
        try:
            result = await registry.execute(...)
        except Exception as e:
            ...
```
This is 5 levels of nesting. Should be refactored with early returns and helper methods.

### 9.4 Inconsistent Formatting

- Some files use 4-space indentation (correct), but `ah/cli/animations.py` has inconsistent spacing.
- Some files have trailing whitespace.
- Import ordering is inconsistent (e.g., `ah/core/agent.py` imports `Console` from rich before `ah` modules).

---

## 10. Additional Issues

### 10.1 Security Concerns

1. **SQL Injection Risk** in `ah/memory/retriever.py:191` — f-string SQL construction (safe only by convention).
2. **SSRF Protection** in `ah/tools/builtins.py:20` — `_is_safe_url()` is good, but `web_search()` doesn't use it for the DuckDuckGo fallback.
3. **Command Injection** in `ah/tools/terminal.py` — `DANGEROUS_CHARS` blocks `;|&$()`<>\`\n` but doesn't block newlines in all contexts. The `shlex.split()` approach is good but the allowlist is overly permissive (includes `rm`, `cp`, `mv`).
4. **Hardcoded Credentials** in `ah/db/connection.py:13` — `DEFAULT_DSN = "postgresql://postgres:***@localhost:5432/agentharness"` — the password is masked but the DSN format is exposed.

### 10.2 Performance Issues

1. **N+1 Queries** in `ah/memory/retriever.py:109` — `update_access()` is called in a loop for each retrieved memory. Should be a batch update.
2. **No Connection Pooling** in `ah/rag/embedder.py` — creates a new `httpx.AsyncClient` per `OpenAIEmbedder` instance. Should reuse a global client.
3. **Unbounded Cache** in `ah/core/session.py:25` — `TTLCache(maxsize=128, ttl=5)` — the 5-second TTL is very short, causing frequent cache misses.

### 10.3 Testing Issues

1. **No test for `ah/cli/animations.py`** — because it's dead code.
2. **No test for error paths** — most tests only cover happy paths.
3. **No integration tests** — the 330 tests are all unit tests with mocks.

---

## Summary Scorecard

| Category | Score | Notes |
|----------|-------|-------|
| Naming | 5/10 | Inconsistent conventions, cryptic abbreviations |
| Documentation | 4/10 | Many missing docstrings, most are inadequate |
| Type Hints | 5/10 | Missing in many places, `Any` overuse |
| Error Handling | 3/10 | 47 bare `except Exception`, inconsistent patterns |
| Logging | 5/10 | Inconsistent setup, missing context |
| Code Duplication | 3/10 | 4x `_row_to_chunk`, 8x embedding string, 2x retry logic |
| Dead Code | 2/10 | Entire `animations.py` (930 lines) unused, 25+ unused methods |
| Complexity | 4/10 | 300+ line methods, deep nesting, magic numbers |
| Readability | 4/10 | Magic numbers, long lines, deep nesting |
| **Overall** | **3.9/10** | **Functional but needs significant refactoring** |

---

## Top 10 Priority Fixes

1. **Delete `ah/cli/animations.py`** — 930 lines of dead code.
2. **Extract `_row_to_chunk` to a shared utility** — eliminates 4x duplication.
3. **Extract embedding string conversion to a utility** — eliminates 8x duplication.
4. **Refactor `ReActAgent.run()` and `run_stream()`** — extract common logic, reduce to <100 lines each.
5. **Replace all bare `except Exception` with specific exceptions** — 47 instances.
6. **Add named constants for all magic numbers** — 25+ instances.
7. **Fix type hints** — remove `Any`, add missing annotations.
8. **Add proper docstrings** — especially for public APIs.
9. **Standardize error handling** — pick one pattern (exceptions or error strings) and use it consistently.
10. **Add logging context** — include `session_id` in all log messages.
