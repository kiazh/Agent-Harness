# Functional Architecture Proposal for AgentHarness

> **Author**: Functional Programming Advocate  
> **Date**: 2026-09-30  
> **Status**: Proposal

---

## Executive Summary

AgentHarness is a well-structured Python async agent framework. But it is built on a foundation of **mutable global singletons**, **side-effect-laden methods**, and **inheritance-based polymorphism**. These are not stylistic choices — they are the root cause of every class of bug that plagues long-running agent systems: race conditions, stale state, untestable code, and action-at-a-distance.

This proposal argues for a **functional core, imperative shell** architecture:

1. **Immutable data structures** for all domain state
2. **Pure functions** for all business logic
3. **IO monad** (or equivalent effect type) for side effects
4. **Function composition** over inheritance
5. **Algebraic data types** for domain modeling

The goal is not academic purity — it is **bug elimination by construction**.

---

## The Problem: Mutable State Is the Root of All Bugs

### Current State: A Web of Mutable Singletons

```python
# ah/db/connection.py
db = Database()                    # global singleton, mutable _pool

# ah/core/context.py
context_manager = ContextManager() # global singleton, no state but relies on db

# ah/core/session.py
session_manager = SessionManager() # global singleton, no state but relies on db

# ah/tools/base.py
registry = ToolRegistry()          # global singleton, mutable _tools dict

# ah/skills/registry.py
skill_registry = SkillRegistry()   # global singleton, mutable _skills dict
```

Every module instantiates a **global mutable singleton** at import time. These singletons are:

- **Shared across all concurrent agent runs** — two `ah chat` processes share the same `registry`, the same `db` pool, the same `context_manager`.
- **Mutated at runtime** — `ToolRegistry.register()` mutates `self._tools`, `SkillRegistry.load_all()` mutates `self._skills`.
- **Impossible to test in isolation** — you cannot instantiate a fresh `ToolRegistry` without also importing the global one and dealing with its state.
- **Impossible to reason about locally** — a function's behavior depends on what has been registered globally, not on its inputs.

### The ReAct Loop: A Mutable State Machine Disguised as a Loop

```python
# ah/core/agent.py — ReActAgent.run()
async def run(self, session_id, user_message, verbose=True):
    session = await session_manager.get(session_id)        # read global state
    assembler = PromptAssembler(session.context_budget)    # derived from state
    
    await context_manager.add_chunk(...)                    # write global state
    recent = await context_manager.get_recent_context(...)  # read global state
    
    prompt = assembler.assemble(...)                        # pure-ish, but uses global token counter
    
    messages = [{"role": "user", "content": prompt}]         # local mutable list
    
    for iteration in range(self.max_iterations):
        tool_defs = registry.get_tool_definitions()        # read global mutable registry
        response = await self._call_llm_with_retry(...)     # side effect: network call
        
        total_tokens += response.usage.get("total_tokens", 0)  # mutate local accumulator
        
        for tc in response.tool_calls:
            result = await registry.execute(tool_name, **tool_args)  # side effect: tool execution
            
            await context_manager.add_chunk(...)            # write global state
            messages.append(...)                            # mutate local list
            messages.append(...)                            # mutate local list again
```

The `run()` method is a **side-effect soup**: it reads from global singletons, writes to global singletons, makes network calls, executes tools, and mutates local accumulators — all interleaved in a single 200-line method. There is no way to test any part of this in isolation.

### Specific Bugs This Architecture Causes

| Bug Class | Root Cause | Example |
|-----------|-----------|---------|
| **Race conditions** | Global mutable singletons shared across concurrent runs | Two agents register tools simultaneously → `dict` corruption |
| **Stale state** | `context_manager` caches nothing but `session_manager` reads stale `last_activity` | Agent reads session, another process updates it, agent writes based on stale data |
| **Untestable code** | Methods depend on global singletons, not injectable dependencies | Cannot test `ReActAgent.run()` without a live database and registered tools |
| **Action at a distance** | `registry.execute()` can invoke any registered tool with arbitrary kwargs | A tool registered in `builtins.py` can be called by name from any agent run |
| **Hidden coupling** | `PromptAssembler` uses global `_token_counter` singleton | Token counting behavior changes depending on whether `tiktoken` is installed |
| **Resource leaks** | `db` singleton pool is never explicitly closed in most code paths | If `db.connect()` fails, `db.close()` is never called; pool leaks |

---

## The Solution: Functional Core, Imperative Shell

The architecture follows the **functional core, imperative shell** pattern:

```
┌─────────────────────────────────────────────────────┐
│  Imperative Shell (I/O, DB, Network, CLI)           │
│  - Reads input, calls pure functions, writes output │
│  - Contains all side effects                        │
├─────────────────────────────────────────────────────┤
│  Functional Core (Pure Business Logic)              │
│  - Immutable data transformations                   │
│  - No side effects, no global state                 │
│  - 100% testable, 100% composable                   │
└─────────────────────────────────────────────────────┘
```

---

## 1. Immutable Data Structures

### Principle

**All domain data is immutable.** State changes are represented as new values, not mutations.

### Current Code (Mutable)

```python
@dataclass
class Session:
    id: uuid.UUID
    title: str | None = None
    status: str = "active"
    state: dict = field(default_factory=dict)  # mutable dict!
    goal: str | None = None
    context_budget: int = 8000
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
```

`Session.state` is a mutable `dict`. Any code can do `session.state["key"] = value` and mutate it in place. The `Session` dataclass itself is mutable — you can do `session.status = "archived"` and the original object changes.

### Proposed Code (Immutable)

```python
from dataclasses import dataclass, replace
from typing import FrozenSet, Mapping, Sequence
from immutables import Map  # or frozenset, tuple, MappingProxyType

@dataclass(frozen=True)
class Session:
    """An agent session — the unit of work. Immutable."""
    id: uuid.UUID
    title: str | None = None
    status: str = "active"
    state: Mapping[str, Any] = field(default_factory=dict)  # read-only mapping
    goal: str | None = None
    model: str | None = None
    provider: str | None = None
    context_budget: int = 8000
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    
    def with_status(self, status: str) -> "Session":
        """Return a new Session with updated status."""
        return replace(self, status=status)
    
    def with_goal(self, goal: str) -> "Session":
        """Return a new Session with updated goal."""
        return replace(self, goal=goal)
    
    def with_state(self, key: str, value: Any) -> "Session":
        """Return a new Session with an updated state key."""
        new_state = dict(self.state)
        new_state[key] = value
        return replace(self, state=new_state)
    
    def touch(self) -> "Session":
        """Return a new Session with updated last_activity."""
        return replace(self, last_activity=datetime.utcnow())
```

Key changes:
- `frozen=True` prevents attribute mutation
- `state` is a read-only `Mapping`, not a mutable `dict`
- State changes return **new instances** via `dataclasses.replace()`
- No method mutates `self` — they all return new values

### Benefits

- **Thread-safe by default** — no locks needed for concurrent reads
- **Time-travel debugging** — keep old versions of a `Session` to see how it evolved
- **Predictable** — a `Session` object means the same thing everywhere in the codebase
- **Cache-friendly** — immutable values can be safely cached and shared

---

## 2. Pure Functions for Business Logic

### Principle

**All business logic is in pure functions**: same input → same output, no side effects.

### Current Code (Impure)

```python
# ah/core/context.py — PromptAssembler
class PromptAssembler:
    def __init__(self, session_budget: int = 8000) -> None:
        self.session_budget = session_budget  # mutable instance state
    
    def assemble(self, system_prompt, goal, recent_chunks, retrieved_chunks, query) -> str:
        parts = []
        used_tokens = 0
        # ... mutates parts, used_tokens, remaining ...
        return "\n".join(parts)
    
    def _compress_chunk(self, chunk_data: dict) -> str:
        # ... uses self._estimate_tokens which uses global _token_counter ...
    
    def _estimate_tokens(self, text: str) -> int:
        return get_token_count(text)  # calls global _token_counter singleton
```

`PromptAssembler` holds mutable state (`self.session_budget`), and `_estimate_tokens` delegates to a global `_token_counter` singleton. You cannot test `assemble()` without the global token counter being initialized.

### Proposed Code (Pure)

```python
from dataclasses import dataclass
from typing import Callable, Sequence

# Token counting is an injectable function, not a global singleton
TokenCounter = Callable[[str], int]

def default_token_counter(text: str) -> int:
    """Default token counter using tiktoken or len//4 fallback."""
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return len(text) // 4

@dataclass(frozen=True)
class PromptConfig:
    """Configuration for prompt assembly. Immutable."""
    system_prompt: str
    goal: str | None
    query: str
    recent_chunks: Sequence[dict]
    retrieved_chunks: Sequence[tuple["ContextChunk", float]]
    token_budget: int
    token_counter: TokenCounter = default_token_counter

def assemble_prompt(config: PromptConfig) -> str:
    """Pure function: assemble a prompt within the token budget.
    
    Given the same PromptConfig, always returns the same string.
    No side effects, no global state.
    """
    parts = []
    used_tokens = 0
    
    # System prompt (always included)
    parts.append(config.system_prompt)
    used_tokens += config.token_counter(config.system_prompt)
    
    # Goal
    if config.goal:
        goal_text = f"\n\n## Current Goal\n{config.goal}"
        parts.append(goal_text)
        used_tokens += config.token_counter(goal_text)
    
    # Current query
    query_text = f"\n\n## Current Query\n{config.query}"
    parts.append(query_text)
    used_tokens += config.token_counter(query_text)
    
    # Recent chunks (last 3 actions, compressed)
    if config.recent_chunks:
        recent_text = "\n\n## Recent Activity\n"
        for chunk_data in config.recent_chunks[:3]:
            compressed = compress_chunk(chunk_data, config.token_counter)
            recent_text += compressed + "\n"
        parts.append(recent_text)
        used_tokens += config.token_counter(recent_text)
    
    # Retrieved chunks (fill remaining budget)
    remaining = config.token_budget - used_tokens
    if config.retrieved_chunks and remaining > 100:
        retrieved_text = "\n\n## Relevant Context\n"
        for chunk, sim in config.retrieved_chunks:
            compressed = compress_chunk(
                {"type": chunk.chunk_type, "payload": chunk.payload},
                config.token_counter,
            )
            chunk_tokens = config.token_counter(compressed)
            if chunk_tokens > remaining:
                compressed = compressed[:remaining * 4]
                retrieved_text += compressed + "...\n"
                break
            retrieved_text += compressed + "\n"
            remaining -= chunk_tokens
            if remaining < 50:
                break
        parts.append(retrieved_text)
    
    return "\n".join(parts)

def compress_chunk(chunk_data: dict, token_counter: TokenCounter) -> str:
    """Pure function: compress a context chunk into minimal text."""
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
        status = payload.get("status", "ok")
        result = payload.get("result", payload.get("content", ""))
        if isinstance(result, str) and len(result) > 200:
            result = result[:200] + "..."
        return f"  -> {status}: {result}"
    # ... etc ...
```

Key changes:
- `PromptAssembler` class → `PromptConfig` frozen dataclass + `assemble_prompt()` pure function
- Token counter is an **injectable function**, not a global singleton
- `compress_chunk` is a pure function that takes the token counter as a parameter
- No mutable instance state, no global state

### Benefits

- **Testable**: `assemble_prompt(config)` can be tested with any `PromptConfig` and any token counter
- **Composable**: pure functions can be chained, memoized, and parallelized
- **Refactoring-safe**: changing `compress_chunk` cannot break `assemble_prompt` as long as the type signature is preserved
- **Concurrent-safe**: no shared mutable state means no race conditions

---

## 3. IO Monad for Side Effects

### Principle

**All side effects are wrapped in an IO monad** (or equivalent effect type). The functional core never performs I/O directly — it returns a description of what to do, and the imperative shell executes it.

### Current Code (Side Effects Everywhere)

```python
# ah/core/agent.py — ReActAgent.run()
async def run(self, session_id, user_message, verbose=True):
    session = await session_manager.get(session_id)           # I/O: DB read
    await context_manager.add_chunk(...)                       # I/O: DB write
    recent = await context_manager.get_recent_context(...)     # I/O: DB read
    response = await self._call_llm_with_retry(...)            # I/O: network call
    result = await registry.execute(tool_name, **tool_args)    # I/O: tool execution
    await context_manager.add_chunk(...)                       # I/O: DB write
    await session_manager.update_activity(session_id)          # I/O: DB write
```

Every line is a side effect. The method is impossible to test without mocking the entire world.

### Proposed Code (IO Monad Pattern)

In Python, we don't have a built-in IO monad, but we can model it with an `Effect` type that represents a computation to be performed:

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")
U = TypeVar("U")

@dataclass(frozen=True)
class Effect(Generic[T]):
    """A description of a side effect to be performed.
    
    This is NOT a monad in the strict Haskell sense, but it serves the same
    purpose: it separates the description of an effect from its execution.
    """
    run: Callable[[], Awaitable[T]]
    
    def map(self, f: Callable[[T], U]) -> "Effect[U]":
        """Transform the result of this effect."""
        return Effect(lambda: self.run().then(f) if hasattr(self.run(), 'then') 
                     else _map_async(self.run, f))
    
    def flat_map(self, f: Callable[[T], "Effect[U]"]) -> "Effect[U]":
        """Chain effects sequentially."""
        return Effect(lambda: _flat_map_async(self.run, f))
    
    async def run_async(self) -> T:
        """Execute the effect. Only called in the imperative shell."""
        return await self.run()

async def _map_async(awaitable, f):
    result = await awaitable
    return f(result)

async def _flat_map_async(awaitable, f):
    result = await awaitable
    effect = f(result)
    return await effect.run()
```

More practically, in Python we can use a simpler approach with `async` functions that are **only called at the boundary**:

```python
# ah/core/effects.py — Effect constructors for common operations

from dataclasses import dataclass
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# An Effect is just an async function that hasn't been called yet.
# The key insight: we pass these around without calling them.
Effect = Callable[[], Awaitable[T]]

# Constructors for effects (these don't execute anything)
def get_session(session_id: uuid.UUID) -> Effect[Session | None]:
    """Effect: get a session by ID."""
    async def _run():
        return await session_manager.get(session_id)
    return _run

def add_context_chunk(session_id, agent_id, chunk_type, payload, token_count=0) -> Effect[ContextChunk]:
    """Effect: add a context chunk."""
    async def _run():
        return await context_manager.add_chunk(
            session_id=session_id, agent_id=agent_id,
            chunk_type=chunk_type, payload=payload, token_count=token_count,
        )
    return _run

def call_llm(messages, tools) -> Effect[LLMResponse]:
    """Effect: call the LLM."""
    async def _run():
        return await provider.complete(messages=messages, tools=tools)
    return _run

def execute_tool(name, **kwargs) -> Effect[Any]:
    """Effect: execute a tool."""
    async def _run():
        return await registry.execute(name, **kwargs)
    return _run
```

Then the ReAct loop becomes a **pure function that returns a description of effects**:

```python
# ah/core/agent_loop.py — Pure agent loop

from dataclasses import dataclass
from typing import Sequence

@dataclass(frozen=True)
class AgentState:
    """Immutable state for one agent run."""
    session: Session
    messages: tuple[dict, ...]           # immutable tuple
    total_tokens: int
    tool_calls_made: tuple[dict, ...]    # immutable tuple
    iteration: int

@dataclass(frozen=True)
class AgentResult:
    """Result of an agent run."""
    content: str
    tool_calls: tuple[dict, ...]
    tokens_used: int
    iterations: int
    final_state: AgentState

# Pure function: given current state and LLM response, compute next state
def compute_next_state(
    state: AgentState,
    llm_response: LLMResponse,
    tool_results: Sequence[tuple[str, dict, str]],  # (tool_name, args, result)
) -> AgentState:
    """Pure function: compute the next agent state.
    
    No side effects. Given the same inputs, always returns the same output.
    """
    new_messages = list(state.messages)
    new_tool_calls = list(state.tool_calls_made)
    
    for tool_name, tool_args, result_str in tool_results:
        new_messages.append({
            "role": "assistant",
            "content": llm_response.content,
            "tool_calls": [{"function": {"name": tool_name, "arguments": str(tool_args)}}],
        })
        new_messages.append({
            "role": "tool",
            "content": result_str[:1000],
            "tool_call_id": "",
        })
        new_tool_calls.append({
            "tool": tool_name,
            "args": tool_args,
            "result_preview": result_str[:200],
        })
    
    return AgentState(
        session=state.session,
        messages=tuple(new_messages),
        total_tokens=state.total_tokens + llm_response.usage.get("total_tokens", 0),
        tool_calls_made=tuple(new_tool_calls),
        iteration=state.iteration + 1,
    )

# Pure function: decide whether to continue the loop
def should_continue(state: AgentState, max_iterations: int, max_tokens: int) -> bool:
    """Pure function: should the agent loop continue?"""
    return state.iteration < max_iterations and state.total_tokens < max_tokens

# Pure function: parse tool calls from LLM response
def parse_tool_calls(response: LLMResponse) -> list[tuple[str, dict]]:
    """Pure function: extract (tool_name, args) pairs from an LLM response."""
    results = []
    for tc in response.tool_calls:
        function_data = tc.get("function", {})
        tool_name = function_data.get("name")
        if not tool_name:
            continue
        try:
            tool_args = json.loads(function_data.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_args = {}
        results.append((tool_name, tool_args))
    return results
```

The **imperative shell** then executes the effects:

```python
# ah/core/agent.py — Imperative shell

class ReActAgent:
    def __init__(self, provider, max_iterations=10, agent_id="harness", system_prompt=None):
        self.provider = provider
        self.max_iterations = max_iterations
        self.agent_id = agent_id
        self.system_prompt = system_prompt or SYSTEM_PROMPT
    
    async def run(self, session_id: uuid.UUID, user_message: str) -> AgentResult:
        """Imperative shell: orchestrate effects."""
        # Effect 1: Get session
        session = await get_session(session_id)()
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        
        # Effect 2: Store user message
        await add_context_chunk(
            session_id=session_id, agent_id=self.agent_id,
            chunk_type="user_message", payload={"content": user_message},
            token_count=len(user_message) // 4,
        )()
        
        # Effect 3: Get recent context
        recent = await context_manager.get_recent_context(session_id, limit=5)()
        
        # Pure: assemble prompt
        config = PromptConfig(
            system_prompt=self.system_prompt,
            goal=session.goal,
            query=user_message,
            recent_chunks=recent,
            retrieved_chunks=[],
            token_budget=session.context_budget,
        )
        prompt = assemble_prompt(config)
        
        # Initialize state
        state = AgentState(
            session=session,
            messages=({"role": "user", "content": prompt},),
            total_tokens=0,
            tool_calls_made=(),
            iteration=0,
        )
        
        # Loop
        while should_continue(state, self.max_iterations, MAX_TOKEN_BUDGET):
            # Effect: Call LLM
            tool_defs = registry.get_tool_definitions()
            response = await self.provider.complete(
                messages=list(state.messages), tools=tool_defs,
            )
            
            # Pure: parse tool calls
            tool_calls = parse_tool_calls(response)
            
            if not tool_calls:
                # No tool calls — final answer
                await add_context_chunk(
                    session_id=session_id, agent_id=self.agent_id,
                    chunk_type="assistant_message", payload={"content": response.content},
                    token_count=len(response.content) // 4,
                )()
                await session_manager.update_activity(session_id)()
                return AgentResult(
                    content=response.content,
                    tool_calls=state.tool_calls_made,
                    tokens_used=state.total_tokens + response.usage.get("total_tokens", 0),
                    iterations=state.iteration + 1,
                    final_state=state,
                )
            
            # Effect: Execute tool calls
            tool_results = []
            for tool_name, tool_args in tool_calls:
                result = await execute_tool(tool_name, **tool_args)()
                result_str = str(result)
                
                # Effect: Store tool call in context
                await add_context_chunk(
                    session_id=session_id, agent_id=self.agent_id,
                    chunk_type="tool_call",
                    payload={"tool": tool_name, "args": tool_args, "result_preview": result_str[:500]},
                    token_count=len(result_str) // 4,
                )()
                
                tool_results.append((tool_name, tool_args, result_str))
            
            # Pure: compute next state
            state = compute_next_state(state, response, tool_results)
        
        return AgentResult(
            content="Reached max iterations. Partial results may be available.",
            tool_calls=state.tool_calls_made,
            tokens_used=state.total_tokens,
            iterations=state.iteration,
            final_state=state,
        )
```

### Benefits

- **Testable**: `compute_next_state`, `should_continue`, `parse_tool_calls` are pure functions — test them without any I/O
- **Composable**: effects can be chained, sequenced, and parallelized
- **Mockable**: to test the shell, you just replace the effect constructors
- **Auditable**: every side effect is explicit and visible in the shell

---

## 4. Function Composition Over Inheritance

### Principle

**Behavior is composed from small, reusable functions, not inherited from base classes.**

### Current Code (Inheritance)

```python
# ah/core/provider.py
class LLMProvider:
    """Base provider interface."""
    async def complete(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None):
        raise NotImplementedError
    
    async def stream_complete(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None):
        raise NotImplementedError
    
    async def embed(self, text):
        raise NotImplementedError

class OpenRouterProvider(LLMProvider):
    # ... implements all three methods ...

class OllamaProvider(LLMProvider):
    # ... implements all three methods ...
```

Problems:
- Adding a new provider means subclassing `LLMProvider` and implementing all methods
- The base class defines the interface, but there's no enforcement (just `raise NotImplementedError`)
- Common logic (retry, error handling) is duplicated or requires mixin classes
- Testing requires creating mock subclasses

### Proposed Code (Function Composition)

```python
# ah/core/provider.py — Composable provider functions

from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Optional

# A provider is just a record of functions, not a class hierarchy
@dataclass(frozen=True)
class Provider:
    """A language model provider — composed of functions."""
    complete: Callable[..., Awaitable["LLMResponse"]]
    stream_complete: Callable[..., AsyncGenerator["StreamEvent", None]]
    embed: Callable[[str], Awaitable[list[float]]]
    name: str

# Retry logic as a higher-order function
def with_retry(
    f: Callable[..., Awaitable[T]],
    max_attempts: int = 4,
    base_delay: float = 1.0,
) -> Callable[..., Awaitable[T]]:
    """Wrap an async function with exponential backoff retry."""
    async def _wrapped(*args, **kwargs):
        last_exception = None
        for attempt in range(max_attempts):
            try:
                return await f(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < max_attempts - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning("Attempt %d/%d failed: %s — retrying in %ds",
                                   attempt + 1, max_attempts, e, delay)
                    await asyncio.sleep(delay)
        raise last_exception
    return _wrapped

# Logging as a higher-order function
def with_logging(
    f: Callable[..., Awaitable[T]],
    label: str,
) -> Callable[..., Awaitable[T]]:
    """Wrap an async function with logging."""
    async def _wrapped(*args, **kwargs):
        logger.debug("%s: starting", label)
        try:
            result = await f(*args, **kwargs)
            logger.debug("%s: success", label)
            return result
        except Exception as e:
            logger.error("%s: failed: %s", label, e)
            raise
    return _wrapped

# Concrete providers built by composing functions
def create_openrouter_provider(
    api_key: str | None = None,
    model: str = "anthropic/claude-3.5-sonnet",
) -> Provider:
    """Create an OpenRouter provider by composing functions."""
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")
    
    client = httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=120.0,
    )
    
    async def complete(messages, model=None, temperature=0.7, max_tokens=4096, tools=None):
        # ... implementation ...
        pass
    
    async def stream_complete(messages, model=None, temperature=0.7, max_tokens=4096, tools=None):
        # ... implementation ...
        pass
    
    async def embed(text):
        # ... implementation ...
        pass
    
    return Provider(
        complete=with_logging(with_retry(complete), "openrouter.complete"),
        stream_complete=with_logging(with_retry(stream_complete), "openrouter.stream"),
        embed=with_logging(with_retry(embed), "openrouter.embed"),
        name="openrouter",
    )

def create_ollama_provider(
    model: str = "llama3.1",
    base_url: str = "http://localhost:11434",
) -> Provider:
    """Create an Ollama provider by composing functions."""
    client = httpx.AsyncClient(base_url=base_url, timeout=120.0)
    
    async def complete(messages, model=None, temperature=0.7, max_tokens=4096, tools=None):
        # ... implementation ...
        pass
    
    async def stream_complete(messages, model=None, temperature=0.7, max_tokens=4096, tools=None):
        # ... implementation ...
        pass
    
    async def embed(text):
        # ... implementation ...
        pass
    
    return Provider(
        complete=with_logging(with_retry(complete), "ollama.complete"),
        stream_complete=with_logging(with_retry(stream_complete), "ollama.stream"),
        embed=with_logging(with_retry(embed), "ollama.embed"),
        name="ollama",
    )

# Factory function (replaces the old get_provider)
def create_provider(provider: str = "openrouter", model: str | None = None) -> Provider:
    """Factory: return a configured provider."""
    if provider == "openrouter":
        return create_openrouter_provider(model=model or "anthropic/claude-3.5-sonnet")
    elif provider == "ollama":
        return create_ollama_provider(model=model or "llama3.1")
    else:
        raise ValueError(f"Unknown provider: {provider}")
```

### Benefits

- **No inheritance**: providers are just records of functions
- **Composable**: `with_retry`, `with_logging` wrap any function
- **Testable**: test `with_retry` once, apply it everywhere
- **Extensible**: add a new provider by writing new functions, not subclassing
- **No base class bloat**: no `LLMProvider` base class with `raise NotImplementedError`

---

## 5. Algebraic Data Types for Domain Modeling

### Principle

**Domain concepts are modeled as algebraic data types (sums and products), not as classes with behavior.**

### Current Code (Classes with Behavior)

```python
# ah/tools/base.py
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable
    is_async: bool = False

class ToolRegistry:
    def register(self, name=None, description=None, parameters=None):
        def decorator(func):
            # ... mutates self._tools ...
            return func
        return decorator
    
    async def execute(self, name, **kwargs):
        # ... looks up and calls ...
```

Problems:
- `Tool` is a data class but `ToolRegistry` is a mutable class with behavior
- Tool registration is a side effect (mutates the registry)
- Tool execution is a side effect (calls arbitrary functions)
- No type safety: `parameters` is a raw `dict`, not a validated schema

### Proposed Code (Algebraic Data Types)

```python
# ah/tools/types.py — Algebraic data types for tools

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Literal, Union

# Product type: a tool definition is a product of its fields
@dataclass(frozen=True)
class ToolDefinition:
    """A tool definition — pure data, no behavior."""
    name: str
    description: str
    parameters: dict  # JSON Schema
    is_async: bool

# Sum type: a tool result is either success or failure
@dataclass(frozen=True)
class ToolSuccess:
    """Tool execution succeeded."""
    tool_name: str
    result: Any

@dataclass(frozen=True)
class ToolFailure:
    """Tool execution failed."""
    tool_name: str
    error: str

ToolResult = Union[ToolSuccess, ToolFailure]

# Sum type: a tool call is either valid or invalid
@dataclass(frozen=True)
class ValidToolCall:
    """A valid tool call."""
    name: str
    args: dict

@dataclass(frozen=True)
class InvalidToolCall:
    """An invalid tool call."""
    raw: dict
    error: str

ParsedToolCall = Union[ValidToolCall, InvalidToolCall]

# Pure function: parse a tool call from LLM output
def parse_tool_call(tc: dict) -> ParsedToolCall:
    """Pure function: parse a tool call from LLM output."""
    function_data = tc.get("function", {})
    tool_name = function_data.get("name")
    if not tool_name:
        return InvalidToolCall(raw=tc, error="Tool call missing 'name' field")
    
    try:
        tool_args = json.loads(function_data.get("arguments", "{}"))
    except json.JSONDecodeError as e:
        return InvalidToolCall(raw=tc, error=f"Invalid JSON in tool arguments: {e}")
    
    return ValidToolCall(name=tool_name, args=tool_args)

# Pure function: execute a tool (returns a description, not a side effect)
def execute_tool_effect(
    tool_name: str,
    args: dict,
    registry: Mapping[str, ToolDefinition],
) -> Effect[ToolResult]:
    """Create an effect that executes a tool."""
    async def _run():
        tool_def = registry.get(tool_name)
        if tool_def is None:
            return ToolFailure(tool_name=tool_name, error=f"Tool '{tool_name}' not registered")
        try:
            # In a real implementation, this would call the tool function
            result = await _invoke_tool(tool_def, args)
            return ToolSuccess(tool_name=tool_name, result=result)
        except Exception as e:
            return ToolFailure(tool_name=tool_name, error=str(e))
    return Effect(_run)

# Pure function: format a tool result for the LLM
def format_tool_result(result: ToolResult) -> str:
    """Pure function: format a tool result for the LLM."""
    match result:
        case ToolSuccess(tool_name=_, result=r):
            return str(r)
        case ToolFailure(tool_name=_, error=e):
            return f"Error: {e}"
```

### Benefits

- **Exhaustive matching**: `match` statements force you to handle all cases
- **Type safety**: `ToolResult = ToolSuccess | ToolFailure` is a closed union
- **No hidden state**: `ToolDefinition` is pure data, not a class with behavior
- **Composable**: `parse_tool_call` → `execute_tool_effect` → `format_tool_result` is a pipeline

---

## Putting It All Together: The Functional AgentHarness

### Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│  Imperative Shell (ah/cli.py, ah/core/agent.py)              │
│  - Reads CLI args, calls pure functions, writes output       │
│  - Contains all side effects (DB, network, file I/O)         │
│  - Orchestrates effects                                      │
├──────────────────────────────────────────────────────────────┤
│  Functional Core (ah/core/pure/)                             │
│  - assemble_prompt(config: PromptConfig) -> str               │
│  - compute_next_state(state, response, results) -> AgentState│
│  - should_continue(state, max_iter, max_tokens) -> bool      │
│  - parse_tool_call(tc: dict) -> ParsedToolCall               │
│  - format_tool_result(result: ToolResult) -> str             │
│  - compress_chunk(chunk: dict, counter: TokenCounter) -> str │
│  - All pure, all immutable, all testable                    │
├──────────────────────────────────────────────────────────────┤
│  Domain Types (ah/core/types.py)                             │
│  - Session, ContextChunk, AgentState, AgentResult            │
│  - ToolDefinition, ToolResult, ParsedToolCall                │
│  - All frozen dataclasses, all algebraic data types          │
├──────────────────────────────────────────────────────────────┤
│  Effect Constructors (ah/core/effects.py)                    │
│  - get_session(id) -> Effect[Session | None]                 │
│  - add_context_chunk(...) -> Effect[ContextChunk]            │
│  - call_llm(messages, tools) -> Effect[LLMResponse]          │
│  - execute_tool(name, args) -> Effect[ToolResult]            │
│  - All return descriptions of side effects, not side effects │
└──────────────────────────────────────────────────────────────┘
```

### Migration Path

The migration is incremental — you don't rewrite everything at once:

1. **Phase 1**: Make `Session`, `ContextChunk`, `AgentState` frozen dataclasses
2. **Phase 2**: Extract `PromptAssembler.assemble()` into a pure function
3. **Phase 3**: Extract `compute_next_state`, `should_continue`, `parse_tool_call` as pure functions
4. **Phase 4**: Wrap side effects in `Effect` types
5. **Phase 5**: Replace `ToolRegistry` class with `ToolDefinition` + effect constructors
6. **Phase 6**: Replace `LLMProvider` inheritance with `Provider` record of functions

Each phase is independently shippable and testable.

---

## Addressing Objections

### "This is too academic for a Python project"

Python is a multi-paradigm language. `dataclasses`, `frozen=True`, `typing.Union`, and `match` statements are all standard library features. The functional style is not about monads and category theory — it is about **making illegal states unrepresentable** and **making side effects explicit**.

### "Performance will suffer"

Immutable data structures have overhead, but:
- AgentHarness is I/O-bound (LLM calls, DB queries), not CPU-bound
- The overhead of copying a small dict is negligible compared to a 120-second LLM call
- `immutables.Map` and `frozendict` provide immutable data structures with near-dict performance

### "We'll have to rewrite everything"

No. The migration is incremental. Start with frozen dataclasses, extract pure functions, wrap effects. Each step is independently valuable.

### "Async doesn't fit functional programming"

Async is just an effect. `Effect[T]` wraps an `Awaitable[T]`. The functional core is synchronous and pure; the shell is async and impure. They compose cleanly.

---

## Conclusion

The current AgentHarness architecture is built on mutable global singletons and side-effect-laden methods. This is not a style preference — it is the root cause of race conditions, untestable code, and action-at-a-distance bugs.

The functional architecture proposed here:

1. **Immutable data structures** eliminate entire classes of bugs by making state changes explicit
2. **Pure functions** make business logic testable and composable
3. **IO monad** (effect types) make side effects explicit and mockable
4. **Function composition** replaces fragile inheritance hierarchies
5. **Algebraic data types** make illegal states unrepresentable

The result is a system where **bugs are impossible by construction**, not just harder to write.

---

## References

- [Functional Core, Imperative Shell — Gary Bernhardt](https://www.destroyallsoftware.com/talks/boundaries)
- [The Functional Programming Paradigm — John Hughes](https://www.cs.kent.ac.uk/people/staff/dat/miranda/whyfp90.pdf)
- [Domain Modeling Made Functional — Scott Wlaschin](https://pragprog.com/titles/swdddf/domain-modeling-made-functional/)
- [immutables — Python immutable data structures](https://github.com/MagicStack/immutables)
