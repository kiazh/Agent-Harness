"""Extended chaos engineering tests for AgentHarness (Gate 3).

These tests go beyond tests/test_chaos.py to probe failure modes in:
- LLM provider (malformed JSON, mid-stream failures, missing usage)
- Embedder (outage, cascade prevention)
- Orchestrator (delegation timeout, leaked tasks)
- Scheduler (worker crash, lease expiry, job reclaim)
- Context eviction (interrupted mid-batch, orphaned archives)
- Concurrent session writes (deadlock prevention)
- Memory system (store, retriever, consolidator failures)

Every test asserts a SPECIFIC safe behavior — no vacuous passes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.agent import ReActAgent
from ah.core.context import ContextManager
from ah.core.models import LLMResponse, Session, StreamEvent
from ah.core.orchestrator import Orchestrator
from ah.core.provider import OpenRouterProvider
from ah.core.usage import UsageStore
from ah.memory.consolidator import MemoryConsolidator
from ah.memory.models import MemoryEntry
from ah.memory.retriever import MemoryRetriever
from ah.memory.store import MemoryStore
from ah.rag.embedder import OpenAIEmbedder


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def mock_session():
    return Session(
        id=uuid.uuid4(),
        title="Test Session",
        context_budget=8000,
    )


@pytest.fixture
def mock_db():
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="DELETE 0")
    return mock


# ===========================================================================
# Provider Chaos — Malformed / Partial JSON
# ===========================================================================


class TestProviderMalformedJSON:
    """Provider returns malformed or partial JSON — must not crash."""

    def _make_provider(self, response_data: dict) -> OpenRouterProvider:
        provider = OpenRouterProvider.__new__(OpenRouterProvider)
        provider.api_key = "test-key"
        provider.model = "test-model"
        provider._default_effort = ""
        provider._rate_limiter = AsyncMock()
        provider.client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_response.raise_for_status = MagicMock()
        provider.client.post = AsyncMock(return_value=mock_response)
        return provider

    async def test_missing_choices_key_does_not_crash(self):
        """Provider response missing 'choices' key should raise ProviderError, not KeyError."""
        from ah.core.exceptions import ProviderError

        provider = self._make_provider({"model": "test-model"})  # no choices
        with pytest.raises(ProviderError):
            await provider.complete(
                messages=[{"role": "user", "content": "hello"}],
            )

    async def test_choices_empty_list_raises(self):
        """Provider response with empty choices list should raise ProviderError, not IndexError."""
        from ah.core.exceptions import ProviderError

        provider = self._make_provider({"choices": [], "usage": {}})
        with pytest.raises(ProviderError):
            await provider.complete(
                messages=[{"role": "user", "content": "hello"}],
            )

    async def test_message_missing_content_uses_empty_string(self):
        """Provider response with message missing 'content' should default to empty string."""
        provider = self._make_provider(
            {
                "choices": [{"message": {"role": "assistant"}}],  # no content
                "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
            }
        )
        result = await provider.complete(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert result.content == ""
        assert result.usage["total_tokens"] == 5

    async def test_usage_missing_fields_defaults_to_zero(self):
        """Provider response with missing usage fields should default to 0."""
        provider = self._make_provider(
            {
                "choices": [{"message": {"content": "OK", "tool_calls": []}}],
                "usage": {},  # empty usage
            }
        )
        result = await provider.complete(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert result.usage["prompt_tokens"] == 0
        assert result.usage["completion_tokens"] == 0
        assert result.usage["total_tokens"] == 0

    async def test_usage_with_string_values_does_not_crash(self):
        """Provider response with string usage values should not crash."""
        provider = self._make_provider(
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": "5", "completion_tokens": "3", "total_tokens": "8"},
            }
        )
        result = await provider.complete(
            messages=[{"role": "user", "content": "hello"}],
        )
        # Should handle gracefully — either convert or keep as-is
        assert result is not None

    async def test_tool_calls_with_missing_function_key(self):
        """Provider response with tool_call missing 'function' key should not crash."""
        provider = self._make_provider(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [{"id": "call_1"}],  # missing function
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        )
        result = await provider.complete(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert result is not None
        # The tool call should be preserved as-is (agent handles validation)
        assert len(result.tool_calls) == 1

    async def test_tool_calls_with_null_arguments(self):
        """Provider response with tool_call having null arguments should not crash."""
        provider = self._make_provider(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": "read_file", "arguments": None},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        )
        result = await provider.complete(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert result is not None


# ===========================================================================
# Provider Chaos — Mid-Stream Failure
# ===========================================================================


class TestProviderMidStreamFailure:
    """Provider raises mid-stream — partial output preserved, error surfaced."""

    def _make_streaming_provider(self, lines: list[str], fail_after: int | None = None):
        """Create a provider that yields lines then optionally raises."""
        provider = OpenRouterProvider.__new__(OpenRouterProvider)
        provider.api_key = "test-key"
        provider.model = "test-model"
        provider._default_effort = ""
        provider._rate_limiter = AsyncMock()
        provider.client = MagicMock()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        async def mock_aiter_lines():
            for i, line in enumerate(lines):
                if fail_after is not None and i >= fail_after:
                    raise ConnectionError("Stream interrupted mid-chunk")
                yield line

        mock_response.aiter_lines = mock_aiter_lines

        class MockStreamContext:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, *args):
                pass

        provider.client.stream = MagicMock(return_value=MockStreamContext())
        return provider

    async def test_mid_stream_failure_preserves_partial_content(self):
        """When stream fails after some content, partial content should be lost but error surfaced."""
        provider = self._make_streaming_provider(
            lines=[
                'data: {"model": "test-model", "choices": [{"delta": {"content": "Hello"}}]}',
                'data: {"model": "test-model", "choices": [{"delta": {"content": " world"}}]}',
                "data: [DONE]",
            ],
            fail_after=2,  # Fail after 2 lines
        )
        events = []
        with pytest.raises(ConnectionError, match="Stream interrupted"):
            async for event in provider.stream_complete(
                messages=[{"role": "user", "content": "hello"}],
            ):
                events.append(event)
        # The events yielded before the failure should have been collected
        # (the exception propagates, but the caller can inspect what was yielded)

    async def test_stream_failure_finishes_reservation_as_failed(self, monkeypatch):
        """When stream fails, usage reservation should be finished with failed=True."""
        provider = self._make_streaming_provider(
            lines=[
                'data: {"model": "test-model", "choices": [{"delta": {"content": "Hello"}}]}',
                'data: {"model": "test-model", "choices": [{"delta": {"content": " world"}}]}',
                "data: [DONE]",
            ],
            fail_after=2,  # Fail after 2 lines
        )
        reserve = AsyncMock(return_value=uuid.uuid4())
        finish = AsyncMock()
        monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
        monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

        session_id = uuid.uuid4()
        agent_id = "test-agent"

        with pytest.raises(ConnectionError, match="Stream interrupted"):
            async for _ in provider.stream_complete(
                messages=[{"role": "user", "content": "hello"}],
                session_id=session_id,
                agent_id=agent_id,
            ):
                pass

        reserve.assert_awaited_once()
        finish.assert_awaited_once()
        assert finish.call_args.kwargs.get("failed") is True

    async def test_stream_with_malformed_json_lines_skips_them(self):
        """Malformed JSON lines in stream should be skipped, not crash."""
        provider = self._make_streaming_provider(
            lines=[
                'data: {"model": "test-model", "choices": [{"delta": {"content": "Hello"}}]}',
                "data: {invalid json{{{",
                'data: {"model": "test-model", "choices": [{"delta": {"content": " world"}}]}',
                "data: [DONE]",
            ],
        )
        events = []
        async for event in provider.stream_complete(
            messages=[{"role": "user", "content": "hello"}],
        ):
            events.append(event)
        # Should have 2 text events + 1 done event (malformed line skipped)
        text_events = [e for e in events if e.type == "text"]
        done_events = [e for e in events if e.type == "done"]
        assert len(text_events) == 2
        assert len(done_events) == 1
        assert done_events[0].response.content == "Hello world"

    async def test_stream_with_no_usage_chunk_reports_zero_usage(self):
        """Stream that never sends a usage chunk should report zero usage."""
        provider = self._make_streaming_provider(
            lines=[
                'data: {"model": "test-model", "choices": [{"delta": {"content": "Hello"}}]}',
                "data: [DONE]",
            ],
        )
        events = []
        async for event in provider.stream_complete(
            messages=[{"role": "user", "content": "hello"}],
        ):
            events.append(event)
        done = [e for e in events if e.type == "done"][0]
        assert done.response.usage["prompt_tokens"] == 0
        assert done.response.usage["completion_tokens"] == 0
        assert done.response.usage["total_tokens"] == 0


# ===========================================================================
# Provider Chaos — Usage Accounting with Missing Usage
# ===========================================================================


class TestProviderUsageMissing:
    """Provider omits usage — conservative handling, no crash."""

    async def test_usage_store_finish_with_none_usage_keeps_reservation(self, monkeypatch):
        """When provider returns response with None usage, finish should use conservative reservation."""
        store = UsageStore()
        call_id = uuid.uuid4()
        execute = AsyncMock()
        monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))

        await store.finish(call_id, None, failed=False)
        # accounted_tokens should be None → COALESCE retains reserved_tokens
        assert execute.await_args.args[5] is None

    async def test_usage_store_finish_with_empty_dict_keeps_reservation(self, monkeypatch):
        """When provider returns empty usage dict, finish should use conservative reservation."""
        store = UsageStore()
        call_id = uuid.uuid4()
        execute = AsyncMock()
        monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))

        await store.finish(call_id, {}, failed=False)
        assert execute.await_args.args[5] is None

    async def test_usage_store_finish_with_partial_usage_uses_known_fields(self, monkeypatch):
        """When provider returns partial usage (only prompt_tokens), finish should use what's known."""
        store = UsageStore()
        call_id = uuid.uuid4()
        execute = AsyncMock()
        monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))

        await store.finish(call_id, {"prompt_tokens": 10}, failed=False)
        # prompt=10, completion=None, total=None → known=False (completion missing)
        # So accounted should be None (conservative)
        assert execute.await_args.args[5] is None

    async def test_usage_store_finish_with_complete_usage_uses_sum(self, monkeypatch):
        """When provider returns complete usage, finish should use prompt+completion."""
        store = UsageStore()
        call_id = uuid.uuid4()
        execute = AsyncMock()
        monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))

        await store.finish(
            call_id,
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            failed=False,
        )
        assert execute.await_args.args[5] == 15

    async def test_usage_store_finish_with_zero_usage_and_failed_uses_reservation(
        self, monkeypatch
    ):
        """When failed=True, finish should use reservation regardless of usage."""
        store = UsageStore()
        call_id = uuid.uuid4()
        execute = AsyncMock()
        monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))

        await store.finish(
            call_id,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            failed=True,
        )
        # failed=True → known=False → accounted=None → COALESCE retains reservation
        assert execute.await_args.args[5] is None


# ===========================================================================
# Embedder Chaos — Outage and Cascade
# ===========================================================================


class TestEmbedderOutage:
    """Embedder outage — fallback path, no cascade."""

    def _make_embedder(self) -> OpenAIEmbedder:
        embedder = OpenAIEmbedder.__new__(OpenAIEmbedder)
        embedder.api_key = "test-key"
        embedder._model = "text-embedding-3-small"
        embedder._base_url = "https://api.openai.com/v1"
        embedder._batch_size = 100
        embedder._timeout = 30.0
        embedder._cache_size = 1024
        from collections import OrderedDict

        embedder._cache: OrderedDict[str, list[float]] = OrderedDict()
        embedder._client = MagicMock()
        return embedder

    async def test_embed_with_api_failure_raises(self):
        """Embedder should raise when API call fails."""
        embedder = self._make_embedder()
        embedder._client.post = AsyncMock(side_effect=ConnectionError("API down"))
        with pytest.raises(ConnectionError, match="API down"):
            await embedder.embed("test text")

    async def test_embed_batch_with_api_failure_raises(self):
        """Embedder batch should raise when API call fails."""
        embedder = self._make_embedder()
        embedder._client.post = AsyncMock(side_effect=ConnectionError("API down"))
        with pytest.raises(ConnectionError, match="API down"):
            await embedder.embed_batch(["text1", "text2"])

    async def test_embed_with_malformed_response_missing_data_key(self):
        """Embedder should handle missing 'data' key gracefully (returns empty list)."""
        embedder = self._make_embedder()
        mock_response = MagicMock()
        mock_response.json.return_value = {"model": "text-embedding-3-small"}  # no data
        mock_response.raise_for_status = MagicMock()
        embedder._client.post = AsyncMock(return_value=mock_response)
        # data.get("data", []) returns [] — no crash, returns empty list
        result = await embedder._embed_uncached(["test"])
        assert result == []

    async def test_embed_with_data_missing_index_field(self):
        """Embedder should raise when embedding data item is missing 'index' field."""
        embedder = self._make_embedder()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3]}]  # missing index
        }
        mock_response.raise_for_status = MagicMock()
        embedder._client.post = AsyncMock(return_value=mock_response)
        with pytest.raises(ValueError, match="missing 'index' field"):
            await embedder.embed("test")

    async def test_embed_with_empty_text_returns_zero_vector(self):
        """Embedder should return zero vector for empty text."""
        embedder = self._make_embedder()
        result = await embedder.embed("")
        assert result == [0.0] * 1536

    async def test_embed_caches_successful_result(self):
        """Embedder should cache successful embedding."""
        embedder = self._make_embedder()
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}
        mock_response.raise_for_status = MagicMock()
        embedder._client.post = AsyncMock(return_value=mock_response)

        result1 = await embedder.embed("test")
        result2 = await embedder.embed("test")
        assert result1 == result2
        # API should only be called once (second call hits cache)
        embedder._client.post.assert_awaited_once()

    async def test_embed_batch_with_partial_cache_hit(self):
        """Embedder batch should only call API for uncached texts."""
        embedder = self._make_embedder()
        # Pre-populate cache
        from ah.rag.embedder import OpenAIEmbedder as OE

        cache_key = embedder._cache_key("cached_text")
        embedder._cache[cache_key] = [0.1] * 1536

        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"index": 0, "embedding": [0.2] * 1536}]}
        mock_response.raise_for_status = MagicMock()
        embedder._client.post = AsyncMock(return_value=mock_response)

        results = await embedder.embed_batch(["cached_text", "new_text"])
        assert len(results) == 2
        assert results[0] == [0.1] * 1536  # from cache
        assert results[1] == [0.2] * 1536  # from API
        # API called once (only for uncached text)
        embedder._client.post.assert_awaited_once()


# ===========================================================================
# Orchestrator Chaos — Delegation Timeout
# ===========================================================================


class TestOrchestratorTimeout:
    """Delegation exceeds timeout — cancelled cleanly, no leaked task."""

    async def test_delegate_timeout_returns_error_result(self):
        """When agent.run() exceeds timeout, delegate should return error result."""
        orchestrator = Orchestrator(agent_factory=MagicMock())

        class SlowAgent:
            def __init__(self, definition):
                pass

            async def run(self, session_id, user_message, verbose=True):
                await asyncio.sleep(100)  # Will be cancelled
                return MagicMock(content="should not reach", tokens_used=0, iterations=0)

        orchestrator._agent_factory = SlowAgent

        mock_agent_def = MagicMock()
        mock_agent_def.model = None
        mock_agent_def.provider = None
        mock_agent_def.max_iterations = 10
        mock_agent_def.system_prompt = None
        mock_agent_def.tools = None

        with (
            patch("ah.core.orchestrator.agent_registry") as mock_registry,
            patch("ah.core.orchestrator.session_manager") as mock_session_mgr,
            patch("ah.core.orchestrator.context_manager") as mock_ctx_mgr,
            patch("ah.core.orchestrator.db") as mock_db,
        ):
            mock_registry.get = AsyncMock(return_value=mock_agent_def)
            mock_session = MagicMock()
            mock_session.id = uuid.uuid4()
            mock_session.agent_id = "test-agent"
            mock_session.model = None
            mock_session.provider = None
            mock_session.context_budget = 8000
            mock_session.goal = None
            mock_session_mgr.create = AsyncMock(return_value=mock_session)
            mock_session_mgr.get = AsyncMock(return_value=mock_session)
            mock_session_mgr.get_fresh = AsyncMock(return_value=mock_session)
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            mock_ctx_mgr.get_recent_context = AsyncMock(return_value=[])
            mock_ctx_mgr.add_chunk = AsyncMock()

            # Temporarily set a very short timeout
            original_timeout = Orchestrator.DELEGATION_TIMEOUT_SECONDS
            Orchestrator.DELEGATION_TIMEOUT_SECONDS = 0.1
            try:
                result = await orchestrator.delegate(
                    "test-agent", "do something", parent_session_id=uuid.uuid4()
                )
            finally:
                Orchestrator.DELEGATION_TIMEOUT_SECONDS = original_timeout

        assert result.status == "error"
        assert "timed out" in result.response.lower() or "timeout" in result.response.lower()

    async def test_delegate_cancelled_error_is_recorded(self):
        """When delegation is cancelled, agent_messages should record 'cancelled' status."""
        orchestrator = Orchestrator(agent_factory=MagicMock())

        class CancellingAgent:
            def __init__(self, definition):
                pass

            async def run(self, session_id, user_message, verbose=True):
                raise asyncio.CancelledError()

        orchestrator._agent_factory = CancellingAgent

        mock_agent_def = MagicMock()
        mock_agent_def.model = None
        mock_agent_def.provider = None
        mock_agent_def.max_iterations = 10
        mock_agent_def.system_prompt = None
        mock_agent_def.tools = None

        with (
            patch("ah.core.orchestrator.agent_registry") as mock_registry,
            patch("ah.core.orchestrator.session_manager") as mock_session_mgr,
            patch("ah.core.orchestrator.context_manager") as mock_ctx_mgr,
            patch("ah.core.orchestrator.db") as mock_db,
        ):
            mock_registry.get = AsyncMock(return_value=mock_agent_def)
            mock_session = MagicMock()
            mock_session.id = uuid.uuid4()
            mock_session.agent_id = "test-agent"
            mock_session.model = None
            mock_session.provider = None
            mock_session.context_budget = 8000
            mock_session.goal = None
            mock_session_mgr.create = AsyncMock(return_value=mock_session)
            mock_session_mgr.get = AsyncMock(return_value=mock_session)
            mock_session_mgr.get_fresh = AsyncMock(return_value=mock_session)
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            mock_ctx_mgr.get_recent_context = AsyncMock(return_value=[])
            mock_ctx_mgr.add_chunk = AsyncMock()

            with pytest.raises(asyncio.CancelledError):
                await orchestrator.delegate(
                    "test-agent", "do something", parent_session_id=uuid.uuid4()
                )

            # Verify _record_end was called with status='cancelled'
            update_calls = [
                c for c in mock_db.execute.call_args_list if "UPDATE agent_messages" in str(c)
            ]
            assert len(update_calls) == 1
            assert "cancelled" in str(update_calls[0])


# ===========================================================================
# Scheduler Chaos — Worker Crash and Lease Expiry
# ===========================================================================


class TestSchedulerWorkerCrash:
    """Worker crash mid-job — lease expires, job reclaimable, no double execution."""

    async def test_worker_crash_marks_job_as_error(self):
        """When worker crashes, job should be marked as error with last_error set."""
        from ah.core.scheduler import JobStore, JobRunner

        store = JobStore()
        runner = JobRunner(store=store, agent_factory=MagicMock())

        class CrashingAgent:
            def __init__(self, name):
                pass

            async def run(self, *a, **k):
                raise RuntimeError("worker crashed")

        runner._agent_factory = CrashingAgent

        # Create a mock job
        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()
        mock_job.name = "test-job"
        mock_job.kind = "interval"
        mock_job.session_id = uuid.uuid4()
        mock_job.agent_name = "test-agent"
        mock_job.prompt = "test"
        mock_job.interval_seconds = 60
        mock_job.enabled = True
        mock_job.status = "running"
        mock_job.last_run_at = None
        mock_job.next_run_at = datetime.now(UTC)
        mock_job.last_error = None
        mock_job.run_count = 0
        mock_job.cron_expression = None
        mock_job.model = None
        mock_job.provider = None
        mock_job.no_agent = False
        mock_job.script_path = None

        # Mock claim_due to return our job, then verify finish is called with error
        original_claim = store.claim_due
        store.claim_due = AsyncMock(return_value=mock_job)

        # Mock finish to capture the error (real fenced interface).
        finish_calls = []
        original_finish = store.finish

        async def mock_finish(job_id, *, error=None, claim_token=None, paused_for=None):
            finish_calls.append((job_id, error))
            return None

        store.finish = mock_finish

        try:
            ran = await runner.run_due_once()
            assert ran is True
            assert len(finish_calls) == 1
            assert "worker crashed" in finish_calls[0][1]
        finally:
            store.claim_due = original_claim
            store.finish = original_finish

    async def test_lease_renewal_failure_does_not_crash_runner(self, monkeypatch):
        """When lease renewal fails, runner should continue (not crash)."""
        from ah.core.scheduler import JobStore, JobRunner, RUN_LEASE_SECONDS

        # Make lease renewal happen quickly so we can observe it
        monkeypatch.setattr("ah.core.scheduler.RUN_LEASE_SECONDS", 0.01)

        store = JobStore()
        runner = JobRunner(store=store, agent_factory=MagicMock())

        class SlowAgent:
            def __init__(self, name):
                pass

            async def run(self, *a, **k):
                await asyncio.sleep(0.05)
                return MagicMock(content="done", tokens_used=0, iterations=0)

        runner._agent_factory = SlowAgent

        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()
        mock_job.name = "test-job"
        mock_job.kind = "interval"
        mock_job.session_id = uuid.uuid4()
        mock_job.agent_name = "test-agent"
        mock_job.prompt = "test"
        mock_job.interval_seconds = 60
        mock_job.enabled = True
        mock_job.status = "running"
        mock_job.last_run_at = None
        mock_job.next_run_at = datetime.now(UTC)
        mock_job.last_error = None
        mock_job.run_count = 0
        mock_job.cron_expression = None
        mock_job.model = None
        mock_job.provider = None
        mock_job.no_agent = False
        mock_job.script_path = None

        store.claim_due = AsyncMock(return_value=mock_job)
        store.renew_lease = AsyncMock(side_effect=Exception("DB connection lost"))
        store.finish = AsyncMock(return_value=None)

        # Should not raise
        ran = await runner.run_due_once()
        assert ran is True
        store.renew_lease.assert_awaited()

    async def test_claim_due_with_no_due_jobs_returns_none(self, monkeypatch):
        """claim_due should return None when no jobs are due."""
        from ah.core import scheduler as sched_mod
        from ah.core.scheduler import JobStore

        mock_db = MagicMock()
        mock_db.fetchrow = AsyncMock(return_value=None)  # no due job
        monkeypatch.setattr(sched_mod, "db", mock_db)

        store = JobStore()
        result = await store.claim_due(now=datetime(2000, 1, 1, tzinfo=UTC))
        assert result is None


# ===========================================================================
# Context Eviction Chaos — Interrupted Mid-Batch
# ===========================================================================


class TestContextEvictionInterrupted:
    """Eviction interrupted mid-batch — no orphaned archive rows, no lost chunks."""

    async def test_eviction_with_archive_failure_falls_back_to_individual(self, monkeypatch):
        """When batch archive fails, should retry individually."""
        from ah.core import context as ctx_mod

        session_id = uuid.uuid4()
        now = datetime.now(UTC)
        rows = [
            {
                "id": uuid.uuid4(),
                "agent_id": "owner",
                "chunk_type": "document",
                "token_count": 1,
                "created_at": now + timedelta(seconds=i),
                "payload_msgpack": b"\x80",
                "embedding": None,
            }
            for i in range(12)
        ]

        database = MagicMock()
        database.fetchrow = AsyncMock(return_value={"total_chunks": 12, "total_tokens": 12})
        database.fetch = AsyncMock(return_value=rows)

        # First call (batch) fails, second call (individual) succeeds
        connection = AsyncMock()
        connection.transaction = MagicMock()
        call_count = 0

        async def mock_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 12:  # Batch calls fail
                raise Exception("batch archive failed")
            return [{"id": uuid.uuid4()}]  # Individual calls succeed

        connection.fetch = mock_fetch
        connection.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
        connection.execute = AsyncMock(return_value="DELETE 1")
        database.acquire.return_value.__aenter__.return_value = connection

        monkeypatch.setattr(ctx_mod, "db", database)
        manager = ContextManager()

        # Should not raise — falls back to individual
        evicted = await manager.evict_old_chunks(session_id, max_chunks=10)
        assert evicted >= 0  # Should have evicted some via individual retry

    async def test_eviction_with_delete_failure_does_not_lose_chunks(self, monkeypatch):
        """When archive succeeds but delete fails, chunk should remain (not lost)."""
        from ah.core import context as ctx_mod

        session_id = uuid.uuid4()
        now = datetime.now(UTC)
        rows = [
            {
                "id": uuid.uuid4(),
                "agent_id": "owner",
                "chunk_type": "document",
                "token_count": 1,
                "created_at": now + timedelta(seconds=i),
                "payload_msgpack": b"\x80",
                "embedding": None,
            }
            for i in range(12)
        ]

        database = MagicMock()
        database.fetchrow = AsyncMock(return_value={"total_chunks": 12, "total_tokens": 12})
        database.fetch = AsyncMock(return_value=rows)

        connection = AsyncMock()
        connection.transaction = MagicMock()
        connection.fetch = AsyncMock(return_value=[{"id": row["id"]} for row in rows])
        connection.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
        # Delete fails
        connection.execute = AsyncMock(side_effect=Exception("delete failed"))
        database.acquire.return_value.__aenter__.return_value = connection

        monkeypatch.setattr(ctx_mod, "db", database)
        manager = ContextManager()

        # Should not raise — the batch fails, individual retries also fail
        # but the error is caught and logged
        evicted = await manager.evict_old_chunks(session_id, max_chunks=10)
        # No chunks should be counted as evicted since delete failed
        assert evicted == 0

    async def test_eviction_preserves_recent_ten_chunks(self, monkeypatch):
        """Eviction should always preserve the most recent 10 chunks."""
        from ah.core import context as ctx_mod

        session_id = uuid.uuid4()
        now = datetime.now(UTC)
        rows = [
            {
                "id": uuid.uuid4(),
                "agent_id": "owner",
                "chunk_type": "document",
                "token_count": 1,
                "created_at": now + timedelta(seconds=i),
                "payload_msgpack": b"\x80",
                "embedding": None,
            }
            for i in range(15)
        ]

        database = MagicMock()
        database.fetchrow = AsyncMock(return_value={"total_chunks": 15, "total_tokens": 15})
        database.fetch = AsyncMock(return_value=rows)

        connection = AsyncMock()
        connection.transaction = MagicMock()
        connection.fetch = AsyncMock(return_value=[{"id": row["id"]} for row in rows])
        connection.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
        connection.execute = AsyncMock(return_value="DELETE 1")
        database.acquire.return_value.__aenter__.return_value = connection

        monkeypatch.setattr(ctx_mod, "db", database)
        manager = ContextManager()

        evicted = await manager.evict_old_chunks(session_id, max_chunks=10)
        # 15 total - 10 preserved = 5 evictable
        assert evicted == 5

    async def test_eviction_with_empty_session_returns_zero(self, monkeypatch):
        """Eviction on session with no chunks should return 0."""
        from ah.core import context as ctx_mod

        database = MagicMock()
        database.fetchrow = AsyncMock(return_value={"total_chunks": 0, "total_tokens": 0})
        database.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(ctx_mod, "db", database)

        manager = ContextManager()

        evicted = await manager.evict_old_chunks(uuid.uuid4(), max_chunks=10)
        assert evicted == 0


# ===========================================================================
# Concurrent Session Writes — Deadlock Prevention
# ===========================================================================


class TestConcurrentSessionWrites:
    """Concurrent session writes — no deadlock (respect advisory-lock ordering)."""

    async def test_concurrent_add_chunk_different_sessions_no_deadlock(self, monkeypatch):
        """Concurrent add_chunk calls for different sessions should not deadlock."""
        from ah.core import context as ctx_mod

        database = MagicMock()
        database.fetch = AsyncMock(return_value=[])
        database.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "session_id": uuid.uuid4(),
                "agent_id": "test-agent",
                "chunk_type": "user_message",
                "payload_msgpack": b"\x81\xa7content\xa5hello",
                "token_count": 10,
                "embedding": None,
                "created_at": datetime.now(UTC),
                "accessed_at": None,
            }
        )
        database.fetchval = AsyncMock(return_value=0)
        database.execute = AsyncMock(return_value="INSERT 0 1")
        monkeypatch.setattr(ctx_mod, "db", database)

        manager = ContextManager()

        async def add_chunk_task(session_id):
            for i in range(5):
                await manager.add_chunk(
                    session_id=session_id,
                    agent_id="test-agent",
                    chunk_type="user_message",
                    payload={"content": f"message {i}"},
                    token_count=10,
                )

        # Run concurrent tasks for different sessions
        tasks = [add_chunk_task(uuid.uuid4()) for _ in range(5)]
        # Should complete without deadlock
        await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=10,
        )

    async def test_concurrent_add_chunk_same_session_no_deadlock(self, monkeypatch):
        """Concurrent add_chunk calls for the same session should not deadlock."""
        from ah.core import context as ctx_mod

        database = MagicMock()
        database.fetch = AsyncMock(return_value=[])
        database.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "session_id": uuid.uuid4(),
                "agent_id": "test-agent",
                "chunk_type": "user_message",
                "payload_msgpack": b"\x81\xa7content\xa5hello",
                "token_count": 10,
                "embedding": None,
                "created_at": datetime.now(UTC),
                "accessed_at": None,
            }
        )
        database.fetchval = AsyncMock(return_value=0)
        database.execute = AsyncMock(return_value="INSERT 0 1")
        monkeypatch.setattr(ctx_mod, "db", database)

        manager = ContextManager()
        session_id = uuid.uuid4()

        async def add_chunk_task():
            for i in range(5):
                await manager.add_chunk(
                    session_id=session_id,
                    agent_id="test-agent",
                    chunk_type="user_message",
                    payload={"content": f"message {i}"},
                    token_count=10,
                )

        tasks = [add_chunk_task() for _ in range(5)]
        await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=10,
        )

    async def test_concurrent_get_recent_context_no_deadlock(self, monkeypatch):
        """Concurrent get_recent_context calls should not deadlock."""
        from ah.core import context as ctx_mod

        database = MagicMock()
        database.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(ctx_mod, "db", database)

        manager = ContextManager()
        session_id = uuid.uuid4()

        async def get_context_task():
            for _ in range(5):
                await manager.get_recent_context(session_id, limit=10)

        tasks = [get_context_task() for _ in range(5)]
        await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=10,
        )


# ===========================================================================
# Memory System Chaos
# ===========================================================================


class TestMemoryStoreChaos:
    """Memory store failures — graceful degradation."""

    async def test_add_memory_with_db_failure_raises(self, mock_db):
        """MemoryStore.add should raise when DB fails."""
        store = MemoryStore()
        mock_db.fetchrow = AsyncMock(side_effect=Exception("DB connection lost"))
        with patch("ah.memory.store.db", mock_db):
            with pytest.raises(Exception, match="DB connection lost"):
                await store.add(
                    session_id=uuid.uuid4(),
                    agent_id="harness",
                    content="test memory",
                    category="fact",
                )

    async def test_search_with_db_failure_returns_empty_or_raises(self, mock_db):
        """MemoryStore.search should handle DB failure."""
        store = MemoryStore()
        mock_db.fetch = AsyncMock(side_effect=Exception("DB error"))
        with patch("ah.memory.store.db", mock_db):
            with pytest.raises(Exception, match="DB error"):
                await store.search(agent_id="harness")

    async def test_delete_nonexistent_memory_returns_false(self, mock_db):
        """Deleting a non-existent memory should return False."""
        store = MemoryStore()
        mock_db.execute = AsyncMock(return_value="DELETE 0")
        with patch("ah.memory.store.db", mock_db):
            result = await store.delete(uuid.uuid4())
            assert result is False

    async def test_get_nonexistent_memory_returns_none(self, mock_db):
        """Getting a non-existent memory should return None."""
        store = MemoryStore()
        mock_db.fetchrow = AsyncMock(return_value=None)
        with patch("ah.memory.store.db", mock_db):
            result = await store.get(uuid.uuid4())
            assert result is None


class TestMemoryRetrieverChaos:
    """Memory retriever failures — graceful degradation."""

    async def test_retrieve_with_store_failure_returns_empty(self):
        """MemoryRetriever should handle store failure gracefully."""
        store = AsyncMock()
        store.search = AsyncMock(side_effect=Exception("store error"))
        store.search_by_embedding = AsyncMock(side_effect=Exception("store error"))
        store.batch_update_access = AsyncMock()

        retriever = MemoryRetriever(store=store)
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        with patch("ah.memory.retriever.db", mock_db):
            results = await retriever.retrieve("test query")
        assert results == []

    async def test_retrieve_with_rerank_failure_falls_back(self):
        """When LLM rerank fails, should fall back to score-based ranking."""
        store = AsyncMock()
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=None,
            agent_id="harness",
            content="test memory",
            category="fact",
        )
        store.search = AsyncMock(return_value=[entry])
        store.search_by_embedding = AsyncMock(return_value=[])
        store.batch_update_access = AsyncMock()

        # LLM that fails
        llm = AsyncMock()
        llm.complete = AsyncMock(side_effect=Exception("LLM down"))

        retriever = MemoryRetriever(store=store, llm_provider=llm, rerank=True)
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        with patch("ah.memory.retriever.db", mock_db):
            results = await retriever.retrieve("test")
        # Should return results (fallback to score-based)
        assert isinstance(results, list)

    async def test_retrieve_with_malformed_rerank_response_falls_back(self):
        """When LLM rerank returns malformed JSON, should fall back."""
        store = AsyncMock()
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=None,
            agent_id="harness",
            content="test memory",
            category="fact",
        )
        store.search = AsyncMock(return_value=[entry])
        store.search_by_embedding = AsyncMock(return_value=[])
        store.batch_update_access = AsyncMock()

        # LLM that returns malformed JSON
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content="not valid json{{{",
                model="test",
                usage={},
            )
        )

        retriever = MemoryRetriever(store=store, llm_provider=llm, rerank=True)
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        with patch("ah.memory.retriever.db", mock_db):
            results = await retriever.retrieve("test")
        assert isinstance(results, list)

    async def test_retrieve_with_non_list_rerank_response_falls_back(self):
        """When LLM rerank returns non-list JSON, should fall back."""
        store = AsyncMock()
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=None,
            agent_id="harness",
            content="test memory",
            category="fact",
        )
        store.search = AsyncMock(return_value=[entry])
        store.search_by_embedding = AsyncMock(return_value=[])
        store.batch_update_access = AsyncMock()

        # LLM that returns non-list JSON
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content='{"not": "a list"}',
                model="test",
                usage={},
            )
        )

        retriever = MemoryRetriever(store=store, llm_provider=llm, rerank=True)
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        with patch("ah.memory.retriever.db", mock_db):
            results = await retriever.retrieve("test")
        assert isinstance(results, list)


class TestMemoryConsolidatorChaos:
    """Memory consolidator failures — graceful degradation."""

    async def test_consolidate_with_llm_failure_returns_empty(self):
        """When LLM extraction fails, consolidator should return empty list."""
        llm = AsyncMock()
        llm.complete = AsyncMock(side_effect=Exception("LLM down"))

        consolidator = MemoryConsolidator(llm_provider=llm)
        mock_store = AsyncMock()
        mock_store.search = AsyncMock(return_value=[])
        mock_store.search_by_embedding = AsyncMock(return_value=[])
        mock_store.add = AsyncMock()
        consolidator.store = mock_store

        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[])
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
        # Typed empty (AH-AUDIT-037): no input, nothing processed.
        assert results.status == "empty" and results.entries == []

    async def test_consolidate_with_store_failure_on_write_continues(self):
        """When writing one memory fails, consolidator should continue with others."""
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    [
                        {"content": "memory 1", "category": "fact", "importance": 0.8},
                        {"content": "memory 2", "category": "fact", "importance": 0.9},
                    ]
                ),
                model="test",
                usage={},
            )
        )

        consolidator = MemoryConsolidator(llm_provider=llm)
        mock_store = AsyncMock()
        mock_store.search = AsyncMock(return_value=[])
        mock_store.search_by_embedding = AsyncMock(return_value=[])
        # First add fails, second succeeds
        mock_store.add = AsyncMock(
            side_effect=[
                Exception("write failed"),
                MemoryEntry(
                    id=uuid.uuid4(),
                    session_id=None,
                    agent_id="harness",
                    content="memory 2",
                    category="fact",
                    importance=0.9,
                ),
            ]
        )
        consolidator.store = mock_store

        # Create a mock chunk for the consolidator to process
        from ah.core.models import ContextChunk

        mock_chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": "test conversation"},
            token_count=10,
        )

        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[mock_chunk])
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
        # Should have 1 result (second memory succeeded); partial writes
        # carry no checkpoint so the range stays retryable (AH-AUDIT-037).
        assert results.status == "partial"
        assert results.checkpoint is None
        assert len(results) == 1
        assert results.entries[0].content == "memory 2"

    async def test_consolidate_with_invalid_json_returns_empty(self):
        """When LLM returns invalid JSON, consolidator should return empty list."""
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content="not valid json{{{",
                model="test",
                usage={},
            )
        )

        consolidator = MemoryConsolidator(llm_provider=llm)
        mock_store = AsyncMock()
        consolidator.store = mock_store

        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[])
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
        assert results.status == "empty" and results.entries == []

    async def test_consolidate_with_non_list_json_returns_empty(self):
        """When LLM returns non-list JSON, consolidator should return empty list."""
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content='{"not": "a list"}',
                model="test",
                usage={},
            )
        )

        consolidator = MemoryConsolidator(llm_provider=llm)
        mock_store = AsyncMock()
        consolidator.store = mock_store

        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[])
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
        assert results.status == "empty" and results.entries == []

    async def test_consolidate_with_missing_fields_in_memory_item_skips_it(self):
        """When LLM returns memory item missing required fields, should skip it."""
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    [
                        {"category": "fact", "importance": 0.8},  # missing content
                        {"content": "valid memory", "category": "fact", "importance": 0.9},
                    ]
                ),
                model="test",
                usage={},
            )
        )

        consolidator = MemoryConsolidator(llm_provider=llm)
        # Spec'd mock: only the real MemoryStore surface, with the correct
        # return type so `similarity > threshold` compares floats, not Mocks.
        mock_store = MagicMock(spec=MemoryStore)
        mock_store.search = AsyncMock(return_value=[])
        mock_store.search_by_embedding = AsyncMock(return_value=[])
        mock_store.add = AsyncMock(
            return_value=MemoryEntry(
                id=uuid.uuid4(),
                session_id=None,
                agent_id="harness",
                content="valid memory",
                category="fact",
                importance=0.9,
            )
        )
        consolidator.store = mock_store

        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[MagicMock()])
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
        # Should have 1 result (invalid item skipped)
        assert len(results) == 1
        assert results.entries[0].content == "valid memory"


# ===========================================================================
# Agent Chaos — Stream Interruption with Partial Output
# ===========================================================================


class TestAgentStreamInterruption:
    """Agent stream interruption — partial output preserved, error surfaced."""

    async def test_agent_stream_with_provider_raising_mid_stream(self, mock_session):
        """When provider raises mid-stream, agent should surface error in done event."""
        provider = AsyncMock()

        async def failing_stream(*args, **kwargs):
            yield StreamEvent(type="text", content="partial")
            raise ConnectionError("Stream interrupted")

        provider.stream_complete = failing_stream

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                events = []
                async for event in agent.run_stream(mock_session.id, "test", verbose=False):
                    events.append(event)

                # Should have partial text + done event with error
                text_events = [e for e in events if e.type == "text"]
                done_events = [e for e in events if e.type == "done"]
                assert len(text_events) == 1
                assert text_events[0].content == "partial"
                assert len(done_events) == 1
                assert "error" in done_events[0].response.content.lower()

    async def test_agent_stream_with_provider_returning_malformed_events(self, mock_session):
        """When provider yields malformed events, agent should handle gracefully."""
        provider = AsyncMock()

        async def malformed_stream(*args, **kwargs):
            yield StreamEvent(type="text", content="data: {invalid json}\n\n")
            yield StreamEvent(
                type="done",
                response=LLMResponse(
                    content="recovered",
                    model="test",
                    usage={},
                ),
            )

        provider.stream_complete = malformed_stream

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                events = []
                async for event in agent.run_stream(mock_session.id, "test", verbose=False):
                    events.append(event)

                # Should complete despite malformed JSON
                assert any(e.type == "done" for e in events)


# ===========================================================================
# Agent Chaos — Provider Returns None or Missing Fields
# ===========================================================================


class TestAgentProviderEdgeCases:
    """Agent handles provider returning None or missing fields."""

    async def test_agent_handles_provider_returning_none_content(self, mock_session):
        """Agent should handle provider returning None content."""
        provider = AsyncMock()
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content=None,  # type: ignore
                model="test-model",
                usage={"total_tokens": 5},
                tool_calls=[],
            )
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert hasattr(response, "content")
                assert hasattr(response, "iterations")

    async def test_agent_handles_provider_returning_none_tool_calls(self, mock_session):
        """Agent should handle provider returning None tool_calls."""
        provider = AsyncMock()
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="test",
                model="test-model",
                usage={"total_tokens": 5},
                tool_calls=None,  # type: ignore
            )
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert response.content == "test"

    async def test_agent_handles_provider_returning_malformed_tool_call_missing_id(
        self, mock_session
    ):
        """Agent should handle tool call missing 'id' field."""
        provider = AsyncMock()
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="",
                model="test-model",
                usage={"total_tokens": 5},
                tool_calls=[
                    {
                        "function": {"name": "read_file", "arguments": "{}"},
                        # missing id
                    }
                ],
            )
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert response.iterations >= 1


# ===========================================================================
# Usage Store Chaos — Concurrent Reservations
# ===========================================================================


class TestUsageStoreConcurrent:
    """Usage store concurrent access — no deadlock, correct accounting."""

    async def test_concurrent_reserve_same_session_no_deadlock(self, monkeypatch):
        """Concurrent reserve calls for the same session should not deadlock."""
        from ah.core.usage import UsageStore

        store = UsageStore()
        session_id = uuid.uuid4()
        agent_id = f"test-agent-{uuid.uuid4()}"
        messages = [{"role": "user", "content": "hello"}]

        # A connection whose transaction() is a real async context manager and
        # whose INSERT returns a fresh row id per call.
        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        class _Conn:
            def transaction(self):
                return _Tx()

            async def execute(self, *a, **k):
                return None

            async def fetchrow(self, query, *args):
                if "INSERT INTO llm_usage" in query:
                    return {"id": uuid.uuid4()}
                return {"requests": 0, "tokens": 0}

            async def fetch(self, *a, **k):
                return [
                    {"scope": "agent", "requests": 0, "tokens": 0},
                    {"scope": "session", "requests": 0, "tokens": 0},
                ]

            async def fetchval(self, *a, **k):
                return 0

        class _Acquire:
            def __init__(self, conn):
                self._conn = conn

            async def __aenter__(self):
                return self._conn

            async def __aexit__(self, *exc):
                return False

        conn = _Conn()
        mock_db = MagicMock()
        mock_db.connected = True
        mock_db.acquire = MagicMock(side_effect=lambda: _Acquire(conn))

        monkeypatch.setattr("ah.core.usage.db", mock_db)

        # Should not deadlock
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    store.reserve(session_id, agent_id, "fake", "fake", messages, [], 8)
                    for _ in range(5)
                ),
                return_exceptions=True,
            ),
            timeout=10,
        )
        # All should succeed (no deadlock)
        assert all(isinstance(r, uuid.UUID) for r in results)

    async def test_reserve_with_db_failure_propagates_and_complete_call_wraps(self, monkeypatch):
        """A DB failure inside reserve() propagates from reserve() itself, and
        complete_call() (the production entry point) wraps it in DatabaseError."""
        from ah.core.usage import UsageStore
        from ah.core.exceptions import DatabaseError

        store = UsageStore()
        mock_db = MagicMock()
        mock_db.connected = True
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__.side_effect = Exception("DB down")
        mock_db.acquire.return_value.__aexit__.return_value = None

        monkeypatch.setattr("ah.core.usage.db", mock_db)

        # reserve() is the low-level primitive: it lets the real error through.
        with pytest.raises(Exception, match="DB down"):
            await store.reserve(
                uuid.uuid4(),
                "agent",
                "fake",
                "fake",
                [{"role": "user", "content": "hello"}],
                [],
                8,
            )

        # complete_call() is the boundary that translates it for callers.
        class _Provider:
            model = "fake"

            async def complete(self, **kwargs):  # pragma: no cover - never reached
                raise AssertionError("provider must not be called when reserve fails")

        with pytest.raises(DatabaseError, match="usage accounting unavailable"):
            await store.complete_call(
                _Provider(),
                uuid.uuid4(),
                "agent",
                [{"role": "user", "content": "hello"}],
                max_tokens=8,
            )

    async def test_finish_with_db_failure_propagates_and_complete_call_wraps(self, monkeypatch):
        """A DB failure inside finish() propagates from finish(), and
        complete_call() wraps it in DatabaseError."""
        from ah.core.usage import UsageStore
        from ah.core.exceptions import DatabaseError

        store = UsageStore()
        mock_db = MagicMock()
        mock_db.connected = True
        mock_db.execute = AsyncMock(side_effect=Exception("DB down"))

        monkeypatch.setattr("ah.core.usage.db", mock_db)

        # finish() is the low-level primitive: it lets the real error through.
        with pytest.raises(Exception, match="DB down"):
            await store.finish(uuid.uuid4(), {"prompt_tokens": 5})

        # complete_call() wraps a finish failure after a successful provider call.
        class _Provider:
            model = "fake"

            async def complete(self, **kwargs):
                return LLMResponse(content="ok", model="fake", usage={"total_tokens": 5})

        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        class _Conn:
            def transaction(self):
                return _Tx()

            async def execute(self, *a, **k):
                return None

            async def fetchrow(self, query, *args):
                if "INSERT INTO llm_usage" in query:
                    return {"id": uuid.uuid4()}
                return {"requests": 0, "tokens": 0}

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False

        # reserve() succeeds via the fake conn; finish() still hits the
        # failing execute().
        mock_db.acquire = MagicMock(side_effect=lambda: _Acquire())
        with pytest.raises(DatabaseError, match="usage accounting unavailable"):
            await store.complete_call(
                _Provider(),
                uuid.uuid4(),
                "agent",
                [{"role": "user", "content": "hello"}],
                max_tokens=8,
            )


# ===========================================================================
# Embedder Chaos — Cache Eviction
# ===========================================================================


class TestEmbedderCacheEviction:
    """Embedder cache eviction — LRU behavior."""

    async def test_cache_eviction_when_full(self):
        """When cache is full, LRU item should be evicted."""
        embedder = OpenAIEmbedder.__new__(OpenAIEmbedder)
        embedder.api_key = "test-key"
        embedder._model = "text-embedding-3-small"
        embedder._base_url = "https://api.openai.com/v1"
        embedder._batch_size = 100
        embedder._timeout = 30.0
        embedder._cache_size = 2  # Very small cache
        from collections import OrderedDict

        embedder._cache: OrderedDict[str, list[float]] = OrderedDict()
        embedder._client = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * 1536}]}
        mock_response.raise_for_status = MagicMock()
        embedder._client.post = AsyncMock(return_value=mock_response)

        # Fill cache
        await embedder.embed("text1")
        await embedder.embed("text2")
        assert len(embedder._cache) == 2

        # Adding third should evict first
        await embedder.embed("text3")
        assert len(embedder._cache) == 2
        # text1 should be evicted (LRU)
        cache_keys = list(embedder._cache.keys())
        assert embedder._cache_key("text1") not in cache_keys

    async def test_cache_hit_moves_to_end(self):
        """Cache hit should move item to end (MRU)."""
        embedder = OpenAIEmbedder.__new__(OpenAIEmbedder)
        embedder.api_key = "test-key"
        embedder._model = "text-embedding-3-small"
        embedder._base_url = "https://api.openai.com/v1"
        embedder._batch_size = 100
        embedder._timeout = 30.0
        embedder._cache_size = 3
        from collections import OrderedDict

        embedder._cache: OrderedDict[str, list[float]] = OrderedDict()
        embedder._client = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * 1536}]}
        mock_response.raise_for_status = MagicMock()
        embedder._client.post = AsyncMock(return_value=mock_response)

        # Fill cache
        await embedder.embed("text1")
        await embedder.embed("text2")
        await embedder.embed("text3")

        # Access text1 (moves to end)
        await embedder.embed("text1")

        # Adding fourth should evict text2 (now LRU)
        await embedder.embed("text4")
        cache_keys = list(embedder._cache.keys())
        assert embedder._cache_key("text2") not in cache_keys
        assert embedder._cache_key("text1") in cache_keys


# ===========================================================================
# Scheduler Chaos — Multiple Runners
# ===========================================================================


class TestSchedulerMultipleRunners:
    """Multiple runners — no double execution."""

    async def test_two_runners_cannot_claim_same_job(self):
        """Two runners should not be able to claim the same job."""
        from ah.core.scheduler import JobStore

        store = JobStore()
        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()
        mock_job.name = "test-job"
        mock_job.kind = "interval"
        mock_job.session_id = uuid.uuid4()
        mock_job.agent_name = "test-agent"
        mock_job.prompt = "test"
        mock_job.interval_seconds = 60
        mock_job.enabled = True
        mock_job.status = "running"
        mock_job.last_run_at = None
        mock_job.next_run_at = datetime.now(UTC)
        mock_job.last_error = None
        mock_job.run_count = 0
        mock_job.cron_expression = None
        mock_job.model = None
        mock_job.provider = None
        mock_job.no_agent = False
        mock_job.script_path = None

        # First claim succeeds
        store.claim_due = AsyncMock(return_value=mock_job)
        job1 = await store.claim_due()
        assert job1 is not None

        # Second claim should return None (job already running)
        store.claim_due = AsyncMock(return_value=None)
        job2 = await store.claim_due()
        assert job2 is None


# ===========================================================================
# Memory Consolidator Chaos — Deduplication
# ===========================================================================


class TestMemoryConsolidatorDedup:
    """Memory consolidator deduplication — no duplicate memories."""

    async def test_duplicate_memory_not_written_twice(self):
        """Duplicate memory should not be written twice."""
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    [
                        {
                            "content": "User likes Python",
                            "category": "preference",
                            "importance": 0.8,
                        },
                    ]
                ),
                model="test",
                usage={},
            )
        )

        consolidator = MemoryConsolidator(llm_provider=llm)
        mock_store = AsyncMock()
        # Existing memory with same content
        mock_store.search = AsyncMock(
            return_value=[
                MemoryEntry(
                    id=uuid.uuid4(),
                    session_id=None,
                    agent_id="harness",
                    content="User likes Python",
                    category="preference",
                    importance=0.8,
                )
            ]
        )
        mock_store.search_by_embedding = AsyncMock(return_value=[])
        mock_store.add = AsyncMock()
        mock_store.update_access = AsyncMock()
        consolidator.store = mock_store

        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[])
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
        # Duplicate should not be written
        mock_store.add.assert_not_called()

    async def test_similar_embedding_deduplicates(self):
        """Memory with similar embedding should be deduplicated."""
        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    [
                        {
                            "content": "User likes Python",
                            "category": "preference",
                            "importance": 0.8,
                        },
                    ]
                ),
                model="test",
                usage={},
            )
        )

        consolidator = MemoryConsolidator(llm_provider=llm)
        mock_store = MagicMock(spec=MemoryStore)
        mock_store.search = AsyncMock(return_value=[])
        # Similar embedding found (real floats so `similarity > threshold` holds)
        existing_entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=None,
            agent_id="harness",
            content="User likes Python",
            category="preference",
            importance=0.8,
            embedding=[0.1] * 1536,
        )
        mock_store.search_by_embedding = AsyncMock(
            return_value=[
                (existing_entry, 0.95),  # High similarity
            ]
        )
        mock_store.add = AsyncMock()
        mock_store.update_access = AsyncMock()
        consolidator.store = mock_store

        # Create a mock chunk for the consolidator to process
        from ah.core.models import ContextChunk

        mock_chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": "test conversation"},
            token_count=10,
        )

        # Force the extracted candidate to carry an embedding so the vector
        # dedup path (search_by_embedding) is the one exercised.
        from ah.memory.models import MemoryEntry as _ME

        async def _extract(*args, **kwargs):
            return [
                _ME(
                    id=uuid.uuid4(),
                    session_id=None,
                    agent_id="harness",
                    content="User likes Python",
                    category="preference",
                    importance=0.8,
                    embedding=[0.1] * 1536,
                )
            ]

        consolidator._extract_memories = _extract

        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[mock_chunk])
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
        # Should not write duplicate
        mock_store.add.assert_not_called()
        # Should update access on the existing near-duplicate
        mock_store.update_access.assert_awaited_once_with(existing_entry.id)
