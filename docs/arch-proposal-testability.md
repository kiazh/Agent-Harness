# Testability Architecture Proposal — AgentHarness

**Verdict: The current codebase is structurally untestable. Not because tests are missing, but because the architecture makes meaningful tests impossible to write without monkey-patching global state.**

---

## 1. The Problem: Global Singletons and Tight Coupling

### 1.1 The Singleton Web

The codebase is built on **five module-level singletons** that are instantiated at import time and shared across the entire application:

| Singleton | File | Created At | Injected Into |
|-----------|------|------------|---------------|
| `db` | `ah/db/connection.py:78` | Import | `SessionManager`, `ContextManager` |
| `session_manager` | `ah/core/session.py:178` | Import | `ReActAgent`, CLI |
| `context_manager` | `ah/core/context.py:331` | Import | `ReActAgent`, CLI |
| `registry` | `ah/tools/base.py:122` | Import | `ReActAgent`, all tool modules |
| `skill_registry` | `ah/skills/registry.py:111` | Import | CLI |

These singletons are **not** injected. They are **imported directly** by consumers:

```python
# ah/core/session.py — SessionManager uses db directly
from ah.db.connection import db

class SessionManager:
    async def create(self, ...):
        row = await db.fetchrow(...)  # ← direct global reference

# ah/core/agent.py — ReActAgent uses session_manager and context_manager directly
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.tools.base import registry

class ReActAgent:
    async def run(self, ...):
        session = await session_manager.get(session_id)  # ← direct global
        await context_manager.add_chunk(...)              # ← direct global
        tool_defs = registry.get_tool_definitions()      # ← direct global
```

### 1.2 Why This Makes Testing Impossible

**You cannot test `ReActAgent.run()` without a real database.** The agent calls `session_manager.get()`, which calls `db.fetchrow()`, which calls `asyncpg`. There is no seam to inject a fake.

**You cannot test `SessionManager.create()` without a real database.** The manager calls `db.fetchrow()` directly. The only way to test it is to monkey-patch the global `db` object, which:

- Mutates global state that leaks between tests
- Requires `patch("ah.core.session.db")` — patching the *consumer's* reference, not the *producer's*
- Makes tests order-dependent (if one test forgets to clean up, the next test sees the mock)
- Cannot run tests in parallel (all tests share the same global `db`)

**You cannot test tool registration in isolation.** The `@registry.register()` decorator mutates the global `registry` at import time. If you import `ah.tools.builtins`, it registers tools on the global registry. You cannot create a fresh registry for a test without reloading the module.

**You cannot test the CLI without a real database.** Every CLI command calls `await db.connect()` at the top. There is no way to inject a test double.

### 1.3 The Test Suite Confirms This

The existing test suite (`tests/test_comprehensive.py`, 1588 lines) works around the singleton problem with increasingly desperate measures:

```python
# Test 1: Patch the global db in the consumer module
with patch("ah.core.session.db") as mock_db:
    mock_db.fetchrow = AsyncMock(return_value=mock_session_row)
    session = await session_manager.create(title="Test Session")

# Test 2: Patch the global db in a different consumer module
with patch("ah.core.context.db") as mock_db:
    mock_db.fetchrow = AsyncMock(return_value=mock_chunk_row)
    chunk = await context_manager.add_chunk(...)

# Test 3: Patch the global session_manager in the agent module
with patch("ah.core.agent.session_manager") as mock_sm:
    mock_sm.get = AsyncMock(return_value=mock_session)
    response = await agent.run(...)

# Test 4: Patch the global context_manager in the agent module
with patch("ah.core.agent.context_manager") as mock_cm:
    mock_cm.add_chunk = AsyncMock()
    response = await agent.run(...)

# Test 5: Patch the global registry in the agent module
# (implicit — the agent uses the real registry, so tests must import all tool modules)
```

This is **not testing**. This is **mocking the entire system**. The tests verify that the code *calls* `db.fetchrow()`, not that the SQL is correct, the msgpack round-trips, or the schema matches.

### 1.4 The Coupling Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        ReActAgent                            │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ session_mgr │  │ context_mgr  │  │ tool registry     │  │
│  │  (global)   │  │  (global)    │  │   (global)        │  │
│  └──────┬──────┘  └──────┬───────┘  └─────────┬─────────┘  │
└─────────┼────────────────┼────────────────────┼────────────┘
          │                │                    │
          ▼                ▼                    ▼
   ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐
   │     db      │  │     db       │  │  tool functions   │
   │  (global)   │  │  (global)    │  │  (side effects)   │
   └──────┬──────┘  └──────────────┘  └───────────────────┘
          │
          ▼
   ┌─────────────┐
   │  asyncpg    │
   │  (real DB)  │
   └─────────────┘
```

Every path from the agent to the database goes through a global singleton. There is no interface, no abstraction, no seam for testing.

---

## 2. The Solution: Five-Part Testability Architecture

### 2.1 Dependency Injection Container

**Principle:** Singletons should be *resolved*, not *imported*. A container composes the object graph at startup and injects dependencies through constructors.

**Proposal:** Introduce a lightweight DI container (no framework needed — a simple class suffices):

```python
# ah/container.py
from dataclasses import dataclass, field
from typing import Any

@dataclass
class Container:
    """Holds all application dependencies. Composed at startup."""
    db: Any = None
    session_manager: Any = None
    context_manager: Any = None
    tool_registry: Any = None
    skill_registry: Any = None
    provider: Any = None

    @classmethod
    def production(cls) -> "Container":
        """Build the production container with real implementations."""
        from ah.db.connection import Database
        from ah.core.session import SessionManager
        from ah.core.context import ContextManager
        from ah.tools.base import ToolRegistry
        from ah.skills.registry import SkillRegistry
        from ah.core.provider import get_provider

        db = Database()
        return cls(
            db=db,
            session_manager=SessionManager(db=db),
            context_manager=ContextManager(db=db),
            tool_registry=ToolRegistry(),
            skill_registry=SkillRegistry(),
            provider=get_provider(),
        )

    @classmethod
    def testing(cls, db=None, provider=None) -> "Container":
        """Build a test container with fakes."""
        from ah.core.session import SessionManager
        from ah.core.context import ContextManager
        from ah.tools.base import ToolRegistry
        from ah.skills.registry import SkillRegistry

        db = db or FakeDatabase()
        return cls(
            db=db,
            session_manager=SessionManager(db=db),
            context_manager=ContextManager(db=db),
            tool_registry=ToolRegistry(),
            skill_registry=SkillRegistry(),
            provider=provider or FakeProvider(),
        )
```

**Usage in CLI:**

```python
# ah/cli.py
from ah.container import Container

container = Container.production()

@app.command()
def chat(message: str = ...):
    async def _chat():
        await container.db.connect()
        try:
            agent = ReActAgent(
                provider=container.provider,
                session_manager=container.session_manager,
                context_manager=container.context_manager,
                tool_registry=container.tool_registry,
            )
            ...
        finally:
            await container.db.close()
    _run(_chat())
```

**Usage in tests:**

```python
# tests/test_agent.py
import pytest
from ah.container import Container

@pytest.fixture
def container():
    return Container.testing()

async def test_agent_run_no_tool_calls(container):
    agent = ReActAgent(
        provider=container.provider,
        session_manager=container.session_manager,
        context_manager=container.context_manager,
        tool_registry=container.tool_registry,
    )
    response = await agent.run(session_id, "test message")
    assert response.content == "Test response"
```

**Benefit:** No more `patch("ah.core.agent.session_manager")`. The agent receives its dependencies through the constructor. Tests build a container with fakes; production builds a container with real implementations.

### 2.2 Interface-Based Design (Protocols)

**Principle:** Define `Protocol` classes (structural subtyping) for every external boundary. Depend on protocols, not concrete classes.

**Proposal:**

```python
# ah/protocols.py
from typing import Protocol, Any, AsyncGenerator, Optional
import uuid

class DatabaseProtocol(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def fetch(self, query: str, *args) -> list[Any]: ...
    async def fetchrow(self, query: str, *args) -> Any | None: ...
    async def fetchval(self, query: str, *args) -> Any: ...
    async def execute(self, query: str, *args) -> str: ...

class SessionManagerProtocol(Protocol):
    async def create(self, **kwargs) -> Any: ...
    async def get(self, session_id: uuid.UUID) -> Any | None: ...
    async def update_state(self, session_id: uuid.UUID, state: dict) -> None: ...
    async def update_activity(self, session_id: uuid.UUID) -> None: ...
    async def list_sessions(self, **kwargs) -> list[Any]: ...

class ContextManagerProtocol(Protocol):
    async def add_chunk(self, **kwargs) -> Any: ...
    async def get_recent_context(self, session_id: uuid.UUID, limit: int = 10) -> list[dict]: ...
    async def get_chunks(self, session_id: uuid.UUID, **kwargs) -> list[Any]: ...

class ToolRegistryProtocol(Protocol):
    def get_tool_definitions(self) -> list[Any]: ...
    async def execute(self, name: str, **kwargs) -> Any: ...
    def list_tools(self) -> list[str]: ...

class ProviderProtocol(Protocol):
    async def complete(self, messages: list[dict], **kwargs) -> Any: ...
    async def stream_complete(self, messages: list[dict], **kwargs) -> AsyncGenerator[Any, None]: ...
    async def embed(self, text: str) -> list[float]: ...
```

**Usage in ReActAgent:**

```python
# ah/core/agent.py
from ah.protocols import ProviderProtocol, SessionManagerProtocol, ContextManagerProtocol, ToolRegistryProtocol

class ReActAgent:
    def __init__(
        self,
        provider: ProviderProtocol,
        session_manager: SessionManagerProtocol,
        context_manager: ContextManagerProtocol,
        tool_registry: ToolRegistryProtocol,
        max_iterations: int = 10,
        agent_id: str = "harness",
        system_prompt: str | None = None,
    ) -> None:
        self.provider = provider
        self.session_manager = session_manager
        self.context_manager = context_manager
        self.tool_registry = tool_registry
        ...
```

**Benefit:** Any object that satisfies the protocol can be used. A `FakeDatabase` that implements `DatabaseProtocol` is a drop-in replacement for `Database`. No monkey-patching needed.

### 2.3 Mockable Boundaries (Fakes, Not Mocks)

**Principle:** For each protocol, provide a `Fake*` implementation that behaves realistically but uses in-memory data. Fakes are reusable across tests and don't require `unittest.mock`.

**Proposal:**

```python
# ah/testing/fakes.py
import uuid
from datetime import datetime
from typing import Any

class FakeDatabase:
    """In-memory database fake. Implements DatabaseProtocol."""
    def __init__(self):
        self.sessions: dict[uuid.UUID, dict] = {}
        self.context_chunks: dict[uuid.UUID, list[dict]] = {}
        self.connected = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def fetch(self, query: str, *args) -> list[dict]:
        if "FROM sessions" in query:
            return list(self.sessions.values())
        if "FROM context_chunks" in query:
            session_id = args[0]
            return self.context_chunks.get(session_id, [])
        return []

    async def fetchrow(self, query: str, *args) -> dict | None:
        if "FROM sessions" in query:
            return self.sessions.get(args[0])
        return None

    async def fetchval(self, query: str, *args):
        if "COUNT(*)" in query and "sessions" in query:
            return len(self.sessions)
        if "COUNT(*)" in query and "context_chunks" in query:
            return sum(len(v) for v in self.context_chunks.values())
        return 0

    async def execute(self, query: str, *args) -> str:
        if "INSERT INTO sessions" in query:
            session = {
                "id": uuid.uuid4(),
                "title": args[0],
                "agent_id": args[1],
                "status": "active",
                "state_msgpack": b"\x80",
                "goal": args[3],
                "model": args[4],
                "provider": args[5],
                "context_budget": args[6],
                "created_at": datetime.utcnow(),
                "last_activity": datetime.utcnow(),
            }
            self.sessions[session["id"]] = session
            return "INSERT 0 1"
        if "UPDATE sessions" in query:
            return "UPDATE 1"
        if "DELETE FROM context_chunks" in query:
            session_id = args[0]
            count = len(self.context_chunks.get(session_id, []))
            self.context_chunks[session_id] = []
            return f"DELETE {count}"
        return "OK"

class FakeProvider:
    """Scripted provider fake. Returns pre-programmed responses."""
    def __init__(self, responses: list[Any] | None = None):
        self.responses = responses or []
        self.call_count = 0
        self.calls: list[dict] = []

    async def complete(self, messages: list[dict], **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        return LLMResponse(content="Done", model="fake", usage={"total_tokens": 0})

    async def stream_complete(self, messages: list[dict], **kwargs):
        response = await self.complete(messages, **kwargs)
        yield StreamEvent(type="done", response=response)

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1536
```

**Usage in tests:**

```python
# tests/test_agent_with_fakes.py
import pytest
from ah.testing.fakes import FakeDatabase, FakeProvider
from ah.core.provider import LLMResponse
from ah.container import Container

@pytest.fixture
def fake_db():
    return FakeDatabase()

@pytest.fixture
def fake_provider():
    return FakeProvider(responses=[
        LLMResponse(content="Hello!", model="fake", usage={"total_tokens": 5}),
    ])

@pytest.fixture
def container(fake_db, fake_provider):
    return Container.testing(db=fake_db, provider=fake_provider)

async def test_agent_returns_response(container):
    agent = ReActAgent(
        provider=container.provider,
        session_manager=container.session_manager,
        context_manager=container.context_manager,
        tool_registry=container.tool_registry,
    )
    # Seed a session
    session = await container.session_manager.create(title="test")
    response = await agent.run(session.id, "hello")
    assert response.content == "Hello!"
    assert response.iterations == 1
```

**Benefit:** Tests are self-contained, deterministic, and fast. No `unittest.mock`, no monkey-patching, no global state. Each test gets a fresh `FakeDatabase` and `FakeProvider`.

### 2.4 Test Fixtures and Builders

**Principle:** Provide pytest fixtures and builder patterns for common test scenarios. Don't make every test re-create the same setup.

**Proposal:**

```python
# tests/conftest.py
import pytest
import uuid
from datetime import datetime
from ah.container import Container
from ah.testing.fakes import FakeDatabase, FakeProvider
from ah.core.provider import LLMResponse, StreamEvent

# ─── Container Fixtures ───────────────────────────────────────────

@pytest.fixture
def fake_db():
    return FakeDatabase()

@pytest.fixture
def fake_provider():
    return FakeProvider()

@pytest.fixture
def container(fake_db, fake_provider):
    return Container.testing(db=fake_db, provider=fake_provider)

@pytest.fixture
def populated_container(container):
    """Container with a pre-existing session and context."""
    # Seed data
    return container

# ─── Session Fixtures ─────────────────────────────────────────────

@pytest.fixture
async def session(container):
    """Create a real session in the fake database."""
    return await container.session_manager.create(
        title="Test Session",
        goal="Test goal",
    )

@pytest.fixture
async def session_with_context(container, session):
    """Create a session with pre-existing context chunks."""
    await container.context_manager.add_chunk(
        session_id=session.id,
        agent_id="harness",
        chunk_type="user_message",
        payload={"content": "previous message"},
        token_count=3,
    )
    return session

# ─── Provider Fixtures ────────────────────────────────────────────

@pytest.fixture
def provider_with_tool_call():
    """Provider that returns a tool call then a final answer."""
    return FakeProvider(responses=[
        LLMResponse(
            content="",
            model="fake",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "read_file", "arguments": '{"path": "/tmp/test"}'},
            }],
        ),
        LLMResponse(content="The file contains: hello", model="fake", usage={"total_tokens": 5}),
    ])

@pytest.fixture
def provider_always_tool_call():
    """Provider that always returns tool calls (for max_iterations test)."""
    return FakeProvider(responses=[
        LLMResponse(
            content="",
            model="fake",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "read_file", "arguments": '{"path": "/tmp/test"}'},
            }],
        ),
    ] * 100)  # Enough for any max_iterations

# ─── Agent Fixtures ───────────────────────────────────────────────

@pytest.fixture
def agent(container):
    from ah.core.agent import ReActAgent
    return ReActAgent(
        provider=container.provider,
        session_manager=container.session_manager,
        context_manager=container.context_manager,
        tool_registry=container.tool_registry,
    )

@pytest.fixture
def agent_with_tool_call(container, provider_with_tool_call):
    from ah.core.agent import ReActAgent
    container.provider = provider_with_tool_call
    return ReActAgent(
        provider=container.provider,
        session_manager=container.session_manager,
        context_manager=container.context_manager,
        tool_registry=container.tool_registry,
    )

# ─── Tool Fixtures ────────────────────────────────────────────────

@pytest.fixture
def temp_file(tmp_path):
    """Create a temporary file with known content."""
    f = tmp_path / "test.txt"
    f.write_text("hello world\nfoo bar\n", encoding="utf-8")
    return f

@pytest.fixture
def registered_tool(container):
    """Register a simple tool and return its name."""
    @container.tool_registry.register(description="Test tool")
    def test_tool(x: int) -> int:
        return x * 2
    return "test_tool"
```

**Benefit:** Tests read like specifications. `async def test_agent_reads_file(agent, session, temp_file):` — no setup boilerplate, no mocking.

### 2.5 Property-Based Testing

**Principle:** Use Hypothesis to generate hundreds of test cases automatically. Find edge cases you didn't think of.

**Proposal:**

```python
# tests/test_property_based.py
import pytest
from hypothesis import given, settings, strategies as st
from ah.core.context import PromptAssembler

class TestPromptAssemblerProperties:
    """Property-based tests for PromptAssembler."""

    @given(
        system_prompt=st.text(min_size=0, max_size=500),
        goal=st.text(min_size=0, max_size=200),
        query=st.text(min_size=0, max_size=500),
        budget=st.integers(min_value=100, max_value=10000),
    )
    @settings(max_examples=100)
    def test_assemble_never_crashes(self, system_prompt, goal, query, budget):
        """PromptAssembler.assemble() should never raise an exception."""
        assembler = PromptAssembler(session_budget=budget)
        try:
            prompt = assembler.assemble(
                system_prompt=system_prompt,
                goal=goal or None,
                recent_chunks=[],
                retrieved_chunks=[],
                query=query,
            )
            assert isinstance(prompt, str)
        except Exception as e:
            pytest.fail(f"PromptAssembler.assemble() raised {e!r}")

    @given(
        system_prompt=st.text(min_size=1, max_size=100),
        goal=st.text(min_size=1, max_size=100),
        query=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=50)
    def test_assemble_includes_required_parts(self, system_prompt, goal, query):
        """Assembled prompt should always include system prompt, goal, and query."""
        assembler = PromptAssembler(session_budget=10000)
        prompt = assembler.assemble(
            system_prompt=system_prompt,
            goal=goal,
            recent_chunks=[],
            retrieved_chunks=[],
            query=query,
        )
        assert system_prompt in prompt
        assert goal in prompt
        assert query in prompt

    @given(
        text=st.text(min_size=0, max_size=1000),
    )
    @settings(max_examples=100)
    def test_estimate_tokens_non_negative(self, text):
        """Token count should always be non-negative."""
        assembler = PromptAssembler()
        tokens = assembler._estimate_tokens(text)
        assert tokens >= 0

    @given(
        text=st.text(min_size=0, max_size=1000),
    )
    @settings(max_examples=100)
    def test_estimate_tokens_proportional_to_length(self, text):
        """Longer text should generally produce more tokens."""
        assembler = PromptAssembler()
        tokens = assembler._estimate_tokens(text)
        # len//4 fallback: tokens should be <= len(text)
        assert tokens <= max(len(text), 1)

    @given(
        chunk_type=st.sampled_from([
            "tool_call", "user_message", "result", "assistant_message",
            "memory", "heartbeat", "system", "user", "assistant", "unknown"
        ]),
        payload=st.dictionaries(
            st.sampled_from(["content", "tool", "args", "status", "result", "prompt", "message"]),
            st.text(min_size=0, max_size=200),
            min_size=0,
            max_size=5,
        ),
    )
    @settings(max_examples=100)
    def test_compress_chunk_never_crashes(self, chunk_type, payload):
        """_compress_chunk should handle any chunk type and payload."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({"type": chunk_type, "payload": payload})
        assert isinstance(result, str)
        assert len(result) > 0
```

**Benefit:** Property-based tests find edge cases that example-based tests miss. They verify *invariants* (things that should always be true) rather than specific outputs.

---

## 3. Migration Path

### Phase 1: Introduce Protocols (Non-Breaking)

1. Create `ah/protocols.py` with all protocol definitions
2. Add type annotations to existing classes (they already satisfy the protocols structurally)
3. No behavior change — purely additive

### Phase 2: Add Constructor Injection (Non-Breaking)

1. Add optional constructor parameters to `SessionManager`, `ContextManager`, `ReActAgent`
2. Default to the global singletons if no injection is provided (backward compatible)
3. Update CLI to use the container

```python
class SessionManager:
    def __init__(self, db=None):
        self._db = db or _global_db  # fallback to singleton
```

### Phase 3: Add Fakes and Fixtures

1. Create `ah/testing/fakes.py` with `FakeDatabase`, `FakeProvider`
2. Create `tests/conftest.py` with all fixtures
3. Write new tests using fakes and fixtures

### Phase 4: Migrate Existing Tests

1. Replace `patch("ah.core.session.db")` tests with fake-based tests
2. Replace `patch("ah.core.agent.session_manager")` tests with constructor injection
3. Remove `pytest.skip("PostgreSQL not available")` — use fakes instead

### Phase 5: Property-Based Tests

1. Add `hypothesis` to dev dependencies
2. Write property-based tests for `PromptAssembler`, `ToolRegistry`, `SkillParser`
3. Run in CI with `@settings(max_examples=100)`

---

## 4. What This Enables

### 4.1 True Unit Tests

```python
# Before: requires monkey-patching globals
with patch("ah.core.agent.session_manager") as mock_sm:
    mock_sm.get = AsyncMock(return_value=mock_session)
    response = await agent.run(...)

# After: clean constructor injection
agent = ReActAgent(provider=fake_provider, session_manager=fake_session_mgr, ...)
response = await agent.run(...)
```

### 4.2 Integration Tests with Fakes

```python
async def test_agent_stores_tool_result_in_context(container, session):
    """Verify the full loop: LLM → tool → context → LLM."""
    # Provider returns a tool call, then a final answer
    container.provider = FakeProvider(responses=[
        LLMResponse(content="", tool_calls=[{
            "id": "call_1",
            "function": {"name": "read_file", "arguments": '{"path": "/tmp/test"}'},
        }]),
        LLMResponse(content="Done", model="fake"),
    ])

    agent = ReActAgent(
        provider=container.provider,
        session_manager=container.session_manager,
        context_manager=container.context_manager,
        tool_registry=container.tool_registry,
    )

    response = await agent.run(session.id, "read the file")

    # Verify the tool result was stored in context
    chunks = await container.context_manager.get_chunks(session.id)
    tool_chunks = [c for c in chunks if c.chunk_type == "tool_call"]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].payload["tool"] == "read_file"
```

### 4.3 Deterministic, Fast, Parallel Tests

- No shared global state → tests can run in parallel with `pytest-xdist`
- No database → tests run in milliseconds, not seconds
- No mocking → tests verify behavior, not implementation

### 4.4 Property-Based Edge Case Discovery

```python
# Hypothesis will find inputs like:
# - Empty strings
# - Very long strings
# - Unicode characters
# - Special characters that break JSON
# - Negative numbers
# - Zero
# - Maximum integer values
```

---

## 5. Summary

| Current State | Proposed State |
|---------------|----------------|
| 5 global singletons | 1 DI container |
| Direct imports of globals | Constructor injection via protocols |
| `patch("ah.core.session.db")` | `FakeDatabase()` passed to constructor |
| `pytest.skip("PostgreSQL not available")` | `Container.testing()` with fakes |
| 136 tests that mock everything | Tests that verify real behavior |
| Tests that assert failure as success | Tests that verify invariants |
| No property-based tests | Hypothesis-generated edge cases |
| 1.33s test suite (mostly skips) | Millisecond test suite (real assertions) |

**The goal is not more tests. The goal is tests that mean something.**

---

*This proposal is additive and backward-compatible. No existing code needs to change in Phase 1-2. The migration can be done incrementally, one test at a time.*
