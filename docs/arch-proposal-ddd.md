# AgentHarness: Domain-Driven Design Architecture Proposal

**Date:** 2026-09-30
**Author:** Architect (DDD Path)
**Verdict:** The current codebase has no domain model. Business logic is scattered across managers that are little more than SQL wrappers, domain data is stored in untyped `dict[str, Any]` bags, and there are no domain events, no repositories, and no application services. This proposal introduces a proper Domain-Driven Design architecture that makes the domain explicit, testable, and evolvable.

---

## Executive Summary

AgentHarness is a framework for orchestrating AI agents. Its domain is rich: agents execute tool calls within sessions, context accumulates and is retrieved by relevance, token budgets constrain behavior, and skills are matched by triggers. Yet the codebase models none of this explicitly. The `Session` is a dataclass with a `state: dict` — a bag of untyped data. The `ContextChunk` has a `payload: dict[str, Any]` — another untyped bag. The `ReActAgent.run()` method is a 130-line God Object that orchestrates, parses JSON, formats messages, counts tokens, and handles errors all in one place.

This proposal introduces:

1. **Aggregate Roots** — `Session`, `Agent`, `Context` as rich domain objects with invariants and behavior
2. **Value Objects** — `TokenBudget`, `Message`, `ToolCall` as immutable, validated domain primitives
3. **Domain Events** — `AgentStarted`, `ToolExecuted`, `ContextAdded` as first-class domain signals
4. **Repositories** — `SessionRepository`, `ContextRepository` as persistence abstractions
5. **Application Services** — `AgentService`, `ContextService` as use-case orchestrators

The goal is not academic purity. It is to make the codebase **maintainable**: when a developer needs to add a new tool type, change the token budgeting strategy, or add a new context retrieval algorithm, they should modify one well-understood class — not hunt through a 130-line method.

---

## The Problem: A Codebase Without a Domain

### 1. `Session.state: dict` — The Untyped Bag

```python
# ah/core/session.py
@dataclass
class Session:
    id: uuid.UUID
    title: str | None = None
    agent_id: str = "harness"
    status: str = "active"
    state: dict = field(default_factory=dict)  # ← What is in here? Nobody knows.
    goal: str | None = None
    model: str | None = None
    provider: str | None = None
    context_budget: int = 8000
```

The `state` field is a `dict` that can contain anything. The comment says "LangGraph checkpoint, etc." — the "etc." is doing a lot of work. There is no schema, no validation, no documented structure. A developer who needs to read or write session state must grep the codebase to find out what keys are used.

**The cost:** Every access to `session.state` is a potential `KeyError` or silent `None`. Refactoring the state structure requires finding every access site. There is no compiler help, no IDE autocompletion, no type safety.

### 2. `ContextChunk.payload: dict[str, Any]` — Another Untyped Bag

```python
# ah/core/context.py
@dataclass
class ContextChunk:
    id: uuid.UUID
    session_id: uuid.UUID
    agent_id: str
    chunk_type: str
    payload: dict[str, Any]  # ← What shape? Depends on chunk_type.
    token_count: int = 0
    embedding: list[float] | None = None
```

The `payload` field is a `dict[str, Any]` whose shape depends on `chunk_type`. A `tool_call` payload has `{"tool": ..., "args": ..., "result_preview": ...}`. A `user_message` payload has `{"content": ...}`. A `memory` payload has `{"content": ...}`. The `PromptAssembler._compress_chunk()` method uses a long if/elif chain to handle each type — a violation of the Open/Closed Principle.

**The cost:** Adding a new chunk type requires modifying `_compress_chunk()`, the database schema (adding to the CHECK constraint), and every place that reads the payload. The domain concept of "a context chunk" is not modeled — it is a dict with a type tag.

### 3. `ReActAgent.run()` — The God Object

```python
# ah/core/agent.py
async def run(self, session_id, user_message, verbose=True):
    session = await session_manager.get(session_id)          # Data access
    assembler = PromptAssembler(session.context_budget)      # Presentation
    await context_manager.add_chunk(...)                      # Data access
    recent = await context_manager.get_recent_context(...)    # Data access
    prompt = assembler.assemble(...)                          # Presentation
    messages = [{"role": "user", "content": prompt}]           # Formatting
    for iteration in range(self.max_iterations):              # Orchestration
        response = await self._call_llm_with_retry(...)       # Infrastructure
        total_tokens += response.usage.get("total_tokens")    # Accounting
        if not response.tool_calls:                            # Decision
            await context_manager.add_chunk(...)               # Data access
            return AgentResponse(...)                          # Formatting
        for tc in response.tool_calls:                         # Parsing
            tool_args = json.loads(tc["function"]["arguments"])  # Parsing
            result = await registry.execute(tool_name, **tool_args)  # Execution
            await context_manager.add_chunk(...)               # Data access
            messages.append({"role": "tool", ...})             # Formatting
```

This method does at least seven distinct things: data access, prompt assembly, LLM calls, JSON parsing, tool execution, token accounting, and message formatting. It is 130 lines of mixed abstraction levels.

**The cost:** A bug in tool execution requires understanding the entire method. A change to the prompt format requires understanding the entire method. Testing any one aspect requires mocking all the others.

### 4. Global Singletons — Hidden Dependencies

```python
# Every module creates a module-level singleton:
db = Database()                    # ah/db/connection.py
session_manager = SessionManager() # ah/core/session.py
context_manager = ContextManager() # ah/core/context.py
registry = ToolRegistry()          # ah/tools/base.py
skill_registry = SkillRegistry()   # ah/skills/registry.py
```

These singletons are **hidden dependencies**. When `ReActAgent.run()` calls `session_manager.get()`, it is not receiving a dependency — it is reaching out to a global. This makes the code untestable in isolation, impossible to configure differently, and prone to initialization order bugs.

### 5. No Domain Events — No Audit Trail

When a tool is executed, the result is stored in a context chunk. But there is no `ToolExecuted` event. When a session is created, there is no `SessionCreated` event. When context is added, there is no `ContextAdded` event. The system has no first-class representation of the things that happen in the domain.

**The cost:** There is no audit trail. There is no way to react to domain events (e.g., send a notification when a tool fails). There is no way to replay events for debugging. The domain has no memory of what happened — only the current state.

---

## The Proposal: Domain-Driven Design

### Architectural Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Application Layer                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ AgentService │  │ContextService│  │  SessionService  │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                 │                    │            │
├─────────┼─────────────────┼────────────────────┼────────────┤
│         │           Domain Layer              │            │
│  ┌──────▼───────┐  ┌──────▼───────┐  ┌───────▼─────────┐  │
│  │   Session    │  │    Agent     │  │    Context      │  │
│  │ (Aggregate)  │  │ (Aggregate)  │  │  (Aggregate)    │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬─────────┘  │
│         │                 │                    │            │
│  ┌──────▼───────┐  ┌──────▼───────┐  ┌───────▼─────────┐  │
│  │  TokenBudget │  │  ToolCall    │  │    Message      │  │
│  │(Value Object)│  │(Value Object)│  │ (Value Object)  │  │
│  └──────────────┘  └──────────────┘  └─────────────────┘  │
│         │                 │                    │            │
│  ┌──────▼─────────────────▼────────────────────▼─────────┐  │
│  │              Domain Events                           │  │
│  │  AgentStarted │ ToolExecuted │ ContextAdded │ ...   │  │
│  └──────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│                  Infrastructure Layer                        │
│  ┌──────────────────┐  ┌──────────────────┐                │
│  │SessionRepository │  │ContextRepository │                │
│  └──────────────────┘  └──────────────────┘                │
│  ┌──────────────────┐  ┌──────────────────┐                │
│  │  LLMProvider     │  │  ToolRegistry    │                │
│  └──────────────────┘  └──────────────────┘                │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. Aggregate Roots

### 1.1 `Session` — The Unit of Work

The `Session` aggregate root encapsulates the lifecycle of an agent's work. It owns its context chunks, enforces its own invariants, and emits domain events.

```python
# ah/domain/session.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from ah.domain.events import SessionCreated, SessionArchived, GoalSet
from ah.domain.value_objects import TokenBudget


class SessionStatus(Enum):
    ACTIVE = "active"
    IDLE = "idle"
    ARCHIVED = "archived"


@dataclass
class Session:
    """Aggregate root — an agent session.

    Encapsulates the lifecycle of an agent's work: creation, goal-setting,
    context accumulation, and archiving. Enforces invariants and emits
    domain events.
    """

    id: uuid.UUID
    agent_id: str
    status: SessionStatus = SessionStatus.ACTIVE
    goal: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    token_budget: TokenBudget = field(default_factory=lambda: TokenBudget(8000))
    title: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    _domain_events: list = field(default_factory=list, repr=False)

    @classmethod
    def create(
        cls,
        agent_id: str,
        goal: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        token_budget: Optional[TokenBudget] = None,
        title: Optional[str] = None,
    ) -> Session:
        """Factory method — create a new session."""
        session = cls(
            id=uuid.uuid4(),
            agent_id=agent_id,
            goal=goal,
            model=model,
            provider=provider,
            token_budget=token_budget or TokenBudget(8000),
            title=title,
        )
        session._domain_events.append(SessionCreated(
            session_id=session.id,
            agent_id=agent_id,
            goal=goal,
        ))
        return session

    def set_goal(self, goal: str) -> None:
        """Set or update the session goal."""
        self.goal = goal
        self._touch()
        self._domain_events.append(GoalSet(
            session_id=self.id,
            goal=goal,
        ))

    def archive(self) -> None:
        """Archive the session — terminal state."""
        if self.status == SessionStatus.ARCHIVED:
            raise ValueError("Session is already archived")
        self.status = SessionStatus.ARCHIVED
        self._domain_events.append(SessionArchived(
            session_id=self.id,
        ))

    def can_accept_context(self, token_count: int) -> bool:
        """Invariant: a session cannot accept context that exceeds its budget."""
        return self.token_budget.can_accommodate(token_count)

    def _touch(self) -> None:
        self.last_activity = datetime.utcnow()

    def pull_domain_events(self) -> list:
        """Pull and clear domain events (for dispatch by repository)."""
        events = self._domain_events[:]
        self._domain_events.clear()
        return events
```

**Key improvements over the current `Session`:**
- `status` is an `Enum`, not a `str` — no more typos like `"archvied"`
- `token_budget` is a `TokenBudget` value object, not a raw `int` — it carries behavior
- Domain events are emitted on state changes — the system has a memory
- `can_accept_context()` encapsulates a business rule — the invariant lives in the domain
- `archive()` enforces the invariant that archived sessions cannot be re-archived

### 1.2 `Agent` — The Actor

The `Agent` aggregate root represents an AI agent with its configuration and capabilities.

```python
# ah/domain/agent.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from ah.domain.events import AgentStarted, AgentStopped


@dataclass
class Agent:
    """Aggregate root — an AI agent.

    Encapsulates agent configuration: which provider it uses, which model,
    its system prompt, and its tool budget.
    """

    id: str
    provider: str
    model: str
    system_prompt: str
    max_iterations: int = 10
    max_token_budget: int = 50_000
    _domain_events: list = field(default_factory=list, repr=False)

    @classmethod
    def create(
        cls,
        agent_id: str = "harness",
        provider: str = "openrouter",
        model: str = "anthropic/claude-3.5-sonnet",
        system_prompt: Optional[str] = None,
        max_iterations: int = 10,
    ) -> Agent:
        return cls(
            id=agent_id,
            provider=provider,
            model=model,
            system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
            max_iterations=max_iterations,
        )

    def start(self, session_id: uuid.UUID) -> None:
        """Mark the agent as started for a session."""
        self._domain_events.append(AgentStarted(
            agent_id=self.id,
            session_id=session_id,
            model=self.model,
            provider=self.provider,
        ))

    def stop(self, session_id: uuid.UUID) -> None:
        """Mark the agent as stopped for a session."""
        self._domain_events.append(AgentStopped(
            agent_id=self.id,
            session_id=session_id,
        ))

    def pull_domain_events(self) -> list:
        events = self._domain_events[:]
        self._domain_events.clear()
        return events
```

### 1.3 `Context` — The Accumulated Knowledge

The `Context` aggregate root represents the accumulated knowledge within a session. It owns its chunks and enforces retrieval invariants.

```python
# ah/domain/context.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ah.domain.events import ContextAdded
from ah.domain.value_objects import Message, ToolCall


@dataclass
class Context:
    """Aggregate root — the accumulated context for a session.

    Owns context chunks, enforces token budget invariants, and provides
    retrieval operations.
    """

    session_id: uuid.UUID
    agent_id: str
    chunks: list[ContextChunk] = field(default_factory=list)
    _domain_events: list = field(default_factory=list, repr=False)

    def add_chunk(self, chunk: ContextChunk) -> None:
        """Add a context chunk — enforces invariants."""
        if chunk.session_id != self.session_id:
            raise ValueError("Chunk belongs to a different session")
        self.chunks.append(chunk)
        self._domain_events.append(ContextAdded(
            session_id=self.session_id,
            chunk_id=chunk.id,
            chunk_type=chunk.chunk_type,
            token_count=chunk.token_count,
        ))

    def get_recent(self, limit: int = 10) -> list[ContextChunk]:
        """Get the most recent chunks."""
        return sorted(self.chunks, key=lambda c: c.created_at, reverse=True)[:limit]

    def get_by_type(self, chunk_type: str) -> list[ContextChunk]:
        """Get chunks filtered by type."""
        return [c for c in self.chunks if c.chunk_type == chunk_type]

    def total_tokens(self) -> int:
        """Total token count across all chunks."""
        return sum(c.token_count for c in self.chunks)

    def pull_domain_events(self) -> list:
        events = self._domain_events[:]
        self._domain_events.clear()
        return events


@dataclass
class ContextChunk:
    """A typed context chunk — part of the Context aggregate."""

    id: uuid.UUID
    session_id: uuid.UUID
    agent_id: str
    chunk_type: str
    payload: dict  # Still a dict, but now validated by the domain
    token_count: int = 0
    embedding: Optional[list[float]] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    accessed_at: Optional[datetime] = None

    @classmethod
    def create(
        cls,
        session_id: uuid.UUID,
        agent_id: str,
        chunk_type: str,
        payload: dict,
        token_count: int = 0,
        embedding: Optional[list[float]] = None,
    ) -> ContextChunk:
        """Factory method — create a validated context chunk."""
        if not chunk_type:
            raise ValueError("chunk_type is required")
        if token_count < 0:
            raise ValueError("token_count must be non-negative")
        return cls(
            id=uuid.uuid4(),
            session_id=session_id,
            agent_id=agent_id,
            chunk_type=chunk_type,
            payload=payload,
            token_count=token_count,
            embedding=embedding,
        )
```

---

## 2. Value Objects

Value objects are immutable, validated domain primitives that carry behavior.

### 2.1 `TokenBudget`

```python
# ah/domain/value_objects.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenBudget:
    """Value object — a token budget with behavior.

    Immutable. Validated at construction. Carries domain logic for
    budget checking and tracking.
    """

    total: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.total <= 0:
            raise ValueError("Token budget must be positive")
        if self.used < 0:
            raise ValueError("Used tokens must be non-negative")
        if self.used > self.total:
            raise ValueError("Used tokens cannot exceed total budget")

    @property
    def remaining(self) -> int:
        return self.total - self.used

    @property
    def exhausted(self) -> bool:
        return self.used >= self.total

    def can_accommodate(self, token_count: int) -> bool:
        """Check if the budget can accommodate additional tokens."""
        return self.used + token_count <= self.total

    def consume(self, token_count: int) -> TokenBudget:
        """Return a new TokenBudget with the given tokens consumed."""
        return TokenBudget(total=self.total, used=self.used + token_count)

    @classmethod
    def from_session_budget(cls, budget: int) -> TokenBudget:
        """Create a TokenBudget from a session's context_budget."""
        return cls(total=budget)
```

**Why this matters:** The current code uses a raw `int` for `context_budget` and a separate `total_tokens` counter in `ReActAgent.run()`. The `TokenBudget` value object encapsulates the invariant that used tokens cannot exceed the total, and provides a `can_accommodate()` method that makes the business rule explicit.

### 2.2 `Message`

```python
# ah/domain/value_objects.py
@dataclass(frozen=True)
class Message:
    """Value object — a message in the conversation.

    Immutable. Validated at construction. Replaces the raw dicts
    currently used in ReActAgent.run().
    """

    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None

    def __post_init__(self) -> None:
        if not self.role:
            raise ValueError("Message role is required")
        if self.role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"Invalid message role: {self.role}")

    def to_llm_format(self) -> dict:
        """Convert to the format expected by LLM APIs."""
        msg: dict = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_llm_format() for tc in self.tool_calls]
        return msg
```

**Why this matters:** The current code builds message dicts inline:
```python
messages.append({"role": "tool", "content": result_str[:1000], "tool_call_id": tc.get("id", "")})
```
This is error-prone (typos in keys, missing fields) and untestable. The `Message` value object validates at construction and provides a `to_llm_format()` method that centralizes the LLM API format.

### 2.3 `ToolCall`

```python
# ah/domain/value_objects.py
@dataclass(frozen=True)
class ToolCall:
    """Value object — a tool call from the LLM.

    Immutable. Validated at construction. Replaces the raw dicts
    currently parsed in ReActAgent.run().
    """

    id: str
    tool_name: str
    arguments: dict

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Tool call ID is required")
        if not self.tool_name:
            raise ValueError("Tool name is required")

    @classmethod
    def from_llm_response(cls, raw: dict) -> ToolCall:
        """Parse a raw tool call from an LLM response.

        Centralizes the parsing logic that is currently scattered
        through ReActAgent.run().
        """
        function_data = raw.get("function", {})
        tool_name = function_data.get("name")
        if not tool_name:
            raise ValueError("Tool call missing 'name' field")
        arguments_str = function_data.get("arguments", "{}")
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in tool arguments: {e}") from e
        return cls(
            id=raw.get("id", ""),
            tool_name=tool_name,
            arguments=arguments,
        )

    def to_llm_format(self) -> dict:
        """Convert to the format expected by LLM APIs."""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.tool_name,
                "arguments": json.dumps(self.arguments),
            },
        }
```

**Why this matters:** The current code parses tool call JSON inline:
```python
tool_args = json.loads(function_data.get("arguments", "{}"))
```
This is duplicated in both `run()` and `run_stream()`. The `ToolCall` value object centralizes this parsing, validates the result, and provides a factory method `from_llm_response()` that handles errors consistently.

---

## 3. Domain Events

Domain events are first-class representations of things that happen in the domain. They are immutable, timestamped, and carry the context of what happened.

```python
# ah/domain/events.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class DomainEvent:
    """Base class for all domain events."""

    occurred_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class SessionCreated(DomainEvent):
    """Emitted when a new session is created."""
    session_id: uuid.UUID = None
    agent_id: str = ""
    goal: Optional[str] = None


@dataclass(frozen=True)
class SessionArchived(DomainEvent):
    """Emitted when a session is archived."""
    session_id: uuid.UUID = None


@dataclass(frozen=True)
class GoalSet(DomainEvent):
    """Emitted when a session goal is set or updated."""
    session_id: uuid.UUID = None
    goal: str = ""


@dataclass(frozen=True)
class AgentStarted(DomainEvent):
    """Emitted when an agent starts processing a session."""
    agent_id: str = ""
    session_id: uuid.UUID = None
    model: str = ""
    provider: str = ""


@dataclass(frozen=True)
class AgentStopped(DomainEvent):
    """Emitted when an agent stops processing a session."""
    agent_id: str = ""
    session_id: uuid.UUID = None


@dataclass(frozen=True)
class ToolExecuted(DomainEvent):
    """Emitted when a tool is executed."""
    session_id: uuid.UUID = None
    agent_id: str = ""
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    result_preview: str = ""
    success: bool = True
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ContextAdded(DomainEvent):
    """Emitted when a context chunk is added."""
    session_id: uuid.UUID = None
    chunk_id: uuid.UUID = None
    chunk_type: str = ""
    token_count: int = 0


@dataclass(frozen=True)
class TokenBudgetExceeded(DomainEvent):
    """Emitted when the token budget is exceeded."""
    session_id: uuid.UUID = None
    total_tokens: int = 0
    budget: int = 0
```

**Why this matters:** Domain events provide:
- **Audit trail:** Every state change is recorded as an event
- **Decoupling:** Reactors can subscribe to events without modifying the emitter
- **Debugging:** Events can be replayed to understand what happened
- **Extensibility:** New features can react to existing events without modifying the domain

---

## 4. Repositories

Repositories are persistence abstractions. They hide the database from the domain and provide collection-like access to aggregates.

### 4.1 `SessionRepository`

```python
# ah/infrastructure/repositories/session_repository.py
from __future__ import annotations

import uuid
from typing import Optional

import asyncpg
import msgpack

from ah.domain.session import Session, SessionStatus
from ah.domain.value_objects import TokenBudget


class SessionRepository:
    """Repository for Session aggregates.

    Hides all database access for sessions. Returns domain objects,
    not database rows.
    """

    def __init__(self, db: asyncpg.Pool) -> None:
        self._db = db

    async def add(self, session: Session) -> Session:
        """Persist a new session."""
        await self._db.execute(
            """
            INSERT INTO sessions (id, title, agent_id, status, goal, model, provider, context_budget)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            session.id,
            session.title,
            session.agent_id,
            session.status.value,
            session.goal,
            session.model,
            session.provider,
            session.token_budget.total,
        )
        return session

    async def get(self, session_id: uuid.UUID) -> Optional[Session]:
        """Get a session by ID."""
        row = await self._db.fetchrow(
            "SELECT * FROM sessions WHERE id = $1",
            session_id,
        )
        return self._row_to_session(row) if row else None

    async def get_last_active(self) -> Optional[Session]:
        """Get the most recently active session."""
        row = await self._db.fetchrow(
            """
            SELECT * FROM sessions
            WHERE status = 'active'
            ORDER BY last_activity DESC
            LIMIT 1
            """,
        )
        return self._row_to_session(row) if row else None

    async def save(self, session: Session) -> None:
        """Persist changes to an existing session."""
        await self._db.execute(
            """
            UPDATE sessions
            SET title = $2, status = $3, goal = $4, model = $5,
                provider = $6, context_budget = $7, last_activity = now()
            WHERE id = $1
            """,
            session.id,
            session.title,
            session.status.value,
            session.goal,
            session.model,
            session.provider,
            session.token_budget.total,
        )

    async def list(
        self,
        status: Optional[SessionStatus] = None,
        limit: int = 20,
    ) -> list[Session]:
        """List sessions, optionally filtered by status."""
        if status:
            rows = await self._db.fetch(
                """
                SELECT * FROM sessions
                WHERE status = $1
                ORDER BY last_activity DESC
                LIMIT $2
                """,
                status.value,
                limit,
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT * FROM sessions
                ORDER BY last_activity DESC
                LIMIT $1
                """,
                limit,
            )
        return [self._row_to_session(r) for r in rows]

    def _row_to_session(self, row: asyncpg.Record) -> Session:
        """Map a database row to a Session aggregate."""
        return Session(
            id=row["id"],
            title=row["title"],
            agent_id=row["agent_id"],
            status=SessionStatus(row["status"]),
            goal=row["goal"],
            model=row["model"],
            provider=row["provider"],
            token_budget=TokenBudget(row["context_budget"]),
            created_at=row["created_at"],
            last_activity=row["last_activity"],
        )
```

### 4.2 `ContextRepository`

```python
# ah/infrastructure/repositories/context_repository.py
from __future__ import annotations

import uuid
from typing import Optional

import asyncpg
import msgpack

from ah.domain.context import Context, ContextChunk


class ContextRepository:
    """Repository for Context aggregates.

    Hides all database access for context chunks. Returns domain objects,
    not database rows.
    """

    def __init__(self, db: asyncpg.Pool) -> None:
        self._db = db

    async def add_chunk(self, chunk: ContextChunk) -> ContextChunk:
        """Persist a new context chunk."""
        payload_msgpack = msgpack.packb(chunk.payload, use_bin_type=True)
        embedding_str = None
        if chunk.embedding is not None:
            embedding_str = "[" + ",".join(str(x) for x in chunk.embedding) + "]"
        row = await self._db.fetchrow(
            """
            INSERT INTO context_chunks (id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
            """,
            chunk.id,
            chunk.session_id,
            chunk.agent_id,
            chunk.chunk_type,
            payload_msgpack,
            chunk.token_count,
            embedding_str,
        )
        return self._row_to_chunk(row)

    async def get_chunks(
        self,
        session_id: uuid.UUID,
        chunk_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[ContextChunk]:
        """Get context chunks for a session."""
        if chunk_type:
            rows = await self._db.fetch(
                """
                SELECT * FROM context_chunks
                WHERE session_id = $1 AND chunk_type = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                session_id,
                chunk_type,
                limit,
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT * FROM context_chunks
                WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id,
                limit,
            )
        return [self._row_to_chunk(r) for r in rows]

    async def search_by_embedding(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.7,
    ) -> list[tuple[ContextChunk, float]]:
        """Search context chunks by embedding similarity."""
        embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
        rows = await self._db.fetch(
            """
            SELECT *, 1 - (embedding <=> $1::vector) AS similarity
            FROM context_chunks
            WHERE session_id = $2 AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            embedding_str,
            session_id,
            top_k,
        )
        results = []
        for row in rows:
            sim = row["similarity"]
            if sim < threshold:
                continue
            chunk = self._row_to_chunk(row)
            results.append((chunk, sim))
        return results

    async def get_token_usage(self, session_id: uuid.UUID) -> int:
        """Get total token count for a session."""
        return await self._db.fetchval(
            "SELECT COALESCE(SUM(token_count), 0) FROM context_chunks WHERE session_id = $1",
            session_id,
        )

    def _row_to_chunk(self, row: asyncpg.Record) -> ContextChunk:
        """Map a database row to a ContextChunk."""
        payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
        embedding = None
        if row["embedding"] is not None:
            embedding = [float(x) for x in str(row["embedding"]).strip("[]").split(",")]
        return ContextChunk(
            id=row["id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            chunk_type=row["chunk_type"],
            payload=payload,
            token_count=row["token_count"],
            embedding=embedding,
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
        )
```

**Why this matters:** The current `SessionManager` and `ContextManager` are SQL wrappers that return dataclasses with `dict` fields. The repository pattern:
- Returns **domain objects** (aggregates), not database rows
- Hides all SQL from the domain layer
- Makes the domain layer testable with in-memory repositories
- Provides a single place to change the persistence strategy

---

## 5. Application Services

Application services orchestrate use cases. They are thin — they coordinate domain objects and repositories, but contain no business logic.

### 5.1 `AgentService`

```python
# ah/application/agent_service.py
from __future__ import annotations

import uuid
from typing import AsyncGenerator, Optional

from ah.domain.agent import Agent
from ah.domain.context import Context, ContextChunk
from ah.domain.events import ToolExecuted, TokenBudgetExceeded
from ah.domain.session import Session
from ah.domain.value_objects import Message, TokenBudget, ToolCall
from ah.infrastructure.repositories.context_repository import ContextRepository
from ah.infrastructure.repositories.session_repository import SessionRepository
from ah.infrastructure.llm import LLMProvider
from ah.infrastructure.tools import ToolRegistry


class AgentService:
    """Application service — orchestrates the ReAct loop.

    Thin coordinator: delegates to domain objects and repositories.
    Contains no business logic.
    """

    def __init__(
        self,
        session_repo: SessionRepository,
        context_repo: ContextRepository,
        tool_registry: ToolRegistry,
        llm_provider: LLMProvider,
    ) -> None:
        self._session_repo = session_repo
        self._context_repo = context_repo
        self._tool_registry = tool_registry
        self._llm_provider = llm_provider

    async def run(
        self,
        session_id: uuid.UUID,
        user_message: str,
    ) -> AgentResponse:
        """Run the ReAct loop for a user message."""
        session = await self._session_repo.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        agent = Agent.create(
            agent_id=session.agent_id,
            provider=session.provider or "openrouter",
            model=session.model or "anthropic/claude-3.5-sonnet",
        )
        agent.start(session_id)

        # Store user message
        await self._store_message(session, agent, "user_message", {"content": user_message})

        # Assemble prompt
        recent_chunks = await self._context_repo.get_chunks(session_id, limit=5)
        prompt = self._assemble_prompt(session, agent, recent_chunks, user_message)

        messages = [Message(role="user", content=prompt)]
        total_tokens = 0
        tool_calls_made = []

        for iteration in range(agent.max_iterations):
            if total_tokens >= agent.max_token_budget:
                yield TokenBudgetExceeded(session_id=session_id, total_tokens=total_tokens, budget=agent.max_token_budget)
                break

            # Call LLM
            response = await self._llm_provider.complete(
                messages=[m.to_llm_format() for m in messages],
                tools=self._tool_registry.get_tool_definitions(),
            )
            total_tokens += response.usage.get("total_tokens", 0)

            if not response.tool_calls:
                # Final answer
                await self._store_message(session, agent, "assistant_message", {"content": response.content})
                yield AgentResponse(
                    content=response.content,
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration + 1,
                )
                return

            # Execute tool calls
            for raw_tc in response.tool_calls:
                try:
                    tool_call = ToolCall.from_llm_response(raw_tc)
                except ValueError as e:
                    messages.append(Message(role="user", content=f"Error: {e}"))
                    continue

                result = await self._tool_registry.execute(tool_call.tool_name, **tool_call.arguments)
                result_str = str(result)

                # Store tool call in context
                await self._store_message(session, agent, "tool_call", {
                    "tool": tool_call.tool_name,
                    "args": tool_call.arguments,
                    "result_preview": result_str[:500],
                })

                tool_calls_made.append({
                    "tool": tool_call.tool_name,
                    "args": tool_call.arguments,
                    "result_preview": result_str[:200],
                })

                # Emit domain event
                yield ToolExecuted(
                    session_id=session_id,
                    agent_id=agent.id,
                    tool_name=tool_call.tool_name,
                    arguments=tool_call.arguments,
                    result_preview=result_str[:200],
                    success=True,
                )

                # Add tool result to messages
                messages.append(Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=[tool_call],
                ))
                messages.append(Message(
                    role="tool",
                    content=result_str[:1000],
                    tool_call_id=tool_call.id,
                ))

        agent.stop(session_id)

    async def _store_message(
        self,
        session: Session,
        agent: Agent,
        chunk_type: str,
        payload: dict,
    ) -> None:
        """Store a message in context."""
        chunk = ContextChunk.create(
            session_id=session.id,
            agent_id=agent.id,
            chunk_type=chunk_type,
            payload=payload,
            token_count=len(str(payload)) // 4,
        )
        await self._context_repo.add_chunk(chunk)

    def _assemble_prompt(
        self,
        session: Session,
        agent: Agent,
        recent_chunks: list[ContextChunk],
        query: str,
    ) -> str:
        """Assemble a prompt from context chunks."""
        # Delegates to a PromptAssembler domain service
        assembler = PromptAssembler(session.token_budget.total)
        return assembler.assemble(
            system_prompt=agent.system_prompt,
            goal=session.goal,
            recent_chunks=[c.to_dict() for c in recent_chunks],
            retrieved_chunks=[],
            query=query,
        )
```

### 5.2 `ContextService`

```python
# ah/application/context_service.py
from __future__ import annotations

import uuid
from typing import Optional

from ah.domain.context import Context, ContextChunk
from ah.infrastructure.repositories.context_repository import ContextRepository


class ContextService:
    """Application service — context operations.

    Thin coordinator: delegates to the Context aggregate and repository.
    """

    def __init__(self, context_repo: ContextRepository) -> None:
        self._context_repo = context_repo

    async def add_chunk(
        self,
        session_id: uuid.UUID,
        agent_id: str,
        chunk_type: str,
        payload: dict,
        token_count: int = 0,
        embedding: Optional[list[float]] = None,
    ) -> ContextChunk:
        """Add a context chunk."""
        chunk = ContextChunk.create(
            session_id=session_id,
            agent_id=agent_id,
            chunk_type=chunk_type,
            payload=payload,
            token_count=token_count,
            embedding=embedding,
        )
        return await self._context_repo.add_chunk(chunk)

    async def get_recent(
        self,
        session_id: uuid.UUID,
        limit: int = 10,
    ) -> list[ContextChunk]:
        """Get recent context chunks."""
        return await self._context_repo.get_chunks(session_id, limit=limit)

    async def search(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.7,
    ) -> list[tuple[ContextChunk, float]]:
        """Search context by embedding similarity."""
        return await self._context_repo.search_by_embedding(
            session_id, query_embedding, top_k, threshold
        )

    async def get_token_usage(self, session_id: uuid.UUID) -> int:
        """Get total token usage for a session."""
        return await self._context_repo.get_token_usage(session_id)
```

---

## 6. Dependency Injection

The current codebase uses global singletons. The DDD approach uses constructor injection:

```python
# ah/composition_root.py
"""Composition root — wires up dependencies."""

from ah.db.connection import Database
from ah.infrastructure.repositories.session_repository import SessionRepository
from ah.infrastructure.repositories.context_repository import ContextRepository
from ah.infrastructure.tools import ToolRegistry
from ah.infrastructure.llm import get_provider
from ah.application.agent_service import AgentService
from ah.application.context_service import ContextService


async def create_agent_service(
    provider: str = "openrouter",
    model: Optional[str] = None,
) -> AgentService:
    """Factory — create an AgentService with all dependencies wired."""
    db = Database()
    await db.connect()

    session_repo = SessionRepository(db.pool)
    context_repo = ContextRepository(db.pool)
    tool_registry = ToolRegistry()
    # Import built-in tools to register them
    from ah.tools import builtins  # noqa: F401

    llm_provider = get_provider(provider=provider, model=model)

    return AgentService(
        session_repo=session_repo,
        context_repo=context_repo,
        tool_registry=tool_registry,
        llm_provider=llm_provider,
    )
```

**Why this matters:** The composition root is the single place where dependencies are wired. The domain layer has no knowledge of the infrastructure. Tests can inject fakes:

```python
# tests/test_agent_service.py
async def test_agent_service():
    # Arrange
    session_repo = FakeSessionRepository()
    context_repo = FakeContextRepository()
    tool_registry = FakeToolRegistry()
    llm_provider = FakeLLMProvider()

    service = AgentService(
        session_repo=session_repo,
        context_repo=context_repo,
        tool_registry=tool_registry,
        llm_provider=llm_provider,
    )

    # Act
    response = await service.run(session_id, "hello")

    # Assert
    assert response.content == "..."
```

---

## 7. Migration Path

This is not a rewrite. It is a **strangler fig** pattern — gradually replace the old architecture with the new one.

### Phase 1: Introduce Value Objects (1-2 days)

- [ ] Create `ah/domain/value_objects.py` with `TokenBudget`, `Message`, `ToolCall`
- [ ] Replace raw dicts in `ReActAgent.run()` with `Message` and `ToolCall`
- [ ] Replace `context_budget: int` with `token_budget: TokenBudget` in `Session`
- [ ] All existing tests should pass

### Phase 2: Introduce Domain Events (1 day)

- [ ] Create `ah/domain/events.py` with all domain events
- [ ] Add `_domain_events` list to `Session`, `Agent`, `Context`
- [ ] Emit events on state changes
- [ ] Add `pull_domain_events()` method
- [ ] All existing tests should pass

### Phase 3: Introduce Repositories (2-3 days)

- [ ] Create `ah/infrastructure/repositories/` package
- [ ] Implement `SessionRepository` and `ContextRepository`
- [ ] Refactor `SessionManager` to use `SessionRepository` internally
- [ ] Refactor `ContextManager` to use `ContextRepository` internally
- [ ] All existing tests should pass

### Phase 4: Introduce Application Services (2-3 days)

- [ ] Create `ah/application/` package
- [ ] Implement `AgentService` and `ContextService`
- [ ] Refactor `ReActAgent.run()` to delegate to `AgentService`
- [ ] Refactor CLI to use application services
- [ ] All existing tests should pass

### Phase 5: Introduce Aggregate Roots (3-5 days)

- [ ] Create `ah/domain/session.py`, `ah/domain/agent.py`, `ah/domain/context.py`
- [ ] Move business logic from managers to aggregates
- [ ] Enforce invariants in aggregates
- [ ] All existing tests should pass

### Phase 6: Clean Up (1-2 days)

- [ ] Remove old `SessionManager`, `ContextManager`, `ReActAgent`
- [ ] Remove global singletons
- [ ] Update all imports
- [ ] All tests should pass

**Total estimated effort:** 10-16 days

---

## 8. What This Buys Us

### 8.1 Maintainability

| Before | After |
|--------|-------|
| `session.state["key"]` — untyped, no IDE support | `session.token_budget.remaining` — typed, autocompleted |
| `json.loads(tc["function"]["arguments"])` — duplicated, error-prone | `ToolCall.from_llm_response(raw_tc)` — centralized, validated |
| `if chunk_type in ("tool_call", "user_message"): ...` — OCP violation | `chunk.to_prompt_text()` — polymorphic, extensible |
| `session_manager.get(id)` — hidden global dependency | `session_repo.get(id)` — explicit, injectable |

### 8.2 Testability

| Before | After |
|--------|-------|
| `patch("ah.core.agent.session_manager")` — fragile | `FakeSessionRepository()` — clean, isolated |
| Test requires database | Test requires only in-memory fakes |
| Test requires LLM provider | Test requires only a fake provider |

### 8.3 Evolvability

| Before | After |
|--------|-------|
| Add a new chunk type: modify `_compress_chunk()`, schema, agent | Add a new chunk type: create a new `ChunkType` subclass |
| Add a new domain event: modify the emitter | Add a new domain event: create a new event class, add a handler |
| Change persistence: modify every manager | Change persistence: modify the repository |

### 8.4 Domain Clarity

The domain model makes the business rules explicit:

- **"A session cannot accept context that exceeds its budget"** → `Session.can_accept_context()`
- **"Used tokens cannot exceed the total budget"** → `TokenBudget.__post_init__()`
- **"A tool call must have a name and valid JSON arguments"** → `ToolCall.from_llm_response()`
- **"An archived session cannot be re-archived"** → `Session.archive()`

These rules are now **in the domain**, not scattered through a 130-line method.

---

## 9. Addressing the Pragmatic Critique

The pragmatic proposal argues that the current architecture is "good enough to ship" and that a rewrite would be "expensive, risky, and would delay shipping." This proposal agrees with the pragmatist on one point: **do not rewrite**. But it disagrees on another point: **the current architecture is not good enough**.

The pragmatic proposal says:
> "Build a rich domain model" — `Session.state` as `dict` is fine for now. Add validation when the state schema is actually defined.

This proposal says: **the state schema is already defined** — it is defined by every place that reads and writes `session.state`. The problem is that it is defined implicitly, in scattered code, rather than explicitly, in a domain model. The cost of not making it explicit is paid every time a developer touches the code.

The pragmatic proposal says:
> "The singletons are a feature, not a bug."

This proposal says: singletons are a feature **for a CLI tool that runs one agent per invocation**. But AgentHarness is not just a CLI tool — it is a **framework**. Frameworks need to be testable, configurable, and extensible. Singletons prevent all three.

The pragmatic proposal says:
> "Don't rewrite. Iterate."

This proposal agrees. The migration path above is incremental — each phase is independently shippable and testable. The value objects can be introduced in a day. The domain events can be introduced in a day. The repositories can be introduced in a few days. The application services can be introduced in a few days. The aggregate roots can be introduced in a few days.

**The cost of not doing this is not zero.** It is paid every time a developer spends 30 minutes understanding a 130-line method. It is paid every time a bug is introduced because two copies of the same logic diverged. It is paid every time a new feature requires modifying a method that already does too much.

---

## 10. Conclusion

AgentHarness has a solid foundation: a working ReAct loop, async PostgreSQL with pgvector, MessagePack context storage, and a functional CLI. But the codebase has no domain model. Business logic is scattered across managers that are little more than SQL wrappers. Domain data is stored in untyped `dict[str, Any]` bags. There are no domain events, no repositories, and no application services.

This proposal introduces a Domain-Driven Design architecture that makes the domain explicit, testable, and evolvable. It is not a rewrite — it is a gradual migration that can be completed in 10-16 days, with each phase independently shippable and testable.

The goal is not academic purity. It is to make the codebase **maintainable**: when a developer needs to add a new tool type, change the token budgeting strategy, or add a new context retrieval algorithm, they should modify one well-understood class — not hunt through a 130-line method.

**The domain is the product. Model it explicitly.**

---

*This proposal is intentionally ambitious. It prioritizes long-term maintainability over short-term shipping speed. The migration path is designed to be incremental — each phase delivers value independently.*
