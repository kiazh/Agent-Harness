# LangGraph Integration Research for AgentHarness

**Date:** 2026-10-01
**Author:** AgentHarness Research
**Status:** Decision Document

---

## Executive Summary

LangGraph is a low-level orchestration framework for building stateful, long-running AI agents using directed cyclic graphs.[^1] It provides durable execution, checkpointing, human-in-the-loop, and explicit state management as first-class primitives.[^3]

AgentHarness currently implements a custom ReAct (Reason + Act) loop in `ah/core/agent.py` — a ~700-line `while` loop that calls an LLM, parses tool calls, executes them, and repeats until the model stops calling tools or a budget is hit. This is the same pattern LangGraph's `create_react_agent()` encapsulates in ~3 lines of code.[^10]

**Recommendation: Do not integrate LangGraph at this time.** The custom ReAct loop is the correct architectural choice for AgentHarness's current scope. LangGraph's advantages (durable checkpointing, multi-agent coordination, complex branching) are not yet needed, and its costs (dependency weight, abstraction overhead, debugging indirection) would degrade the project's simplicity and self-hostability. Revisit when the project needs multi-agent coordination, human-in-the-loop pause/resume, or crash-recovery across process restarts.

---

## 1. What is LangGraph?

LangGraph is a Python (and TypeScript) library built by LangChain Inc for orchestrating stateful, multi-step LLM applications.[^1] It models agent workflows as **directed cyclic graphs** where:

- **State** — a typed dictionary (or Pydantic model) that is the single source of truth shared across all nodes.[^2]
- **Nodes** — Python functions that receive the current state and return a partial update.[^2]
- **Edges** — routing logic (static or conditional) that determines which node executes next based on the current state.[^2]

The key insight is that LangGraph replaces the implicit `while` loop of a ReAct agent with an explicit, inspectable graph structure. The graph is compiled, validated, and executed by a runtime that checkpoints state after every "super-step" (one round of node execution).[^2][^8]

LangGraph is **not** LangChain. It is an independent library focused purely on orchestration — it can be used with direct LLM SDKs (OpenAI, Anthropic, Ollama) without importing LangChain.[^1][^7]

### Core primitives

| Primitive | Description |
|-----------|-------------|
| **State** | TypedDict or Pydantic model; every node reads and writes to it |
| **Nodes** | Functions `(state) -> partial_state_update`; can call LLMs, run tools, or do pure logic |
| **Edges** | Static (always go to node B) or conditional (router function returns next node name) |
| **Checkpointer** | Persists state after every super-step (MemorySaver, SqliteSaver, PostgresSaver) |
| **Interrupts** | Pause graph execution at any node for human input, then resume |
| **Reducers** | Define how concurrent writes merge (e.g., `add_messages` appends, `operator.add` concatenates) |

[^1][^2][^7][^8]

---

## 2. LangGraph Architecture

### 2.1 Execution model

LangGraph uses a **Pregel-style bulk-synchronous parallel** execution model inspired by Google's Pregel system.[^2][^8] Execution proceeds in **super-steps**:

1. All nodes scheduled for this round receive the current state snapshot.
2. They run (potentially in parallel).
3. Their updates are collected and merged via reducers.
4. The new state is checkpointed.
5. The next set of nodes is scheduled based on edges.

This is fundamentally different from a simple `while` loop — it supports fan-out (one node triggers N parallel nodes) and fan-in (a node waits for all inbound branches).[^2][^9]

### 2.2 The ReAct pattern in LangGraph

A ReAct agent in LangGraph is a graph with two nodes and a conditional edge:

```
START → Agent Node (LLM) → Conditional Edge
                                ├── tool_calls present → Tool Node → Agent Node (loop back)
                                └── no tool_calls → END
```

The conditional edge IS the ReAct loop. LangGraph provides `create_react_agent()` as a one-liner that builds this graph automatically.[^10]

### 2.3 Persistence and durability

LangGraph's most significant architectural advantage is **checkpointing**. By compiling a graph with a checkpointer (`SqliteSaver`, `PostgresSaver`), the full state is persisted after every super-step.[^7][^8] This enables:

- **Crash recovery** — resume from the last checkpoint after a process restart.
- **Time-travel debugging** — retrieve state from N steps ago, modify it, and branch execution.
- **Human-in-the-loop** — pause at any node, wait for human input (hours or days), then resume.

### 2.4 Multi-agent coordination

LangGraph supports several multi-agent topologies:

- **Supervisor pattern** — a central supervisor LLM routes to worker agents.
- **Hierarchical agents** — supervisors of supervisors.
- **Agent handoff** — agents transfer control directly via `Command(goto=...)`.

Subgraphs can be nested as nodes within a parent graph, enabling composable multi-agent systems.[^7][^8]

---

## 3. AgentHarness Current Architecture

### 3.1 The custom ReAct loop

AgentHarness implements a ReAct loop in `ah/core/agent.py` (`ReActAgent` class):

```python
for iteration in range(self.max_iterations):
    # 1. Check token budget
    if total_tokens >= MAX_TOKEN_BUDGET: return ...

    # 2. Call LLM with retry (exponential backoff)
    response = await self._call_llm_with_retry(messages, tools)

    # 3. If no tool calls → final answer
    if not response.tool_calls:
        return AgentResponse(content=response.content, ...)

    # 4. Execute tool calls
    for tc in response.tool_calls:
        result = await registry.execute(tool_name, **tool_args)
        messages.append(tool_result)
```

The loop is bounded by `max_iterations` (default 10) and `MAX_TOKEN_BUDGET` (50,000 tokens). It includes:

- **Exponential backoff retry** (3 retries: 1s, 2s, 4s delays)
- **Tool argument validation** against JSON Schema
- **Audit logging** for every LLM call, tool call, and error
- **Context persistence** — every message, tool call, and result is stored in PostgreSQL via `ContextManager`
- **Streaming support** — `run_stream()` yields `StreamEvent` objects for real-time output

### 3.2 Supporting infrastructure

| Component | File | Purpose |
|-----------|------|---------|
| **Models** | `ah/core/models.py` | Typed dataclasses: `Session`, `ContextChunk`, `LLMResponse`, `AgentResponse`, `StreamEvent` |
| **Provider** | `ah/core/provider.py` | `LLMProvider` base + `OpenRouterProvider` + `OllamaProvider`; rate limiting, audit logging |
| **Session** | `ah/core/session.py` | `SessionManager` — PostgreSQL-backed with 5-second TTLCache |
| **Context** | `ah/core/context.py` | `ContextManager` — CRUD for context chunks with embedding search |
| **Assembler** | `ah/core/assembler.py` | `PromptAssembler` — builds prompts within token budget |
| **Container** | `ah/core/container.py` | DI container wiring all singletons |
| **Tools** | `ah/tools/base.py` | `ToolRegistry` — decorator-based with JSON Schema inference |
| **Skills** | `ah/skills/registry.py` | `SkillRegistry` — SKILL.md parser with YAML frontmatter |
| **CLI** | `ah/cli.py` | Typer CLI: `ah chat`, `ah status`, `ah sessions`, `ah context` |
| **Database** | `ah/db/schema.sql` | 2 tables: `sessions`, `context_chunks` (with pgvector) |

### 3.3 What AgentHarness already has

AgentHarness already implements several features that LangGraph provides:

| Feature | AgentHarness | LangGraph |
|---------|-------------|-----------|
| ReAct loop | ✅ Custom `while` loop | ✅ `create_react_agent()` |
| State management | ✅ `messages` list + `Session.state` dict | ✅ Typed `StateGraph` state |
| Persistence | ✅ PostgreSQL (sessions + context_chunks) | ✅ Checkpointer (Sqlite/Postgres) |
| Streaming | ✅ `run_stream()` with `StreamEvent` | ✅ Native streaming |
| Token budget | ✅ `MAX_TOKEN_BUDGET` hard cap | ❌ Not built-in |
| Retry logic | ✅ Exponential backoff | ❌ Not built-in |
| Audit logging | ✅ Every LLM/tool call logged | ❌ Not built-in (LangSmith separate) |
| Tool validation | ✅ JSON Schema validation | ✅ `ToolNode` with `handle_tool_errors` |
| Multi-agent | ❌ Single agent only | ✅ Supervisor, hierarchical, handoff |
| Human-in-the-loop | ❌ Not implemented | ✅ `interrupt_before` / `interrupt_after` |
| Crash recovery | ❌ In-memory loop dies on restart | ✅ Checkpoint resume |
| Parallel execution | ❌ Sequential tool calls | ✅ Fan-out/fan-in |

---

## 4. Head-to-Head Comparison

### 4.1 Control flow

| Aspect | AgentHarness (Custom ReAct) | LangGraph |
|--------|---------------------------|-----------|
| **Loop structure** | Implicit `while` loop in Python | Explicit graph with nodes and edges |
| **Routing** | LLM decides (tool_calls → tools, else → END) | Conditional edge function decides |
| **Termination** | `max_iterations` or no tool_calls | `END` node or `recursion_limit` |
| **Branching** | None — linear loop only | Conditional edges, parallel branches |
| **Observability** | Audit logs (custom) | LangSmith tracing (external service) |

**Verdict:** LangGraph's explicit graph is more inspectable and testable. AgentHarness's implicit loop is simpler to read and debug (stack traces point to your code, not framework internals).[^6]

### 4.2 State management

| Aspect | AgentHarness | LangGraph |
|--------|-------------|-----------|
| **State schema** | Unstructured `messages: list[dict]` | Typed `TypedDict` / Pydantic model |
| **State merging** | Manual `messages.append()` | Reducers (`add_messages`, `operator.add`) |
| **Persistence** | PostgreSQL via `ContextManager` | Checkpointer (MemorySaver, SqliteSaver, PostgresSaver) |
| **Context window** | Token-budget-aware prompt assembly | Raw message list (no built-in budget) |

**Verdict:** LangGraph's typed state with reducers is more robust for complex workflows. AgentHarness's unstructured message list is simpler but sufficient for a single-agent ReAct loop. AgentHarness's token budget management is actually **better** than LangGraph's out-of-the-box offering.[^6]

### 4.3 Durability and recovery

| Aspect | AgentHarness | LangGraph |
|--------|-------------|-----------|
| **Crash recovery** | ❌ Loop dies; context preserved in DB but run lost | ✅ Resume from last checkpoint |
| **Long-running runs** | ❌ In-memory only | ✅ Durable across hours/days |
| **Human-in-the-loop** | ❌ Not implemented | ✅ `interrupt_before` / `interrupt_after` |
| **Time-travel** | ❌ Not possible | ✅ Retrieve and branch from any checkpoint |

**Verdict:** LangGraph wins decisively for long-running, multi-step, or human-in-the-loop workflows. AgentHarness's runs are short-lived (seconds to minutes) and self-contained, so crash recovery is less critical.

### 4.4 Performance and cost

| Aspect | AgentHarness | LangGraph |
|--------|-------------|-----------|
| **Token tracking** | ✅ Built-in (`total_tokens` accumulated) | ❌ Hidden unless callbacks wired[^6] |
| **Rate limiting** | ✅ `AsyncTokenBucket` | ❌ Not built-in |
| **Overhead** | Minimal (direct LLM calls) | Graph runtime + checkpointing overhead |
| **Dependencies** | `httpx`, `asyncpg`, `typer`, `rich`, `msgpack` | `langgraph`, `langchain-core`, + checkpointer deps |

**Verdict:** AgentHarness has superior cost control and rate limiting out of the box. LangGraph's abstraction hides token accounting unless explicitly wired.[^6]

### 4.5 Development experience

| Aspect | AgentHarness | LangGraph |
|--------|-------------|-----------|
| **Lines of code** | ~700 (full ReAct loop) | ~3 (`create_react_agent()`)[^10] |
| **Debugging** | Direct — read your own code | Indirect — framework internals[^6] |
| **Learning curve** | Minimal (standard Python) | Steeper (graph concepts, reducers, checkpointers)[^4] |
| **API stability** | You control it | Fast-moving; patterns change[^6] |
| **Testing** | 436 tests pass | Framework-coupled tests |

**Verdict:** AgentHarness is simpler to develop, debug, and maintain. LangGraph reduces boilerplate but introduces framework coupling and a steeper learning curve.

---

## 5. Integration Analysis

### 5.1 What integration would look like

Integrating LangGraph into AgentHarness would require:

1. **Add `langgraph` dependency** to `pyproject.toml`
2. **Define a `StateGraph`** with `AgentState(TypedDict)` containing messages, tool calls, and metadata
3. **Wrap existing components** as LangGraph nodes:
   - `agent_node` — calls `LLMProvider.complete()` (existing)
   - `tool_node` — calls `ToolRegistry.execute()` (existing)
4. **Replace `ReActAgent.run()`** with `graph.invoke()`
5. **Add a checkpointer** (SqliteSaver for dev, PostgresSaver for prod)
6. **Migrate `SessionManager`** to use LangGraph's `thread_id` for checkpoint isolation

### 5.2 What would be gained

- **Crash recovery** — resume runs after process restart
- **Human-in-the-loop** — pause for approval before dangerous tool calls
- **Multi-agent support** — supervisor pattern for complex tasks
- **Time-travel debugging** — replay and branch from any step
- **Parallel execution** — fan-out for concurrent tool calls

### 5.3 What would be lost

- **Simplicity** — 700-line loop becomes a graph definition + node functions + edge functions
- **Token budget control** — LangGraph has no built-in token budget; would need custom callbacks
- **Rate limiting** — would need to wrap LLM calls inside nodes
- **Audit logging** — would need to wire into LangGraph's callback system
- **Self-hostability** — adds `langgraph` + `langchain-core` dependencies (~50MB)
- **Debugging transparency** — stack traces point into LangGraph internals

### 5.4 Cost-benefit assessment

| Criteria | Weight | Custom ReAct | LangGraph |
|----------|--------|-------------|-----------|
| Meets current needs | High | ✅ | ✅ |
| Simplicity | High | ✅ | ❌ |
| Future-proofing | Medium | ❌ | ✅ |
| Performance/cost control | Medium | ✅ | ❌ |
| Multi-agent ready | Low | ❌ | ✅ |
| Crash recovery | Low | ❌ | ✅ |
| Team familiarity | Medium | ✅ | ❌ |
| **Total** | | **5/7** | **3/7** |

---

## 6. Recommendation

### 6.1 Decision: Do not integrate LangGraph now

**Rationale:**

1. **AgentHarness's ReAct loop already works.** 436 tests pass. The loop handles tool calls, retries, token budgets, streaming, and audit logging. It is debuggable, performant, and self-contained.

2. **LangGraph's advantages are not yet needed.** The project does not currently require multi-agent coordination, human-in-the-loop pause/resume, or crash recovery across restarts. These are real needs for long-running workflows (hours/days), but AgentHarness runs are short-lived (seconds/minutes).

3. **The costs are real.** Adding LangGraph introduces framework coupling, hides token accounting, adds ~50MB of dependencies, and makes debugging harder. The project's value proposition is being a lightweight, self-hostable framework — LangGraph works against that.

4. **The custom loop has features LangGraph lacks.** Token budget management, rate limiting, and audit logging are built-in. In LangGraph, these require custom callbacks or wrappers.

### 6.2 When to revisit

Consider LangGraph integration when:

- **Multi-agent coordination** is needed (supervisor + workers pattern)
- **Human-in-the-loop** is required (pause for approval before dangerous actions)
- **Crash recovery** becomes critical (runs must survive process restarts)
- **Parallel tool execution** would significantly reduce latency
- **Long-running workflows** (hours/days) become a use case

### 6.3 Alternative: selective adoption

If specific LangGraph features are needed without full integration:

- **Checkpointing** — implement a simple checkpoint mechanism in `ContextManager` (already persists state to PostgreSQL; could add resume logic)
- **Human-in-the-loop** — add an `interrupt` flag to `ReActAgent` that pauses before tool execution and waits for approval
- **Multi-agent** — add a `SupervisorAgent` that routes to specialized `ReActAgent` instances

These can be built incrementally without adopting the full LangGraph framework.

---

## 7. Comparison Summary Table

| Feature | AgentHarness (Custom) | LangGraph | Winner |
|---------|----------------------|-----------|--------|
| ReAct loop | ✅ Custom | ✅ `create_react_agent()` | Tie |
| Typed state | ❌ Unstructured dict | ✅ TypedDict + reducers | LangGraph |
| Persistence | ✅ PostgreSQL | ✅ Checkpointer | Tie |
| Crash recovery | ❌ | ✅ | LangGraph |
| Human-in-the-loop | ❌ | ✅ | LangGraph |
| Multi-agent | ❌ | ✅ | LangGraph |
| Parallel execution | ❌ | ✅ | LangGraph |
| Token budget | ✅ Built-in | ❌ | AgentHarness |
| Rate limiting | ✅ Built-in | ❌ | AgentHarness |
| Audit logging | ✅ Built-in | ❌ (LangSmith separate) | AgentHarness |
| Streaming | ✅ `run_stream()` | ✅ Native | Tie |
| Simplicity | ✅ | ❌ | AgentHarness |
| Debugging | ✅ Direct | ❌ Framework internals | AgentHarness |
| Dependencies | Minimal | ~50MB | AgentHarness |
| Testability | ✅ 436 tests | Framework-coupled | AgentHarness |
| Self-hostable | ✅ | ❌ | AgentHarness |

---

## 8. Conclusion

LangGraph is a well-designed framework for complex, long-running, multi-agent workflows. It solves real problems: durable execution, checkpointing, human-in-the-loop, and multi-agent coordination. For teams building production agents with these needs, it is the right choice.[^3][^6]

AgentHarness is a lightweight, self-hosted ReAct agent with 436 passing tests, token budget management, rate limiting, audit logging, and PostgreSQL persistence. Its custom ReAct loop is the correct architectural choice for its current scope. The project should continue iterating on its custom loop and revisit LangGraph when multi-agent coordination, human-in-the-loop, or crash recovery become requirements.

**The best time to adopt a framework is when you have pain it solves — not before.**

---

## Sources

[^1]: [LangGraph Overview](https://langchain-ai.github.io/langgraph/index) — Official documentation
[^2]: [Graph API Overview](https://langchain-ai.github.io/langgraph/how-tos/graph-api) — Official documentation
[^3]: [LangGraph GitHub README](https://github.com/langchain-ai/langgraph) — Official repository
[^4]: [LangChain, LangGraph, or Custom? Choosing the Right Agentic Framework](https://www.turgon.ai/post/langchain-langgraph-or-custom-choosing-the-right-agentic-framework) — CTO-level comparison
[^5]: [LangGraph vs ReAct: When Should You Use Which](https://amitavroy.com/articles/2025-06-29-LangGraph-vs-ReAct-When-Should-You-Use-Which-for-Your-Next-AI-Agent) — Pattern comparison
[^6]: [AI Agent Stack in 2026: LangGraph vs Custom vs DIY](https://lazar-milicevic.com/blog/ai-agent-stack-in-2026-langgraph-vs-custom-vs-diy) — Decision framework
[^7]: [LangGraph Complete Guide](https://stackmindset.com/articles/langgraph/langgraph-complete-guide) — Architecture deep dive
[^8]: [LangGraph Deep Dive: Building Production-Grade Stateful AI Agents](https://himanshuai.substack.com/p/langgraph-deep-dive-building-production) — Production patterns
[^9]: [LangGraph State-Machine Architecture](https://callsphere.ai/blog/langgraph-state-machine-architecture-deep-dive-2026) — Principal-engineer perspective
[^10]: [LangGraph ReAct Agent: Build from Scratch](https://machinelearningplus.com/gen-ai/langgraph-react-agent-from-scratch/) — Step-by-step implementation
