# AgentHarness Architecture Audit — Final Verdict

**Date:** 2026-10-01  
**Scope:** Complete codebase audit (47 Python files, ~6,500 LOC)  
**Verdict:** A prototype with good bones but architectural debt that will compound exponentially. The codebase works for a single developer on a single machine. It will not survive contact with multiple contributors, production load, or feature expansion without significant refactoring.

---

## Executive Summary

The codebase has a clear domain model and sensible module boundaries at the top level (`core/`, `db/`, `tools/`, `memory/`, `rag/`, `cli/`). The PostgreSQL + pgvector choice is sound. The ReAct loop concept is correctly implemented. However, the architecture is undermined by five systemic issues:

1. **Global singleton state** — every manager is a module-level global, making testing and multi-tenancy impossible
2. **Massive code duplication** — `run()` and `run_stream()` in `ReActAgent` are ~90% identical
3. **No dependency injection** — components import globals directly, creating tight coupling
4. **Inconsistent error handling** — three different patterns coexist (exceptions, error strings, silent swallowing)
5. **Scattered configuration** — env vars read directly in 8+ files, bypassing the config system

These are not stylistic quibbles. They are structural defects that make the codebase harder to test, extend, and maintain with every feature added.

---

## 1. Module Boundaries

### 1.1 What Works

- **Top-level package structure is logical**: `core/`, `db/`, `tools/`, `memory/`, `rag/`, `cli/`, `skills/` — each has a clear domain
- **`ah/core/models.py`** is a clean separation of domain types from behavior
- **`ah/db/schema.sql`** is well-designed with proper indexes, constraints, and pgvector integration
- **`ah/rag/`** has good internal boundaries: `chunker.py`, `embedder.py`, `loaders.py`, `reranker.py`, `search.py`, `pipeline.py` — each has a single responsibility

### 1.2 What's Broken

**`ah/core/agent.py` — 792 lines, two God methods**

`ReActAgent.run()` (lines 162–463) and `ReActAgent.run_stream()` (lines 487–792) are two ~300-line methods that share ~90% identical logic:
- Session fetching
- Context storage
- Memory retrieval
- RAG retrieval
- Prompt assembly
- LLM call with retry
- Tool call parsing
- Tool execution
- Result storage
- Iteration counting
- Token budget checking
- Audit logging

The only difference is that `run()` collects the final response while `run_stream()` yields `StreamEvent` objects. This is a textbook case for the **Template Method** or **Strategy** pattern — extract the common loop into a base class, let subclasses handle output.

**`ah/core/provider.py` — 566 lines, four unrelated responsibilities**

This file contains:
1. Audit logging infrastructure (lines 22–41)
2. Rate limiting (lines 47–78)
3. Input validation helpers (lines 90–108)
4. Provider implementations (lines 114–566)

These should be separate modules: `ah/core/audit.py`, `ah/core/rate_limit.py`, `ah/core/validation.py`.

**`ah/cli/__init__.py` — 523 lines, all CLI commands inline**

Every Typer command (`chat`, `repl`, `status`, `sessions`, `context`, `skills`, `doctor`, `init`, `version`, `config`, `config-set`, `memory-list`, `memory-search`, `memory-forget`) is defined in a single file. This should be split into `ah/cli/commands/` with one file per command group.

**`ah/cli/animations.py` — 930 lines of dead code**

The animation library (Spinner, ProgressBar, LoadingDots, SquareLoader, TypingEffect, FadeTransition, FrameAnimation, ThinkingAnimation, ToolExecutionAnimation, StreamingAnimation, ErrorAnimation, SuccessAnimation, AnimationRunner) is 930 lines. None of it is used anywhere in the actual CLI — `interactive.py` only imports `Spinner`, `SquareLoader`, and `ThinkingAnimation` from it, and even those are never instantiated in the REPL loop. This is 930 lines of speculative generality.

**`ah/tools/registry.py` — 8 lines, pure re-export**

This file exists only to re-export from `ah/tools/base.py`. It adds indirection without value.

---

## 2. Dependency Direction

### 2.1 The Inversion Problem

The dependency graph has **cycles** and **upward dependencies**:

```
ah/cli/__init__.py
  → ah/core/agent.py
    → ah/core/context.py
      → ah/db.connection.py
    → ah/core/session.py
      → ah/db.connection.py
    → ah/core/provider.py
    → ah/memory/consolidator.py
      → ah/core/context.py  ← CYCLE: core → memory → core
    → ah/memory/retriever.py
      → ah/core/provider.py  ← CYCLE: core → memory → core
    → ah/rag/pipeline.py
      → ah/db.connection.py
    → ah/tools/base.py
      → ah/core/provider.py  ← tools → core (upward)
```

**`ah/memory/consolidator.py` imports from `ah.core.context`** — this means the memory subsystem depends on the core subsystem. But `ah.core.agent.py` imports from `ah.memory.consolidator`. This is a **circular dependency** that works only because Python's import system is forgiving.

**`ah/tools/base.py` imports `audit_log` from `ah.core.provider`** — tools should not depend on core. The audit logging should be in its own module, or tools should receive a logger via injection.

**`ah/tools/memory.py` imports from `ah.core.provider`, `ah.memory.models`, `ah.memory.scorer`, `ah.memory.store`, `ah.memory.retriever`** — the memory tool depends on 5 different modules. It's a facade over the entire memory subsystem.

### 2.2 The Global Singleton Chain

Every manager is a module-level singleton:

```python
# ah/db/connection.py
db = Database()

# ah/core/session.py
session_manager = SessionManager()

# ah/core/context.py
context_manager = ContextManager()

# ah/tools/base.py
registry = ToolRegistry()

# ah/skills/registry.py
skill_registry = SkillRegistry()

# ah/memory/store.py
memory_store = MemoryStore()

# ah/core/config.py
config = Config.load()
```

These are imported directly throughout the codebase:

```python
# ah/core/agent.py
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.memory.store import memory_store
from ah.tools.base import registry
```

This means:
- **Tests cannot isolate components** — you must reset globals between tests
- **Multiple agent instances share state** — two agents in the same process share the same `context_manager`, `session_manager`, etc.
- **Configuration changes affect everything globally** — changing `config.model` affects all subsequent agent runs

The `Container` class in `ah/core/container.py` was supposed to solve this, but it doesn't — `Container.production()` just references the same global singletons:

```python
@classmethod
def production(cls) -> Container:
    return cls(
        _db=db,                    # ← same global
        _session_manager=session_manager,  # ← same global
        _context_manager=context_manager,  # ← same global
        _tool_registry=registry,          # ← same global
        _skill_registry=skill_registry,    # ← same global
    )
```

The Container is a **facade**, not a dependency injection container. It provides no lifecycle management, no scoping, and no test isolation.

---

## 3. Coupling

### 3.1 Tight Coupling

**`ReActAgent` is coupled to 12+ modules:**

```python
from ah.core.assembler import PromptAssembler
from ah.core.context import context_manager
from ah.core.models import AgentResponse, LLMResponse, StreamEvent, ToolDefinition
from ah.core.provider import LLMProvider, audit_log, get_provider
from ah.core.session import Session, session_manager
from ah.memory.consolidator import MemoryConsolidator
from ah.memory.retriever import MemoryRetriever
from ah.memory.store import memory_store
from ah.rag.pipeline import RAGPipeline
from ah.tools.base import registry
```

The agent directly imports concrete classes, not interfaces. It calls `context_manager.add_chunk()`, `session_manager.get()`, `registry.execute()` — all global singletons.

**`MemoryRetriever` directly imports `db`:**

```python
# ah/memory/retriever.py, line 190
from ah.db.connection import db
```

The retriever receives a `MemoryStore` in its constructor but then bypasses it to use the global `db` directly for keyword search. This is **inconsistent abstraction** — some queries go through the store, others go directly to the database.

**`RAGPipeline` directly imports `db`:**

```python
# ah/rag/pipeline.py, line 14
from ah.db.connection import db
```

The pipeline doesn't receive a database connection or store — it uses the global `db` directly. This makes it impossible to test the pipeline without a real database.

### 3.2 Temporal Coupling

The `Container.start()` method must be called before any component works:

```python
container = Container.production()
await container.start()  # ← must be called first
# now container.db, container.session_manager, etc. are usable
```

But the global singletons (`db`, `session_manager`, etc.) are created at import time and can be used without the container. This creates **temporal coupling** — the order of imports and initialization matters, but nothing enforces it.

---

## 4. Cohesion

### 4.1 Low Cohesion

**`ah/core/assembler.py` — TokenCounter + PromptAssembler**

`TokenCounter` (lines 16–39) is a utility class for counting tokens. `PromptAssembler` (lines 42–145) is a prompt builder. These are unrelated responsibilities. `TokenCounter` should be in `ah/core/token_counter.py` or `ah/core/utils.py`.

**`ah/core/provider.py` — Audit + Rate Limiting + Validation + Providers**

Four unrelated responsibilities in one file (see §1.2).

**`ah/cli/visual.py` — Colors + Panels + Tables + Prompts + Status + Utilities**

This 489-line file contains:
- `ColorScheme` (color definitions)
- `PanelStyles` (panel formatting)
- `TableStyles` (table formatting)
- `PromptStyles` (prompt formatting)
- `StatusIndicators` (status dots, badges)
- `VisualContext` (context manager)
- Utility functions (`get_terminal_width`, `truncate_text`, `pad_text`, `create_box`)

The utility functions at the bottom are unrelated to the visual design system.

### 4.2 High Cohesion (Good)

- **`ah/rag/chunker.py`** — single responsibility: text chunking
- **`ah/rag/embedder.py`** — single responsibility: text embedding
- **`ah/rag/reranker.py`** — single responsibility: document reranking
- **`ah/memory/scorer.py`** — single responsibility: importance scoring
- **`ah/memory/forgetting.py`** — single responsibility: decay calculation
- **`ah/db/connection.py`** — single responsibility: connection pool management

---

## 5. Interface Design

### 5.1 `LLMProvider` — Not an Abstract Base Class

```python
class LLMProvider:
    """Base provider interface."""

    async def complete(self, ...) -> LLMResponse:
        raise NotImplementedError

    async def stream_complete(self, ...) -> AsyncGenerator[StreamEvent, None]:
        raise NotImplementedError

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError
```

This is a **base class**, not an interface. It should be an `ABC` with `@abstractmethod` decorators. As written, you can instantiate `LLMProvider()` directly and it won't error until you call a method. The `raise NotImplementedError` pattern is weaker than `@abstractmethod` because it doesn't prevent instantiation.

### 5.2 `ToolDefinition` — Just a Dataclass

```python
@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON Schema
```

This is a plain dataclass with no validation. The `parameters` field is typed as `dict` but should be a JSON Schema object. There's no validation that `parameters` is a valid JSON Schema, no helper methods, and no serialization logic.

### 5.3 `ToolRegistry` — God Registry

`ToolRegistry` is both a registry (stores tools) and an executor (validates and runs tools). These should be separate:
- `ToolRegistry` — stores and retrieves tool definitions
- `ToolExecutor` — validates arguments and executes tools

### 5.4 `RAGPipeline` — Mixed Abstraction Levels

`RAGPipeline.index_document()` does:
1. Load document (delegates to `FileLoader`)
2. Chunk document (delegates to `RecursiveCharacterTextSplitter`)
3. Embed chunks (delegates to `Embedder`)
4. Store chunks (direct SQL with `db.fetchrow()`)

Step 4 breaks the abstraction — the pipeline should delegate storage to a repository or store, not execute raw SQL.

### 5.5 `HybridSearch` — Takes `db: Any`

```python
async def search(
    self,
    session_id: uuid.UUID,
    query_embedding: list[float],
    query_text: str,
    db: Any,  # ← untyped!
    top_k: int | None = None,
) -> list[SearchResult]:
```

The `db` parameter is typed as `Any` — it could be anything. This should be typed as `Database` or, better, as a protocol/interface that defines the required methods.

---

## 6. Error Handling Patterns

### 6.1 Three Inconsistent Patterns

**Pattern 1: Raise exceptions** (good)
```python
# ah/core/provider.py
raise ValueError("OPENROUTER_API_KEY not set")
```

**Pattern 2: Return error strings** (bad)
```python
# ah/tools/file.py
return f"Error: File not found: {path}"
return f"Error: Path escapes the allowed base directory: {path}"
return f"Error reading file: {e}"
```

**Pattern 3: Silently swallow** (worst)
```python
# ah/tools/builtins.py
except Exception:
    pass
```

The `web_search` function in `ah/tools/builtins.py` catches all exceptions and falls through to a fallback. If both SearXNG and DuckDuckGo fail, it returns a generic error message with no indication of what went wrong. This makes debugging impossible.

### 6.2 Inconsistent Validation

**`ah/tools/terminal.py`** has a command allowlist:
```python
ALLOWED_COMMANDS = frozenset({
    "git", "ls", "cat", "grep", "find", "pytest", "python", "pip",
    "npm", "node", "echo", "pwd", "cd", "mkdir", "cp", "mv", "rm",
    "touch", "head", "tail", "wc", "diff", "curl", "wget",
})
```

But `ah/tools/file.py` has path validation:
```python
def _resolve_path(path: str) -> Path:
    candidate = (_BASE_DIR / path).resolve()
    if not candidate.is_relative_to(_BASE_DIR):
        raise ValueError(f"Path '{path}' escapes the allowed base directory")
    return candidate
```

And `ah/tools/builtins.py` has SSRF validation:
```python
def _is_safe_url(url: str) -> bool:
    # ... validates URL against private IP ranges
```

These three validation strategies are completely independent. There's no shared validation framework, no common error type, and no consistent way to report validation failures to the LLM.

### 6.3 Tool Execution Error Handling

In `ReActAgent.run()`:
```python
try:
    result = await registry.execute(tool_name, **tool_args)
except Exception as e:
    logger.exception("Tool execution failed for '%s'", tool_name)
    result = f"Error: {e}"
```

The agent catches all exceptions and converts them to strings. This means the LLM sees `"Error: Tool 'read_file' not registered"` as a string result, with no structured way to distinguish between "tool not found", "invalid arguments", "execution failed", and "timeout".

---

## 7. Configuration Management

### 7.1 Scattered Environment Variables

Environment variables are read directly in 8+ files:

| File | Env Vars |
|------|----------|
| `ah/core/provider.py` | `OPENROUTER_API_KEY`, `LLM_RATE_LIMIT_CALLS_PER_MINUTE` |
| `ah/core/config.py` | `AGENT_HARNESS_*` (all config keys) |
| `ah/db/connection.py` | `DATABASE_URL` |
| `ah/tools/builtins.py` | `SEARXNG_URL` |
| `ah/tools/file.py` | `AGENT_HARNESS_HOME` |
| `ah/tools/terminal.py` | `TMPDIR`, `TEMP` |
| `ah/rag/embedder.py` | `OPENAI_API_KEY`, `OPENROUTER_API_KEY` |
| `ah/rag/reranker.py` | `COHERE_API_KEY` |

The `Config` class in `ah/core/config.py` exists but is not used by most of these files. They read env vars directly, bypassing the config system's type coercion, validation, and session override mechanisms.

### 7.2 Hardcoded Defaults

```python
# ah/core/provider.py
DEFAULT_DSN = "postgresql://postgres:***@localhost:5432/agentharness"

# ah/core/agent.py
MAX_TOKEN_BUDGET = 50_000

# ah/memory/scorer.py
CATEGORY_WEIGHTS = {"preference": 0.9, "decision": 0.8, ...}
RECENCY_WINDOW_DAYS = 30.0
FREQUENCY_NORMALIZATION = 10.0

# ah/memory/forgetting.py
DEFAULT_HALF_LIFE_DAYS = 14.0
ACCESS_BOOST = 0.1
DEFAULT_EVICTION_THRESHOLD = 0.05

# ah/rag/chunker.py
DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]

# ah/rag/embedder.py
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_CACHE_SIZE = 1024
DEFAULT_BATCH_SIZE = 100
```

These should all be configurable via the `Config` class or at least via constructor parameters with defaults.

### 7.3 The `config` Singleton

```python
# ah/core/config.py
config = Config.load()
```

This is a module-level singleton that's imported directly:
```python
# ah/cli/interactive.py
from ah.core.config import config
```

The `Config` class has a `reset()` method that sets `cls._instance = None`, but `config` is a module-level variable, not `cls._instance`. The `reset()` method doesn't actually reset the module-level `config` variable — it's broken.

---

## 8. Extensibility

### 8.1 Adding a New Provider

To add a new LLM provider (e.g., Anthropic, Google), you must:
1. Create a new class in `ah/core/provider.py`
2. Add a new `elif` branch in `get_provider()`
3. Update the `Config` defaults
4. Update the CLI `--provider` choices

This violates the **Open/Closed Principle** — the system should be open for extension but closed for modification. A plugin architecture or registry pattern would allow adding providers without modifying existing code.

### 8.2 Adding a New Tool

To add a new tool, you must:
1. Create a function in `ah/tools/` (e.g., `ah/tools/my_tool.py`)
2. Decorate it with `@registry.register()`
3. Import the module in `ah/tools/__init__.py`

The tool registry is global, so any tool registered anywhere is immediately available everywhere. There's no namespacing, no tool groups, and no way to enable/disable tools per session or per agent.

### 8.3 Adding a New Memory Strategy

The memory system has a fixed pipeline: `MemoryConsolidator` → `ImportanceScorer` → `MemoryStore` → `MemoryRetriever`. There's no way to:
- Use a different importance scoring algorithm
- Add a new retrieval strategy (e.g., graph-based)
- Plug in a different storage backend

The `ImportanceScorer` and `ForgettingModel` are concrete classes, not interfaces. They're injected into `MemoryConsolidator` and `MemoryRetriever` via constructor parameters, but there's no abstract base class defining the contract.

### 8.4 Adding a New RAG Component

The RAG pipeline is more extensible than other subsystems — `Embedder`, `Reranker`, and `HybridSearch` are injected via constructor parameters. However:
- `Embedder` and `Reranker` are ABCs (good)
- `HybridSearch` is a concrete class (should be an interface)
- `FileLoader` is a concrete class (should be an interface)
- The pipeline still executes raw SQL directly (see §5.4)

---

## 9. Maintainability

### 9.1 Code Duplication

**`ReActAgent.run()` vs `ReActAgent.run_stream()`** — ~90% identical logic across 600 lines. This is the single biggest maintainability issue. Any bug fix or feature addition must be applied twice.

**`_row_to_chunk()` duplicated in 3 files:**
- `ah/core/context.py` (line 218)
- `ah/rag/pipeline.py` (line 305)
- `ah/rag/search.py` (line 238)

**`_row_to_entry()` in `ah/memory/store.py`** — same pattern, different file.

**Embedding string formatting duplicated in 5+ files:**
```python
embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
```

This appears in `context.py`, `session.py`, `store.py`, `retriever.py`, `pipeline.py`, and `search.py`. It should be a utility function.

### 9.2 SQL Scattered Across 6 Files

Raw SQL is embedded in:
- `ah/core/context.py` — 6 SQL queries
- `ah/core/session.py` — 7 SQL queries
- `ah/memory/store.py` — 8 SQL queries
- `ah/memory/retriever.py` — 2 SQL queries
- `ah/rag/pipeline.py` — 3 SQL queries
- `ah/rag/search.py` — 2 SQL queries

There's no repository pattern, no query builder, and no ORM. Every manager writes its own SQL. This means:
- Schema changes require updating 6+ files
- SQL injection risks are handled inconsistently
- Query optimization is ad hoc
- Testing requires a real database

### 9.3 Type Hints Are Inconsistent

Some files have thorough type hints:
```python
async def add_chunk(
    self,
    session_id: uuid.UUID,
    agent_id: str,
    chunk_type: str,
    payload: dict[str, Any],
    token_count: int = 0,
    embedding: list[float] | None = None,
) -> ContextChunk:
```

Others have none:
```python
# ah/rag/search.py
async def search(
    self,
    session_id: uuid.UUID,
    query_embedding: list[float],
    query_text: str,
    db: Any,  # ← no type
    top_k: int | None = None,
) -> list[SearchResult]:
```

And some use `Optional` from `typing` instead of `| None`:
```python
from typing import Optional
# ah/tools/builtins.py
def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None) -> str:
```

### 9.4 Logging Is Inconsistent

Some modules use `logging`:
```python
logger = logging.getLogger(__name__)
logger.info("Consolidated %d new memories", len(new_memories))
```

Others use `console.print()`:
```python
# ah/cli/__init__.py
console.print(f"[dim]New session: {session.id}[/dim]")
```

And some use both:
```python
# ah/core/agent.py
logger = logging.getLogger(__name__)
console = Console()
# ...
console.print(f"[dim]Iteration {iteration + 1}/{self.max_iterations}[/dim]")
```

There's no unified logging strategy. The audit logging in `ah/core/provider.py` uses a separate logger (`ah.audit`) with a StreamHandler, but it's not integrated with the rest of the logging.

### 9.5 Testability

The global singleton pattern makes testing difficult:

```python
# tests/test_comprehensive.py
from ah.core.context import ContextManager, context_manager
from ah.core.session import SessionManager, session_manager
```

Tests import the global singletons directly. To isolate tests, you need to call `reset()` on each singleton:

```python
# ah/core/context.py
@classmethod
def reset(cls) -> None:
    global context_manager
    context_manager = cls()
```

But `Config.reset()` is broken (see §7.3), and there's no `reset()` for `MemoryStore`, `MemoryRetriever`, or `RAGPipeline`.

The `Container.testing()` method creates fresh instances, but it doesn't reset the global singletons — it just creates new local instances that shadow the globals within the container. The globals remain unchanged.

---

## 10. Specific Design Flaws

### 10.1 `PromptAssembler._compress_chunk()` — Manual Serialization

```python
def _compress_chunk(self, chunk_data: dict[str, Any]) -> str:
    chunk_type = chunk_data.get("type", "unknown")
    payload = chunk_data.get("payload", {})

    if chunk_type in ("tool_call", "user_message"):
        tool = payload.get("tool", payload.get("content", "unknown"))
        args = payload.get("args", {})
        if args:
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            return f"[{chunk_type}] {tool}({args_str})"
        return f"[{chunk_type}] {tool}"
    elif chunk_type in ("result", "assistant_message"):
        # ...
```

This is a manual serialization format that:
- Doesn't handle nested objects
- Doesn't handle non-string values
- Doesn't escape special characters
- Is not parseable by the LLM in a structured way
- Will break if the payload structure changes

### 10.2 `MemoryRetriever._keyword_search()` — Bypasses the Store

```python
async def _keyword_search(self, ...) -> list[tuple[MemoryEntry, float]]:
    # ...
    from ah.db.connection import db  # ← imports global db
    rows = await db.fetch(...)  # ← bypasses MemoryStore
```

The retriever receives a `MemoryStore` in its constructor but then bypasses it to use the global `db` directly. This is inconsistent — some queries go through the store, others don't.

### 10.3 `RAGPipeline` — Direct SQL in the Pipeline

```python
# ah/rag/pipeline.py
row = await db.fetchrow(
    """
    INSERT INTO context_chunks
        (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    RETURNING ...
    """,
    session_id, agent_id, "document", payload_msgpack, chunk.token_count, embedding_str, search_text,
)
```

The pipeline should delegate storage to a repository or store, not execute raw SQL. This makes the pipeline impossible to test without a real database.

### 10.4 `HybridSearch._bm25_search()` — Fallback to ILIKE

```python
try:
    rows = await db.fetch("""... ts_rank ... tsvector ...""")
except asyncpg.UndefinedColumnError:
    # search_text column doesn't exist — fall back to ILIKE
    rows = await db.fetch("""... ILIKE ...""")
```

This is a runtime schema detection — the code tries FTS and falls back to ILIKE if the column doesn't exist. This should be detected at startup, not at query time.

### 10.5 `OpenRouterProvider` — Hardcoded Model Default

```python
def __init__(self, api_key: str | None = None, model: str = "anthropic/claude-3.5-sonnet") -> None:
```

The default model is hardcoded. If the model is deprecated or renamed, every user must update their config. This should be a configurable default with a fallback.

### 10.6 `OllamaProvider` — No Tool Call Streaming

```python
# Ollama streaming tool calls
tc = message.get("tool_calls", [])
if tc:
    tool_calls.extend(tc)
```

Ollama's streaming API sends tool calls differently from OpenAI's. The current implementation just extends the list, which means tool call arguments are not accumulated — they're taken as-is from each chunk. This will produce incorrect results for multi-chunk tool calls.

### 10.7 `ForgettingModel` — Integer Day Calculation

```python
days_since_access = (now - reference_time).days
```

`.days` returns an integer, so a memory accessed 23 hours ago has `days_since_access = 0`. This means the decay calculation is quantized to days, which is too coarse for a memory system.

### 10.8 `ImportanceScorer` — Average of Averages

```python
return min(1.0, max(0.0, sum(scores) / len(scores)))
```

The final score is the **average** of category weight, explicit importance, recency, and frequency. This means a memory with `category=transient` (weight 0.2) but `explicitly_important=True` (weight 1.0) gets a score of ~0.55 — higher than a `preference` (weight 0.9) that is not explicitly important. The scoring formula needs weights, not a simple average.

---

## 11. Security Concerns

### 11.1 Terminal Tool — Allowlist Bypass

```python
ALLOWED_COMMANDS = frozenset({
    "git", "ls", "cat", "grep", "find", "pytest", "python", "pip",
    "npm", "node", "echo", "pwd", "cd", "mkdir", "cp", "mv", "rm",
    "touch", "head", "tail", "wc", "diff", "curl", "wget",
})
```

The allowlist checks `os.path.basename(args[0])`, but:
- `git` can run arbitrary commands via `git -c core.sshCommand=...`
- `python` can run arbitrary code via `python -c "..."`
- `find` can execute commands via `find -exec ...`
- `curl` and `wget` can make arbitrary network requests

The allowlist gives a false sense of security. A sandboxed execution environment (e.g., Docker, nsjail) would be more appropriate.

### 11.2 SSRF Protection — Incomplete

```python
def _is_safe_url(url: str) -> bool:
    # ...
    ip_str = socket.gethostbyname(hostname)
    ip = ipaddress.ip_address(ip_str)
    if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
        return False
    return True
```

This checks the resolved IP, but:
- DNS rebinding: the hostname could resolve to a public IP during validation and a private IP during the request
- Redirects: `follow_redirects=False` is set, but the initial request could still hit a private IP if DNS rebinding occurs
- IPv6: `ipaddress.ip_address()` handles IPv6, but the check doesn't account for IPv6 private ranges like `fc00::/7`

### 11.3 Path Traversal — Partial Protection

```python
# ah/tools/file.py
_BASE_DIR = Path(os.environ.get("AGENT_HARNESS_HOME", os.getcwd())).resolve()

def _resolve_path(path: str) -> Path:
    candidate = (_BASE_DIR / path).resolve()
    if not candidate.is_relative_to(_BASE_DIR):
        raise ValueError(f"Path '{path}' escapes the allowed base directory")
    return candidate
```

This is good, but:
- Symlinks inside the base directory could point outside
- The base directory is set at import time from `AGENT_HARNESS_HOME` or `os.getcwd()`, which could be anywhere
- There's no validation that the base directory itself is safe

---

## 12. Performance Issues

### 12.1 N+1 Queries in `MemoryRetriever.retrieve()`

```python
# Update access stats for retrieved memories
for rm in candidates[: self.top_k]:
    try:
        await self.store.update_access(rm.memory.id)
    except Exception:
        pass
```

This executes one UPDATE per retrieved memory. For `top_k=5`, that's 5 individual UPDATE statements. This should be a single batch UPDATE.

### 12.2 No Connection Pooling in `Embedder`

```python
# ah/rag/embedder.py
self._client = httpx.AsyncClient(...)
```

Each `OpenAIEmbedder` instance creates its own `httpx.AsyncClient`. If multiple embedders are created (e.g., one per agent), each has its own connection pool. This should be shared or managed by a factory.

### 12.3 `SessionManager` Cache — 5-Second TTL

```python
self._cache: TTLCache = TTLCache(maxsize=128, ttl=5)
```

The 5-second TTL means that after any session update, the cache is invalidated and the next read hits the database. In a multi-agent scenario, this could cause a thundering herd of database queries.

### 12.4 `RAGPipeline.index_document()` — One INSERT Per Chunk

```python
for chunk, embedding in zip(chunks, embeddings):
    row = await db.fetchrow("INSERT INTO context_chunks ...")
```

This executes one INSERT per chunk. For a 100-chunk document, that's 100 individual INSERTs. This should use `executemany` or a bulk insert.

---

## 13. Recommendations

### 13.1 Critical (Do First)

1. **Extract `ReActAgent.run()` and `run_stream()` into a shared base class** — eliminate the 600-line duplication
2. **Introduce proper dependency injection** — remove global singletons, use constructor injection
3. **Create a repository pattern for all database access** — centralize SQL, enable testing
4. **Standardize error handling** — use exceptions consistently, not error strings
5. **Move all configuration through the `Config` class** — eliminate direct env var access

### 13.2 High Priority

6. **Make `LLMProvider` an ABC** — use `@abstractmethod`
7. **Split `ah/core/provider.py`** — separate audit, rate limiting, validation, providers
8. **Split `ah/cli/__init__.py`** — one file per command group
9. **Delete `ah/cli/animations.py`** — 930 lines of dead code
10. **Add a `ToolExecutor` class** — separate validation from execution
11. **Fix `Config.reset()`** — it's broken
12. **Fix `MemoryRetriever._keyword_search()`** — don't bypass the store

### 13.3 Medium Priority

13. **Add interfaces for `HybridSearch`, `FileLoader`, `ImportanceScorer`, `ForgettingModel`**
14. **Centralize embedding string formatting** — utility function
15. **Add batch operations** — `update_access`, `index_document`
16. **Fix `ImportanceScorer` formula** — use weighted average, not simple average
17. **Fix `ForgettingModel` day calculation** — use `total_seconds()`, not `.days`
18. **Add schema version detection** — don't try/catch at query time
19. **Improve terminal tool security** — use sandboxing, not allowlist

### 13.4 Low Priority

20. **Add type hints to all public methods**
21. **Standardize logging** — use `logging` everywhere, not `console.print()`
22. **Add a plugin architecture for providers and tools**
23. **Add metrics and observability** — token usage, latency, error rates
24. **Add async context managers for resource cleanup**

---

## 14. Conclusion

The AgentHarness codebase is a functional prototype that demonstrates a solid understanding of AI agent architecture, PostgreSQL schema design, and RAG pipeline construction. The domain model is sound, the module boundaries are logical at the top level, and the ReAct loop is correctly implemented.

However, the architecture is undermined by systemic issues that will compound with every feature added: global singletons prevent testing and multi-tenancy, code duplication doubles maintenance burden, scattered configuration makes deployment fragile, and inconsistent error handling makes debugging difficult.

The codebase is at an inflection point. It can continue as a prototype and accumulate technical debt, or it can invest in architectural refactoring to become a maintainable framework. The recommendations above are ordered by impact — the critical items (dependency injection, code duplication, repository pattern) will unlock all other improvements.

**The single highest-leverage change is extracting the shared ReAct loop from `run()` and `run_stream()`.** This one refactoring would eliminate ~300 lines of duplication, make the agent loop testable in isolation, and create a foundation for all other improvements.
