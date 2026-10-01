# AgentHarness: Minimalist Architecture Proposal

**Premise:** AgentHarness is a single-user, single-process AI agent CLI. It is not a distributed system. It does not need a plugin architecture, a multi-provider abstraction layer, or a 10-table database schema. The current codebase is a prototype that has accumulated framework-shaped complexity without the framework-shaped requirements to justify it.

This proposal argues for radical simplification: fewer abstractions, simpler data flow, less indirection, more direct code, and disciplined YAGNI.

---

## 1. The Core Problem: Complexity Without Requirements

The current architecture implies requirements that don't exist:

| Implies | Reality |
|---------|---------|
| Multi-provider LLM abstraction | 2 providers, both OpenAI-compatible, never switched at runtime |
| 10-table PostgreSQL schema | 2 tables used (`sessions`, `context_chunks`) |
| Global singleton managers | Single-user CLI, one agent, one session at a time |
| Tool registry with decorator side-effects | 7 tools, all built-in, never extended by users |
| Skill system with DB + filesystem | 0 skills in production, filesystem-only loading |
| Streaming + non-streaming agent paths | Streaming is the only path users care about |
| `embed()` method on LLMProvider | Never called anywhere in the codebase |
| `sqlalchemy` + `alembic` dependencies | Not imported anywhere; raw asyncpg is used |
| `pydantic` dependency | Not used; dataclasses throughout |
| `tiktoken` dependency | Optional import, falls back to `len//4` |

Every unused abstraction is a liability: it must be understood, maintained, tested, and kept compatible. The cost is paid every time someone reads the code, not just when the feature is added.

---

## 2. Proposed Architecture: 4 Files

```
ah/
├── agent.py       # ReAct loop — the entire agent
├── llm.py         # LLM client — one provider, direct HTTP
├── store.py       # Database — 2 tables, direct SQL
├── tools.py       # All tools — plain functions, explicit registration
└── cli.py         # CLI — Typer app, unchanged interface
```

**Total: 5 files, ~600 lines.** Down from 18 files, ~2,400 lines.

### 2.1 `llm.py` — Kill the Provider Abstraction

The `LLMProvider` base class, `OpenRouterProvider`, `OllamaProvider`, `LLMResponse`, `StreamEvent`, `ToolDefinition`, and `get_provider()` factory — all replaced by a single async function:

```python
# llm.py
import httpx, os, json

async def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Call OpenRouter. Return {"content": str, "tool_calls": list, "usage": dict}."""
    async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", timeout=120) as client:
        resp = await client.post("/chat/completions", json={
            "model": os.environ.get("AH_MODEL", "anthropic/claude-3.5-sonnet"),
            "messages": messages,
            "tools": tools or [],
        }, headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        return {
            "content": msg.get("content", ""),
            "tool_calls": msg.get("tool_calls", []),
            "usage": data.get("usage", {}),
        }
```

**What's deleted:** `LLMProvider`, `OpenRouterProvider`, `OllamaProvider`, `LLMResponse`, `StreamEvent`, `ToolDefinition`, `get_provider()`, 418 lines → 20 lines.

**Rationale:** If Ollama support is needed later, add a second function or an `if` branch. The abstraction isn't paying rent today.

### 2.2 `store.py` — Kill the Manager Singletons

`SessionManager`, `ContextManager`, `ContextChunk`, `Session`, `PromptAssembler`, `TokenCounter` — all replaced by direct SQL in the agent:

```python
# store.py
import asyncpg, os, msgpack

_pool: asyncpg.Pool | None = None

async def connect():
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])

async def close():
    await _pool.close()

async def create_session(goal: str | None = None) -> str:
    row = await _pool.fetchrow(
        "INSERT INTO sessions (goal) VALUES ($1) RETURNING id", goal)
    return str(row["id"])

async def add_message(session_id: str, role: str, content: str, tool_calls: list | None = None):
    await _pool.execute(
        "INSERT INTO context_chunks (session_id, chunk_type, payload_msgpack) VALUES ($1, $2, $3)",
        session_id, role, msgpack.packb({"content": content, "tool_calls": tool_calls or []}))

async def get_messages(session_id: str, limit: int = 20) -> list[dict]:
    rows = await _pool.fetch(
        "SELECT payload_msgpack FROM context_chunks WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
        session_id, limit)
    return [msgpack.unpackb(r["payload_msgpack"], raw=False) for r in reversed(rows)]
```

**What's deleted:** `SessionManager` (178 lines), `ContextManager` (222 lines), `ContextChunk` dataclass, `Session` dataclass, `PromptAssembler` (105 lines), `TokenCounter` (47 lines), `Database` wrapper (78 lines). ~630 lines → ~30 lines.

**Rationale:** The agent needs to store messages and retrieve them. That's it. The "managers" add indirection without adding capability. Direct SQL is honest: you can see exactly what the code does.

### 2.3 `tools.py` — Kill the Registry

`ToolRegistry`, `Tool` dataclass, decorator-based registration, `_infer_schema()`, `get_tool_names()` alias — all replaced by a plain dict:

```python
# tools.py
TOOLS: dict[str, dict] = {
    "read_file": {
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "func": read_file,
    },
    "write_file": { ... },
    "terminal": { ... },
    "web_search": { ... },
}

async def execute(name: str, **kwargs) -> str:
    if name not in TOOLS:
        return f"Error: unknown tool '{name}'"
    try:
        result = TOOLS[name]["func"](**kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return str(result)
    except Exception as e:
        return f"Error: {e}"
```

**What's deleted:** `ToolRegistry` (122 lines), `Tool` dataclass, decorator machinery, `_infer_schema()`, `get_tool_names()`, `tools/registry.py` re-export, `builtins.py` duplicates. ~350 lines → ~50 lines.

**Rationale:** A dict is a registry. The decorator pattern adds import-order side effects and makes it impossible to see what tools exist without importing everything. An explicit dict is grep-able, testable, and dead simple.

### 2.4 `agent.py` — One Loop, No Abstraction

The ReAct loop becomes a single async function. No `ReActAgent` class. No `AgentResponse` dataclass. No `run()` / `run_stream()` split. No retry logic (let it crash; the CLI catches and reports).

```python
# agent.py
from ah.llm import chat
from ah.store import add_message, get_messages
from ah.tools import TOOLS, execute

SYSTEM_PROMPT = "You are AgentHarness. Use tools to accomplish tasks. Be concise."

async def run(session_id: str, user_message: str) -> str:
    await add_message(session_id, "user", user_message)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += await get_messages(session_id)

    for _ in range(10):  # max iterations
        tool_defs = [{"type": "function", "function": {
            "name": name, "description": t["description"], "parameters": t["parameters"]
        }} for name, t in TOOLS.items()]

        response = await chat(messages, tool_defs)

        if not response["tool_calls"]:
            await add_message(session_id, "assistant", response["content"])
            return response["content"]

        for tc in response["tool_calls"]:
            fn = tc["function"]
            args = json.loads(fn.get("arguments", "{}"))
            result = await execute(fn["name"], **args)
            messages.append({"role": "tool", "content": result, "tool_call_id": tc["id"]})

    return "Max iterations reached."
```

**What's deleted:** `ReActAgent` class (562 lines), `AgentResponse` dataclass, `_call_llm_with_retry()`, `_stream_llm_with_retry()`, `run_stream()`, `MAX_TOKEN_BUDGET` check, `PromptAssembler` integration. ~560 lines → ~30 lines.

**Rationale:** The ReAct loop is 20 lines of logic. Wrapping it in a class with two run methods, retry helpers, and a streaming variant multiplies the surface area without multiplying the capability. A function is the right abstraction level.

---

## 3. What Gets Deleted

| Component | Lines | Reason |
|-----------|-------|--------|
| `LLMProvider` + 2 implementations + factory | 418 | One provider, one function |
| `StreamEvent` dataclass | 12 | Return a dict instead |
| `LLMResponse` dataclass | 10 | Return a dict instead |
| `ToolDefinition` dataclass | 5 | Use a dict literal |
| `SessionManager` + `Session` | 178 | Direct SQL |
| `ContextManager` + `ContextChunk` | 222 | Direct SQL |
| `PromptAssembler` | 105 | Inline in agent |
| `TokenCounter` | 47 | `len(text) // 4` inline |
| `Database` wrapper | 78 | Use `asyncpg.Pool` directly |
| `ToolRegistry` + `Tool` | 122 | Use a dict |
| `tools/registry.py` re-export | 4 | Delete |
| `builtins.py` (duplicates) | 179 | Delete |
| `ReActAgent` class | 562 | One function |
| `AgentResponse` dataclass | 7 | Return a string |
| `SkillRegistry` + `SkillParser` + `Skill` | 111 | Not used in agent loop |
| `ah/memory/` (empty) | 0 | Delete |
| `ah/rag/` (empty) | 0 | Delete |
| `ah/skills/__init__.py` (empty) | 0 | Delete |
| **Total deleted** | **~2,060** | |

**Net result:** ~2,400 lines → ~600 lines. 75% reduction.

---

## 4. Schema: 2 Tables

```sql
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE context_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,  -- 'user', 'assistant', 'tool'
    content TEXT NOT NULL,
    tool_calls JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

**Deleted:** `state_msgpack`, `agent_id`, `status`, `model`, `provider`, `context_budget`, `title`, `last_activity`, `payload_msgpack`, `token_count`, `embedding`, `accessed_at`, and 8 unused tables.

**Rationale:** The agent needs to remember what was said. A `(session_id, role, content)` tuple is the minimum viable memory. If embeddings, token budgets, or multi-agent routing are needed later, add them then. The `embedding` column with pgvector is particularly egregious: the `search_by_embedding()` method exists but is never called.

---

## 5. Dependencies: Trim the Fat

| Keep | Delete | Reason |
|------|--------|--------|
| `typer` | `sqlalchemy` | Not used |
| `rich` | `alembic` | Not used |
| `asyncpg` | `pydantic` | Not used |
| `httpx` | `tiktoken` | `len//4` is fine for a prototype |
| `msgpack` | `pyyaml` | Not used after skill system removal |
| `python-dotenv` | | |

**6 dependencies → 6 dependencies, but only the ones actually imported.**

---

## 6. What This Sacrifices

Be honest about what you lose:

1. **Multi-provider support** — if you want Ollama, add an `if` in `chat()`. 5 lines.
2. **Streaming** — if the CLI needs token-by-token display, add an async generator variant of `chat()`. 15 lines.
3. **Embedding search** — if you need RAG, add an `embed()` function and a vector column. 20 lines.
4. **Skill system** — if you need dynamic prompt injection, load SKILL.md files in the CLI and prepend to the system prompt. 10 lines.
5. **Tool schema inference** — if you add tools frequently, write a helper that builds the dict from type hints. 10 lines.

Each of these is a small, local change when the requirement actually arrives. The current architecture pays the complexity cost upfront, for requirements that may never materialize.

---

## 7. The YAGNI Principle

> "You aren't gonna need it."

The current codebase is a museum of speculative design:

- **8 unused database tables** — `skills`, `memories`, `agent_messages`, `heartbeat_config`, `external_context`, `subagent_sessions`, `subagent_messages`, `subagent_results`. These imply a multi-agent, heartbeat-driven, externally-integrated system. None of it exists.
- **`embed()` on LLMProvider** — implies RAG. Never called.
- **`PromptAssembler` with token budgeting** — implies sophisticated context management. The agent passes `retrieved_chunks=[]` always.
- **`StreamEvent` with 8 fields** — implies a rich streaming protocol. Only 3 fields are used in practice.
- **`Session.state: dict`** — implies LangGraph checkpointing. Never written to.
- **`context_budget` on Session** — implies per-session token limits. Never enforced (the agent uses a global `MAX_TOKEN_BUDGET` instead).

Every speculative feature is a tax on comprehension. A new developer must understand the full surface area to change any part of the system. The minimalist architecture makes the system comprehensible in one sitting.

---

## 8. Migration Path

This is not a big-bang rewrite. The CLI interface (`ah chat`, `ah status`, `ah sessions`, `ah context`) stays identical. The migration is:

1. **Create `llm.py`** — move `chat()` function, test it works
2. **Create `store.py`** — move SQL helpers, test with existing DB
3. **Create `tools.py`** — move tool functions into a dict, test each tool
4. **Rewrite `agent.py`** — replace `ReActAgent` with `run()` function
5. **Update `cli.py`** — call `run()` instead of `ReActAgent.run_stream()`
6. **Delete** — `provider.py`, `context.py`, `session.py`, `connection.py`, `base.py`, `registry.py`, `builtins.py`, `file.py`, `terminal.py`, `skills/`, `memory/`, `rag/`
7. **Simplify schema** — drop unused columns and tables

Each step is independently testable. The CLI contract doesn't change.

---

## 9. Conclusion

AgentHarness does not need to be a framework. It needs to be a tool: accept a message, call an LLM, execute tools, return a response. The current architecture wraps that simple loop in six layers of abstraction — provider interfaces, manager singletons, registry decorators, context assemblers, session models, and a streaming protocol — none of which are exercised by the actual use case.

The minimalist architecture is not a compromise. It is the correct design for a single-user agent CLI. It is easier to read, easier to test, easier to debug, and easier to extend when real requirements arrive. The 75% code reduction is not the goal — it is the natural consequence of removing abstractions that don't pay rent.

**Build the thing you need. Not the thing you might need.**
