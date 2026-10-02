"""Context compression — rolling compaction and manual compression.

Provides:
- ContextCompressor: compresses older context chunks while preserving tool call/result pairs
- RollingCompaction: monitors token usage and triggers compression when threshold exceeded
- CompressionConfig: configurable threshold and target ratio
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ah.core.models import ContextChunk
from ah.core.assembler import get_token_count

logger = logging.getLogger(__name__)


@dataclass
class CompressionConfig:
    """Configuration for context compression.

    Attributes:
        enabled: Whether compression is active.
        threshold: Fraction of context_budget that triggers compression (0.0-1.0).
        target_ratio: Target fraction of context_budget after compression (0.0-1.0).
        preserve_recent: Number of most-recent chunks to always preserve uncompressed.
        llm_summarize: Whether to use LLM for summarization (falls back to truncation if False).
    """
    enabled: bool = True
    threshold: float = 0.8
    target_ratio: float = 0.5
    preserve_recent: int = 3
    llm_summarize: bool = True


@dataclass
class CompressionResult:
    """Result of a compression operation.

    Attributes:
        compressed_chunks: The compressed/summarized chunks.
        original_count: Number of original chunks compressed.
        original_tokens: Total tokens in original chunks.
        compressed_tokens: Total tokens in compressed chunks.
        compression_ratio: Ratio of compressed to original tokens (0.0-1.0).
        method: Compression method used ("llm_summarize" or "truncate").
    """
    compressed_chunks: list[ContextChunk]
    original_count: int
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    method: str


class ContextCompressor:
    """Compresses context chunks while preserving tool call/result pairs.

    The compressor identifies tool_call chunks and their corresponding results,
    keeping them together during compression. Older chunks are summarized or
    truncated to fit within the target token budget.
    """

    def __init__(self, config: CompressionConfig | None = None) -> None:
        self.config = config or CompressionConfig()

    def compress(
        self,
        chunks: list[ContextChunk],
        session_id: uuid.UUID,
        agent_id: str,
        llm_provider: Any = None,
    ) -> CompressionResult:
        """Compress a list of context chunks.

        Strategy:
        1. Identify tool_call/result pairs and keep them together
        2. Preserve the most recent N chunks uncompressed
        3. Compress older chunks using LLM summarization or truncation
        4. Return the compressed chunk list

        Args:
            chunks: The context chunks to compress (newest first).
            session_id: The session ID.
            agent_id: The agent ID.
            llm_provider: Optional LLM provider for summarization.

        Returns:
            CompressionResult with compressed chunks and metadata.
        """
        if not chunks:
            return CompressionResult(
                compressed_chunks=[],
                original_count=0,
                original_tokens=0,
                compressed_tokens=0,
                compression_ratio=0.0,
                method="none",
            )

        # Calculate total tokens
        original_tokens = sum(c.token_count for c in chunks)
        if original_tokens == 0:
            original_tokens = sum(get_token_count(str(c.payload)) for c in chunks)

        # Identify tool_call/result pairs
        pairs = self._identify_tool_pairs(chunks)

        # Split into recent (preserve) and old (compress)
        preserve_count = self.config.preserve_recent
        if len(chunks) <= preserve_count:
            # Nothing to compress
            return CompressionResult(
                compressed_chunks=chunks,
                original_count=0,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio=1.0,
                method="none",
            )

        recent_chunks = chunks[:preserve_count]
        old_chunks = chunks[preserve_count:]

        # Compress old chunks
        if self.config.llm_summarize and llm_provider is not None:
            import asyncio
            try:
                compressed = asyncio.run(
                    self._llm_summarize(old_chunks, session_id, agent_id, llm_provider)
                )
                method = "llm_summarize"
            except Exception:
                try:
                    compressed = self._truncate_compress(old_chunks, session_id, agent_id)
                    method = "truncate"
                except Exception:
                    # If all compression fails, preserve original chunks to avoid context loss
                    compressed = old_chunks
                    method = "none"
        else:
            try:
                compressed = self._truncate_compress(old_chunks, session_id, agent_id)
                method = "truncate"
            except Exception:
                # If truncation fails, preserve original chunks to avoid context loss
                compressed = old_chunks
                method = "none"

        # Combine: compressed old chunks + preserved recent chunks
        all_compressed = compressed + recent_chunks
        compressed_tokens = sum(c.token_count for c in all_compressed)
        if compressed_tokens == 0:
            compressed_tokens = sum(get_token_count(str(c.payload)) for c in all_compressed)

        ratio = compressed_tokens / original_tokens if original_tokens > 0 else 0.0

        return CompressionResult(
            compressed_chunks=all_compressed,
            original_count=len(old_chunks),
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            method=method,
        )

    def _identify_tool_pairs(self, chunks: list[ContextChunk]) -> dict[int, int]:
        """Identify tool_call/result pairs by matching tool names.

        Returns a dict mapping chunk index -> paired chunk index.
        """
        pairs: dict[int, int] = {}
        # Build a map of tool_name -> list of indices
        tool_call_indices: dict[str, list[int]] = {}
        for i, chunk in enumerate(chunks):
            if chunk.chunk_type == "tool_call":
                tool_name = chunk.payload.get("tool", "")
                if tool_name:
                    tool_call_indices.setdefault(tool_name, []).append(i)

        # Match results to tool calls
        for i, chunk in enumerate(chunks):
            if chunk.chunk_type == "result":
                tool_name = chunk.payload.get("tool", "")
                if tool_name and tool_name in tool_call_indices:
                    # Find the most recent unmatched tool_call for this tool
                    for call_idx in reversed(tool_call_indices[tool_name]):
                        if call_idx not in pairs:
                            pairs[call_idx] = i
                            pairs[i] = call_idx
                            break

        return pairs

    async def _llm_summarize(
        self,
        chunks: list[ContextChunk],
        session_id: uuid.UUID,
        agent_id: str,
        llm_provider: Any,
    ) -> list[ContextChunk]:
        """Use LLM to summarize older context chunks.

        Falls back to truncation if LLM call fails.
        """
        try:
            # Build a text representation of the chunks
            text_parts = []
            for chunk in chunks:
                text_parts.append(self._chunk_to_text(chunk))

            summary_prompt = (
                "Summarize the following conversation context concisely. "
                "Preserve key facts, decisions, and tool results. "
                "Keep it under 200 words.\n\n"
                + "\n".join(text_parts)
            )

            response = await llm_provider.complete(
                messages=[{"role": "user", "content": summary_prompt}],
                temperature=0.3,
                max_tokens=500,
            )

            summary_text = response.content

            # Create a single summary chunk
            summary_chunk = ContextChunk(
                id=uuid.uuid4(),
                session_id=session_id,
                agent_id=agent_id,
                chunk_type="compression_summary",
                payload={
                    "content": summary_text,
                    "original_count": len(chunks),
                    "original_tokens": sum(c.token_count for c in chunks),
                },
                token_count=get_token_count(summary_text),
            )
            return [summary_chunk]

        except Exception as e:
            logger.warning("LLM summarization failed, falling back to truncation: %s", e)
            raise

    def _truncate_compress(
        self,
        chunks: list[ContextChunk],
        session_id: uuid.UUID,
        agent_id: str,
    ) -> list[ContextChunk]:
        """Truncate older chunks to fit within target budget.

        Keeps the most important chunks and truncates payloads.
        """
        # Sort by importance: tool_call/result pairs first, then by recency
        pairs = self._identify_tool_pairs(chunks)

        # Build a priority score for each chunk
        def priority(idx: int) -> int:
            chunk = chunks[idx]
            score = 0
            # Tool pairs are high priority
            if idx in pairs:
                score += 100
            # Recent chunks are higher priority
            score += len(chunks) - idx
            # Memory chunks are important
            if chunk.chunk_type == "memory":
                score += 50
            return score

        # Sort by priority (highest first)
        sorted_indices = sorted(range(len(chunks)), key=priority, reverse=True)

        # Select chunks that fit within target budget
        target_tokens = int(sum(c.token_count for c in chunks) * self.config.target_ratio)
        selected: list[ContextChunk] = []
        current_tokens = 0

        for idx in sorted_indices:
            chunk = chunks[idx]
            if current_tokens + chunk.token_count <= target_tokens:
                selected.append(chunk)
                current_tokens += chunk.token_count
            else:
                # Try to truncate this chunk
                remaining = target_tokens - current_tokens
                if remaining > 50:  # Only if we have meaningful space
                    truncated = self._truncate_chunk(chunk, remaining)
                    if truncated:
                        selected.append(truncated)
                        current_tokens += truncated.token_count

        # Sort back to original order (by created_at)
        selected.sort(key=lambda c: c.created_at, reverse=True)
        return selected

    def _truncate_chunk(self, chunk: ContextChunk, max_tokens: int) -> ContextChunk | None:
        """Truncate a chunk's payload to fit within max_tokens."""
        max_chars = max_tokens * 4  # Rough estimate: 4 chars per token

        payload = chunk.payload.copy()
        truncated = False

        # Truncate string values in payload
        for key, value in payload.items():
            if isinstance(value, str) and len(value) > max_chars:
                payload[key] = value[:max_chars] + "..."
                truncated = True
            elif isinstance(value, dict):
                for k2, v2 in value.items():
                    if isinstance(v2, str) and len(v2) > max_chars:
                        value[k2] = v2[:max_chars] + "..."
                        truncated = True

        if not truncated and chunk.token_count <= max_tokens:
            return chunk

        new_text = str(payload)
        new_tokens = get_token_count(new_text)
        if new_tokens > max_tokens:
            # Still too big, truncate the string representation
            payload = {"content": new_text[:max_chars] + "..."}
            new_tokens = get_token_count(str(payload))

        return ContextChunk(
            id=chunk.id,
            session_id=chunk.session_id,
            agent_id=chunk.agent_id,
            chunk_type=chunk.chunk_type,
            payload=payload,
            token_count=new_tokens,
            embedding=chunk.embedding,
            created_at=chunk.created_at,
            accessed_at=chunk.accessed_at,
        )

    def _chunk_to_text(self, chunk: ContextChunk) -> str:
        """Convert a chunk to text for summarization."""
        chunk_type = chunk.chunk_type
        payload = chunk.payload

        if chunk_type == "tool_call":
            tool = payload.get("tool", "unknown")
            args = payload.get("args", {})
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            return f"[Tool Call] {tool}({args_str})"
        elif chunk_type == "result":
            status = payload.get("status", "ok")
            result = payload.get("result", "")
            if isinstance(result, str) and len(result) > 200:
                result = result[:200] + "..."
            return f"[Result] {status}: {result}"
        elif chunk_type == "user_message":
            return f"[User] {payload.get('content', '')}"
        elif chunk_type == "assistant_message":
            return f"[Assistant] {payload.get('content', '')}"
        elif chunk_type == "memory":
            return f"[Memory] {payload.get('content', '')}"
        else:
            return f"[{chunk_type}] {str(payload)[:200]}"


class RollingCompaction:
    """Monitors token usage and triggers compression when threshold is exceeded.

    Used by the agent to automatically compress context when it grows too large.
    """

    def __init__(self, config: CompressionConfig | None = None) -> None:
        self.config = config or CompressionConfig()
        self._last_compression_tokens: int = 0
        self._compression_count: int = 0

    def should_compress(self, current_tokens: int, context_budget: int) -> bool:
        """Check if compression should be triggered.

        Args:
            current_tokens: Current total token count for the session.
            context_budget: The session's context budget.

        Returns:
            True if compression should be triggered.
        """
        if not self.config.enabled:
            return False

        threshold_tokens = int(context_budget * self.config.threshold)

        # Trigger if we're over threshold and haven't recently compressed
        if current_tokens >= threshold_tokens:
            # Don't compress again until we've grown since last compression
            growth_since_last = current_tokens - self._last_compression_tokens
            if growth_since_last >= threshold_tokens * 0.2:  # At least 20% growth
                return True

        return False

    def record_compression(self, tokens_after: int) -> None:
        """Record that compression happened at this token count."""
        self._last_compression_tokens = tokens_after
        self._compression_count += 1

    @property
    def compression_count(self) -> int:
        """Number of times compression has been triggered."""
        return self._compression_count

    def reset(self) -> None:
        """Reset compaction state."""
        self._last_compression_tokens = 0
        self._compression_count = 0
