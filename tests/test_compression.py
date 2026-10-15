"""Tests for context compression — ContextCompressor, CompressionConfig."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from machina.core.compression import (
    CompressionConfig,
    CompressionResult,
    ContextCompressor,
)
from machina.core.models import ContextChunk, LLMResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_chunks() -> list[ContextChunk]:
    """Create a list of sample context chunks (newest first)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": "hello"},
            token_count=5,
            created_at=now,
        ),
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="assistant_message",
            payload={"content": "Hi there!"},
            token_count=5,
            created_at=now - timedelta(minutes=1),
        ),
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="tool_call",
            payload={"tool": "read_file", "args": {"path": "/tmp/test"}},
            token_count=10,
            created_at=now - timedelta(minutes=2),
        ),
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="result",
            payload={"tool": "read_file", "status": "ok", "result": "file content here"},
            token_count=10,
            created_at=now - timedelta(minutes=3),
        ),
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="memory",
            payload={"content": "User prefers Python"},
            token_count=8,
            created_at=now - timedelta(minutes=4),
        ),
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": "what is the weather?"},
            token_count=6,
            created_at=now - timedelta(minutes=5),
        ),
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="assistant_message",
            payload={"content": "I don't know"},
            token_count=5,
            created_at=now - timedelta(minutes=6),
        ),
    ]


@pytest.fixture
def mock_llm_provider():
    """Create a mock LLM provider for summarization."""
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            content="Summary: User asked about weather and file contents.",
            model="test-model",
            usage={"total_tokens": 10},
        )
    )
    return provider


# ---------------------------------------------------------------------------
# CompressionConfig Tests
# ---------------------------------------------------------------------------


class TestCompressionConfig:
    """Tests for CompressionConfig dataclass."""

    def test_defaults(self):
        """Test default configuration values."""
        config = CompressionConfig()
        assert config.enabled is True
        assert config.threshold == 0.8
        assert config.target_ratio == 0.5
        assert config.preserve_recent == 3
        assert config.llm_summarize is True

    def test_custom_values(self):
        """Test custom configuration values."""
        config = CompressionConfig(
            enabled=False,
            threshold=0.9,
            target_ratio=0.3,
            preserve_recent=5,
            llm_summarize=False,
        )
        assert config.enabled is False
        assert config.threshold == 0.9
        assert config.target_ratio == 0.3
        assert config.preserve_recent == 5
        assert config.llm_summarize is False


# ---------------------------------------------------------------------------
# ContextCompressor Tests
# ---------------------------------------------------------------------------


class TestContextCompressor:
    """Tests for ContextCompressor."""

    def test_compress_empty_chunks(self):
        """Test compressing empty chunk list."""
        compressor = ContextCompressor()
        result = compressor.compress([], uuid.uuid4(), "harness")
        assert result.original_count == 0
        assert result.compressed_tokens == 0
        assert result.method == "none"

    def test_compress_few_chunks_no_compression(self, sample_chunks):
        """Test that few chunks (≤ preserve_recent) are not compressed."""
        config = CompressionConfig(preserve_recent=10)
        compressor = ContextCompressor(config=config)
        result = compressor.compress(sample_chunks[:3], uuid.uuid4(), "harness")
        assert result.original_count == 0
        assert result.compression_ratio == 1.0
        assert result.method == "none"

    def test_compress_with_truncation(self, sample_chunks):
        """Test compression using truncation (no LLM)."""
        config = CompressionConfig(llm_summarize=False, preserve_recent=2)
        compressor = ContextCompressor(config=config)
        result = compressor.compress(sample_chunks, uuid.uuid4(), "harness")
        assert result.original_count > 0
        assert result.method == "truncate"
        assert len(result.compressed_chunks) > 0

    def test_compress_with_llm(self, sample_chunks, mock_llm_provider):
        """Test compression using LLM summarization."""
        config = CompressionConfig(llm_summarize=True, preserve_recent=2)
        compressor = ContextCompressor(config=config)
        result = compressor.compress(
            sample_chunks, uuid.uuid4(), "harness", llm_provider=mock_llm_provider
        )
        assert result.original_count > 0
        assert result.method == "llm_summarize"
        mock_llm_provider.complete.assert_called_once()

    def test_compress_llm_fallback_to_truncation(self, sample_chunks):
        """Test that LLM failure falls back to truncation."""
        config = CompressionConfig(llm_summarize=True, preserve_recent=2)
        compressor = ContextCompressor(config=config)

        failing_provider = AsyncMock()
        failing_provider.complete = AsyncMock(side_effect=Exception("LLM error"))

        result = compressor.compress(
            sample_chunks, uuid.uuid4(), "harness", llm_provider=failing_provider
        )
        assert result.method == "truncate"
        assert len(result.compressed_chunks) > 0

    def test_compress_llm_fallback_records_diagnostic(self, sample_chunks):
        """W7: an LLM-summarize failure is counted, not silently downgraded."""
        from machina.core.metrics import metrics

        config = CompressionConfig(llm_summarize=True, preserve_recent=2)
        compressor = ContextCompressor(config=config)
        failing_provider = AsyncMock()
        failing_provider.complete = AsyncMock(side_effect=Exception("LLM error"))

        before = metrics.get_counter("diagnostic.failures")
        result = compressor.compress(
            sample_chunks, uuid.uuid4(), "harness", llm_provider=failing_provider
        )
        assert result.method == "truncate"
        assert metrics.get_counter("diagnostic.failures") == before + 1

    def test_compress_records_double_fallback_diagnostics(self, sample_chunks, monkeypatch):
        """W7: when LLM and truncation both fail, both boundaries are counted."""
        from machina.core.metrics import metrics

        config = CompressionConfig(llm_summarize=True, preserve_recent=2)
        compressor = ContextCompressor(config=config)
        failing_provider = AsyncMock()
        failing_provider.complete = AsyncMock(side_effect=Exception("LLM error"))

        def explode(*_args):
            raise RuntimeError("truncate error")

        monkeypatch.setattr(compressor, "_truncate_compress", explode)
        before = metrics.get_counter("diagnostic.failures")
        result = compressor.compress(
            sample_chunks, uuid.uuid4(), "harness", llm_provider=failing_provider
        )
        assert result.method == "none"
        assert {c.id for c in result.compressed_chunks} == {c.id for c in sample_chunks}
        assert metrics.get_counter("diagnostic.failures") == before + 2

    def test_compress_truncate_failure_records_diagnostic(self, sample_chunks, monkeypatch):
        """W7: a truncation-only failure (no LLM) is counted."""
        from machina.core.metrics import metrics

        config = CompressionConfig(llm_summarize=False, preserve_recent=2)
        compressor = ContextCompressor(config=config)

        def explode(*_args):
            raise RuntimeError("truncate error")

        monkeypatch.setattr(compressor, "_truncate_compress", explode)
        before = metrics.get_counter("diagnostic.failures")
        result = compressor.compress(sample_chunks, uuid.uuid4(), "harness")
        assert result.method == "none"
        assert {c.id for c in result.compressed_chunks} == {c.id for c in sample_chunks}
        assert metrics.get_counter("diagnostic.failures") == before + 1

    async def test_acompress_records_fallback_diagnostics(self, sample_chunks, monkeypatch):
        """W7: acompress counts LLM fallback and truncation failure separately."""
        from machina.core.metrics import metrics

        config = CompressionConfig(llm_summarize=True, preserve_recent=2)
        compressor = ContextCompressor(config=config)
        failing_provider = AsyncMock()
        failing_provider.complete = AsyncMock(side_effect=Exception("LLM error"))

        before = metrics.get_counter("diagnostic.failures")
        result = await compressor.acompress(
            sample_chunks, uuid.uuid4(), "harness", llm_provider=failing_provider
        )
        assert result.method == "truncate"
        assert metrics.get_counter("diagnostic.failures") == before + 1

        def explode(*_args):
            raise RuntimeError("truncate error")

        monkeypatch.setattr(compressor, "_truncate_compress", explode)
        before = metrics.get_counter("diagnostic.failures")
        result = await compressor.acompress(
            sample_chunks, uuid.uuid4(), "harness", llm_provider=failing_provider
        )
        assert result.method == "none"
        assert metrics.get_counter("diagnostic.failures") == before + 2

    def test_compress_preserves_recent_chunks(self, sample_chunks):
        """Test that recent chunks are preserved."""
        config = CompressionConfig(llm_summarize=False, preserve_recent=3)
        compressor = ContextCompressor(config=config)
        result = compressor.compress(sample_chunks, uuid.uuid4(), "harness")
        # The last 3 chunks should be preserved
        assert len(result.compressed_chunks) >= 3

    def test_identify_tool_pairs(self, sample_chunks):
        """Test tool call/result pair identification."""
        compressor = ContextCompressor()
        pairs = compressor._identify_tool_pairs(sample_chunks)
        # Should find the tool_call and result pair
        assert len(pairs) > 0

    def test_chunk_to_text_tool_call(self):
        """Test converting tool_call chunk to text."""
        compressor = ContextCompressor()
        chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="tool_call",
            payload={"tool": "read_file", "args": {"path": "/tmp/test"}},
        )
        text = compressor._chunk_to_text(chunk)
        assert "read_file" in text
        assert "/tmp/test" in text

    def test_chunk_to_text_result(self):
        """Test converting result chunk to text."""
        compressor = ContextCompressor()
        chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="result",
            payload={"status": "ok", "result": "success"},
        )
        text = compressor._chunk_to_text(chunk)
        assert "ok" in text
        assert "success" in text

    def test_chunk_to_text_user_message(self):
        """Test converting user_message chunk to text."""
        compressor = ContextCompressor()
        chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": "hello"},
        )
        text = compressor._chunk_to_text(chunk)
        assert "hello" in text

    def test_truncate_chunk(self):
        """Test truncating a chunk to fit token budget."""
        compressor = ContextCompressor()
        chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="result",
            payload={"content": "x" * 1000},
            token_count=250,
        )
        truncated = compressor._truncate_chunk(chunk, 50)
        assert truncated is not None
        assert truncated.token_count < chunk.token_count

    def test_truncate_chunk_small_chunk(self):
        """Test truncating a chunk that already fits."""
        compressor = ContextCompressor()
        chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": "hi"},
            token_count=1,
        )
        result = compressor._truncate_chunk(chunk, 100)
        assert result is not None
        assert result.token_count <= 100


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestCompressionIntegration:
    """Integration tests for compression with other components."""

    def test_compress_context_chunks_function(self, sample_chunks):
        """Test the compress_context_chunks convenience function."""
        from machina.core.assembler import compress_context_chunks

        result = compress_context_chunks(
            chunks=sample_chunks,
            session_id=uuid.uuid4(),
            agent_id="harness",
        )
        assert isinstance(result, CompressionResult)
        assert result.original_count > 0

    def test_config_from_global_config(self):
        """Test building CompressionConfig from global config."""
        from machina.core.config import config

        comp_config = CompressionConfig(
            enabled=config.get("compression_enabled"),
            threshold=config.get("compression_threshold"),
            target_ratio=config.get("compression_target_ratio"),
            preserve_recent=config.get("compression_preserve_recent"),
            llm_summarize=config.get("compression_llm_summarize"),
        )
        assert comp_config.enabled is True
        assert comp_config.threshold == 0.8
        assert comp_config.target_ratio == 0.5


# ---------------------------------------------------------------------------
# R4-011 E2E: compaction keeps delegated result text (real DB)
# ---------------------------------------------------------------------------

_CHILD_MARKER = "R4-011 delegated child output: 7af3 checks passed"


@pytest_asyncio.fixture
async def live_db():
    """Connect the shared DB to MACHINA_TEST_DATABASE_URL (skip when absent)."""
    if not os.environ.get("MACHINA_TEST_DATABASE_URL"):
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import db

    await db.connect()
    yield db
    await db.close()


class _EchoSummarizer:
    """Provider-boundary fake: echoes the summarization prompt it receives."""

    model = "echo-summarizer"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, **kwargs):
        prompt = kwargs["messages"][-1]["content"]
        self.prompts.append(prompt)
        return LLMResponse(
            content="SUMMARY: " + prompt,
            model=self.model,
            usage={"total_tokens": 1},
        )

    async def close(self) -> None:
        pass


async def test_e2e_compaction_keeps_delegated_result_text(live_db, monkeypatch):
    """Real DB + real ``services.compress_session`` path; fake only the network.

    The orchestrator stores delegated results as
    ``payload={"agent": ..., "content": ...}`` (orchestrator.py:417-423);
    the summarizer prompt must carry that content so the persisted summary
    cannot silently drop the child's output.
    """
    from machina import services as svc
    from machina.core import provider as provmod
    from machina.core.config import config
    from machina.core.context import context_manager
    from machina.core.session import session_manager

    fake = _EchoSummarizer()
    monkeypatch.setattr(provmod, "get_provider", lambda provider=None, model=None: fake)
    monkeypatch.setattr(config, "compression_llm_summarize", True)
    monkeypatch.setattr(config, "compression_preserve_recent", 1)

    session = await session_manager.create(title="r4-011-compaction", agent_id="harness")
    try:
        oldest = await context_manager.add_chunk(
            session.id, "harness", "user_message", {"content": "please delegate"}, token_count=5
        )
        delegated = await context_manager.add_chunk(
            session.id,
            "researcher",
            "result",
            {"agent": "researcher", "content": f"[researcher] {_CHILD_MARKER}"},
            token_count=12,
        )
        newest = await context_manager.add_chunk(
            session.id, "harness", "user_message", {"content": "continue"}, token_count=5
        )
        # Deterministic recency: add_chunk has no created_at argument.
        now = datetime.now(UTC).replace(tzinfo=None)
        for chunk, age in ((newest, 0), (delegated, 1), (oldest, 2)):
            await live_db.execute(
                "UPDATE context_chunks SET created_at = $2 WHERE id = $1",
                chunk.id,
                now - timedelta(seconds=age),
            )

        outcome = await svc.compress_session_with_status(session)
        assert outcome.status == "completed"
        assert outcome.result is not None
        assert outcome.result.method == "llm_summarize"

        summaries = [
            c
            for c in await context_manager.get_all_chunks(session.id)
            if c.chunk_type == "compression_summary"
        ]
        assert len(summaries) == 1
        # The prompt at the provider boundary carried the delegated output,
        # and the persisted summary (echo) kept it.
        assert any(_CHILD_MARKER in p for p in fake.prompts)
        assert _CHILD_MARKER in summaries[0].payload["content"]
    finally:
        await live_db.execute("DELETE FROM sessions WHERE id = $1", session.id)
