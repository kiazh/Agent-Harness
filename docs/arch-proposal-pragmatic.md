# AgentHarness: Pragmatic Architecture Proposal

**Date:** 2026-09-30
**Author:** Architect (Pragmatic Path)
**Verdict:** The current architecture is ~80% correct. A rewrite would be expensive, risky, and would delay shipping. Instead, make 5 targeted incremental improvements that fix the worst structural issues without disrupting what already works.

---

## Executive Summary

The codebase has a solid foundation: a working ReAct loop, async PostgreSQL with pgvector, MessagePack context storage, decorator-based tool registry, 1,588 lines of tests, and a functional CLI. The architectural critiques are *academically* correct but *practically* over-engineered for a v0.1.0 project.

**The case against a rewrite:**

1. **The ReAct loop works.** It handles tool calling, iteration limits, context persistence, token budgeting, and error recovery. Rewriting it risks introducing bugs into the one component that's actually functional.
2. **The test suite is real.** 1,588 lines of tests with mocked databases, mocked HTTP providers, and CLI integration tests. A rewrite means rewriting all of these.
3. **The dependency graph is shallow.** `agent → provider`, `agent → session`, `agent → context`, `agent → tools`. That's four dependencies — not a tangled mess.
4. **The "God Object" critique is overstated.** `ReActAgent.run()` is 130 lines, but it's a *linear* orchestration loop, not a nested tangle. It's readable top-to-bottom in 30 seconds.
5. **The singletons are a feature, not a bug.** For a CLI tool that runs one agent per invocation, global singletons are the *correct* pattern. DI containers are for long-running services with complex lifecycle management.

**The case for incremental improvement:**

The critiques identified real issues — duplicate tools, mixed responsibilities in `context.py`, no service layer, and testability gaps. These can be fixed in days, not weeks, without touching the working core.

---

## What's Actually Wrong (The Short List)

| # | Issue | Severity | Fix Effort |
|---|-------|----------|------------|
| 1 | Duplicate tool definitions in `builtins.py` | HIGH (bug) | 15 min |
| 2 | `PromptAssembler` lives in `context.py` | MEDIUM (organization) | 30 min |
| 3 | Global singletons are hard to test in isolation | MEDIUM (testability) | 1 hr |
| 4 | No service layer — CLI talks directly to managers | MEDIUM (separation) | 1 hr |
| 5 | `run()` and `run_stream()` share 80% duplicate code | LOW (maintenance) | 30 min |

Everything else in the critiques — speculative schema, circular dependencies, lack of DI container, no domain model, no transaction management — is either already handled, not yet needed, or over-engineered for the current scale.

---

## Proposal: 5 Incremental Improvements

### 1. Remove Duplicate Tool Definitions (15 min)

**Problem:** `ah/tools/builtins.py` defines `read_file`, `write_file`, `list_files`, and `terminal` — all of which are also defined in `ah/tools/file.py` and `ah/tools/terminal.py`. The `builtins.py` versions are registered later and silently override the module versions. The implementations differ subtly (line numbers, `workdir` support).

**Fix:** Delete the duplicate definitions from `builtins.py`. Keep only `web_search`, `web_extract`, and `search_files` (which have no separate module). Import `file` and `terminal` modules for their side-effect registrations.

```python
# ah/tools/builtins.py — AFTER
"""Built-in tools — web search, web extract, and file search."""
from __future__ import annotations

import os
import re
from typing import Optional

import httpx

from ah.tools.base import registry
from ah.tools import file  # noqa: F401 — registers read_file, write_file, list_files
from ah.tools import terminal  # noqa: F401 — registers terminal


@registry.register(description="Search the web for information")
def web_search(query: str, limit: int = 5) -> str:
    ...


@registry.register(description="Extract content from a URL")
def web_extract(url: str) -> str:
    ...


@registry.register(description="Search file contents with regex")
def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None) -> str:
    ...
```

**Why this matters:** This is a correctness bug. The `builtins.py` versions lack `workdir` support and have different output formats. A reviewer who spots this will question the entire codebase.

**Risk:** Near zero. The module versions are strictly better (more features, better validation).

---

### 2. Extract `PromptAssembler` from `context.py` (30 min)

**Problem:** `ah/core/context.py` contains three unrelated abstractions: `ContextChunk` (data model), `ContextManager` (data access), and `PromptAssembler` (presentation logic). These are different abstraction levels.

**Fix:** Move `PromptAssembler` to `ah/core/assembler.py`. Update imports in `agent.py` and tests.

```python
# ah/core/assembler.py — NEW FILE
"""Prompt assembly — build LLM prompts from context chunks with token budget."""
from __future__ import annotations

from typing import Any

from ah.core.context import ContextChunk, get_token_count


class PromptAssembler:
    """Assembles prompts from context chunks with token budget."""

    def __init__(self, session_budget: int = 8000) -> None:
        self.session_budget = session_budget

    def assemble(self, ...) -> str:
        ...

    def _compress_chunk(self, chunk_data: dict[str, Any]) -> str:
        ...

    def _estimate_tokens(self, text: str) -> int:
        ...
```

**Changes:**
- `ah/core/context.py`: Remove `PromptAssembler` class, keep `ContextChunk`, `ContextManager`, `TokenCounter`, `get_token_count`
- `ah/core/assembler.py`: New file with `PromptAssembler`
- `ah/core/agent.py`: Change `from ah.core.context import PromptAssembler` → `from ah.core.assembler import PromptAssembler`
- `tests/test_basic.py` and `tests/test_comprehensive.py`: Update import

**Why this matters:** `context.py` is currently 331 lines mixing SQL, MessagePack, and prompt formatting. Separating prompt assembly makes both modules focused and testable.

**Risk:** Low. Pure code movement, no behavior change.

---

### 3. Make Singletons Testable Without Eliminating Them (1 hr)

**Problem:** Global singletons (`db`, `session_manager`, `context_manager`, `registry`, `skill_registry`) make it impossible to test components in isolation. The current tests use `unittest.mock.patch()` to replace singletons, which is fragile and verbose.

**Fix:** Add a `reset()` function to each manager and a `configure()` function to `Database`. This keeps the singleton pattern (correct for a CLI tool) but allows tests to swap implementations.

```python
# ah/db/connection.py — ADD
class Database:
    ...
    @classmethod
    def configure(cls, dsn: str) -> "Database":
        """Create a new Database instance and replace the global singleton."""
        global db
        db = cls(dsn)
        return db


# ah/core/session.py — ADD
class SessionManager:
    ...
    @classmethod
    def reset(cls) -> None:
        """Reset the global singleton (for testing)."""
        global session_manager
        session_manager = cls()


# ah/core/context.py — ADD
class ContextManager:
    ...
    @classmethod
    def reset(cls) -> None:
        """Reset the global singleton (for testing)."""
        global context_manager
        context_manager = cls()


# ah/tools/base.py — ADD
class ToolRegistry:
    ...
    @classmethod
    def reset(cls) -> None:
        """Reset the global registry (for testing)."""
        global registry
        registry = cls()
```

**Test fixture:**

```python
# tests/conftest.py — NEW FILE
import pytest
from ah.db.connection import Database, db
from ah.core.session import SessionManager, session_manager
from ah.core.context import ContextManager, context_manager
from ah.tools.base import ToolRegistry, registry


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset all global singletons before each test."""
    Database.configure("postgresql://localhost/test")
    SessionManager.reset()
    ContextManager.reset()
    ToolRegistry.reset()
    yield
    # Cleanup after test
```

**Why this matters:** The current `patch("ah.core.agent.session_manager")` approach works but is brittle — it patches the *reference* in the agent module, not the singleton itself. A `reset()` method is cleaner and allows true isolation.

**Risk:** Low. The singletons still exist; we're just adding a way to replace them.

---

### 4. Add a Thin Service Layer (1 hr)

**Problem:** The CLI (`ah/cli.py`) directly calls `session_manager`, `context_manager`, and `ReActAgent`. This means business logic (session selection, agent creation, error handling) lives in the CLI commands.

**Fix:** Add `ah/services/` with two services that encapsulate the most common operations:

```python
# ah/services/__init__.py
from ah.services.agent_service import AgentService
from ah.services.session_service import SessionService

__all__ = ["AgentService", "SessionService"]
```

```python
# ah/services/session_service.py
"""Session service — high-level session operations."""
from __future__ import annotations

import uuid
from typing import Optional

from ah.core.session import Session, session_manager


class SessionService:
    """High-level session operations."""

    @staticmethod
    async def get_or_create(
        session_id: str | None = None,
        continue_last: bool = False,
        title: str | None = None,
        goal: str | None = None,
    ) -> Optional[Session]:
        """Get an existing session or create a new one."""
        if continue_last:
            return await session_manager.get_last_active()
        if session_id:
            return await session_manager.get(uuid.UUID(session_id))
        return await session_manager.create(title=title, goal=goal)

    @staticmethod
    async def list_recent(status: str | None = None, limit: int = 20) -> list[Session]:
        """List recent sessions."""
        return await session_manager.list_sessions(status=status, limit=limit)
```

```python
# ah/services/agent_service.py
"""Agent service — high-level agent operations."""
from __future__ import annotations

import uuid
from typing import AsyncGenerator

from ah.core.agent import AgentResponse, ReActAgent
from ah.core.provider import LLMProvider, StreamEvent, get_provider
from ah.core.session import session_manager
from ah.core.context import context_manager
from ah.core.assembler import PromptAssembler


class AgentService:
    """High-level agent operations."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        max_iterations: int = 10,
    ) -> None:
        self.provider = provider or get_provider()
        self.max_iterations = max_iterations

    async def run(
        self,
        session_id: uuid.UUID,
        message: str,
        verbose: bool = True,
    ) -> AgentResponse:
        """Run the agent for a user message."""
        agent = ReActAgent(
            provider=self.provider,
            max_iterations=self.max_iterations,
        )
        return await agent.run(session_id, message, verbose=verbose)

    async def run_stream(
        self,
        session_id: uuid.UUID,
        message: str,
        verbose: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run the agent with streaming output."""
        agent = ReActAgent(
            provider=self.provider,
            max_iterations=self.max_iterations,
        )
        async for event in agent.run_stream(session_id, message, verbose=verbose):
            yield event
```

**CLI simplification:**

```python
# ah/cli.py — SIMPLIFIED
@app.command()
def chat(
    message: str = typer.Argument(None),
    continue_: bool = typer.Option(False, "--continue", "-c"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s"),
    model: str = typer.Option(None, "--model", "-m"),
    provider: str = typer.Option("openrouter", "--provider", "-p"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q"),
):
    """Chat with the agent."""
    from ah.services import AgentService, SessionService

    async def _chat():
        await db.connect()
        try:
            session = await SessionService.get_or_create(
                session_id=session_id,
                continue_last=continue_,
                title=message[:50] if message else None,
                goal=message[:100] if message else None,
            )
            if not session:
                console.print("[red]No session available.[/red]")
                raise typer.Exit(1)

            service = AgentService(provider=get_provider(provider=provider, model=model))
            # ... streaming logic ...
        finally:
            await db.close()

    _run(_chat())
```

**Why this matters:** The service layer is a *thin* abstraction — it doesn't hide the managers, it just composes them. This makes the CLI commands shorter and gives a natural place to add caching, logging, or metrics later.

**Risk:** Low. The service layer is a pure refactoring — no behavior change.

---

### 5. Deduplicate `run()` and `run_stream()` (30 min)

**Problem:** `ReActAgent.run()` and `ReActAgent.run_stream()` share ~80% identical code: session lookup, prompt assembly, tool execution, context storage, message formatting. The only difference is that `run_stream()` yields `StreamEvent` objects.

**Fix:** Extract the shared logic into a private `_run_loop()` method that yields events, and have `run()` collect them:

```python
# ah/core/agent.py — REFACTORED
class ReActAgent:
    ...
    async def _run_loop(
        self,
        session_id: uuid.UUID,
        user_message: str,
        verbose: bool,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Core ReAct loop — yields events."""
        session = await session_manager.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        assembler = PromptAssembler(session.context_budget)

        await context_manager.add_chunk(
            session_id=session_id,
            agent_id=self.agent_id,
            chunk_type="user_message",
            payload={"content": user_message},
            token_count=len(user_message) // 4,
        )

        recent = await context_manager.get_recent_context(session_id, limit=5)
        prompt = assembler.assemble(
            system_prompt=self.system_prompt,
            goal=session.goal,
            recent_chunks=recent,
            retrieved_chunks=[],
            query=user_message,
        )

        messages = [{"role": "user", "content": prompt}]
        total_tokens = 0
        tool_calls_made = []

        for iteration in range(self.max_iterations):
            if verbose:
                console.print(f"[dim]Iteration {iteration + 1}/{self.max_iterations}[/dim]")

            if total_tokens >= MAX_TOKEN_BUDGET:
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(
                        content="Reached maximum token budget.",
                        tool_calls=tool_calls_made,
                        tokens_used=total_tokens,
                        iterations=iteration,
                    ),
                )
                return

            tool_defs = registry.get_tool_definitions()

            try:
                response = await self._call_llm_with_retry(messages=messages, tools=tool_defs)
            except Exception as e:
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(
                        content=f"LLM provider error: {e}",
                        tool_calls=tool_calls_made,
                        tokens_used=total_tokens,
                        iterations=iteration + 1,
                    ),
                )
                return

            total_tokens += response.usage.get("total_tokens", 0)
            yield StreamEvent(type="token_usage", tokens_used=total_tokens)

            if not response.tool_calls:
                await context_manager.add_chunk(
                    session_id=session_id,
                    agent_id=self.agent_id,
                    chunk_type="assistant_message",
                    payload={"content": response.content},
                    token_count=len(response.content) // 4,
                )
                await session_manager.update_activity(session_id)
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(
                        content=response.content,
                        tool_calls=tool_calls_made,
                        tokens_used=total_tokens,
                        iterations=iteration + 1,
                    ),
                )
                return

            for tc in response.tool_calls:
                # ... tool execution logic ...
                yield StreamEvent(type="tool_call", tool_name=tool_name, tool_args=tool_args)
                # ... execute tool ...
                yield StreamEvent(type="tool_result", tool_name=tool_name, tool_result=result_str)

        yield StreamEvent(
            type="done",
            response=AgentResponse(
                content="Reached max iterations.",
                tool_calls=tool_calls_made,
                tokens_used=total_tokens,
                iterations=self.max_iterations,
            ),
        )

    async def run(self, session_id: uuid.UUID, user_message: str, verbose: bool = True) -> AgentResponse:
        """Run the ReAct loop and return the final response."""
        final_response = None
        async for event in self._run_loop(session_id, user_message, verbose):
            if event.type == "done":
                final_response = event.response
        return final_response or AgentResponse(content="No response generated.")

    async def run_stream(self, session_id: uuid.UUID, user_message: str, verbose: bool = True) -> AsyncGenerator[StreamEvent, None]:
        """Run the ReAct loop with streaming output."""
        async for event in self._run_loop(session_id, user_message, verbose):
            yield event
```

**Why this matters:** Currently, any bug fix in `run()` must be manually applied to `run_stream()`. This is how bugs breed.

**Risk:** Medium. This is the most invasive change. Must ensure `run()` still returns the same `AgentResponse` type.

---

## What We're NOT Doing (And Why)

| Critique | Why We're Skipping It |
|----------|----------------------|
| "Remove global singletons, add DI container" | For a CLI tool that runs one agent per invocation, singletons are correct. DI is for long-running services. |
| "Split LLMProvider into complete/embed interfaces" | The `embed()` method is used by the RAG pipeline (coming in Phase 5). YAGNI. |
| "Remove speculative schema tables" | The schema is already written. Removing tables requires migrations. Do it when the tables are actually needed. |
| "Add transaction management" | The current operations are single-statement. Add transactions when multi-step operations are introduced. |
| "Build a rich domain model" | `Session.state` as `dict` is fine for now. Add validation when the state schema is actually defined. |
| "Add a TUI" | The CLI works. A TUI is a Phase 3 feature. |
| "Add LangGraph" | The ReAct loop is a legitimate orchestration pattern. LangGraph adds complexity for marginal benefit at this scale. |
| "Add caching, rate limiting, observability" | These are operational concerns. Add them when the system is deployed and metrics show bottlenecks. |

---

## Implementation Order

| Step | Change | Effort | Risk | Dependencies |
|------|--------|--------|------|--------------|
| 1 | Remove duplicate tools | 15 min | Near zero | None |
| 2 | Extract `PromptAssembler` | 30 min | Low | None |
| 3 | Add singleton `reset()` methods | 1 hr | Low | None |
| 4 | Add service layer | 1 hr | Low | Step 2 (for `PromptAssembler` import) |
| 5 | Deduplicate `run()`/`run_stream()` | 30 min | Medium | None |

**Total effort:** ~3 hours
**Total risk:** Low (steps 1-4), Medium (step 5)
**Test impact:** All 1,588 lines of tests should pass after each step.

---

## Success Criteria

- [ ] All duplicate tool definitions removed from `builtins.py`
- [ ] `PromptAssembler` lives in `ah/core/assembler.py`
- [ ] All managers have `reset()` class methods
- [ ] `tests/conftest.py` resets singletons before each test
- [ ] `ah/services/` package exists with `AgentService` and `SessionService`
- [ ] CLI uses service layer instead of calling managers directly
- [ ] `run()` and `run_stream()` share `_run_loop()` implementation
- [ ] All 1,588 lines of tests pass
- [ ] No behavior change visible to users

---

## The Bottom Line

The current architecture is **good enough to ship**. The critiques identified real issues, but most are academic purity concerns that don't affect functionality, performance, or security. The five incremental improvements above fix the worst structural problems in ~3 hours without touching the working core.

**Don't rewrite. Iterate.**

---

*This proposal is intentionally pragmatic. It prioritizes shipping over purity, working code over perfect architecture, and incremental improvement over risky rewrites.*
