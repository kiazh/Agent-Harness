# AgentHarness Architecture Critique

**Verdict:** The codebase is a prototype masquerading as a framework. It violates core design principles at every layer — from module organization to dependency flow to abstraction boundaries. What follows is a systematic indictment.

---

## 1. The God Object: `ReActAgent` — ⚠️ PARTIALLY ADDRESSED

**File:** `ah/core/agent.py`

`ReActAgent.run()` is a 130-line method that does *everything*:

- Fetches session state
- Assembles prompts (delegating to `PromptAssembler`, but passing raw data)
- Stores user messages in context
- Retrieves recent context
- Calls the LLM
- Parses tool call JSON
- Executes tools
- Stores tool results in context
- Formats messages for the next iteration
- Counts tokens
- Handles verbose logging
- Manages iteration limits

This is a textbook **God Object** — a single class that knows about and controls every aspect of the ReAct loop. It violates the **Single Responsibility Principle** in the most egregious way. The agent should orchestrate; it should not *be* the orchestration.

**Specific SRP violations:**
- The agent should not know how tool call JSON is parsed (`json.loads(tc["function"]["arguments"])`)
- The agent should not know how messages are formatted for the LLM API
- The agent should not know how context chunks are stored
- The agent should not know how tokens are estimated

Each of these should be a separate collaborator with its own interface.

---

## 2. Tangled Module Organization — ✅ FIXED

### 2.1 `context.py` — Two Unrelated Responsibilities — ✅ FIXED

**File:** `ah/core/context.py`

`PromptAssembler` has been extracted to `ah/core/assembler.py`. `context.py` now only contains `ContextManager` (data access layer with raw SQL). The separation of concerns is now correct.

### 2.2 `provider.py` — Providers + Factory + Data Classes — ✅ FIXED

**File:** `ah/core/provider.py`

`LLMResponse`, `ToolDefinition`, and `StreamEvent` have been moved to `ah/core/models.py`. `provider.py` now only contains the provider implementations and the factory function.

### 2.3 `builtins.py` — Duplicate Tool Definitions — ✅ FIXED

**File:** `ah/tools/builtins.py`

Duplicate tool definitions have been removed. `builtins.py` now only contains `web_search`, `web_extract`, and `search_files`. The `read_file`, `write_file`, `list_files`, and `terminal` tools are only defined in their respective modules (`file.py`, `terminal.py`).

---

## 3. Global Singletons — The Dependency Inversion Violation — ⚠️ PARTIALLY ADDRESSED

**Files:** `ah/db/connection.py`, `ah/core/session.py`, `ah/core/context.py`, `ah/tools/base.py`, `ah/skills/registry.py`

Every module creates a module-level singleton:

```python
# db/connection.py
db = Database()

# core/session.py
session_manager = SessionManager()

# core/context.py
context_manager = ContextManager()

# tools/base.py
registry = ToolRegistry()

# skills/registry.py
skill_registry = SkillRegistry()
```

These singletons are **hidden dependencies**. When `ReActAgent.run()` calls `session_manager.get()`, it's not receiving a dependency — it's reaching out to a global. This makes the code:

- **Untestable in isolation** — you must patch the global singleton to test any component
- **Impossible to configure differently** — you can't have two agents with different session managers
- **Prone to initialization order bugs** — if `db` isn't connected before `session_manager` is used, you get a runtime error

The correct approach is **Dependency Injection**: pass dependencies through constructors or method parameters. The `ReActAgent` already accepts a `provider` parameter — why not `session_manager`, `context_manager`, and `tool_registry` too?

---

## 4. Circular Dependency at the Package Level — ✅ FIXED

**Dependency graph:**

```
ah.core.agent → ah.tools.base → ah.core.provider
ah.core.agent → ah.core.provider
ah.core.agent → ah.core.context → ah.db.connection
ah.core.agent → ah.core.session → ah.db.connection
```

`ah.tools.base` imports `ToolDefinition` from `ah.core.provider`. `ah.core.agent` imports `registry` from `ah.tools.base`. This creates a **circular package dependency**: `core` depends on `tools`, and `tools` depends on `core`.

While Python can handle this (because the imports are at module level, not package level), it's a design smell. `ToolDefinition` should live in its own module (e.g., `ah/core/types.py` or `ah/tools/types.py`) to break the cycle.

---

## 5. Leaky Abstractions

### 5.1 `Database` — A Wrapper That Adds Nothing

**File:** `ah/db/connection.py`

The `Database` class is a thin wrapper over `asyncpg.Pool`:

```python
async def execute(self, query: str, *args) -> str:
    async with self.acquire() as conn:
        return await conn.execute(query, *args)
```

This adds zero value over using the pool directly. Worse, it **leaks** the underlying `asyncpg` types — `fetch()` returns `list[asyncpg.Record]`, `fetchrow()` returns `asyncpg.Record | None`. The rest of the codebase is coupled to `asyncpg.Record` (e.g., `row["payload_msgpack"]`).

A proper abstraction would return domain objects, not database rows. The `_row_to_chunk()` and `_row_to_session()` methods in `ContextManager` and `SessionManager` are row-mapping functions that should be part of a repository pattern, not scattered across managers.

### 5.2 `LLMProvider` — Fat Interface

**File:** `ah/core/provider.py`

The `LLMProvider` base class defines both `complete()` and `embed()`. But:
- Not all LLM providers support embeddings
- The agent never calls `embed()` — it's dead code in the current implementation
- This violates the **Interface Segregation Principle**: clients shouldn't depend on methods they don't use

### 5.3 `ToolRegistry` — A Registry That's Really Just a Dict

**File:** `ah/tools/base.py`

`ToolRegistry` wraps a `dict[str, Tool]` and provides `register()`, `execute()`, `list_tools()`, `get_tool_names()`, `get_tool()`. The `get_tool_names()` method is an alias for `list_tools()` — pure bloat. The `_infer_schema()` method is a utility function that doesn't belong in a registry.

---

## 6. No Domain Model — ✅ FIXED

The "domain" of AgentHarness is:
- Sessions (with goals, status, context budgets)
- Context chunks (typed, embedding-backed)
- Tools (with JSON Schema definitions)
- Skills (with triggers and content)

But there's **no rich domain model**. `Session` is a dataclass with a `state: dict` — a bag of untyped data. `ContextChunk` has a `payload: dict[str, Any]` — another untyped bag. There are no domain events, no invariants, no business rules encapsulated in the domain.

The `state` field on `Session` is particularly egregious — it's a `dict` that can contain anything, with no schema, no validation, and no documented structure. The comment says "LangGraph checkpoint, etc." — the "etc." is doing a lot of work.

---

## 7. Speculative Design — Schema Bloat — ✅ FIXED

**File:** `ah/db/schema.sql`

The schema defines **10 tables**, but the code only uses **3** (`sessions`, `context_chunks`, `memories` — and `memories` isn't even implemented):

| Table | Used in code? |
|-------|--------------|
| `sessions` | Yes |
| `context_chunks` | Yes |
| `skills` | No (skills are loaded from filesystem) |
| `memories` | No |
| `agent_messages` | No |
| `heartbeat_config` | No |
| `external_context` | No |
| `subagent_sessions` | No |
| `subagent_messages` | No |
| `subagent_results` | No |

This is **speculative design** — building for hypothetical future features. It adds complexity, increases the cognitive load for new developers, and creates a false sense of the system's capabilities. The schema should be minimal and grow with actual requirements.

---

## 8. No Transaction Management

Every database operation acquires its own connection from the pool:

```python
# In ContextManager.add_chunk():
row = await db.fetchrow("INSERT ...")

# In Agent.run():
await context_manager.add_chunk(...)  # Connection 1
recent = await context_manager.get_recent_context(...)  # Connection 2
```

There's no way to perform multi-step operations atomically. If `add_chunk()` succeeds but `get_recent_context()` fails, the system is in an inconsistent state. For a system that claims to be a "multi-agent orchestration framework," the lack of transaction management is a critical gap.

---

## 9. Prompt Assembly via String Concatenation

**File:** `ah/core/context.py`, `PromptAssembler`

The `PromptAssembler` builds prompts via string concatenation:

```python
parts.append(system_prompt)
parts.append(f"\n\n## Current Goal\n{goal}")
parts.append(f"\n\n## Current Query\n{query}")
```

This is fragile:
- No escaping of special characters in user input
- No structured message format (just raw strings)
- Token budget enforcement is a rough estimate (`len(text) // 4`)
- The `_compress_chunk()` method uses a long if/elif chain on chunk types — a violation of the **Open/Closed Principle** (adding a new chunk type requires modifying this method)

---

## 10. Error Handling is Ad-Hoc — ⚠️ PARTIALLY ADDRESSED

Errors are caught as strings and returned to the LLM:

```python
try:
    result = await registry.execute(tool_name, **tool_args)
except Exception as e:
    result = f"Error: {e}"
```

This means:
- No structured error types
- No retry logic
- No circuit breakers
- No distinction between transient and permanent errors
- The LLM receives a string like "Error: Tool 'foo' not registered" and must decide what to do

A proper architecture would have typed errors, a retry policy, and a fallback strategy.

---

## 11. Tool Registration via Side Effects

**File:** `ah/tools/base.py`, `ah/tools/builtins.py`

Tools are registered via decorators that mutate a global registry:

```python
@registry.register(description="Read a file")
def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    ...
```

This is a **side-effect-based architecture**. The `@registry.register()` decorator mutates global state as a side effect of importing a module. This makes it:
- Impossible to have multiple isolated tool sets
- Impossible to know which tools are registered without importing all modules
- Impossible to test tool registration in isolation
- Dependent on import order (as demonstrated by the `builtins.py` / `file.py` collision)

---

## 12. Empty Placeholder Modules

**Files:** `ah/memory/__init__.py`, `ah/rag/__init__.py`, `ah/skills/__init__.py`

These are empty files that exist only to create Python packages. They add no value and create the illusion of a more complete architecture than actually exists. `ah/skills/__init__.py` is empty, but `ah/skills/registry.py` contains the actual implementation — the package structure is misleading.

---

## 13. The `tools/registry.py` Re-export

**File:** `ah/tools/registry.py`

```python
from ah.tools.base import Tool, ToolRegistry, registry
__all__ = ["Tool", "ToolRegistry", "registry"]
```

This is a pure re-export for "backward compatibility" — but this is a v0.1.0 project with no backward compatibility to maintain. It's an unnecessary indirection that adds confusion about which module is the canonical source.

---

## 14. Mixed Abstraction Levels in `ReActAgent.run()`

The `run()` method mixes high-level orchestration with low-level details:

```python
# High-level: "Run the ReAct loop"
for iteration in range(self.max_iterations):
    # Low-level: "Parse JSON"
    tool_args = json.loads(tc["function"]["arguments"])
    # Low-level: "Format message"
    messages.append({"role": "tool", "content": result_str[:1000], ...})
    # Low-level: "Count tokens"
    total_tokens += response.usage.get("total_tokens", 0)
```

A clean architecture would separate these into distinct layers:
- **Application layer:** Orchestrate the ReAct loop
- **Domain layer:** Manage context, sessions, tool execution
- **Infrastructure layer:** LLM API calls, database access, message formatting

---

## 15. No Dependency Injection — ⚠️ PARTIALLY ADDRESSED

Everything is hardcoded:
- `ReActAgent` creates its own provider via `get_provider()` if none is passed
- `ReActAgent` uses the global `session_manager`, `context_manager`, and `registry`
- `CLI` commands create their own `ReActAgent` instances with hardcoded dependencies

There's no DI container, no composition root, no way to swap implementations. This makes the system impossible to test, configure, or extend.

---

## 16. The `get_provider()` Factory is in the Wrong Place

**File:** `ah/core/provider.py`

The `get_provider()` function is a factory that creates provider instances based on a string parameter. This is a **composition concern** that doesn't belong in the provider module. It should live in a separate `factory.py` or be part of a configuration/DI setup.

---

## 17. No Async Context Manager for the Agent

The `ReActAgent.run()` method doesn't have proper resource management. If an exception occurs mid-iteration:
- The LLM provider may not be closed
- The database connection may not be released
- The session may be left in an inconsistent state

A proper implementation would use `async with` or `try/finally` to ensure cleanup.

---

## 18. The `_compress_chunk()` Open/Closed Violation

**File:** `ah/core/context.py`

```python
def _compress_chunk(self, chunk_data: dict[str, Any]) -> str:
    if chunk_type in ("tool_call", "user_message"):
        ...
    elif chunk_type in ("result", "assistant_message"):
        ...
    elif chunk_type == "memory":
        ...
    elif chunk_type == "heartbeat":
        ...
    elif chunk_type == "system":
        ...
    elif chunk_type == "user":
        ...
    elif chunk_type == "assistant":
        ...
    else:
        ...
```

Adding a new chunk type requires modifying this method. This violates the **Open/Closed Principle**. A proper design would use a strategy pattern or polymorphic chunk types.

---

## Summary of Violations

| Principle | Violation | Status | Location |
|-----------|-----------|--------|----------|
| **Single Responsibility** | `ReActAgent` does everything | ⚠️ PARTIAL | `ah/core/agent.py` |
| **Single Responsibility** | `context.py` has 3 unrelated classes | ✅ FIXED | `ah/core/context.py` → `assembler.py` |
| **Open/Closed** | `_compress_chunk()` if/elif chain | ⚠️ STILL VALID | `ah/core/assembler.py` |
| **Interface Segregation** | `LLMProvider` has unused `embed()` | ⚠️ STILL VALID | `ah/core/provider.py` |
| **Dependency Inversion** | Global singletons everywhere | ⚠️ PARTIAL | All modules (reset() added) |
| **Dependency Inversion** | No DI container | ⚠️ PARTIAL | `ah/core/container.py` created |
| **Separation of Concerns** | Data access + prompt assembly in same file | ✅ FIXED | Extracted to `assembler.py` |
| **Separation of Concerns** | Domain model = database row mapping | ✅ FIXED | `ah/core/models.py` |
| **Leaky Abstraction** | `Database` wraps asyncpg but leaks types | ✅ FIXED | `_row_to_chunk`/`_row_to_session` |
| **Leaky Abstraction** | `asyncpg.Record` used throughout | ✅ FIXED | Row mapping in managers |
| **Circular Dependency** | `core` ↔ `tools` package cycle | ✅ FIXED | `ToolDefinition` → `models.py` |
| **Speculative Design** | 7 unused database tables | ✅ FIXED | Schema reduced to 2 tables |
| **Side Effects** | Tool registration via global mutation | ⚠️ STILL VALID | `ah/tools/base.py` |
| **Naming Collision** | Duplicate tool definitions | ✅ FIXED | `builtins.py` cleaned |
| **Dead Code** | `get_tool_names()`, `embed()`, empty modules | ⚠️ PARTIAL | `get_tool_names()` still exists |

---

## Recommendation

Most of the original issues have been addressed through incremental improvements:

1. **Introduce Dependency Injection** — ⚠️ PARTIAL: DI container created (`container.py`), singletons have `reset()` methods
2. **Separate layers** — ✅ DONE: `assembler.py` extracted, `models.py` created, `container.py` added
3. **Break up God Objects** — ⚠️ PARTIAL: `PromptAssembler` extracted, but `run()`/`run_stream()` still share code
4. **Define proper interfaces** — ✅ DONE: `ToolDefinition` moved to `models.py`, row mapping in managers
5. **Eliminate speculative design** — ✅ DONE: Schema reduced to 2 tables, duplicates removed
6. **Fix the circular dependency** — ✅ DONE: `ToolDefinition` moved to `models.py`

Remaining issues are acceptable for a v0.1.0 project and are tracked in the roadmap.
