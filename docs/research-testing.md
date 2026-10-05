# Production Testing for AI Agent Frameworks — Research & Recommendations for AgentHarness

**Date:** 2026-10-01  
**Scope:** AgentHarness (`ah/`) — a self-hosted AI agent framework with ReAct loop, tool registry, skill system, LLM provider abstraction, and PostgreSQL persistence.

> **Status:** Research snapshot. Fixed test counts and coverage assumptions
> below are historical observations, not a live CI result. Run the current
> suite and inspect CI artifacts for the active branch before quoting them.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [The Agent Testing Pyramid](#2-the-agent-testing-pyramid)
3. [Unit Testing](#3-unit-testing)
4. [Integration Testing](#4-integration-testing)
5. [End-to-End Testing](#5-end-to-end-testing)
6. [LLM Mocking Strategies](#6-llm-mocking-strategies)
7. [Property-Based Testing](#7-property-based-testing)
8. [Chaos Engineering](#8-chaos-engineering)
9. [Load Testing](#9-load-testing)
10. [Coverage Requirements](#10-coverage-requirements)
11. [Current State Assessment](#11-current-state-assessment)
12. [Recommended Test Architecture](#12-recommended-test-architecture)
13. [Implementation Roadmap](#13-implementation-roadmap)
14. [References](#14-references)

---

## 1. Executive Summary

Production testing for an AI agent framework differs fundamentally from traditional software testing. The system under test is **non-deterministic** (LLM outputs vary), **stateful** (conversations accumulate context), and **multi-component** (agent loop + tools + providers + database + skills). A robust test strategy must account for all three properties.

**Key findings from industry research:**

- **95% of tests should use mocked LLM responses** — real API calls are slow, expensive, and non-deterministic. Only a thin layer of E2E smoke tests should hit real APIs. ([OpenHelm, 2024](https://openhelm.ai/blog/agent-testing-strategies-unit-integration-e2e))
- **The test pyramid inverts for agents** — unit tests are fast and deterministic but can't verify agent behavior; E2E tests verify behavior but are slow and flaky. The sweet spot is **integration tests with mocked LLM + real tools + real database**. ([Zylos Research, 2026](https://zylos.ai/research/2026-05-07-ai-agent-testing-strategies-production-validation/))
- **Property-based testing is underutilized** — Hypothesis-style testing can find edge cases in prompt assembly, tool argument parsing, and context management that example-based tests miss. ([PBT-Bench, 2026](https://arxiv.org/abs/2605.15229))
- **Chaos engineering is essential** — LLM agents fail in production not from bugs but from unexpected inputs, tool failures, and context overflow. Chaos testing validates recovery paths. ([Owotogbe, 2025](https://arxiv.org/abs/2505.03096))
- **Load testing must simulate conversation dynamics** — traditional request/response load testing misses the "context avalanche" where long conversations degrade performance. ([The New Stack, 2026](https://thenewstack.io/why-load-tests-lie-harsh-truth-about-ai-agent-performance))

---

## 2. The Agent Testing Pyramid

### 2.1 Classical Pyramid vs Agent Pyramid

```
Classical:                    Agent:
                             
    E2E (few)                    Production Monitors (synthetic probes)
   Integration (some)          E2E / Behavioral (did agent achieve goal?)
  Unit (many)                 Integration (tool-call sequences, state transitions)
                              Component (parsers, routers, tool wrappers)
                             Unit (pure functions, schema inference)
```

The agent pyramid adds two layers:
- **Component tests** — between unit and integration; test individual agent components (PromptAssembler, ToolRegistry, SkillParser) in isolation with mocked dependencies.
- **Production monitors** — above E2E; continuous synthetic probes that run against production to detect regressions.

### 2.2 Recommended Distribution for AgentHarness

| Layer | Count | Run Frequency | LLM Mocked? | DB | Tools | Duration |
|-------|-------|---------------|-------------|-----|-------|----------|
| Unit | ~200 | Every commit | N/A (no LLM) | Mocked | Real | <5s |
| Component | ~50 | Every commit | Yes (stub) | Mocked | Real | <10s |
| Integration | ~50 | Every PR | Yes (fake) | Real (testcontainer) | Real | <2min |
| E2E | ~10 | Nightly | Real or LLM-as-judge | Real | Real | <10min |
| Chaos | ~20 | Weekly | Yes (fault injection) | Real | Real | <30min |
| Load | ~5 | Pre-release | Yes (mocked) | Real | Real | <1hr |

---

## 3. Unit Testing

### 3.1 What to Test

Unit tests target **pure functions and isolated components** with no external dependencies:

- **PromptAssembler** — string assembly, token estimation, chunk compression, truncation
- **ToolRegistry** — schema inference, registration, execution dispatch
- **SkillParser** — YAML frontmatter parsing, trigger matching
- **Data models** — dataclass defaults, serialization round-trips
- **Provider validation** — `_validate_messages`, `_validate_params`
- **Rate limiter** — `AsyncTokenBucket` token acquisition logic

### 3.2 Current State

AgentHarness has **~80 unit tests** in `test_comprehensive.py` covering:
- `PromptAssembler` (14 tests) — assembly, compression, token estimation
- `ToolRegistry` (12 tests) — registration, schema inference, execution
- `SkillParser` (5 tests) — YAML parsing
- `SkillRegistry` (8 tests) — loading, trigger matching
- `Dataclasses` (8 tests) — defaults, custom values
- `EdgeCases` (10 tests) — empty strings, unicode, special chars
- `ErrorHandling` (12 tests) — file not found, invalid args, timeouts

### 3.3 Gaps

- **No property-based tests** for token estimation, chunk compression, or schema inference
- **No tests for `AsyncTokenBucket`** — the rate limiter is completely untested
- **No tests for streaming** — `stream_complete` SSE parsing is untested
- **No tests for msgpack serialization** — pack/unpack round-trips are untested
- **No tests for `audit_log`** — the audit logging function is untested

### 3.4 Recommended Unit Test Patterns

```python
# Pattern 1: Parametrized edge cases
@pytest.mark.parametrize("input_text,expected_min,expected_max", [
    ("", 0, 0),
    ("a", 1, 1),
    ("hello world", 2, 3),
    ("x" * 400, 99, 101),
    ("你好世界", 1, 4),
])
def test_estimate_tokens_bounds(input_text, expected_min, expected_max):
    assembler = PromptAssembler()
    tokens = assembler._estimate_tokens(input_text)
    assert expected_min <= tokens <= expected_max

# Pattern 2: Round-trip serialization
def test_msgpack_roundtrip():
    payload = {"content": "hello", "nested": {"key": [1, 2, 3]}}
    packed = msgpack.packb(payload, use_bin_type=True)
    unpacked = msgpack.unpackb(packed, raw=False)
    assert unpacked == payload

# Pattern 3: Schema inference properties
def test_infer_schema_preserves_all_params():
    def func(a: str, b: int, c: float = 1.0, d: bool = True) -> str:
        return "ok"
    schema = ToolRegistry()._infer_schema(func)
    assert set(schema["properties"].keys()) == {"a", "b", "c", "d"}
    assert set(schema["required"]) == {"a", "b"}
```

---

## 4. Integration Testing

### 4.1 What to Test

Integration tests verify **component interactions** with real dependencies where feasible:

- **Agent loop + real tools** — mock only the LLM provider, use real `ToolRegistry` with real file/terminal tools
- **Agent loop + real database** — use `testcontainers-python` to spin up PostgreSQL, test actual SQL queries
- **Provider + HTTP mocking** — mock `httpx.AsyncClient` at the transport level, test full request/response cycle
- **Session + Context + Agent** — test the full flow: create session → add chunks → run agent → verify context updated

### 4.2 The Testcontainers Pattern

```python
import pytest
from testcontainers.postgres import PostgresContainer

@pytest.fixture(scope="module")
def postgres():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url()

@pytest.fixture
async def real_db(postgres):
    db = Database(postgres)
    await db.connect()
    await db.initialize_schema()
    yield db
    await db.close()

async def test_session_crud_real(real_db):
    """Test actual SQL queries against real database."""
    session = await session_manager.create(title="test")
    assert session.id is not None
    
    loaded = await session_manager.get(session.id)
    assert loaded.title == "test"
    
    await session_manager.update_state(session.id, {"key": "value"})
    loaded = await session_manager.get(session.id)
    assert loaded.state == {"key": "value"}
```

### 4.3 Agent Loop Integration Test

```python
async def test_agent_executes_tool_for_real():
    """Test that the agent actually executes a tool and uses its result."""
    # Create a real temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("secret content")
        f.flush()
    
    # Mock provider: first call returns tool call, second returns final answer
    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=[{
            "id": "call_1",
            "function": {"name": "read_file", "arguments": json.dumps({"path": f.name})}
        }]),
        LLMResponse(content="The file contains: secret content"),
    ])
    
    agent = ReActAgent(provider=provider, max_iterations=5)
    response = await agent.run(session.id, f"Read the file {f.name}")
    
    # Verify the tool actually executed
    assert "secret content" in response.content
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["tool"] == "read_file"
```

### 4.4 Current State

AgentHarness has **~30 integration tests** but they are **mocked integration tests** — they mock the database with `AsyncMock` and verify that code *calls* `db.fetchrow`, not that the SQL is correct or the schema matches. The critique document (`docs/critique-testing.md`) identifies this as a critical gap.

### 4.5 Gaps

- **Zero tests against real database** — all DB tests use `AsyncMock`
- **Zero tests for real tool execution in agent loop** — tools are mocked
- **Zero tests for `web_search` or `web_extract`** — external HTTP calls untested
- **Zero tests for streaming** — `stream_complete` is untested
- **Zero tests for concurrent session updates** — race conditions untested

---

## 5. End-to-End Testing

### 5.1 What to Test

E2E tests verify **complete user workflows** through the CLI or API:

- **CLI workflow** — `agent-harness chat "message"` → agent responds correctly
- **Multi-turn conversation** — context accumulates across turns
- **Skill activation** — skill triggers on relevant queries
- **Error recovery** — agent recovers from tool failures, malformed LLM responses
- **Session persistence** — sessions survive restart, context is retrievable

### 5.2 LLM-as-Judge Pattern

For E2E tests, exact string matching is insufficient because LLM outputs vary. Use **LLM-as-judge** or **behavioral assertions**:

```python
async def test_agent_answers_question():
    """E2E test with behavioral assertion."""
    agent = ReActAgent(provider=get_provider(), max_iterations=5)
    response = await agent.run(session_id, "What is 2+2?")
    
    # Behavioral assertion, not exact match
    assert response.content is not None
    assert len(response.content) > 0
    assert "4" in response.content  # Correct answer present
    
    # LLM-as-judge (optional, for subjective quality)
    judge_prompt = f"Does this answer correctly respond to 'What is 2+2'? Answer: {response.content}"
    judge_response = await provider.complete([{"role": "user", "content": judge_prompt}])
    assert "yes" in judge_response.content.lower()
```

### 5.3 E2E Test Matrix

| Scenario | LLM | DB | Tools | Assertion |
|----------|-----|-----|-------|-----------|
| Simple Q&A | Real | Real | None | Answer contains expected fact |
| File read | Real | Real | read_file | File content in response |
| Multi-tool | Real | Real | read_file + write_file | Both tools executed |
| Error recovery | Real | Real | failing tool | Agent reports error gracefully |
| Skill activation | Real | Real | skill content | Skill content in prompt |
| Session continuity | Real | Real | None | Context from turn 1 in turn 2 |
| Max iterations | Real | Real | None | Graceful termination |
| Token budget | Real | Real | None | Budget not exceeded |

### 5.4 Current State

AgentHarness has **zero true E2E tests**. The CLI tests use `CliRunner` but mock the database and session manager. No test exercises the full `chat` command with a real agent loop.

---

## 6. LLM Mocking Strategies

### 6.1 The Five Test Doubles for LLM

Following Martin Fowler's taxonomy ([Test Double](https://martinfowler.com/bliki/TestDouble.html)):

| Double | Use Case | Example |
|--------|----------|---------|
| **Dummy** | Fill parameter lists, never used | `LLMResponse(content="")` for tests that don't check LLM output |
| **Stub** | Return canned responses | `provider.complete = AsyncMock(return_value=LLMResponse(content="test"))` |
| **Fake** | Working implementation, simplified | `FakeProvider` that returns predetermined responses based on input patterns |
| **Spy** | Record calls for verification | `SpyProvider` that wraps real provider and records all calls |
| **Mock** | Pre-programmed expectations | `mock_provider.complete.assert_called_with(messages=...)` |

### 6.2 Recommended: FakeProvider Pattern

```python
class FakeProvider(LLMProvider):
    """Deterministic provider for testing — no API calls."""
    
    def __init__(self, responses: list[LLMResponse]):
        self.responses = responses
        self.call_count = 0
        self.calls: list[dict] = []
    
    async def complete(self, messages, **kwargs) -> LLMResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.call_count >= len(self.responses):
            raise RuntimeError("FakeProvider ran out of responses")
        response = self.responses[self.call_count]
        self.call_count += 1
        return response
    
    async def stream_complete(self, messages, **kwargs):
        response = await self.complete(messages, **kwargs)
        yield StreamEvent(type="text", content=response.content)
        yield StreamEvent(type="done", response=response)
    
    async def embed(self, text: str) -> list[float]:
        return [0.1] * 1536  # Dummy embedding
```

### 6.3 Mocking at the Right Level

**Don't mock too high** (mocking the agent loop itself):
```python
# BAD: Tests nothing about the agent
with patch("ah.core.agent.ReActAgent.run") as mock_run:
    mock_run.return_value = AgentResponse(content="test")
    # This tests that the mock was called, not that the agent works
```

**Don't mock too low** (mocking `json.loads`):
```python
# BAD: Tests that json.loads was called, not that the agent handles JSON
with patch("json.loads") as mock_loads:
    mock_loads.return_value = {"path": "/tmp/test"}
    # This tests the mock, not the agent
```

**Do mock at the provider boundary**:
```python
# GOOD: Mock only the LLM, test everything else for real
provider = FakeProvider([
    LLMResponse(content="", tool_calls=[...]),
    LLMResponse(content="final answer"),
])
agent = ReActAgent(provider=provider, max_iterations=5)
response = await agent.run(session_id, "test")
# Now verify the agent actually executed the tool, stored context, etc.
```

### 6.4 Mocking HTTP for Provider Tests

```python
@pytest.fixture
def mock_httpx():
    """Mock httpx at the transport level."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "test", "tool_calls": []}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        
        yield mock_client
```

### 6.5 Current State

AgentHarness uses `AsyncMock` for provider mocking in `TestReActAgent` and `TestOpenRouterProvider`. This is appropriate for unit tests but insufficient for integration tests where the full request/response cycle should be tested.

---

## 7. Property-Based Testing

### 7.1 Why Property-Based Testing for Agents

Property-based testing (PBT) generates hundreds of random inputs to verify **invariants** — properties that should hold for all inputs. This is especially valuable for agent frameworks because:

- **Prompt assembly** should never produce empty prompts, regardless of input
- **Token estimation** should always be positive and monotonic (longer text → more tokens)
- **Tool schema inference** should always produce valid JSON Schema
- **Context compression** should never exceed the original size
- **Agent loop** should always terminate (no infinite loops)

### 7.2 Hypothesis for AgentHarness

```python
from hypothesis import given, strategies as st, settings

class TestPromptAssemblerProperties:
    """Property-based tests for PromptAssembler."""
    
    @given(
        system_prompt=st.text(min_size=0, max_size=1000),
        goal=st.text(min_size=0, max_size=500),
        query=st.text(min_size=0, max_size=500),
    )
    @settings(max_examples=100)
    def test_assemble_never_empty(self, system_prompt, goal, query):
        """Property: Assembled prompt is never empty."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt=system_prompt,
            goal=goal or None,
            recent_chunks=[],
            retrieved_chunks=[],
            query=query,
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0
    
    @given(text=st.text(min_size=1, max_size=10000))
    def test_estimate_tokens_positive(self, text):
        """Property: Token estimate is always positive for non-empty text."""
        assembler = PromptAssembler()
        tokens = assembler._estimate_tokens(text)
        assert tokens > 0
    
    @given(
        text1=st.text(min_size=1, max_size=1000),
        text2=st.text(min_size=1, max_size=1000),
    )
    def test_estimate_tokens_monotonic(self, text1, text2):
        """Property: Longer text produces more tokens."""
        assembler = PromptAssembler()
        combined = text1 + text2
        assert assembler._estimate_tokens(combined) >= assembler._estimate_tokens(text1)
    
    @given(
        chunk_type=st.sampled_from(["user_message", "tool_call", "result", "memory", "system"]),
        payload=st.dictionaries(
            keys=st.text(min_size=1, max_size=10),
            values=st.text(min_size=0, max_size=500),
        ),
    )
    def test_compress_chunk_never_exceeds_original(self, chunk_type, payload):
        """Property: Compressed chunk is never larger than original."""
        assembler = PromptAssembler()
        original = json.dumps({"type": chunk_type, "payload": payload})
        compressed = assembler._compress_chunk({"type": chunk_type, "payload": payload})
        assert len(compressed) <= len(original) + 50  # Small overhead for formatting
```

### 7.3 Property-Based Testing for Tool Registry

```python
class TestToolRegistryProperties:
    """Property-based tests for ToolRegistry."""
    
    @given(
        func_name=st.text(
            min_size=1, max_size=50,
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"),
        ),
        description=st.text(min_size=0, max_size=200),
    )
    def test_register_and_retrieve(self, func_name, description):
        """Property: Registered tool can be retrieved by name."""
        reg = ToolRegistry()
        
        @reg.register(name=func_name, description=description)
        def dummy_tool() -> str:
            return "ok"
        
        tool = reg.get_tool(func_name)
        assert tool is not None
        assert tool.name == func_name
        assert tool.description == description
    
    @given(
        args=st.dictionaries(
            keys=st.text(min_size=1, max_size=10),
            values=st.integers(),
        ),
    )
    def test_execute_with_arbitrary_args(self, args):
        """Property: Tool execution with arbitrary args doesn't crash."""
        reg = ToolRegistry()
        
        @reg.register()
        def echo_tool(**kwargs) -> dict:
            return kwargs
        
        result = asyncio.run(reg.execute("echo_tool", **args))
        assert result == args
```

### 7.4 Current State

AgentHarness has **zero property-based tests**. This is a significant gap — PBT would catch edge cases in prompt assembly, token estimation, and tool schema inference that the current example-based tests miss.

---

## 8. Chaos Engineering

### 8.1 What is Chaos Engineering for AI Agents

Chaos engineering for AI agents involves **deliberately injecting failures** to validate that the agent recovers gracefully. Unlike traditional chaos engineering (which kills servers), agent chaos focuses on:

- **LLM failures** — malformed responses, timeouts, rate limits, empty content
- **Tool failures** — crashes, timeouts, invalid outputs, permission errors
- **Context failures** — corrupted data, missing chunks, embedding failures
- **Database failures** — connection drops, pool exhaustion, slow queries

### 8.2 ChaosLLM Taxonomy

From [ChaosLLM (ISSRE 2025)](https://orbilu.uni.lu/bitstream/10993/67676/1/ISSRE2025_Iannillo.pdf), four classes of tool-level failures:

| Failure Class | Description | AgentHarness Example |
|---------------|-------------|---------------------|
| **Crash** | Tool raises exception | `read_file` on non-existent file |
| **Timeout** | Tool hangs | `terminal("sleep 1000")` |
| **Corruption** | Tool returns wrong data | `read_file` returns wrong file content |
| **Latency** | Tool is slow | `web_search` with 30s delay |

### 8.3 Chaos Test Implementation

```python
class TestAgentChaos:
    """Chaos engineering tests for AgentHarness."""
    
    async def test_agent_handles_malformed_tool_json(self):
        """Agent should not crash on invalid tool call JSON."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "read_file", "arguments": "not valid json{"},
            }],
        ))
        
        agent = ReActAgent(provider=provider, max_iterations=5)
        response = await agent.run(session_id, "test")
        
        # Should not crash — should return an error or retry
        assert response is not None
        assert response.iterations >= 1
    
    async def test_agent_handles_tool_timeout(self):
        """Agent should not hang when a tool times out."""
        # Register a tool that sleeps forever
        @registry.register()
        async def slow_tool():
            await asyncio.sleep(1000)
        
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "slow_tool", "arguments": "{}"},
            }],
        ))
        
        agent = ReActAgent(provider=provider, max_iterations=5)
        
        # Should timeout, not hang forever
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agent.run(session_id, "test"), timeout=5)
    
    async def test_agent_handles_empty_llm_response(self):
        """Agent should handle empty LLM response gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            tool_calls=[],
        ))
        
        agent = ReActAgent(provider=provider, max_iterations=5)
        response = await agent.run(session_id, "test")
        
        assert response is not None
        assert response.content == ""
    
    async def test_agent_handles_nonexistent_tool(self):
        """Agent should handle non-existent tool gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "nonexistent_tool", "arguments": "{}"},
            }],
        ))
        
        agent = ReActAgent(provider=provider, max_iterations=5)
        response = await agent.run(session_id, "test")
        
        # Should report error, not crash
        assert response is not None
        assert "Error" in response.content or "not registered" in response.content
    
    async def test_agent_recovers_from_tool_failure(self):
        """Agent should continue after a tool failure."""
        call_count = 0
        
        @registry.register()
        async def flaky_tool(should_fail: bool = True) -> str:
            nonlocal call_count
            call_count += 1
            if should_fail:
                raise RuntimeError("Tool failed!")
            return "success"
        
        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=[
            # First call: tool that fails
            LLMResponse(content="", tool_calls=[{
                "id": "call_1",
                "function": {"name": "flaky_tool", "arguments": json.dumps({"should_fail": True})},
            }]),
            # Second call: tool that succeeds
            LLMResponse(content="", tool_calls=[{
                "id": "call_2",
                "function": {"name": "flaky_tool", "arguments": json.dumps({"should_fail": False})},
            }]),
            # Third call: final answer
            LLMResponse(content="Recovered from failure"),
        ])
        
        agent = ReActAgent(provider=provider, max_iterations=5)
        response = await agent.run(session_id, "test")
        
        assert "Recovered" in response.content
        assert call_count == 2
```

### 8.4 Chaos Engineering for Database

```python
class TestDatabaseChaos:
    """Chaos tests for database failures."""
    
    async def test_agent_handles_db_connection_drop(self):
        """Agent should handle database connection drop gracefully."""
        # Simulate DB failure during agent run
        with patch("ah.core.context.db") as mock_db:
            mock_db.fetchrow = AsyncMock(side_effect=[
                Session(id=uuid.uuid4()),  # First call succeeds
                ConnectionDoesNotExistError("Connection lost"),  # Second call fails
            ])
            
            agent = ReActAgent(provider=mock_provider, max_iterations=5)
            response = await agent.run(session_id, "test")
            
            # Should not crash — should return error
            assert response is not None
    
    async def test_agent_handles_slow_db(self):
        """Agent should handle slow database queries."""
        async def slow_fetchrow(*args, **kwargs):
            await asyncio.sleep(5)  # Simulate slow query
            return None
        
        with patch("ah.core.context.db") as mock_db:
            mock_db.fetchrow = AsyncMock(side_effect=slow_fetchrow)
            
            agent = ReActAgent(provider=mock_provider, max_iterations=5)
            
            # Should complete, even if slow
            response = await asyncio.wait_for(
                agent.run(session_id, "test"),
                timeout=30,
            )
            assert response is not None
```

### 8.5 Current State

AgentHarness has **zero chaos engineering tests**. The critique document identifies several production crash paths that are untested:
- Malformed tool call JSON
- Tool execution timeout
- Empty LLM response
- Non-existent tool
- Msgpack serialization failure
- Database connection drop

---

## 9. Load Testing

### 9.1 What Makes Agent Load Testing Different

Traditional load testing assumes **stateless, deterministic** requests. AI agents violate both assumptions:

- **Stateful** — conversations accumulate context, making each request heavier than the last
- **Non-deterministic** — LLM outputs vary, making response times unpredictable
- **Expensive** — every LLM call costs money, so load testing can be costly
- **Context-dependent** — performance degrades as context grows ("context avalanche")

### 9.2 Load Testing Strategy for AgentHarness

```python
import asyncio
import time
from statistics import mean, stdev

class AgentLoadTest:
    """Load testing for AgentHarness."""
    
    async def run_concurrent_agents(self, num_agents: int, messages_per_agent: int):
        """Run multiple agents concurrently."""
        latencies = []
        errors = []
        
        async def run_agent(agent_id: int):
            provider = FakeProvider([...])  # Pre-configured responses
            agent = ReActAgent(provider=provider, max_iterations=5)
            
            for msg_num in range(messages_per_agent):
                start = time.monotonic()
                try:
                    response = await agent.run(
                        session_id=uuid.uuid4(),
                        user_message=f"Message {msg_num} from agent {agent_id}",
                    )
                    latency = time.monotonic() - start
                    latencies.append(latency)
                except Exception as e:
                    errors.append((agent_id, msg_num, str(e)))
        
        await asyncio.gather(*[
            run_agent(i) for i in range(num_agents)
        ])
        
        return {
            "total_requests": num_agents * messages_per_agent,
            "successful": len(latencies),
            "failed": len(errors),
            "mean_latency": mean(latencies) if latencies else 0,
            "p95_latency": sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0,
            "errors": errors,
        }
```

### 9.3 Load Testing Scenarios

| Scenario | Concurrency | Messages/Agent | Assertion |
|----------|-------------|----------------|-----------|
| Baseline | 1 | 1 | Establishes baseline latency |
| Low load | 5 | 5 | Latency within 2x of baseline |
| Medium load | 20 | 10 | No errors, p95 < 30s |
| High load | 50 | 20 | Graceful degradation |
| Stress | 100 | 50 | System doesn't crash |
| Context avalanche | 10 | 100 | Context doesn't exceed budget |

### 9.4 Load Testing Tools

- **Locust** — Python-based, good for HTTP-level load testing
- **k6** — JavaScript-based, good for API load testing
- **Gatling** — Scala-based, high performance
- **Custom asyncio** — for agent-specific load testing (as shown above)

### 9.5 Current State

AgentHarness has **zero load testing**. This is expected for an early-stage project but should be addressed before production deployment.

---

## 10. Coverage Requirements

### 10.1 Coverage Targets

| Module | Line Coverage | Branch Coverage | Critical Paths |
|--------|--------------|-----------------|----------------|
| `ah/core/agent.py` | 90% | 85% | 100% |
| `ah/core/provider.py` | 85% | 80% | 100% |
| `ah/core/assembler.py` | 95% | 90% | 100% |
| `ah/core/context.py` | 85% | 80% | 100% |
| `ah/core/session.py` | 85% | 80% | 100% |
| `ah/tools/` | 80% | 75% | 100% |
| `ah/skills/` | 90% | 85% | 100% |
| `ah/cli/` | 70% | 60% | 100% |
| **Overall** | **85%** | **80%** | **100%** |

### 10.2 Critical Paths Requiring 100% Coverage

These paths must have complete test coverage because failure here means production outage:

1. **Agent loop termination** — the agent must always terminate (no infinite loops)
2. **Tool execution error handling** — tool failures must not crash the agent
3. **LLM response parsing** — malformed LLM responses must be handled gracefully
4. **Database connection management** — connections must be acquired and released correctly
5. **Context budget enforcement** — token budgets must be respected
6. **Session state consistency** — concurrent updates must not corrupt state

### 10.3 Coverage Measurement

```bash
# Run tests with coverage
pytest --cov=ah --cov-report=term-missing --cov-report=html

# Fail if coverage drops below threshold
pytest --cov=ah --cov-fail-under=85
```

### 10.4 Current State

AgentHarness has **~1,030 tests** with unknown coverage. Based on the critique document, coverage is likely:
- **High** for `PromptAssembler`, `ToolRegistry`, `SkillParser` (well-tested)
- **Medium** for `ReActAgent` (mocked tests, doesn't verify real behavior)
- **Low** for `Database`, `ContextManager`, `SessionManager` (mocked DB tests)
- **Zero** for `web_search`, `web_extract`, streaming, `memory`, `rag`

---

## 11. Current State Assessment

### 11.1 Test Inventory

| Category | Count | Quality | Notes |
|----------|-------|---------|-------|
| Unit tests | ~80 | Good | Cover pure functions well |
| Integration tests | ~30 | Poor | All use mocked DB, no real SQL tested |
| E2E tests | 0 | N/A | No full workflow tests |
| Property-based tests | 0 | N/A | No Hypothesis tests |
| Chaos tests | 0 | N/A | No fault injection tests |
| Load tests | 0 | N/A | No performance tests |
| **Total** | **~1,030** | | **Mixed** | |

### 11.2 Critical Gaps

1. **No real database tests** — SQL correctness is unverified
2. **No real tool execution in agent loop** — tools are mocked
3. **No streaming tests** — `stream_complete` is untested
4. **No chaos tests** — error recovery is untested
5. **No property-based tests** — edge cases are missed
6. **No load tests** — performance is unverified
7. **No E2E tests** — full workflows are untested

### 11.3 Misleading Tests

The critique document identifies several tests that provide false confidence:

- `test_assemble_token_budget` — asserts string inclusion, not budget enforcement
- `test_agent_run_with_tool_calls` — verifies provider call count, not tool execution
- `test_session_crud` — skipped when PostgreSQL unavailable
- `test_status_command_no_db` — asserts failure as success

---

## 12. Recommended Test Architecture

### 12.1 Test Directory Structure

```
tests/
├── conftest.py                    # Shared fixtures
├── unit/
│   ├── test_assembler.py          # PromptAssembler unit tests
│   ├── test_tool_registry.py      # ToolRegistry unit tests
│   ├── test_skill_parser.py       # SkillParser unit tests
│   ├── test_models.py             # Dataclass unit tests
│   ├── test_provider.py           # Provider validation unit tests
│   └── test_rate_limiter.py       # AsyncTokenBucket unit tests
├── integration/
│   ├── test_agent_loop.py         # Agent + real tools, mocked LLM
│   ├── test_database.py           # Real database tests (testcontainers)
│   ├── test_session_manager.py    # Session + real DB
│   ├── test_context_manager.py    # Context + real DB
│   └── test_provider_http.py      # Provider + mocked HTTP
├── e2e/
│   ├── test_cli_workflow.py       # Full CLI workflow tests
│   ├── test_multi_turn.py         # Multi-turn conversation tests
│   └── test_skill_activation.py   # Skill activation tests
├── property/
│   ├── test_assembler_properties.py
│   ├── test_tool_properties.py
│   └── test_agent_properties.py
├── chaos/
│   ├── test_agent_chaos.py        # Agent failure recovery
│   ├── test_tool_chaos.py         # Tool failure injection
│   └── test_db_chaos.py           # Database failure injection
├── load/
│   ├── test_concurrent_agents.py  # Concurrent agent load test
│   └── test_context_avalanche.py  # Context growth load test
└── fixtures/
    ├── fake_provider.py           # FakeProvider implementation
    ├── mock_responses.py          # Pre-configured LLM responses
    └── test_data.py               # Test data generators
```

### 12.2 Shared Fixtures

```python
# conftest.py
import pytest
from unittest.mock import AsyncMock
from ah.core.models import LLMResponse

@pytest.fixture
def fake_provider():
    """Create a FakeProvider with default responses."""
    from tests.fixtures.fake_provider import FakeProvider
    return FakeProvider([
        LLMResponse(content="test response", model="fake"),
    ])

@pytest.fixture
def mock_tool_response():
    """Create a mock tool response."""
    return {"tool": "read_file", "args": {"path": "/tmp/test"}, "result": "file content"}

@pytest.fixture
async def real_db():
    """Create a real database connection using testcontainers."""
    from testcontainers.postgres import PostgresContainer
    from ah.db.connection import Database
    
    with PostgresContainer("postgres:16-alpine") as pg:
        db = Database(pg.get_connection_url())
        await db.connect()
        await db.initialize_schema()
        yield db
        await db.close()

@pytest.fixture
def temp_session(tmp_path):
    """Create a temporary session with real database."""
    # Implementation depends on test setup
    pass
```

### 12.3 Test Configuration

```ini
# pytest.ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts = -v --tb=short --strict-markers
markers =
    unit: Unit tests (fast, no external deps)
    integration: Integration tests (may use testcontainers)
    e2e: End-to-end tests (slow, may use real APIs)
    property: Property-based tests (Hypothesis)
    chaos: Chaos engineering tests (fault injection)
    load: Load tests (performance)
    slow: Tests that take > 10 seconds
```

---

## 13. Implementation Roadmap

### Phase 1: Foundation (Week 1-2)

1. **Set up test infrastructure**
   - Add `pytest-cov`, `hypothesis`, `testcontainers` to dev dependencies
   - Create `conftest.py` with shared fixtures
   - Create `tests/fixtures/` directory with `FakeProvider` and test data

2. **Add property-based tests**
   - `PromptAssembler` properties (never empty, token bounds, monotonic)
   - `ToolRegistry` properties (register/retrieve, schema validity)
   - `Agent` properties (always terminates, respects budget)

3. **Fix misleading tests**
   - Fix `test_assemble_token_budget` to assert actual budget enforcement
   - Fix `test_agent_run_with_tool_calls` to verify tool execution
   - Fix `test_status_command_no_db` to assert graceful degradation

### Phase 2: Integration (Week 3-4)

4. **Add real database tests**
   - Set up `testcontainers-python` for PostgreSQL
   - Test `SessionManager` CRUD with real SQL
   - Test `ContextManager` CRUD with real SQL
   - Test msgpack serialization round-trips

5. **Add agent loop integration tests**
   - Test agent + real tools (mocked LLM)
   - Test agent + real database (mocked LLM)
   - Test full flow: session → context → agent → response

6. **Add streaming tests**
   - Test `stream_complete` SSE parsing
   - Test `stream_complete` NDJSON parsing (Ollama)
   - Test streaming tool call accumulation

### Phase 3: Chaos & E2E (Week 5-6)

7. **Add chaos engineering tests**
   - Malformed tool JSON
   - Tool timeout
   - Empty LLM response
   - Non-existent tool
   - Database connection drop
   - Msgpack serialization failure

8. **Add E2E tests**
   - CLI workflow test
   - Multi-turn conversation test
   - Skill activation test
   - Error recovery test

### Phase 4: Load & Monitoring (Week 7-8)

9. **Add load tests**
   - Concurrent agent test
   - Context avalanche test
   - Database connection pool test

10. **Set up CI/CD**
    - Run unit tests on every commit
    - Run integration tests on every PR
    - Run E2E tests nightly
    - Run chaos tests weekly
    - Run load tests pre-release

11. **Add coverage gates**
    - Enforce 85% line coverage
    - Enforce 100% coverage on critical paths
    - Generate coverage reports in CI

---

## 14. References

1. **OpenHelm** — "Agent Testing Strategies: Unit, Integration, and End-to-End Testing for AI Systems" (2024) — https://openhelm.ai/blog/agent-testing-strategies-unit-integration-e2e

2. **Zylos Research** — "AI Agent Testing Strategies — From Unit Tests to Production Validation" (2026) — https://zylos.ai/research/2026-05-07-ai-agent-testing-strategies-production-validation/

3. **Vargas, L.** — "Production AI Systems: The Unit Testing Paradox" (2025) — https://aienhancedengineer.substack.com/p/production-ai-systems-the-unit-testing

4. **Owotogbe, J.** — "Assessing and Enhancing the Robustness of LLM-based Multi-Agent Systems Through Chaos Engineering" (2025) — https://arxiv.org/abs/2505.03096

5. **Iannillo et al.** — "ChaosLLM: A Dependability Testing Approach for Tool-Calling Agents" (ISSRE 2025) — https://orbilu.uni.lu/bitstream/10993/67676/1/ISSRE2025_Iannillo.pdf

6. **Jing et al.** — "PBT-Bench: Benchmarking AI Agents on Property-Based Testing" (2026) — https://arxiv.org/abs/2605.15229

7. **Fowler, M.** — "Test Double" — https://martinfowler.com/bliki/TestDouble.html

8. **The New Stack** — "Why Load Tests Lie: Harsh Truth About AI Agent Performance" (2026) — https://thenewstack.io/why-load-tests-lie-harsh-truth-about-ai-agent-performance

9. **IBM** — "AI Agent Testing" — https://www.ibm.com/think/topics/ai-agent-testing

10. **Gatling** — "Load testing for AI and LLMs" — https://gatling.io/use-cases/ai-llms

11. **Artificial Analysis** — "AA-AgentPerf Methodology" — https://artificialanalysis.ai/methodology/agentperf

12. **AgentHarness Internal** — `docs/critique-testing.md` — Comprehensive critique of current test suite

---

*This document is a living reference. Update it as the test suite evolves and new testing patterns are discovered.*
