"""Property-based tests using Hypothesis for Machina components.

These tests verify invariants that should hold for all inputs:
- Prompt assembly never produces empty prompts
- Token estimation is positive and monotonic
- Tool schema inference always produces valid JSON Schema
- Context compression never exceeds original size
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from machina.core.assembler import PromptAssembler, get_token_count
from machina.core.models import ContextChunk
from machina.core.provider import AsyncTokenBucket
from machina.core.provider.transport import _validate_messages, _validate_params
from machina.tools.base import ToolRegistry

# ===========================================================================
# PromptAssembler Properties
# ===========================================================================


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

    @given(
        system_prompt=st.text(min_size=0, max_size=500),
        goal=st.text(min_size=0, max_size=200),
        query=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=100)
    def test_assemble_always_includes_system_prompt(self, system_prompt, goal, query):
        """Property: System prompt is always included in output."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt=system_prompt,
            goal=goal or None,
            recent_chunks=[],
            retrieved_chunks=[],
            query=query,
        )
        if system_prompt:
            assert system_prompt in prompt

    @given(
        system_prompt=st.text(min_size=0, max_size=500),
        goal=st.text(min_size=0, max_size=200),
        query=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=100)
    def test_assemble_always_includes_query(self, system_prompt, goal, query):
        """Property: Query is always included in output."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt=system_prompt,
            goal=goal or None,
            recent_chunks=[],
            retrieved_chunks=[],
            query=query,
        )
        if query:
            assert query in prompt

    @given(
        text=st.text(min_size=10, max_size=10000).filter(lambda s: sum(c.isalpha() for c in s) >= 5)
    )
    @settings(max_examples=200)
    def test_estimate_tokens_positive(self, text):
        """Property: Token estimate is always positive for non-trivial text."""
        assembler = PromptAssembler()
        tokens = assembler._estimate_tokens(text)
        assert tokens > 0

    @given(text=st.text(min_size=0, max_size=100))
    @settings(max_examples=100)
    def test_estimate_tokens_empty(self, text):
        """Property: Token estimate for empty text is 0."""
        assembler = PromptAssembler()
        tokens = assembler._estimate_tokens(text)
        if not text:
            assert tokens == 0
        else:
            assert tokens >= 0

    @given(
        text1=st.text(min_size=1, max_size=1000),
        text2=st.text(min_size=1, max_size=1000),
    )
    @settings(max_examples=200)
    def test_estimate_tokens_monotonic(self, text1, text2):
        """Property: Longer text produces >= tokens (monotonicity)."""
        assembler = PromptAssembler()
        combined = text1 + text2
        assert assembler._estimate_tokens(combined) >= assembler._estimate_tokens(text1)

    @given(
        text1=st.text(min_size=10, max_size=500).filter(lambda s: any(c.isalpha() for c in s)),
        text2=st.text(min_size=10, max_size=500).filter(lambda s: any(c.isalpha() for c in s)),
    )
    @settings(max_examples=100)
    def test_estimate_tokens_subadditive(self, text1, text2):
        """Property: Tokens(a+b) <= Tokens(a) + Tokens(b) + overhead."""
        assembler = PromptAssembler()
        combined = text1 + text2
        # BPE tokenizers may have overhead at boundaries; allow generous margin
        assert assembler._estimate_tokens(combined) <= (
            assembler._estimate_tokens(text1) + assembler._estimate_tokens(text2) + 20
        )

    @given(
        chunk_type=st.sampled_from(
            [
                "user_message",
                "tool_call",
                "result",
                "memory",
                "heartbeat",
                "system",
                "user",
                "assistant",
            ]
        ),
        payload=st.dictionaries(
            keys=st.text(min_size=1, max_size=10),
            values=st.text(min_size=0, max_size=500),
        ),
    )
    @settings(max_examples=100)
    def test_compress_chunk_never_exceeds_original(self, chunk_type, payload):
        """Property: Compressed chunk is never much larger than original."""
        assembler = PromptAssembler()
        original = json.dumps({"type": chunk_type, "payload": payload})
        compressed = assembler._compress_chunk({"type": chunk_type, "payload": payload})
        # Compressed should not exceed original by more than formatting overhead
        assert len(compressed) <= len(original) + 100

    @given(
        chunk_type=st.sampled_from(
            [
                "user_message",
                "tool_call",
                "result",
                "memory",
                "heartbeat",
                "system",
                "user",
                "assistant",
            ]
        ),
        content=st.text(min_size=0, max_size=1000),
    )
    @settings(max_examples=100)
    def test_compress_chunk_preserves_content(self, chunk_type, content):
        """Property: Compressed chunk preserves some content."""
        assembler = PromptAssembler()
        compressed = assembler._compress_chunk(
            {
                "type": chunk_type,
                "payload": {"content": content},
            }
        )
        # Should be a non-empty string
        assert isinstance(compressed, str)
        assert len(compressed) > 0

    @given(
        system_prompt=st.text(min_size=0, max_size=200),
        goal=st.text(min_size=0, max_size=100),
        query=st.text(min_size=0, max_size=100),
        budget=st.integers(min_value=100, max_value=50000),
    )
    @settings(max_examples=100)
    def test_assemble_respects_budget(self, system_prompt, goal, query, budget):
        """Property: optional (retrieved) context never pushes the prompt past
        the budget. System prompt, goal and query are always included, so the
        bound is max(budget, mandatory tokens)."""

        assembler = PromptAssembler(session_budget=budget)
        mandatory = assembler.assemble(
            system_prompt=system_prompt,
            goal=goal or None,
            recent_chunks=[],
            retrieved_chunks=[],
            query=query,
        )
        mandatory_tokens = assembler._estimate_tokens(mandatory)

        big = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="h",
            chunk_type="memory",
            payload={"content": (system_prompt + query + " ctx") * 50},
        )
        prompt = assembler.assemble(
            system_prompt=system_prompt,
            goal=goal or None,
            recent_chunks=[],
            retrieved_chunks=[(big, 0.9)] * 20,
            query=query,
        )
        prompt_tokens = assembler._estimate_tokens(prompt)
        assert prompt_tokens <= max(budget, mandatory_tokens) + 5

    @given(
        num_chunks=st.integers(min_value=0, max_value=20),
        chunk_size=st.integers(min_value=1, max_value=500),
    )
    @settings(max_examples=50)
    def test_assemble_with_variable_chunks(self, num_chunks, chunk_size):
        """Property: Assembly works with variable number of chunks."""
        assembler = PromptAssembler(session_budget=8000)
        chunks = [
            {"type": "user_message", "payload": {"content": "x" * chunk_size}}
            for _ in range(num_chunks)
        ]
        prompt = assembler.assemble(
            system_prompt="System",
            goal="Goal",
            recent_chunks=chunks,
            retrieved_chunks=[],
            query="Query",
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0


# ===========================================================================
# Token Counter Properties
# ===========================================================================


class TestTokenCounterProperties:
    """Property-based tests for token counting."""

    @given(text=st.text(min_size=0, max_size=5000))
    @settings(max_examples=200)
    def test_token_count_non_negative(self, text):
        """Property: Token count is always non-negative."""
        count = get_token_count(text)
        assert count >= 0

    @given(
        text=st.text(min_size=10, max_size=1000).filter(lambda s: sum(c.isalpha() for c in s) >= 5)
    )
    @settings(max_examples=100)
    def test_token_count_positive_for_non_empty(self, text):
        """Property: Token count is positive for non-trivial text."""
        count = get_token_count(text)
        assert count > 0

    @given(
        text=st.text(min_size=1, max_size=500),
        repeat=st.integers(min_value=2, max_value=5),
    )
    @settings(max_examples=50)
    def test_token_count_scales_with_repetition(self, text, repeat):
        """Property: Token count scales with text repetition."""
        single = get_token_count(text)
        repeated = get_token_count(text * repeat)
        # Repeated text should have more tokens
        assert repeated >= single

    @given(text=st.text(min_size=0, max_size=1000))
    @settings(max_examples=100)
    def test_token_count_deterministic(self, text):
        """Property: Token count is deterministic."""
        count1 = get_token_count(text)
        count2 = get_token_count(text)
        assert count1 == count2


# ===========================================================================
# ToolRegistry Properties
# ===========================================================================


class TestToolRegistryProperties:
    """Property-based tests for ToolRegistry."""

    @given(
        func_name=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                whitelist_characters="_",
            ),
        ),
        description=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=100)
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
        func_name=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll"),
                whitelist_characters="_",
            ),
        ),
    )
    @settings(max_examples=100)
    def test_infer_schema_valid_json_schema(self, func_name):
        """Property: Inferred schema is valid JSON Schema."""
        reg = ToolRegistry()

        @reg.register(name=func_name)
        def tool_with_params(a: str, b: int, c: float = 1.0) -> str:
            return "ok"

        tool = reg.get_tool(func_name)
        schema = tool.parameters
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "a" in schema["properties"]
        assert "b" in schema["properties"]
        assert schema["properties"]["a"]["type"] == "string"
        assert schema["properties"]["b"]["type"] == "integer"
        assert schema["properties"]["c"]["type"] == "number"
        assert "a" in schema["required"]
        assert "b" in schema["required"]
        assert "c" not in schema["required"]

    @given(
        num_params=st.integers(min_value=0, max_value=10),
    )
    @settings(max_examples=50)
    def test_infer_schema_preserves_all_params(self, num_params):
        """Property: Schema inference preserves all parameters."""
        reg = ToolRegistry()

        # Create a function with num_params parameters
        params = [f"param{i}" for i in range(num_params)]
        func_def = f"def dynamic_tool({', '.join(params)}) -> str: return 'ok'"
        namespace = {}
        exec(func_def, namespace)
        dynamic_tool = namespace["dynamic_tool"]

        reg.register(name="dynamic_tool")(dynamic_tool)
        tool = reg.get_tool("dynamic_tool")
        assert len(tool.parameters["properties"]) == num_params

    @given(
        args=st.dictionaries(
            keys=st.text(min_size=1, max_size=10).filter(lambda k: k not in ("self", "name")),
            values=st.integers(),
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_execute_with_arbitrary_args(self, args):
        """Property: Tool execution with arbitrary args doesn't crash.

        ``deadline=None``: every example performs a real ``asyncio.run`` and
        logs audit events for validation errors, so per-example wall time is
        I/O-bound and load-sensitive (observed 338ms under a concurrent
        full-suite run vs ~1.5ms idle — a hypothesis DeadlineExceeded flake,
        not a logic failure). Timing is irrelevant to this property.
        """
        reg = ToolRegistry()

        @reg.register()
        def echo_tool(**kwargs) -> dict:
            return kwargs

        import asyncio

        try:
            result = asyncio.run(reg.execute("echo_tool", **args))
            assert result == args
        except ValueError:
            # Schema validation may reject unknown params - that's OK
            pass

    @given(
        tool_name=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                whitelist_characters="_",
            ),
        ),
    )
    @settings(max_examples=50)
    def test_get_tool_definitions_cached(self, tool_name):
        """Property: Tool definitions are cached."""
        reg = ToolRegistry()

        @reg.register(name=tool_name)
        def cached_tool() -> str:
            return "ok"

        defs1 = reg.get_tool_definitions()
        defs2 = reg.get_tool_definitions()
        assert defs1 is defs2  # Same object (cached)

    @given(
        num_tools=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=30)
    def test_list_tools_count(self, num_tools):
        """Property: List tools returns correct count."""
        reg = ToolRegistry()
        for i in range(num_tools):
            reg.register(name=f"tool_{i}")(lambda: "ok")
        assert len(reg.list_tools()) == num_tools


# ===========================================================================
# AsyncTokenBucket Properties
# ===========================================================================


class TestAsyncTokenBucketProperties:
    """Property-based tests for AsyncTokenBucket."""

    @given(
        rate=st.floats(min_value=0.1, max_value=100.0),
        capacity=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=50)
    def test_bucket_initial_state(self, rate, capacity):
        """Property: Bucket starts with full capacity."""
        bucket = AsyncTokenBucket(rate=rate, capacity=capacity)
        assert bucket.tokens == float(capacity)
        assert bucket.capacity == capacity
        assert bucket.rate == rate

    @given(
        rate=st.floats(min_value=0.1, max_value=100.0),
        capacity=st.integers(min_value=1, max_value=50),
        tokens=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=50)
    def test_acquire_reduces_tokens(self, rate, capacity, tokens):
        """Property: acquire() debits the bucket by exactly the requested amount.

        A fresh bucket is full, so the refill clamps at capacity and the debit
        is exact regardless of elapsed wall time. ``min`` keeps the request
        within capacity so ``acquire`` never has to wait.
        """
        bucket = AsyncTokenBucket(rate=rate, capacity=capacity)
        amount = min(tokens, capacity)
        initial = bucket.tokens
        asyncio.run(bucket.acquire(amount))
        assert bucket.tokens == initial - amount

    @given(
        rate=st.floats(min_value=0.1, max_value=100.0),
        capacity=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=50)
    def test_acquire_within_capacity(self, rate, capacity):
        """Property: acquiring one token from a full bucket succeeds immediately."""
        bucket = AsyncTokenBucket(rate=rate, capacity=capacity)
        asyncio.run(bucket.acquire(1))
        # Refill is clamped at capacity before the debit, so the result is exact.
        assert bucket.tokens == bucket.capacity - 1

    @given(
        rate=st.floats(min_value=0.1, max_value=10.0),
        capacity=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=30)
    def test_tokens_never_negative(self, rate, capacity):
        """Property: the bucket drains to zero and then blocks instead of going negative.

        The rate limiter's clock is frozen (its boundary dependency), so the
        post-drain request cannot be satisfied by refill: ``acquire`` must
        wait rather than debit below zero. ``wait_for`` fails the property if
        the bucket would ever satisfy the request without waiting.
        """
        from machina.core.provider import rate_limit as rate_limit_module

        class _FrozenClock:
            def monotonic(self) -> float:
                return 0.0

        with patch.object(rate_limit_module, "time", _FrozenClock()):
            bucket = AsyncTokenBucket(rate=rate, capacity=capacity)
            asyncio.run(bucket.acquire(capacity))
            assert bucket.tokens == 0.0

            async def _over_acquire() -> None:
                await asyncio.wait_for(bucket.acquire(1), timeout=0.05)

            with pytest.raises(TimeoutError):
                asyncio.run(_over_acquire())
            assert bucket.tokens >= 0.0


# ===========================================================================
# Validation Properties
# ===========================================================================


class TestValidationProperties:
    """Property-based tests for input validation."""

    @given(
        messages=st.lists(
            st.dictionaries(
                keys=st.sampled_from(["role", "content"]),
                values=st.text(min_size=0, max_size=100),
            ),
            min_size=0,
            max_size=10,
        ),
    )
    @settings(max_examples=100)
    def test_validate_messages_rejects_empty(self, messages):
        """Property: Empty message list is rejected; non-empty valid passes."""
        if not messages:
            with pytest.raises(ValueError):
                _validate_messages(messages)
        else:
            # Only assert no-raise for well-formed messages; invalid shapes
            # must raise (hypothesis generates both).
            valid = all(isinstance(m, dict) and "role" in m and "content" in m for m in messages)
            if valid:
                _validate_messages(messages)
            else:
                with pytest.raises(ValueError):
                    _validate_messages(messages)

    @given(
        messages=st.lists(
            st.fixed_dictionaries(
                {
                    "role": st.text(min_size=1, max_size=20),
                    "content": st.text(min_size=1, max_size=100),
                }
            ),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(max_examples=50)
    def test_validate_messages_accepts_valid(self, messages):
        """Property: Valid messages are accepted."""
        # Should not raise
        _validate_messages(messages)

    @given(
        temperature=st.floats(min_value=0.0, max_value=2.0),
        max_tokens=st.integers(min_value=1, max_value=32768),
    )
    @settings(max_examples=100)
    def test_validate_params_accepts_valid(self, temperature, max_tokens):
        """Property: Valid params are accepted."""
        _validate_params(temperature, max_tokens)

    @given(
        temperature=st.floats(min_value=-10.0, max_value=-0.01),
        max_tokens=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=50)
    def test_validate_params_rejects_negative_temperature(self, temperature, max_tokens):
        """Property: Negative temperature is rejected."""
        with pytest.raises(ValueError):
            _validate_params(temperature, max_tokens)

    @given(
        temperature=st.floats(min_value=2.01, max_value=100.0),
        max_tokens=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=50)
    def test_validate_params_rejects_high_temperature(self, temperature, max_tokens):
        """Property: Temperature > 2.0 is rejected."""
        with pytest.raises(ValueError):
            _validate_params(temperature, max_tokens)

    @given(
        temperature=st.floats(min_value=0.0, max_value=2.0),
        max_tokens=st.integers(min_value=0, max_value=0),
    )
    @settings(max_examples=20)
    def test_validate_params_rejects_zero_max_tokens(self, temperature, max_tokens):
        """Property: max_tokens < 1 is rejected."""
        with pytest.raises(ValueError):
            _validate_params(temperature, max_tokens)


# ===========================================================================
# ContextManager Properties (Mocked)
# ===========================================================================


class TestContextManagerProperties:
    """Property-based tests for the real ContextManager (DB boundary mocked)."""

    @given(
        chunk_type=st.sampled_from(
            [
                "tool_call",
                "result",
                "memory",
                "heartbeat",
                "system",
                "user",
                "assistant",
                "user_message",
                "assistant_message",
            ]
        ),
        payload=st.dictionaries(
            keys=st.text(min_size=1, max_size=10).filter(lambda key: key not in ("images", "data")),
            values=st.text(min_size=0, max_size=200),
        ),
    )
    @settings(max_examples=50)
    def test_add_chunk_preserves_type(self, chunk_type, payload):
        """Property: ``ContextManager.add_chunk`` round-trips type and payload.

        Only the DB boundary is mocked: the row echoes back what the INSERT
        args carried, so the real serialization (MessagePack pack/unpack) and
        the returned chunk assembly both run.
        """
        from machina.core import context as context_module

        session_id = uuid.uuid4()
        agent_id = "property-agent"

        async def fake_fetchrow(query: str, *args):
            return {
                "id": uuid.uuid4(),
                "session_id": args[0],
                "agent_id": args[1],
                "chunk_type": args[2],
                "payload_msgpack": args[3],
                "token_count": args[4],
                "embedding": args[5],
                "created_at": datetime.now(UTC),
                "accessed_at": None,
                "document_source": args[7],
            }

        with patch("machina.core.context.db") as fake_db:
            fake_db.fetchrow = fake_fetchrow
            chunk = asyncio.run(
                context_module.ContextManager().add_chunk(
                    session_id=session_id,
                    agent_id=agent_id,
                    chunk_type=chunk_type,
                    payload=payload,
                )
            )
        assert chunk.chunk_type == chunk_type
        assert chunk.payload == payload
        assert chunk.session_id == session_id
        assert chunk.agent_id == agent_id

    @given(
        chunk_types=st.lists(
            st.sampled_from(["tool_call", "result", "memory", "user_message"]),
            min_size=0,
            max_size=12,
        ),
    )
    @settings(max_examples=30)
    def test_add_chunks_batch_preserves_every_chunk(self, chunk_types):
        """Property: batch insert returns one chunk per input, in order, with
        payloads intact. The fake DB echoes the UNNEST column arrays back as
        rows, so the real batch preparation and row mapping run."""
        from machina.core import context as context_module

        session_id = uuid.uuid4()
        agent_id = "property-agent"
        chunks = [
            {
                "session_id": session_id,
                "agent_id": agent_id,
                "chunk_type": chunk_type,
                "payload": {"text": f"chunk-{i}", "seq": i},
                "token_count": 1,
            }
            for i, chunk_type in enumerate(chunk_types)
        ]

        async def fake_fetch(query: str, *args):
            (
                ids,
                session_ids,
                agent_ids,
                types,
                payloads,
                token_counts,
                embeddings,
                _search_texts,
                doc_sources,
            ) = args
            return [
                {
                    "id": ids[i],
                    "session_id": session_ids[i],
                    "agent_id": agent_ids[i],
                    "chunk_type": types[i],
                    "payload_msgpack": payloads[i],
                    "token_count": token_counts[i],
                    "embedding": embeddings[i],
                    "created_at": datetime.now(UTC),
                    "accessed_at": None,
                    "document_source": doc_sources[i],
                }
                for i in range(len(ids))
            ]

        with patch("machina.core.context.db") as fake_db:
            fake_db.fetch = fake_fetch
            out = asyncio.run(context_module.ContextManager().add_chunks_batch(chunks))
        assert [chunk.chunk_type for chunk in out] == chunk_types
        assert [chunk.payload for chunk in out] == [c["payload"] for c in chunks]
        assert all(chunk.session_id == session_id for chunk in out)


# ===========================================================================
# Model Properties
# ===========================================================================


class TestModelProperties:
    """Property-based tests for data models."""

    @given(
        title=st.text(min_size=0, max_size=200),
        agent_id=st.text(min_size=1, max_size=50),
        context_budget=st.integers(min_value=100, max_value=100000),
    )
    @settings(max_examples=50)
    def test_session_creation(self, title, agent_id, context_budget):
        """Property: Session can be created with various parameters."""
        from machina.core.models import Session

        s = Session(
            id=uuid.uuid4(),
            title=title or None,
            agent_id=agent_id,
            context_budget=context_budget,
        )
        assert s.status == "active"
        assert s.context_budget == context_budget

    @given(
        content=st.text(min_size=0, max_size=1000),
        model=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=50)
    def test_llm_response_creation(self, content, model):
        """Property: LLMResponse can be created with various content."""
        from machina.core.models import LLMResponse

        r = LLMResponse(content=content, model=model)
        assert r.content == content
        assert r.model == model
        assert r.usage == {}
        assert r.tool_calls == []

    @given(
        name=st.text(min_size=1, max_size=50),
        description=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=50)
    def test_tool_definition_creation(self, name, description):
        """Property: ToolDefinition can be created."""
        from machina.core.models import ToolDefinition

        td = ToolDefinition(
            name=name,
            description=description,
            parameters={"type": "object", "properties": {}},
        )
        assert td.name == name
        assert td.description == description
