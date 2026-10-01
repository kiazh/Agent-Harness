# DX-First Architecture Proposal for AgentHarness

> **Goal:** Make AgentHarness a joy to develop against — fast to iterate on, easy to debug, trivial to extend. This document argues that the current architecture creates unnecessary friction for contributors, then proposes concrete fixes across five axes.

---

## 1. The Current Architecture Is Painful — Here's Why

### 1.1 Module Boundaries Are Fuzzy and Implicit

The codebase is organized into `ah/core/`, `ah/tools/`, `ah/db/`, `ah/skills/`, and `ah/cli.py` — but the boundaries between these modules are **not enforced, not documented, and frequently violated**:

- **`ah/tools/base.py` imports `from ah.core.provider import ToolDefinition`**. The tools layer depends on the core layer's provider module for a data class. This is a circular dependency in the making: core defines the tool definition, tools register against it, core's agent executes via tools. A contributor adding a new tool needs to understand three modules before writing their first line.

- **`ah/core/agent.py` imports `from ah.tools.base import registry`**. The agent loop directly imports the global tool registry singleton. There is no interface, no abstraction — just a hard-coded import of a global. You cannot test the agent without the tool registry, and you cannot swap the tool registry without touching the agent.

- **`ah/core/context.py` and `ah/core/session.py` both import `from ah.db.connection import db`**. Every module that touches the database imports the same global `db` singleton. There is no repository pattern, no interface — just direct SQL in every file. A contributor who wants to add a new query needs to know: (a) the global `db` exists, (b) it's an asyncpg pool wrapper, (c) the schema. None of this is documented or abstracted.

- **`ah/cli.py` is a 378-line god file** that imports from every other module, manages database connections inline (`await db.connect()` / `await db.close()` in every command), and mixes async orchestration with Rich rendering. There is no separation between "CLI parsing" and "application logic."

### 1.2 No Consistent Patterns — Every Module Does Its Own Thing

- **Database access:** `ah/core/context.py` and `ah/core/session.py` both write raw SQL with `asyncpg`, but they each have their own `_row_to_chunk` / `_row_to_session` deserialization. There is no shared base class, no query builder, no consistent error handling. If you add a new table, you write a new manager class from scratch.

- **Tool registration:** Tools are registered via a decorator (`@registry.register(...)`) that mutates a global dict. There is no plugin discovery, no lazy loading, no way to unregister or reload. Adding a tool means: (1) write a function, (2) decorate it, (3) import the module somewhere (usually `ah/tools/__init__.py`). The import side-effect is the registration mechanism — this is fragile and makes testing harder.

- **Provider abstraction:** `LLMProvider` is a base class with `complete()`, `stream_complete()`, and `embed()`. But `OpenRouterProvider` and `OllamaProvider` have different constructor signatures, different error handling, and different streaming implementations. The `get_provider()` factory is a simple if/else — adding a third provider means editing the factory.

- **Error handling is inconsistent:** Some tools return `f"Error: {e}"` as strings (e.g., `ah/tools/file.py`, `ah/tools/terminal.py`). Others raise exceptions (e.g., `ToolRegistry.execute()` raises `ValueError`). The agent loop catches exceptions and converts them to strings. This means the caller can't distinguish between "tool returned an error string" and "tool raised an exception" without parsing the string.

### 1.3 Error Messages Are Unhelpful

- `ValueError(f"Session {session_id} not found")` — doesn't tell you what sessions exist, or how to create one.
- `ValueError(f"Tool '{name}' not registered")` — doesn't list available tools.
- `ValueError(f"Unknown provider: {provider}")` — doesn't list valid providers.
- `RuntimeError("Database not connected. Call connect() first.")` — doesn't tell you how to connect, or what the DSN is.
- Tool errors like `f"Error: {e}"` swallow the original exception type and traceback. When a tool fails in production, you get a string with no stack trace, no exception type, and no context about what went wrong.

### 1.4 Debugging Support Is Minimal

- **No structured logging:** The codebase uses `logging.getLogger(__name__)` in some places (`ah/core/agent.py`) but not others. There is no consistent log format, no request/session correlation IDs, no debug-level tracing of tool calls or LLM interactions.
- **No debug mode:** There is no `--debug` flag, no verbose logging toggle, no way to dump the full prompt sent to the LLM or the raw response. When the agent behaves unexpectedly, you're flying blind.
- **No REPL or interactive debugger:** There is no `ah debug` command to inspect sessions, replay prompts, or step through the agent loop.
- **No request tracing:** When a tool call fails, there is no trace ID linking the tool call to the LLM request that triggered it, to the session, to the user message.

### 1.5 No Hot Reloading

- **Tools are registered at import time** and live in a global dict. To add a new tool, you must restart the process. There is no way to register a tool at runtime, reload a module, or discover tools from a directory.
- **Skills are loaded once** via `skill_registry.load_all()` and cached. There is no file watcher, no hot reload, no way to add a skill without restarting.
- **Provider configuration is read once** at import time (`load_dotenv()` in `ah/core/provider.py`). Changing the provider or model requires a restart.
- **The CLI is a batch process** — each `ah chat` invocation is a fresh process. There is no persistent daemon, no REPL, no way to iterate on a conversation without restarting.

### 1.6 The Test Suite Masks the Pain

The test suite is comprehensive (1588 lines) but it **masks the DX problems** rather than revealing them:

- Tests use `unittest.mock.patch` extensively to mock the global `db` singleton, the `session_manager`, and the `context_manager`. This means tests pass even when the architecture is untestable in practice.
- There are no integration tests that exercise the full stack (CLI → agent → tools → database) without mocking.
- There are no tests for error paths that a developer would actually hit (e.g., "what happens when the database is down?").
- The tests are slow because they all spin up async event loops and mock the database.

---

## 2. Proposed Architecture

### 2.1 Clear Module Boundaries

**Principle:** Each module has a single responsibility and a well-defined interface. Dependencies flow in one direction.

```
ah/
├── core/                  # Pure business logic — no I/O, no globals
│   ├── agent.py           # ReAct loop (depends on abstractions, not concretions)
│   ├── context.py         # Context chunk model + prompt assembly
│   ├── provider.py        # LLM provider interface + implementations
│   └── session.py         # Session model
├── db/                    # Database layer — all SQL lives here
│   ├── connection.py      # Connection pool management
│   ├── schema.sql         # Schema definition
│   ├── repositories.py    # Repository pattern: one class per table
│   └── unit_of_work.py    # Transaction management
├── tools/                 # Tool layer — self-contained, no core imports
│   ├── base.py            # Tool interface + registry (no imports from core)
│   ├── builtin/           # Built-in tools as a package
│   │   ├── __init__.py    # Auto-discovery
│   │   ├── web.py
│   │   ├── file.py
│   │   └── terminal.py
│   └── plugin.py          # Plugin discovery and loading
├── skills/                # Skill system — independent of tools
│   ├── registry.py
│   └── parser.py
├── cli/                   # CLI layer — thin, delegates to application layer
│   ├── app.py             # Typer app definition
│   ├── commands/          # One file per command
│   └── rendering.py       # Rich rendering utilities
├── application/           # Application layer — orchestrates core + db + tools
│   ├── agent_service.py   # High-level agent operations
│   ├── session_service.py # Session lifecycle
│   └── config.py          # Configuration management
└── shared/                # Shared utilities — no business logic
    ├── errors.py          # Custom exception hierarchy
    ├── logging.py         # Structured logging setup
    └── tracing.py         # Request tracing
```

**Key changes:**

1. **`ah/tools/base.py` no longer imports from `ah.core.provider`.** The `ToolDefinition` dataclass moves to `ah/shared/types.py` (or `ah/tools/types.py`). Both `core` and `tools` depend on `shared`, but `tools` never depends on `core`.

2. **`ah/core/agent.py` no longer imports `from ah.tools.base import registry`.** Instead, the agent receives a `ToolRegistry` instance via constructor injection. The default is the global registry, but tests can pass a mock.

3. **All database access goes through repositories.** `ah/db/repositories.py` contains `SessionRepository`, `ContextRepository`, etc. Each repository has methods like `get_by_id()`, `create()`, `update()`, `delete()`. The `db` singleton is only accessed from `ah/db/`.

4. **The CLI is thin.** `ah/cli/commands/chat.py` parses arguments, calls `application.agent_service.run_chat()`, and renders the result. No database connections, no async orchestration in the CLI layer.

### 2.2 Consistent Patterns

**Principle:** Every module follows the same structural conventions so a developer knows what to expect.

#### 2.2.1 Database Access Pattern

```python
# ah/db/repositories.py
class SessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_by_id(self, session_id: uuid.UUID) -> Session | None:
        row = await self._db.fetchrow(
            "SELECT * FROM sessions WHERE id = $1", session_id
        )
        return self._row_to_session(row) if row else None

    async def create(self, title: str | None = None, **kwargs) -> Session:
        ...

    def _row_to_session(self, row: asyncpg.Record) -> Session:
        ...
```

Every repository follows the same pattern: constructor takes `db`, methods are async, row-to-model conversion is a private method.

#### 2.2.2 Tool Registration Pattern

```python
# ah/tools/base.py
class ToolRegistry:
    def register(self, name: str, description: str, func: Callable, ...) -> None:
        """Register a tool programmatically (no decorator side-effects)."""
        ...

    def discover(self, package: str = "ah.tools.builtin") -> None:
        """Auto-discover tools from a package."""
        ...

# ah/tools/builtin/__init__.py
def register_all(registry: ToolRegistry) -> None:
    """Register all built-in tools. Called explicitly at startup."""
    from .web import register
    from .file import register
    from .terminal import register
    register(registry)
    register(registry)
    register(registry)
```

Tools are registered explicitly at startup, not via import side-effects. This makes it easy to see which tools are loaded, and easy to conditionally load tools.

#### 2.2.3 Provider Pattern

```python
# ah/core/provider.py
class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, messages: list[dict], **kwargs) -> LLMResponse: ...

    @abstractmethod
    async def stream_complete(self, messages: list[dict], **kwargs) -> AsyncGenerator[StreamEvent, None]: ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

# ah/core/provider.py
_PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {}

def register_provider(name: str, cls: type[LLMProvider]) -> None:
    _PROVIDER_REGISTRY[name] = cls

def get_provider(name: str, **kwargs) -> LLMProvider:
    if name not in _PROVIDER_REGISTRY:
        raise UnknownProviderError(name, available=list(_PROVIDER_REGISTRY.keys()))
    return _PROVIDER_REGISTRY[name](**kwargs)
```

Providers are registered by name. Adding a new provider means creating a class and calling `register_provider()` — no factory editing.

#### 2.2.4 Error Handling Pattern

```python
# ah/shared/errors.py
class AgentHarnessError(Exception):
    """Base exception for all AgentHarness errors."""
    pass

class DatabaseError(AgentHarnessError):
    """Database operation failed."""
    def __init__(self, message: str, *, query: str | None = None, original: Exception | None = None):
        super().__init__(message)
        self.query = query
        self.original = original

class ToolError(AgentHarnessError):
    """Tool execution failed."""
    def __init__(self, tool_name: str, message: str, *, original: Exception | None = None):
        super().__init__(f"Tool '{tool_name}' failed: {message}")
        self.tool_name = tool_name
        self.original = original

class ProviderError(AgentHarnessError):
    """LLM provider operation failed."""
    def __init__(self, provider: str, message: str, *, original: Exception | None = None):
        super().__init__(f"Provider '{provider}' failed: {message}")
        self.provider = provider
        self.original = original

class UnknownProviderError(ProviderError):
    def __init__(self, name: str, *, available: list[str]):
        super().__init__(name, f"Unknown provider. Available: {', '.join(available)}")
        self.available = available

class SessionNotFoundError(AgentHarnessError):
    def __init__(self, session_id: uuid.UUID):
        super().__init__(f"Session {session_id} not found. Use `ah chat` to create a new session.")
        self.session_id = session_id

class ToolNotFoundError(AgentHarnessError):
    def __init__(self, tool_name: str, *, available: list[str]):
        super().__init__(f"Tool '{tool_name}' not found. Available: {', '.join(available)}")
        self.tool_name = tool_name
        self.available = available
```

Every error is a subclass of `AgentHarnessError`. Every error includes actionable context (what went wrong, what the available options are, how to fix it). Tools raise exceptions instead of returning error strings.

### 2.3 Good Error Messages

**Principle:** Error messages tell the developer what went wrong, why, and how to fix it.

| Current | Proposed |
|---------|----------|
| `ValueError(f"Session {session_id} not found")` | `SessionNotFoundError`: "Session `abc-123` not found. Use `ah chat "your message"` to create a new session, or `ah sessions` to list active sessions." |
| `ValueError(f"Tool '{name}' not registered")` | `ToolNotFoundError`: "Tool `read_fil` not found. Did you mean `read_file`? Available tools: read_file, write_file, list_files, terminal, web_search, web_extract, search_files." |
| `ValueError(f"Unknown provider: {provider}")` | `UnknownProviderError`: "Unknown provider `openai`. Available providers: openrouter, ollama. Set `AH_PROVIDER=openrouter` or pass `--provider openrouter`." |
| `RuntimeError("Database not connected. Call connect() first.")` | `DatabaseError`: "Database not connected. Run `ah init` to initialize the schema, or set `DATABASE_URL` environment variable." |
| `f"Error: {e}"` (in tools) | `ToolError`: "Tool `read_file' failed: File not found: `/path/to/file`. Verify the path exists and is within the allowed base directory." |

**Implementation:**

```python
# ah/shared/errors.py
class ToolError(AgentHarnessError):
    def __init__(self, tool_name: str, message: str, *, original: Exception | None = None):
        # Build a helpful message with suggestions
        suggestion = self._get_suggestion(tool_name, message, original)
        full_message = f"Tool '{tool_name}' failed: {message}"
        if suggestion:
            full_message += f"\n  → {suggestion}"
        super().__init__(full_message)
        self.tool_name = tool_name
        self.original = original

    def _get_suggestion(self, tool_name: str, message: str, original: Exception | None) -> str | None:
        if isinstance(original, FileNotFoundError):
            return "Verify the path exists and is within the allowed base directory."
        if isinstance(original, PermissionError):
            return "Check file permissions or run with elevated privileges."
        if isinstance(original, json.JSONDecodeError):
            return "Tool arguments must be valid JSON. Check the LLM output format."
        return None
```

### 2.4 Debugging Support

**Principle:** A developer should be able to understand what the system is doing at any point in time, without adding print statements.

#### 2.4.1 Structured Logging

```python
# ah/shared/logging.py
import logging
import json
from datetime import datetime

class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "session_id"):
            log_entry["session_id"] = str(record.session_id)
        if hasattr(record, "tool_name"):
            log_entry["tool_name"] = record.tool_name
        if hasattr(record, "trace_id"):
            log_entry["trace_id"] = record.trace_id
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logging.basicConfig(level=level, handlers=[handler])
```

Every log entry includes a timestamp, level, logger name, and message. Contextual information (session ID, tool name, trace ID) is attached when available. Logs are JSON-formatted for easy parsing.

#### 2.4.2 Debug Mode

```python
# ah/cli/app.py
@app.command()
def chat(
    ...,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging and prompt dumping"),
):
    if debug:
        setup_logging(level="DEBUG")
        # Dump full prompt to a file for inspection
        os.environ["AH_DEBUG_PROMPT_DIR"] = "/tmp/ah-debug"
```

When `--debug` is passed:
- Logging level is set to DEBUG
- Full prompts sent to the LLM are dumped to `/tmp/ah-debug/prompt-{timestamp}.txt`
- Raw LLM responses are dumped to `/tmp/ah-debug/response-{timestamp}.json`
- Tool call traces (input, output, duration, errors) are logged

#### 2.4.3 Interactive Debug REPL

```python
# ah/cli/commands/debug.py
@app.command()
def debug(
    session_id: Optional[str] = typer.Option(None, "--session", "-s"),
):
    """Interactive debugger — inspect sessions, replay prompts, step through agent loop."""
    # Launch an IPython-like REPL with:
    # - session inspection (view context, state, goal)
    # - prompt replay (re-run a prompt with different parameters)
    # - tool testing (call tools directly with custom arguments)
    # - step-through (pause at each iteration of the ReAct loop)
```

#### 2.4.4 Request Tracing

```python
# ah/shared/tracing.py
import uuid
from contextvars import ContextVar

trace_id: ContextVar[str] = ContextVar("trace_id", default="")

def get_trace_id() -> str:
    return trace_id.get()

def set_trace_id(tid: str) -> None:
    trace_id.set(tid)

class TracedTool:
    """Wraps a tool execution with tracing."""
    def __init__(self, tool_func: Callable, tool_name: str) -> None:
        self._func = tool_func
        self._name = tool_name

    async def __call__(self, **kwargs) -> Any:
        tid = get_trace_id() or str(uuid.uuid4())
        set_trace_id(tid)
        start = time.monotonic()
        try:
            result = await self._func(**kwargs)
            duration = time.monotonic() - start
            logger.debug(
                "Tool executed",
                extra={"trace_id": tid, "tool_name": self._name, "duration_ms": duration * 1000, "success": True},
            )
            return result
        except Exception as e:
            duration = time.monotonic() - start
            logger.error(
                "Tool failed",
                extra={"trace_id": tid, "tool_name": self._name, "duration_ms": duration * 1000, "success": False, "error": str(e)},
            )
            raise
```

Every tool call gets a trace ID. The trace ID is propagated through the agent loop, LLM calls, and database queries. When something fails, you can grep logs by trace ID to see the full execution path.

### 2.5 Hot Relloading

**Principle:** A developer should be able to add tools, skills, and configuration changes without restarting the process.

#### 2.5.1 Tool Hot Reloading

```python
# ah/tools/plugin.py
import importlib
import pkgutil
from pathlib import Path

class ToolPluginLoader:
    """Discovers and loads tools from a directory at runtime."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._loaded_modules: dict[str, str] = {}  # module_name -> file_hash

    def discover(self, directory: str | Path) -> list[str]:
        """Discover tool modules in a directory."""
        directory = Path(directory)
        discovered = []
        for file in directory.glob("*.py"):
            if file.name.startswith("_"):
                continue
            module_name = f"ah_tools_dynamic.{file.stem}"
            discovered.append(module_name)
        return discovered

    def load(self, module_name: str) -> None:
        """Load a tool module and register its tools."""
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "register"):
            module.register(self._registry)

    def reload(self, module_name: str) -> None:
        """Reload a tool module (for development)."""
        if module_name in self._loaded_modules:
            # Unregister old tools
            self._registry.unregister_module(module_name)
        self.load(module_name)

    def watch(self, directory: str | Path, interval: float = 1.0) -> None:
        """Watch a directory for changes and auto-reload."""
        # Use watchdog or polling to detect file changes
        ...
```

#### 2.5.2 Skill Hot Reloading

```python
# ah/skills/registry.py
class SkillRegistry:
    def __init__(self, skills_dir: str | Path = "skills") -> None:
        self.skills_dir = Path(skills_dir)
        self._skills: dict[str, Skill] = {}
        self._file_mtimes: dict[str, float] = {}

    def load_all(self) -> None:
        """Load all skills, tracking file modification times."""
        for skill_dir in self.skills_dir.iterdir():
            if skill_dir.is_dir():
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    mtime = skill_file.stat().st_mtime
                    self._file_mtimes[str(skill_file)] = mtime
                    skill = SkillParser.parse(skill_file)
                    self._skills[skill.name] = skill

    def reload_changed(self) -> list[str]:
        """Check for changed skill files and reload them. Returns list of reloaded skill names."""
        reloaded = []
        for skill_file_str, old_mtime in self._file_mtimes.items():
            skill_file = Path(skill_file_str)
            if not skill_file.exists():
                continue
            new_mtime = skill_file.stat().st_mtime
            if new_mtime > old_mtime:
                skill = SkillParser.parse(skill_file)
                self._skills[skill.name] = skill
                self._file_mtimes[skill_file_str] = new_mtime
                reloaded.append(skill.name)
        return reloaded
```

#### 2.5.3 Configuration Hot Reloading

```python
# ah/application/config.py
import os
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Config:
    """Application configuration — reloadable at runtime."""
    provider: str = "openrouter"
    model: str = "anthropic/claude-3.5-sonnet"
    database_url: str = "postgresql://postgres:***@localhost:5432/agentharness"
    max_iterations: int = 10
    context_budget: int = 8000
    verbose: bool = True
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            provider=os.environ.get("AH_PROVIDER", "openrouter"),
            model=os.environ.get("AH_MODEL", "anthropic/claude-3.5-sonnet"),
            database_url=os.environ.get("DATABASE_URL", "postgresql://postgres:***@localhost:5432/agentharness"),
            max_iterations=int(os.environ.get("AH_MAX_ITERATIONS", "10")),
            context_budget=int(os.environ.get("AH_CONTEXT_BUDGET", "8000")),
            verbose=os.environ.get("AH_VERBOSE", "true").lower() == "true",
            debug=os.environ.get("AH_DEBUG", "false").lower() == "true",
        )

    def reload(self) -> None:
        """Reload configuration from environment variables."""
        new_config = self.from_env()
        self.__dict__.update(new_config.__dict__)
```

#### 2.5.4 Development Server Mode

```python
# ah/cli/commands/serve.py
@app.command()
def serve(
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(True, "--reload/--no-reload"),
):
    """Run AgentHarness as a persistent server with hot reloading."""
    # Start a FastAPI/uvicorn server that:
    # - Serves the agent as an API (POST /chat, GET /sessions, etc.)
    # - Watches the tools/ and skills/ directories for changes
    # - Auto-reloads tools and skills when files change
    # - Provides a WebSocket endpoint for streaming agent responses
    # - Exposes a /debug endpoint for inspecting the current state
```

---

## 3. Migration Path

The proposed architecture is a **target state**, not a big-bang rewrite. Here's how to get there incrementally:

### Phase 1: Foundation (1-2 days)
1. Create `ah/shared/errors.py` with the exception hierarchy.
2. Create `ah/shared/logging.py` with structured logging.
3. Move `ToolDefinition` from `ah/core/provider.py` to `ah/shared/types.py`.
4. Update all imports to use the new locations.

### Phase 2: Database Layer (2-3 days)
1. Create `ah/db/repositories.py` with `SessionRepository` and `ContextRepository`.
2. Refactor `ah/core/session.py` to use `SessionRepository`.
3. Refactor `ah/core/context.py` to use `ContextRepository`.
4. Remove direct `db` imports from `ah/core/`.

### Phase 3: Tool Layer (2-3 days)
1. Refactor `ah/tools/base.py` to remove the `from ah.core.provider import ToolDefinition` dependency.
2. Convert `ah/tools/builtin/` from a flat module to a package with explicit `register_all()`.
3. Add `ah/tools/plugin.py` for runtime tool discovery.
4. Update `ah/tools/__init__.py` to call `register_all()` explicitly.

### Phase 4: CLI and Application Layer (2-3 days)
1. Split `ah/cli.py` into `ah/cli/app.py` + `ah/cli/commands/*.py`.
2. Create `ah/application/agent_service.py` and `ah/application/session_service.py`.
3. Move database connection management from CLI commands to application services.
4. Add `--debug` flag to CLI.

### Phase 5: Debugging and Hot Reloading (3-5 days)
1. Add `ah/shared/tracing.py` with request tracing.
2. Add `ah/cli/commands/debug.py` with interactive REPL.
3. Add file watching to `ToolPluginLoader` and `SkillRegistry`.
4. Add `ah/cli/commands/serve.py` for persistent server mode.

### Phase 6: Testing and Documentation (2-3 days)
1. Add integration tests that exercise the full stack without mocking.
2. Write a developer guide (`docs/developer-guide.md`) explaining the architecture.
3. Add type hints to all public APIs.
4. Set up pre-commit hooks for linting and type checking.

---

## 4. Summary of Benefits

| Pain Point | Current State | Proposed State |
|------------|--------------|----------------|
| Module boundaries | Fuzzy, implicit, circular dependencies | Clear, enforced, one-directional |
| Database access | Raw SQL in every module | Repository pattern, centralized |
| Tool registration | Import side-effects, global mutation | Explicit registration, plugin discovery |
| Error messages | Generic, unhelpful | Specific, actionable, with suggestions |
| Debugging | Print statements, no tracing | Structured logging, trace IDs, debug mode |
| Hot Reloading | None | File watching, runtime registration |
| Testability | Heavy mocking, slow | Dependency injection, fast unit tests |
| CLI | 378-line god file | Thin commands, application layer |
| Configuration | Read once at import | Reloadable from environment |

---

## 5. Open Questions

1. **Should we use a framework (FastAPI, Litestar) for the server mode, or keep it minimal with `asyncio` + `uvicorn`?**
2. **Should the tool registry be persistent (stored in the database) or ephemeral (rebuilt at startup)?**
3. **Should we support multiple agent configurations (e.g., different system prompts, tool sets, or models per session)?**
4. **Should the skill system support versioning and rollback?**
5. **Should we add a plugin API for third-party tools, or keep it internal?**

---

*This proposal is a living document. As the architecture evolves, this document should be updated to reflect the current state and future direction.*
