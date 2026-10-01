# AgentHarness: Architecture Purist Proposal

**Author:** Architecture Purist  
**Date:** 2026-09-30  
**Status:** Proposal — not yet implemented

---

## Executive Summary

The current AgentHarness codebase is a prototype masquerading as a framework. It has a working ReAct loop and a clean database schema, but its architecture is fundamentally broken at every layer. This document argues for a complete redesign from first principles — not incremental fixes, but a ground-up restructuring that treats the codebase as a *framework* rather than a *script*.

The core thesis: **AgentHarness should be a composable toolkit of small, single-responsibility components wired together by a composition root — not a collection of global singletons and a God Object.**

---

## Part I: The Indictment — Why the Current Architecture Is Broken

### 1.1 The God Object: `ReActAgent`

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

This is a textbook **God Object**. It violates the Single Responsibility Principle in the most egregious way. The agent should *orchestrate*; it should not *be* the orchestration.

**Specific SRP violations:**
- The agent should not know how tool call JSON is parsed (`json.loads(tc["function"]["arguments"])`)
- The agent should not know how messages are formatted for the LLM API
- The agent should not know how context chunks are stored
- The agent should not know how tokens are estimated

Each of these should be a separate collaborator with its own interface.

### 1.2 Five Global Singletons

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

### 1.3 Circular Dependency at the Package Level

```
ah.core.agent → ah.tools.base → ah.core.provider
ah.core.agent → ah.core.provider
ah.core.agent → ah.core.context → ah.db.connection
ah.core.agent → ah.core.session → ah.db.connection
```

`ah.tools.base` imports `ToolDefinition` from `ah.core.provider`. `ah.core.agent` imports `registry` from `ah.tools.base`. This creates a **circular package dependency**: `core` depends on `tools`, and `tools` depends on `core`.

### 1.4 Leaky Abstractions

**`Database`** is a thin wrapper over `asyncpg.Pool` that adds zero value. Worse, it *leaks* the underlying `asyncpg` types — `fetch()` returns `list[asyncpg.Record]`, `fetchrow()` returns `asyncpg.Record | None`. The rest of the codebase is coupled to `asyncpg.Record` (e.g., `row["payload_msgpack"]`).

**`LLMProvider`** defines both `complete()` and `embed()`. But not all LLM providers support embeddings, and the agent never calls `embed()` — it's dead code. This violates the Interface Segregation Principle.

### 1.5 No Domain Model

The "domain" of AgentHarness is:
- Sessions (with goals, status, context budgets)
- Context chunks (typed, embedding-backed)
- Tools (with JSON Schema definitions)
- Skills (with triggers and content)

But there's **no rich domain model**. `Session` is a dataclass with a `state: dict` — a bag of untyped data. `ContextChunk` has a `payload: dict[str, Any]` — another untyped bag. There are no domain events, no invariants, no business rules encapsulated in the domain.

### 1.6 Speculative Design — Schema Bloat

The schema defines **10 tables**, but the code only uses **2** (`sessions` and `context_chunks`). The other 8 are speculative — built for hypothetical future features that don't exist yet. This adds complexity, increases cognitive load, and creates a false sense of the system's capabilities.

### 1.7 Tool Registration via Side Effects

Tools are registered via decorators that mutate a global registry:

```python
@registry.register(description="Read a file")
def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    ...
```

This is a **side-effect-based architecture**. The `@registry.register()` decorator mutates global state as a side effect of importing a module. This makes it:
- Impossible to have multiple isolated tool sets
- Impossible to know which tools are registered without importing all modules
- Dependent on import order (as demonstrated by the `builtins.py` / `file.py` collision)

### 1.8 Mixed Abstraction Levels

`ReActAgent.run()` mixes high-level orchestration with low-level details:

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

## Part II: The Redesign — From First Principles

### 2.1 Design Principles

1. **Single Responsibility:** Every class and function does exactly one thing.
2. **Dependency Inversion:** High-level modules depend on abstractions, not concretions.
3. **Explicit Dependencies:** All dependencies are passed in, never imported as globals.
4. **Domain-Driven Design:** Rich domain models with invariants, not anemic data bags.
5. **Composition over Inheritance:** Small, composable units wired together at a composition root.
6. **Interface Segregation:** Small, focused interfaces — not fat base classes.
7. **Open/Closed:** Extensible via new implementations, not modification of existing code.

### 2.2 The New Module Structure

```
ah/
├── domain/                    # Pure domain logic — no I/O, no framework deps
│   ├── __init__.py
│   ├── session.py             # Session entity + SessionRepository interface
│   ├── context.py             # ContextChunk entity + ContextRepository interface
│   ├── tool.py                # Tool entity + ToolRegistry interface
│   ├── skill.py               # Skill entity + SkillRegistry interface
│   └── events.py              # Domain events (SessionCreated, ChunkAdded, etc.)
│
├── application/               # Use cases — orchestrates domain objects
│   ├── __init__.py
│   ├── react.py               # ReAct loop orchestrator (replaces God Object)
│   ├── prompt.py              # Prompt assembly strategy
│   └── streaming.py           # Streaming response handler
│
├── infrastructure/            # External concerns — DB, HTTP, filesystem
│   ├── __init__.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── connection.py      # asyncpg pool management
│   │   ├── repositories/      # Concrete repository implementations
│   │   │   ├── __init__.py
│   │   │   ├── session_repo.py
│   │   │   └── context_repo.py
│   │   └── schema.sql         # Minimal schema (only what's used)
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py            # LLMProvider interface (complete only)
│   │   ├── openrouter.py      # OpenRouter implementation
│   │   ├── ollama.py          # Ollama implementation
│   │   └── factory.py         # Provider factory (composition concern)
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── registry.py        # ToolRegistry (implements domain interface)
│   │   ├── file.py            # File tools
│   │   ├── terminal.py        # Terminal tool
│   │   └── web.py             # Web tools
│   └── skills/
│       ├── __init__.py
│       └── registry.py        # Skill registry (implements domain interface)
│
├── composition.py             # Composition root — wires everything together
└── cli.py                     # CLI entry point — thin, delegates to composition root
```

### 2.3 Breaking Up the God Object

The current `ReActAgent` is split into four single-responsibility components:

#### A. `ReactOrchestrator` (application layer)

The orchestrator owns the ReAct loop. It does *not* parse JSON, format messages, or count tokens. It delegates:

```python
class ReactOrchestrator:
    def __init__(
        self,
        llm: LLMClient,           # Infrastructure: makes LLM calls
        prompt_builder: PromptBuilder,  # Application: assembles prompts
        tool_executor: ToolExecutor,    # Application: executes tools
        context_tracker: ContextTracker, # Application: records context
        token_tracker: TokenTracker,     # Application: tracks token usage
        max_iterations: int = 10,
    ) -> None:
        ...

    async def run(self, session: Session, message: str) -> AgentResponse:
        """Run the ReAct loop. Orchestration only — no low-level details."""
        await self.context_tracker.record_user_message(session.id, message)
        
        for iteration in range(self.max_iterations):
            if self.token_tracker.is_budget_exceeded():
                return AgentResponse.budget_exceeded()
            
            prompt = await self.prompt_builder.build(session, message)
            response = await self.llm.complete(prompt)
            self.token_tracker.record(response.usage)
            
            if not response.tool_calls:
                await self.context_tracker.record_assistant_message(session.id, response.content)
                return AgentResponse.success(response.content)
            
            for tool_call in response.tool_calls:
                result = await self.tool_executor.execute(tool_call)
                await self.context_tracker.record_tool_call(session.id, tool_call, result)
        
        return AgentResponse.max_iterations_reached()
```

#### B. `PromptBuilder` (application layer)

Owns prompt assembly. Replaces the current `PromptAssembler` with a strategy pattern:

```python
class PromptBuilder:
    def __init__(
        self,
        context_repo: ContextRepository,
        token_counter: TokenCounter,
        budget: int = 8000,
    ) -> None:
        ...

    async def build(self, session: Session, query: str) -> list[Message]:
        """Build a prompt within the token budget."""
        # 1. Always include system prompt + goal + query
        # 2. Add recent context (compressed)
        # 3. Fill remaining budget with retrieved chunks
        ...
```

#### C. `ToolExecutor` (application layer)

Owns tool call parsing and execution:

```python
class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """Parse and execute a tool call."""
        # 1. Parse JSON arguments
        # 2. Look up tool in registry
        # 3. Execute with error handling
        # 4. Return structured result
        ...
```

#### D. `ContextTracker` (application layer)

Owns context recording:

```python
class ContextTracker:
    def __init__(self, context_repo: ContextRepository) -> None:
        self._repo = context_repo

    async def record_user_message(self, session_id: UUID, content: str) -> None:
        await self._repo.add_chunk(session_id, ChunkType.USER_MESSAGE, {"content": content})

    async def record_tool_call(self, session_id: UUID, call: ToolCall, result: ToolResult) -> None:
        await self._repo.add_chunk(session_id, ChunkType.TOOL_CALL, {
            "tool": call.name,
            "args": call.args,
            "result_preview": result.preview,
        })

    async def record_assistant_message(self, session_id: UUID, content: str) -> None:
        await self._repo.add_chunk(session_id, ChunkType.ASSISTANT_MESSAGE, {"content": content})
```

### 2.4 Dependency Injection Pattern

**No global singletons. No hidden dependencies. Everything is explicit.**

#### The Composition Root

All wiring happens in one place — `ah/composition.py`:

```python
"""Composition root — the single place where all dependencies are wired together."""

from ah.infrastructure.db.connection import Database
from ah.infrastructure.db.repositories.session_repo import PostgresSessionRepository
from ah.infrastructure.db.repositories.context_repo import PostgresContextRepository
from ah.infrastructure.llm.openrouter import OpenRouterProvider
from ah.infrastructure.llm.factory import create_provider
from ah.infrastructure.tools.registry import ToolRegistry
from ah.infrastructure.skills.registry import SkillRegistry
from ah.application.react import ReactOrchestrator
from ah.application.prompt import PromptBuilder
from ah.application.tool_executor import ToolExecutor
from ah.application.context_tracker import ContextTracker
from ah.application.token_tracker import TokenTracker


class AppContainer:
    """DI container — holds all configured dependencies."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._db: Database | None = None
        self._llm: LLMClient | None = None

    async def initialize(self) -> None:
        """Initialize all infrastructure dependencies."""
        self._db = Database(self.config.database_url)
        await self._db.connect()
        self._llm = create_provider(self.config.provider, self.config.model)

    async def shutdown(self) -> None:
        """Clean up all resources."""
        if self._llm:
            await self._llm.close()
        if self._db:
            await self._db.close()

    def session_repository(self) -> SessionRepository:
        return PostgresSessionRepository(self._db)

    def context_repository(self) -> ContextRepository:
        return PostgresContextRepository(self._db)

    def tool_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        # Register built-in tools explicitly — no side-effect decorators
        from ah.infrastructure.tools.file import register_file_tools
        from ah.infrastructure.tools.terminal import register_terminal_tools
        from ah.infrastructure.tools.web import register_web_tools
        register_file_tools(registry)
        register_terminal_tools(registry)
        register_web_tools(registry)
        return registry

    def orchestrator(self) -> ReactOrchestrator:
        return ReactOrchestrator(
            llm=self._llm,
            prompt_builder=PromptBuilder(
                context_repo=self.context_repository(),
                token_counter=TokenCounter(),
                budget=self.config.context_budget,
            ),
            tool_executor=ToolExecutor(self.tool_registry()),
            context_tracker=ContextTracker(self.context_repository()),
            token_tracker=TokenTracker(self.config.max_token_budget),
            max_iterations=self.config.max_iterations,
        )
```

#### Usage in CLI

```python
# ah/cli.py
import typer
from ah.composition import AppContainer, AppConfig

app = typer.Typer()

@app.command()
def chat(message: str, provider: str = "openrouter", model: str | None = None):
    """Chat with the agent."""
    config = AppConfig(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DSN),
        provider=provider,
        model=model,
    )
    container = AppContainer(config)
    
    async def _run():
        await container.initialize()
        try:
            orchestrator = container.orchestrator()
            session = await container.session_repository().create(goal=message[:100])
            response = await orchestrator.run(session, message)
            console.print(response.content)
        finally:
            await container.shutdown()
    
    asyncio.run(_run())
```

### 2.5 Domain Model Design

Rich domain models with invariants — not anemic data bags.

#### Session

```python
# ah/domain/session.py
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SessionStatus(Enum):
    ACTIVE = "active"
    IDLE = "idle"
    ARCHIVED = "archived"


@dataclass
class Session:
    """An agent session — the unit of work."""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    title: str | None = None
    agent_id: str = "harness"
    status: SessionStatus = SessionStatus.ACTIVE
    goal: str | None = None
    model: str | None = None
    provider: str | None = None
    context_budget: int = 8000
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)

    def archive(self) -> None:
        """Archive this session."""
        self.status = SessionStatus.ARCHIVED

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = datetime.utcnow()

    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE


class SessionRepository(ABC):
    """Abstract repository for sessions."""

    @abstractmethod
    async def create(self, session: Session) -> Session: ...

    @abstractmethod
    async def get(self, session_id: uuid.UUID) -> Session | None: ...

    @abstractmethod
    async def update(self, session: Session) -> None: ...

    @abstractmethod
    async def list(self, status: SessionStatus | None = None, limit: int = 20) -> list[Session]: ...
```

#### ContextChunk

```python
# ah/domain/context.py
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ChunkType(Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MEMORY = "memory"
    HEARTBEAT = "heartbeat"
    SYSTEM = "system"


@dataclass(frozen=True)
class ContextChunk:
    """An immutable context chunk — a typed, embedding-backed record."""
    id: uuid.UUID
    session_id: uuid.UUID
    agent_id: str
    chunk_type: ChunkType
    payload: dict[str, Any]  # Still a dict, but typed by ChunkType
    token_count: int = 0
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    accessed_at: datetime | None = None

    def mark_accessed(self) -> ContextChunk:
        """Return a new chunk with updated accessed_at (immutable update)."""
        return replace(self, accessed_at=datetime.utcnow())


class ContextRepository(ABC):
    """Abstract repository for context chunks."""

    @abstractmethod
    async def add(self, chunk: ContextChunk) -> ContextChunk: ...

    @abstractmethod
    async def get_recent(
        self, session_id: uuid.UUID, limit: int = 10
    ) -> list[ContextChunk]: ...

    @abstractmethod
    async def search_by_embedding(
        self, session_id: uuid.UUID, query_embedding: list[float], top_k: int = 5
    ) -> list[tuple[ContextChunk, float]]: ...

    @abstractmethod
    async def delete_by_session(self, session_id: uuid.UUID) -> int: ...
```

#### Tool

```python
# ah/domain/tool.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass(frozen=True)
class ToolDefinition:
    """A tool definition with JSON Schema."""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    """A tool call from the LLM."""
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The result of executing a tool."""
    tool_name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def preview(self) -> str:
        return self.content[:200]


class ToolRegistry(ABC):
    """Abstract tool registry."""

    @abstractmethod
    def register(self, definition: ToolDefinition, handler: Callable[..., Awaitable[Any]]) -> None: ...

    @abstractmethod
    async def execute(self, call: ToolCall) -> ToolResult: ...

    @abstractmethod
    def get_definitions(self) -> list[ToolDefinition]: ...
```

### 2.6 Module Boundaries

The architecture enforces strict module boundaries:

```
┌─────────────────────────────────────────────────────────────┐
│                        CLI (ah/cli.py)                       │
│  Thin entry point — parses args, delegates to composition    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│              Composition Root (ah/composition.py)             │
│  Wires all dependencies together — the only place that       │
│  knows about both application and infrastructure layers      │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                  Application Layer (ah/application/)          │
│  ReactOrchestrator, PromptBuilder, ToolExecutor,             │
│  ContextTracker, TokenTracker                                │
│  — Orchestrates use cases, depends on domain interfaces     │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                    Domain Layer (ah/domain/)                  │
│  Session, ContextChunk, ToolDefinition, ToolCall, ToolResult │
│  SessionRepository (ABC), ContextRepository (ABC),           │
│  ToolRegistry (ABC)                                          │
│  — Pure business logic, no I/O, no framework dependencies    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                Infrastructure Layer (ah/infrastructure/)      │
│  db/ (Postgres repositories), llm/ (OpenRouter, Ollama),    │
│  tools/ (concrete tool implementations), skills/             │
│  — Implements domain interfaces, handles all I/O             │
└─────────────────────────────────────────────────────────────┘
```

**Dependency rules:**
- CLI → Composition Root → Application → Domain
- Infrastructure → Domain (implements interfaces)
- Domain → nothing (pure)
- Application → Domain (uses interfaces)
- Infrastructure → Application (never — this would be a violation)

### 2.7 The Minimal Schema

Only tables that are actually used. No speculative design:

```sql
-- ah/infrastructure/db/schema.sql
-- Minimal schema — only what the code actually uses

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    agent_id TEXT NOT NULL DEFAULT 'harness',
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'idle', 'archived')),
    goal TEXT,
    model TEXT,
    provider TEXT,
    context_budget INT DEFAULT 8000,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_activity TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_last_activity ON sessions(last_activity);

CREATE TABLE IF NOT EXISTS context_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    chunk_type TEXT NOT NULL CHECK (chunk_type IN (
        'user_message', 'assistant_message', 'tool_call', 'tool_result',
        'memory', 'heartbeat', 'system'
    )),
    payload_msgpack BYTEA NOT NULL,
    embedding vector(1536),
    token_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    accessed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_context_chunks_session ON context_chunks(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_chunks_type ON context_chunks(chunk_type);
CREATE INDEX IF NOT EXISTS idx_context_chunks_embedding ON context_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

**Removed tables:** `skills`, `memories`, `agent_messages`, `heartbeat_config`, `external_context`, `subagent_sessions`, `subagent_messages`, `subagent_results` — all speculative, none used.

### 2.8 Explicit Tool Registration (No Side Effects)

Replace the decorator-based registration with explicit registration functions:

```python
# ah/infrastructure/tools/file.py
from ah.domain.tool import ToolRegistry, ToolDefinition

def register_file_tools(registry: ToolRegistry) -> None:
    """Register all file tools. Explicit — no side effects."""
    registry.register(
        ToolDefinition(
            name="read_file",
            description="Read the contents of a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "offset": {"type": "integer", "description": "Line number to start (1-indexed)"},
                    "limit": {"type": "integer", "description": "Maximum lines to read"},
                },
                "required": ["path"],
            },
        ),
        handler=read_file,
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Write content to a file.",
            parameters={...},
        ),
        handler=write_file,
    )
    registry.register(
        ToolDefinition(
            name="list_files",
            description="List files in a directory.",
            parameters={...},
        ),
        handler=list_files,
    )
```

### 2.9 Structured Error Handling

No more string errors passed to the LLM:

```python
# ah/domain/errors.py
from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    TRANSIENT = "transient"      # Retry might succeed
    PERMANENT = "permanent"      # Retry will not succeed
    BUDGET = "budget"            # Token/iteration budget exceeded
    VALIDATION = "validation"    # Invalid input


@dataclass(frozen=True)
class AgentError:
    category: ErrorCategory
    message: str
    tool_name: str | None = None
    retryable: bool = False


# In ToolExecutor:
async def execute(self, call: ToolCall) -> ToolResult:
    try:
        tool = self._registry.get(call.name)
        if tool is None:
            return ToolResult(
                tool_name=call.name,
                content=f"Tool '{call.name}' not registered",
                is_error=True,
            )
        result = await tool.handler(**call.args)
        return ToolResult(tool_name=call.name, content=str(result))
    except json.JSONDecodeError as e:
        return ToolResult(
            tool_name=call.name,
            content=f"Invalid JSON in tool arguments: {e}",
            is_error=True,
        )
    except Exception as e:
        return ToolResult(
            tool_name=call.name,
            content=f"Tool execution failed: {e}",
            is_error=True,
        )
```

### 2.10 Transaction Management

The composition root provides a transaction context manager:

```python
# ah/infrastructure/db/connection.py
class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[asyncpg.Connection, None]:
        """Execute operations within a transaction."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[asyncpg.Connection, None]:
        """Acquire a connection from the pool."""
        async with self._pool.acquire() as conn:
            yield conn
```

---

## Part III: Migration Strategy

### Phase 1: Extract Domain Layer (1-2 days)

1. Create `ah/domain/` with `Session`, `ContextChunk`, `ToolDefinition`, `ToolCall`, `ToolResult`
2. Define abstract repository interfaces (`SessionRepository`, `ContextRepository`, `ToolRegistry`)
3. No changes to existing code — just add the new modules

### Phase 2: Extract Application Layer (2-3 days)

1. Create `ah/application/` with `ReactOrchestrator`, `PromptBuilder`, `ToolExecutor`, `ContextTracker`, `TokenTracker`
2. Write unit tests for each component (mock the domain interfaces)
3. No changes to existing code — just add the new modules

### Phase 3: Implement Infrastructure (2-3 days)

1. Create `ah/infrastructure/` with concrete implementations
2. `PostgresSessionRepository`, `PostgresContextRepository` — implement domain interfaces
3. `ToolRegistry` — implement domain interface with explicit registration
4. `OpenRouterProvider`, `OllamaProvider` — implement `LLMClient` interface
5. Write integration tests against a real database

### Phase 4: Composition Root + CLI (1-2 days)

1. Create `ah/composition.py` with `AppContainer`
2. Rewrite `ah/cli.py` to use the composition root
3. Remove all global singletons
4. Remove the old `ReActAgent`, `ContextManager`, `SessionManager`

### Phase 5: Cleanup (1 day)

1. Remove `ah/core/` (replaced by `ah/domain/` + `ah/application/`)
2. Remove `ah/tools/base.py` decorator registry (replaced by explicit registration)
3. Remove `ah/tools/registry.py` re-export
4. Remove empty placeholder modules (`ah/memory/`, `ah/rag/`)
5. Trim schema to minimal tables
6. Update tests

**Total estimated effort: 7-11 days**

---

## Part IV: What This Design Enables

### Testability

Every component can be tested in isolation by mocking its dependencies:

```python
async def test_orchestrator_stops_on_budget():
    llm = MockLLMClient()
    token_tracker = TokenTracker(max_budget=100)
    token_tracker.record(Usage(total_tokens=150))
    
    orchestrator = ReactOrchestrator(
        llm=llm,
        prompt_builder=MockPromptBuilder(),
        tool_executor=MockToolExecutor(),
        context_tracker=MockContextTracker(),
        token_tracker=token_tracker,
    )
    
    response = await orchestrator.run(session, "hello")
    assert response.content == "budget_exceeded"
```

### Configurability

Different configurations for different environments:

```python
# Development
dev_container = AppContainer(AppConfig(
    database_url="postgresql://localhost:5432/agentharness_dev",
    provider="ollama",
    model="llama3.1",
    max_iterations=5,
))

# Production
prod_container = AppContainer(AppConfig(
    database_url=os.environ["DATABASE_URL"],
    provider="openrouter",
    model="anthropic/claude-3.5-sonnet",
    max_iterations=20,
))
```

### Extensibility

Adding a new tool requires zero changes to existing code:

```python
# ah/infrastructure/tools/git.py
def register_git_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(name="git_status", description="Show working tree status", ...),
        handler=git_status,
    )
    registry.register(
        ToolDefinition(name="git_diff", description="Show changes between commits", ...),
        handler=git_diff,
    )

# In composition.py:
from ah.infrastructure.tools.git import register_git_tools
register_git_tools(registry)
```

### Composability

The orchestrator can be used with different LLM providers, different tool sets, and different context strategies — all without modification.

---

## Part V: Summary of Changes

| Current | New | Rationale |
|---------|-----|-----------|
| `ReActAgent` (God Object) | `ReactOrchestrator` + `PromptBuilder` + `ToolExecutor` + `ContextTracker` + `TokenTracker` | Single Responsibility |
| 5 global singletons | `AppContainer` (composition root) | Explicit dependencies |
| `context.py` (3 unrelated classes) | `domain/context.py` + `application/prompt.py` + `application/context_tracker.py` | Separation of concerns |
| `provider.py` (providers + factory) | `infrastructure/llm/base.py` + `infrastructure/llm/factory.py` | Separation of concerns |
| `tools/base.py` (decorator registry) | `infrastructure/tools/registry.py` (explicit registration) | No side effects |
| `tools/registry.py` (re-export) | Removed | Dead code |
| `builtins.py` (duplicate tools) | Removed | Naming collision |
| `db/connection.py` (leaky wrapper) | `infrastructure/db/connection.py` (clean pool) | Proper abstraction |
| `schema.sql` (10 tables) | `schema.sql` (2 tables) | No speculative design |
| `Session.state: dict` | `Session` with typed fields | Rich domain model |
| `ContextChunk.payload: dict[str, Any]` | `ContextChunk` with `ChunkType` enum | Type safety |
| String errors | `AgentError` with `ErrorCategory` | Structured error handling |
| No transactions | `Database.transaction()` | Data consistency |
| `core/` + `tools/` + `skills/` | `domain/` + `application/` + `infrastructure/` | Clean layering |

---

## Conclusion

The current AgentHarness codebase works as a prototype but fails as a framework. The God Object, global singletons, circular dependencies, and lack of domain model make it impossible to test, extend, or configure. The redesign proposed here is not a rewrite — it is a *restructuring* that preserves the working ReAct loop while replacing the architectural foundation with clean, composable, single-responsibility components.

The key insight: **a framework is not a collection of features — it is a set of composable abstractions with a clear composition root.** AgentHarness should be the latter.

---

*This proposal is intentionally opinionated. It prioritizes architectural purity over backward compatibility. The existing codebase is v0.1.0 — there is no backward compatibility to maintain.*
