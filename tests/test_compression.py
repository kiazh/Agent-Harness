"""Tests for context compression — ContextCompressor, RollingCompaction, CompressionConfig."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ah.core.compression import (
    CompressionConfig,
    CompressionResult,
    ContextCompressor,
    RollingCompaction,
)
from ah.core.models import ContextChunk, LLMResponse

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
# RollingCompaction Tests
# ---------------------------------------------------------------------------


class TestRollingCompaction:
    """Tests for RollingCompaction."""

    def test_should_compress_disabled(self):
        """Test that compression is not triggered when disabled."""
        config = CompressionConfig(enabled=False)
        compaction = RollingCompaction(config=config)
        assert compaction.should_compress(10000, 8000) is False

    def test_should_compress_over_threshold(self):
        """Test compression triggers when over threshold."""
        config = CompressionConfig(enabled=True, threshold=0.8)
        compaction = RollingCompaction(config=config)
        # 8000 * 0.8 = 6400, so 7000 should trigger
        assert compaction.should_compress(7000, 8000) is True

    def test_should_not_compress_under_threshold(self):
        """Test compression doesn't trigger when under threshold."""
        config = CompressionConfig(enabled=True, threshold=0.8)
        compaction = RollingCompaction(config=config)
        # 8000 * 0.8 = 6400, so 5000 should not trigger
        assert compaction.should_compress(5000, 8000) is False

    def test_should_not_compress_repeatedly(self):
        """Test compression doesn't trigger repeatedly without growth."""
        config = CompressionConfig(enabled=True, threshold=0.8)
        compaction = RollingCompaction(config=config)
        # First call should trigger
        assert compaction.should_compress(7000, 8000) is True
        # Record compression
        compaction.record_compression(4000)
        # Small growth (less than 20% of threshold) should not trigger
        assert compaction.should_compress(4500, 8000) is False

    def test_should_compress_after_growth(self):
        """Test compression triggers again after sufficient growth."""
        config = CompressionConfig(enabled=True, threshold=0.8)
        compaction = RollingCompaction(config=config)
        # First compression
        assert compaction.should_compress(7000, 8000) is True
        compaction.record_compression(4000)
        # After significant growth, should trigger again
        assert compaction.should_compress(7000, 8000) is True

    def test_compression_count(self):
        """Test compression count tracking."""
        config = CompressionConfig(enabled=True, threshold=0.5)
        compaction = RollingCompaction(config=config)
        assert compaction.compression_count == 0
        compaction.record_compression(1000)
        assert compaction.compression_count == 1
        compaction.record_compression(800)
        assert compaction.compression_count == 2

    def test_reset(self):
        """Test resetting compaction state."""
        config = CompressionConfig(enabled=True, threshold=0.5)
        compaction = RollingCompaction(config=config)
        compaction.record_compression(1000)
        assert compaction.compression_count == 1
        compaction.reset()
        assert compaction.compression_count == 0


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestCompressionIntegration:
    """Integration tests for compression with other components."""

    def test_compress_context_chunks_function(self, sample_chunks):
        """Test the compress_context_chunks convenience function."""
        from ah.core.assembler import compress_context_chunks

        result = compress_context_chunks(
            chunks=sample_chunks,
            session_id=uuid.uuid4(),
            agent_id="harness",
        )
        assert isinstance(result, CompressionResult)
        assert result.original_count > 0

    def test_config_from_global_config(self):
        """Test building CompressionConfig from global config."""
        from ah.core.config import config

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
